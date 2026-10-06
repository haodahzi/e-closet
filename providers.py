"""模型接入层：每家一组 async 函数，输入本地 Path 或 http(s) URL，输出结果图 URL。

- 阿里云百炼（DashScope）：aitryon-plus 试衣、aitryon-refiner 精修、qwen-image-edit-plus 多图编辑
- 可灵（Kling）：kolors-virtual-try-on 试穿
- fal.ai：多图编辑 + 试穿（原方案，保留做对照）
"""
import asyncio, base64, hashlib, hmac, json, math, os, time
from pathlib import Path

import httpx


class GenError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


def env(k, d=""):
    return os.environ.get(k, d).strip()


def is_url(x):
    return isinstance(x, str) and x.startswith(("http://", "https://"))


def b64(p: Path):
    return base64.b64encode(p.read_bytes()).decode()


def small_uri(x, side=1280):
    """发给即梦的图先缩到长边 1280、JPEG 85，请求小很多、上传更快（结果清晰度不受影响）。"""
    if not isinstance(x, Path):
        return x
    from PIL import Image
    import io
    im = Image.open(x).convert("RGB")
    im.thumbnail((side, side))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def timeout(read=180):
    """连不上 15 秒就放弃；连上以后最多等 read 秒出图。"""
    return httpx.Timeout(connect=15, read=read, write=60, pool=15)


def data_uri(x):
    return "data:image/jpeg;base64," + b64(x) if isinstance(x, Path) else x


async def poll(fn, ok, bad, label, every=2.0, limit=300):
    """反复调用 fn() 拿状态，直到 ok(状态) 为真；bad(状态) 返回错误文案时抛出。"""
    deadline = time.time() + limit
    while True:
        j = await fn()
        if ok(j):
            return j
        err = bad(j)
        if err:
            raise GenError(f"{label} 失败：{err}")
        if time.time() > deadline:
            raise GenError(f"{label} 超过 {limit // 60} 分钟没有出图，已放弃。稍后再试。")
        await asyncio.sleep(every)


# ---------------------------------------------------------------- 阿里云百炼
DS = env("DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/api/v1")


def ds_key():
    return env("DASHSCOPE_API_KEY")


def ds_headers(**extra):
    return {"Authorization": f"Bearer {ds_key()}", "Content-Type": "application/json", **extra}


def ds_check(r, label):
    if r.status_code in (401, 403):
        raise GenError("百炼拒绝了 DASHSCOPE_API_KEY（无效、没开通该模型或余额不足）。", r.status_code)
    if r.status_code >= 400:
        try:
            j = r.json()
            msg = f"{j.get('code', '')} {j.get('message', '')}".strip()
        except Exception:
            msg = r.text[:300]
        raise GenError(f"{label} 请求失败（{r.status_code}）：{msg}", r.status_code)
    return r.json()


async def ds_upload(client, model, src):
    """试衣接口只收链接：本地图先传到百炼的临时存储（48 小时有效），得到 oss:// 地址。"""
    if not isinstance(src, Path):
        return src
    r = await client.get(f"{DS}/uploads", params={"action": "getPolicy", "model": model},
                         headers={"Authorization": f"Bearer {ds_key()}"})
    j = ds_check(r, "获取上传凭证")
    p = j.get("data") or j.get("output") or j
    key = f"{p['upload_dir']}/{src.name}"
    form = {"OSSAccessKeyId": p["oss_access_key_id"], "Signature": p["signature"], "policy": p["policy"],
            "key": key, "x-oss-object-acl": p["x_oss_object_acl"],
            "x-oss-forbid-overwrite": p["x_oss_forbid_overwrite"], "success_action_status": "200",
            "x-oss-content-type": "image/jpeg"}
    r = await client.post(p["upload_host"], data=form, files={"file": (src.name, src.read_bytes(), "image/jpeg")})
    if r.status_code != 200:
        raise GenError(f"上传图片到百炼失败（{r.status_code}）：{r.text[:200]}")
    return "oss://" + key


async def ds_task(client, payload, label):
    """提交异步任务并轮询，返回 output。"""
    oss = any(isinstance(v, str) and v.startswith("oss://") for v in payload["input"].values())
    h = ds_headers(**{"X-DashScope-Async": "enable"}, **({"X-DashScope-OssResourceResolve": "enable"} if oss else {}))
    j = ds_check(await client.post(f"{DS}/services/aigc/image2image/image-synthesis", json=payload, headers=h), label)
    tid = j["output"]["task_id"]

    async def status():
        return ds_check(await client.get(f"{DS}/tasks/{tid}", headers=ds_headers()), label)["output"]

    out = await poll(status, lambda o: o.get("task_status") == "SUCCEEDED",
                     lambda o: f"{o.get('code', '')} {o.get('message', '')}".strip() or o.get("task_status")
                     if o.get("task_status") in ("FAILED", "CANCELED", "UNKNOWN") else None, label, every=3)
    if not out.get("image_url"):
        raise GenError(f"{label} 的返回里没有图片：{json.dumps(out, ensure_ascii=False)[:300]}")
    return out["image_url"]


async def bailian_tryon(client, person, top=None, bottom=None):
    """上下装可以一次换。只给一件时，另一件由模型自行生成。"""
    m = env("BL_TRYON_MODEL", "aitryon-plus")
    inp = {"person_image_url": await ds_upload(client, m, person)}
    if top:
        inp["top_garment_url"] = await ds_upload(client, m, top)
    if bottom:
        inp["bottom_garment_url"] = await ds_upload(client, m, bottom)
    return await ds_task(client, {"model": m, "input": inp,
                                  "parameters": {"resolution": -1, "restore_face": True}}, "百炼试衣")


async def bailian_refine(client, coarse, person, top=None, bottom=None):
    m = "aitryon-refiner"
    inp = {"coarse_image_url": coarse, "person_image_url": await ds_upload(client, m, person)}
    if top:
        inp["top_garment_url"] = await ds_upload(client, m, top)
    if bottom:
        inp["bottom_garment_url"] = await ds_upload(client, m, bottom)
    return await ds_task(client, {"model": m, "input": inp,
                                  "parameters": {"gender": env("GENDER", "woman")}}, "百炼精修")


async def bailian_edit(client, images, prompts):
    """qwen-image-edit-plus：最多 3 张图 + 一段指令，同步返回。"""
    m = env("BL_EDIT_MODEL", "qwen-image-edit-plus")
    if len(images) > 3:
        raise GenError("通义图像编辑一次最多 3 张图。")
    last = None
    for p in prompts:  # 第一种措辞被内容审核误拦时，换一种措辞再试
        content = [{"image": data_uri(x)} for x in images] + [{"text": p}]
        body = {"model": m, "input": {"messages": [{"role": "user", "content": content}]},
                "parameters": {"n": 1, "watermark": False, "prompt_extend": False,
                               "negative_prompt": "deformed face, different person, extra limbs, blurry, cartoon"}}
        try:
            r = await client.post(f"{DS}/services/aigc/multimodal-generation/generation", json=body,
                                  headers=ds_headers())
            j = ds_check(r, "通义图像编辑")
            for part in j["output"]["choices"][0]["message"]["content"]:
                if part.get("image"):
                    return part["image"]
            raise GenError(f"通义图像编辑没有返回图片：{json.dumps(j, ensure_ascii=False)[:300]}")
        except GenError as e:
            last = e
            if e.status in (401, 403):
                break
    raise last


# ---------------------------------------------------------------- 可灵
KLING = env("KLING_BASE_URL", "https://api-beijing.klingai.com")


def kling_ready():
    return bool(env("KLING_ACCESS_KEY") and env("KLING_SECRET_KEY"))


def kling_token():
    """可灵用 AccessKey/SecretKey 自签 JWT（HS256，30 分钟有效）。"""
    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d, separators=(",", ":")).encode()).rstrip(b"=")
    now = int(time.time())
    head = enc({"alg": "HS256", "typ": "JWT"}) + b"." + enc({"iss": env("KLING_ACCESS_KEY"), "exp": now + 1800,
                                                            "nbf": now - 5})
    sig = hmac.new(env("KLING_SECRET_KEY").encode(), head, hashlib.sha256).digest()
    return (head + b"." + base64.urlsafe_b64encode(sig).rstrip(b"=")).decode()


def kling_check(r, label):
    if r.status_code in (401, 403):
        raise GenError("可灵拒绝了 KLING_ACCESS_KEY / KLING_SECRET_KEY（无效或资源包用完）。", r.status_code)
    try:
        j = r.json()
    except Exception:
        raise GenError(f"{label} 请求失败（{r.status_code}）：{r.text[:300]}", r.status_code)
    if r.status_code >= 400 or j.get("code", 0) != 0:
        raise GenError(f"{label} 请求失败（{r.status_code}）：{j.get('code')} {j.get('message')}", r.status_code)
    return j["data"]


async def kling_tryon(client, human, cloth):
    url = f"{KLING}/v1/images/kolors-virtual-try-on"
    h = lambda: {"Authorization": f"Bearer {kling_token()}", "Content-Type": "application/json"}
    pic = lambda x: b64(x) if isinstance(x, Path) else x  # 本地图传纯 base64，不带 data: 前缀
    d = kling_check(await client.post(url, headers=h(), json={
        "model_name": env("KLING_TRYON_MODEL", "kolors-virtual-try-on-v1-5"),
        "human_image": pic(human), "cloth_image": pic(cloth)}), "可灵试穿")
    tid = d["task_id"]

    async def status():
        return kling_check(await client.get(f"{url}/{tid}", headers=h()), "可灵试穿")

    d = await poll(status, lambda d: d.get("task_status") == "succeed",
                   lambda d: d.get("task_status_msg") or "任务失败"
                   if str(d.get("task_status", "")).startswith("fail") else None, "可灵试穿")
    imgs = (d.get("task_result") or {}).get("images") or []
    if not imgs or not imgs[0].get("url"):
        raise GenError(f"可灵试穿的返回里没有图片：{json.dumps(d, ensure_ascii=False)[:300]}")
    return imgs[0]["url"]


# ---------------------------------------------------------------- 火山方舟（即梦 Seedream）
ARK = env("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")


def ark_size(first):
    """输出和人像照片同样的长宽比，约 2K 像素量（4.0/4.5 要求总像素在 1280x720 到 4096x4096 之间）。"""
    try:
        from PIL import Image
        w, h = Image.open(first).size if isinstance(first, Path) else (3, 4)
    except Exception:
        w, h = 3, 4
    k = (2048 * 2048 / (w * h)) ** 0.5
    return f"{round(w * k / 16) * 16}x{round(h * k / 16) * 16}"


async def seedream_edit(client, images, prompts):
    """Seedream 多图参考生成：图 1 是人，后面是衣服、鞋、配饰，最多 14 张，同步返回。"""
    if len(images) > 14:
        raise GenError("即梦一次最多 14 张参考图。")
    last = None
    for p in prompts:
        body = {"model": env("SEEDREAM_MODEL", "doubao-seedream-4-5-251128"), "prompt": p,
                "image": [small_uri(x) for x in images], "size": ark_size(images[0]),
                "sequential_image_generation": "disabled", "response_format": "url", "watermark": False}
        try:
            r = await client.post(f"{ARK}/images/generations", json=body,
                                  headers={"Authorization": f"Bearer {env('ARK_API_KEY')}",
                                           "Content-Type": "application/json"})
            if r.status_code in (401, 403):
                raise GenError("火山方舟拒绝了 ARK_API_KEY（无效、没开通 Seedream 模型或余额不足）。", r.status_code)
            j = r.json() if "json" in r.headers.get("content-type", "") else {}
            if r.status_code >= 400 or "error" in j:
                e = j.get("error") or {}
                raise GenError(f"即梦生成失败（{r.status_code}）：{e.get('code', '')} {e.get('message', r.text[:300])}",
                               r.status_code)
            url = ((j.get("data") or [{}])[0]).get("url")
            if not url:
                raise GenError(f"即梦没有返回图片：{json.dumps(j, ensure_ascii=False)[:300]}")
            return url
        except GenError as e:
            last = e
            if e.status in (401, 403):
                break
    raise last


async def seedream_views(client, images, prompt, n):
    """Seedream 组图：一次生成 n 张同一人同一身的不同角度，用来做“转一圈”。"""
    body = {"model": env("SEEDREAM_MODEL", "doubao-seedream-4-5-251128"), "prompt": prompt,
            "image": [small_uri(x) for x in images], "size": ark_size(images[0]),
            "sequential_image_generation": "auto", "sequential_image_generation_options": {"max_images": n},
            "response_format": "url", "watermark": False}
    r = await client.post(f"{ARK}/images/generations", json=body, timeout=600,
                          headers={"Authorization": f"Bearer {env('ARK_API_KEY')}", "Content-Type": "application/json"})
    if r.status_code in (401, 403):
        raise GenError("火山方舟拒绝了 ARK_API_KEY（无效、没开通 Seedream 模型或余额不足）。", r.status_code)
    j = r.json() if "json" in r.headers.get("content-type", "") else {}
    if r.status_code >= 400 or "error" in j:
        e = j.get("error") or {}
        raise GenError(f"生成多角度失败（{r.status_code}）：{e.get('code', '')} {e.get('message', r.text[:300])}")
    urls = [d["url"] for d in j.get("data") or [] if d.get("url")]
    if not urls:
        raise GenError(f"即梦没有返回多角度图片：{json.dumps(j, ensure_ascii=False)[:300]}")
    return urls


SEEDANCE_CANDIDATES = ["doubao-seedance-2-0-fast-260128", "doubao-seedance-2-0-260128",
                       "doubao-seedance-1-0-pro-fast-251015", "doubao-seedance-1-0-pro-250528",
                       "doubao-seedance-1-0-lite-i2v-250428"]


VIDEO_RATIOS = [(16, 9), (4, 3), (1, 1), (3, 4), (9, 16), (21, 9)]


def video_frame_uri(x, side=1280, quality=90):
    """发给视频模型的首帧：按照片四边的颜色补边成最接近的标准比例（居中，只补不裁）。
    不补的话模型会自己裁切或补黑边，人在视频里的位置就和照片对不上，播放时像分了层。"""
    from PIL import Image
    import io
    im = Image.open(x).convert("RGB")
    im.thumbnail((side, side))
    w, h = im.size
    rw, rh = min(VIDEO_RATIOS, key=lambda r: abs(math.log((r[0] / r[1]) / (w / h))))
    W, H = (round(h * rw / rh), h) if w * rh < h * rw else (w, round(w * rh / rw))
    if (W, H) != (w, h):
        edge = list(im.crop((0, 0, w, 3)).getdata()) + list(im.crop((0, 0, 3, h)).getdata()) + \
            list(im.crop((w - 3, 0, w, h)).getdata())
        bg = tuple(sorted(p[k] for p in edge)[len(edge) // 2] for k in range(3))
        canvas = Image.new("RGB", (W, H), bg)
        canvas.paste(im, ((W - w) // 2, (H - h) // 2))
        im = canvas
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


async def seedance_turn(client, image, prompt, on_wait=lambda s: None):
    """Seedance 图生视频：首帧、尾帧都是正面站立的试穿图，中间原地转一圈。返回 (视频 URL, 是否用上了尾帧)。
    尾帧是关键：转回正面时模型要对上照片，脸和姿势才不走样。所以先把开通了的模型挨个用“首尾帧”试一遍，
    都不支持尾帧才退回“只给首帧”。.env 的 SEEDANCE_MODEL 排在最前面。"""
    pic = video_frame_uri(image)
    h = {"Authorization": f"Bearer {env('ARK_API_KEY')}", "Content-Type": "application/json"}
    models = [m for m in dict.fromkeys([env("SEEDANCE_MODEL"), *SEEDANCE_CANDIDATES]) if m]
    closed = set()  # 没开通、不存在的模型，第二轮不再试
    last = None

    pic2 = video_frame_uri(image, quality=88)  # 和首帧看起来一样、文件不同：有的模型不收两张一模一样的图
    refused = []  # 带尾帧被拒绝的原因，显示给用户看

    def variants(model, both):
        v2 = "seedance-2" in model
        secs = int(env("VIDEO_SECONDS", "6" if v2 else "5"))
        frames = ["first_frame", "last_frame"] if both else ["first_frame"]
        cam = {"camera_fixed": env("TURN_MODE", "turntable") != "orbit"}
        out = [  # 从完整到最简：时长、音频开关、固定镜头这些参数不被支持就去掉再试
            {"frames": frames, "extra": {"generate_audio": False, **cam} if v2 else cam, "secs": secs},
            {"frames": frames, "extra": cam, "secs": 5},
            {"frames": frames, "extra": {}, "secs": 5},
        ]
        if both:  # 尾帧再多试两种：只留必需的参数；尾帧换成文件不同的同一张图
            out += [{"frames": frames, "extra": {}, "secs": secs, "bare": True},
                    {"frames": frames, "extra": {}, "secs": secs, "bare": True, "alt": True}]
        return out

    for both in (True, False):  # 第一轮：所有模型都试首尾帧；第二轮：只给首帧
        for model in models:
            if model in closed:
                continue
            for t in variants(model, both):
                imgs = [pic2 if (role == "last_frame" and t.get("alt")) else pic for role in t["frames"]]
                body = {"model": model,
                        "content": [{"type": "text", "text": prompt}] +
                                   [{"type": "image_url", "image_url": {"url": u}, "role": role} for u, role in zip(imgs, t["frames"])],
                        "duration": t["secs"], **t["extra"]}
                if not t.get("bare"):
                    body.update({"resolution": env("VIDEO_RESOLUTION", "720p"), "ratio": "adaptive", "watermark": False})
                r = await client.post(f"{ARK}/contents/generations/tasks", json=body, headers=h)
                j = r.json() if "json" in r.headers.get("content-type", "") else {}
                if r.status_code < 400 and j.get("id"):
                    print(f"\n  · 转一圈：{model}，{'首尾帧（照片同时作首帧和尾帧）' if both else '只有首帧（这个模型不支持尾帧）'}", flush=True)
                    break
                e = j.get("error") or {}
                msg = f"{e.get('code', '')} {e.get('message', r.text[:300])}"
                last = GenError(f"视频任务提交失败（{r.status_code}，{model}）：{msg}", r.status_code)
                print(f"\n  · Seedance 提交没成功，换个方式再试：{model} {r.status_code} {msg[:160]}", flush=True)
                if both and not ("ModelNotOpen" in msg or "NotFound" in msg or r.status_code == 404):
                    refused.append(f"{model}：{msg.strip()[:160]}")
                if r.status_code == 401:
                    raise GenError("火山方舟 API Key 不对或已失效（401）。检查 .env 里的 ARK_API_KEY。", 401)
                if "ModelNotOpen" in msg or "NotFound" in msg or r.status_code == 404:
                    closed.add(model)
                    break  # 这个模型用不了，换下一个模型
            else:
                continue
            if r.status_code < 400 and j.get("id"):
                break
        else:
            continue
        break
    else:
        raise GenError(f"{last}。到火山方舟「开通管理」开通任意一个 Seedance 视频模型，"
                       "并把它的模型 ID 填到 .env 的 SEEDANCE_MODEL。" if last else "没有可用的 Seedance 模型。")
    anchored = both
    why = "" if anchored else (refused[0] if refused else "")
    tid = j["id"]
    t0 = time.time()

    async def status():
        on_wait(int(time.time() - t0))
        q = await client.get(f"{ARK}/contents/generations/tasks/{tid}", headers=h)
        return q.json()

    done = await poll(status, lambda s: s.get("status") == "succeeded",
                      lambda s: ((s.get("error") or {}).get("message") or s.get("status"))
                      if s.get("status") in ("failed", "cancelled", "expired") else None,
                      "生成转身视频", every=4, limit=900)
    url = (done.get("content") or {}).get("video_url")
    if not url:
        raise GenError(f"视频生成完了但没有返回视频地址：{json.dumps(done, ensure_ascii=False)[:300]}")
    return url, anchored, why


async def ark_describe(client, image, category):
    """看图写一句英文描述（颜色、材质、关键细节），生成时和图一起交给即梦，鞋子等小件更不容易画错。"""
    kind = {"shirt": "shirt", "pants": "trousers", "shoes": "pair of shoes", "belt": "belt", "hat": "hat"}.get(
        category, "accessory")
    try:
        r = await client.post(f"{ARK}/chat/completions", headers={"Authorization": f"Bearer {env('ARK_API_KEY')}",
                                                                 "Content-Type": "application/json"},
                              json={"model": env("CHECK_MODEL", "doubao-seed-1-6-250615"), "temperature": 0,
                                    "messages": [{"role": "user", "content": [
                                        {"type": "text", "text": f"Describe this {kind} for a fashion catalogue in one "
                                         "English phrase of at most 22 words: type, exact colours, material, pattern, "
                                         "and the 2-3 most distinctive details (laces, sole, buckle, logo, collar...). "
                                         "Only the phrase."},
                                        {"type": "image_url", "image_url": {"url": small_uri(image, 768)}}]}]})
        text = r.json()["choices"][0]["message"]["content"].strip().strip('"').split("\n")[0]
        return text[:200] or None
    except Exception as e:
        print(f"\n  · 单品描述没写成（不影响使用）：{type(e).__name__}: {e}", flush=True)
        return None


CHECKS = ("same_person", "shirt_match", "pants_match", "outfit_complete", "natural", "wear_correct",
          "shoes_match", "belt_match", "hat_match", "accessories_match", "white_bg", "light_visible")
# 哪项没过就只修哪一处；natural / outfit_complete 属于整体问题，只能整身重做
FIXABLE = {"shirt_match": "shirt", "pants_match": "pants", "shoes_match": "shoes", "belt_match": "belt",
           "hat_match": "hat", "accessories_match": "accessory", "same_person": "face", "wear_correct": "wear",
           "white_bg": "background", "light_visible": "light"}


async def ark_judge(client, refs, result, wear_txt):
    """后台自动检查：用视觉模型对照原照和单品图给结果打分。返回 {检查项: bool, "note": 问题}；检查不了时返回 None。"""
    names = ["original photo of the person"] + [f"reference {n}" for n, _ in refs[1:]] + ["generated try-on result"]
    content = [{"type": "text", "text": (
        "You are a strict quality checker for a virtual try-on app. The images are, in order: "
        + "; ".join(f"image {i + 1} = {n}" for i, n in enumerate(names)) + ". "
        "Judge the LAST image against the others. Answer only with JSON, no other text: "
        '{"same_person": bool (same face, hair, skin tone and body shape as image 1), '
        '"shirt_match": bool (shirt colour, stripe/check scale and spacing, collar and buttons match the shirt reference; '
        'true if there is no shirt reference), '
        '"white_bg": bool (the background is plain pure white with nothing else in it), '
        '"pants_match": bool (trousers colour, fabric, cut and length match the trousers reference; true if none), '
        '"outfit_complete": bool (every referenced item is worn, in the right place and at a natural scale), '
        '"natural": bool (natural lighting and a believable body; no collage look, no extra limbs or artefacts), '
        '"light_visible": bool (any white or very light garment is clearly visible against the white background, '
        'with visible folds, shading and edges, not washed out or merging into it; true if there is none), '
        f'"wear_correct": bool (the shirt is worn and the person stands as requested: {wear_txt or "no specific request"}), '
        '"shoes_match": bool (the shoes have the same type, colours, laces and sole as the shoes reference; true if none), '
        '"belt_match": bool (a belt like the belt reference is visibly worn at the waist; true if no belt reference), '
        '"hat_match": bool (the hat matches the hat reference; true if no hat reference), '
        '"accessories_match": bool (other referenced accessories are present and look the same; true if none), '
        '"note": "the main problem in under 15 words, or empty"}')}]
    content += [{"type": "image_url", "image_url": {"url": small_uri(p, 768)}} for _, p in refs]
    content += [{"type": "image_url", "image_url": {"url": result}}]
    try:
        r = await client.post(f"{ARK}/chat/completions", headers={"Authorization": f"Bearer {env('ARK_API_KEY')}",
                                                                 "Content-Type": "application/json"},
                              json={"model": env("CHECK_MODEL", "doubao-seed-1-6-250615"), "temperature": 0,
                                    "messages": [{"role": "user", "content": content}]})
        text = r.json()["choices"][0]["message"]["content"]
        j = json.loads(text[text.index("{"):text.rindex("}") + 1])
        return {k: bool(j.get(k, True)) for k in CHECKS} | {"note": str(j.get("note", ""))[:120]}
    except Exception as e:
        body = ""
        try:
            body = r.text[:200]
        except Exception:
            pass
        print(f"\n  · 自动检查没做成（不影响出图）：{type(e).__name__}: {e} {body}", flush=True)
        return None  # 检查失败不影响出图


# ---------------------------------------------------------------- fal.ai
def fal_key():
    return env("FAL_KEY")


async def fal_run(client, model, payload):
    headers = {"Authorization": f"Key {fal_key()}", "Content-Type": "application/json"}
    r = await client.post(f"https://queue.fal.run/{model}", json=payload, headers=headers)
    if r.status_code in (401, 403):
        raise GenError("fal.ai 拒绝了这个 FAL_KEY（无效或余额不足）。", r.status_code)
    if r.status_code >= 400:
        raise GenError(f"{model} 提交失败（{r.status_code}）：{r.text[:300]}", r.status_code)
    j = r.json()

    async def status():
        return (await client.get(j["status_url"], headers=headers)).json()

    await poll(status, lambda s: s.get("status") == "COMPLETED",
               lambda s: s.get("status") if s.get("status") not in ("IN_QUEUE", "IN_PROGRESS", "COMPLETED") else None,
               model, every=1.5)
    res = await client.get(j["response_url"], headers=headers)
    if res.status_code >= 400:
        raise GenError(f"{model} 没有出图（{res.status_code}）：{res.text[:300]}", res.status_code)
    out = res.json()
    img = (out.get("images") or [None])[0] or out.get("image")
    if not img or not img.get("url"):
        raise GenError(f"{model} 的返回里没有图片：{json.dumps(out)[:300]}")
    return img["url"]


async def fal_edit(client, images, prompts):
    last = None
    for p in prompts:
        try:
            return await fal_run(client, env("FAL_EDIT_MODEL", "fal-ai/nano-banana-pro/edit"), {
                "prompt": p, "image_urls": [data_uri(x) for x in images], "num_images": 1,
                "aspect_ratio": "auto", "resolution": env("RESOLUTION", "1K"), "output_format": "jpeg"})
        except GenError as e:
            last = e
            if e.status in (401, 403):
                break
    raise last


async def fal_tryon(client, human, cloth):
    return await fal_run(client, env("FAL_VTON_MODEL", "fal-ai/kling/v1-5/kolors-virtual-try-on"),
                         {"human_image_url": data_uri(human), "garment_image_url": data_uri(cloth)})


# ---------------------------------------------------------------- 选择与价格
def ready(provider):
    return {"seedream": bool(env("ARK_API_KEY")), "bailian": bool(ds_key()), "kling": kling_ready(),
            "fal": bool(fal_key())}[provider]


EDIT = {"seedream": seedream_edit, "bailian": bailian_edit, "fal": fal_edit}
EDIT_MAX_IMAGES = {"seedream": 14, "bailian": 3, "fal": 8}
NAMES = {"seedream": "即梦", "bailian": "阿里云百炼", "kling": "可灵", "fal": "fal.ai"}


def price(kind, provider):
    """界面估价用，按控制台实际价格在 .env 里改。"""
    defaults = {("video", "seedance"): 1.5, ("edit", "seedream"): 0.25, ("tryon", "bailian"): 0.5, ("refine", "bailian"): 0.5, ("edit", "bailian"): 0.3,
                ("tryon", "kling"): 0.5, ("edit", "fal"): 1.1, ("tryon", "fal"): 0.5, ("refine", "fal"): 0.5}
    return float(env(f"PRICE_{kind}_{provider}".upper(), str(defaults.get((kind, provider), 0))))

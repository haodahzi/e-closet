"""模型接入层：每家一组 async 函数，输入本地 Path 或 http(s) URL，输出结果图 URL。

- 阿里云百炼（DashScope）：aitryon-plus 试衣、aitryon-refiner 精修、qwen-image-edit-plus 多图编辑
- 可灵（Kling）：kolors-virtual-try-on 试穿
- fal.ai：多图编辑 + 试穿（原方案，保留做对照）
"""
import asyncio, base64, hashlib, hmac, json, os, time
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
                "image": [data_uri(x) for x in images], "size": ark_size(images[0]),
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


CHECKS = ("same_person", "shirt_match", "outfit_complete", "natural", "wear_correct")


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
        '"outfit_complete": bool (every referenced item is worn, in the right place and at a natural scale), '
        '"natural": bool (pose, background and lighting match image 1; no collage look, no extra limbs or artefacts), '
        f'"wear_correct": bool (the shirt is worn as requested: {wear_txt or "no specific request"}), '
        '"note": "the main problem in under 15 words, or empty"}')}]
    content += [{"type": "image_url", "image_url": {"url": data_uri(p)}} for _, p in refs]
    content += [{"type": "image_url", "image_url": {"url": result}}]
    try:
        r = await client.post(f"{ARK}/chat/completions", headers={"Authorization": f"Bearer {env('ARK_API_KEY')}",
                                                                 "Content-Type": "application/json"},
                              json={"model": env("CHECK_MODEL", "doubao-seed-1-6-250615"), "temperature": 0,
                                    "messages": [{"role": "user", "content": content}]})
        text = r.json()["choices"][0]["message"]["content"]
        j = json.loads(text[text.index("{"):text.rindex("}") + 1])
        return {k: bool(j.get(k)) for k in CHECKS} | {"note": str(j.get("note", ""))[:120]}
    except Exception:
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
    defaults = {("edit", "seedream"): 0.25, ("tryon", "bailian"): 0.5, ("refine", "bailian"): 0.5, ("edit", "bailian"): 0.3,
                ("tryon", "kling"): 0.5, ("edit", "fal"): 1.1, ("tryon", "fal"): 0.5, ("refine", "fal"): 0.5}
    return float(env(f"PRICE_{kind}_{provider}".upper(), str(defaults.get((kind, provider), 0))))

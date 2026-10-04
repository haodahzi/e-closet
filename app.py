"""衬衫衣橱 —— 本地运行的电子衣橱 + 真人试穿（走 fal.ai API）。

启动:  python app.py   然后浏览器打开 http://127.0.0.1:8000
没有填 FAL_KEY 时自动进入演示模式（不花钱，只拼一张示意图）。
"""
import asyncio, base64, hashlib, io, json, os, threading, time, uuid, webbrowser
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageDraw, ImageOps
from pydantic import BaseModel

try:  # iPhone 的 HEIC 照片
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:
    pass

ROOT = Path(__file__).parent
DATA, ITEMS, RESULTS = ROOT / "data", ROOT / "data" / "items", ROOT / "data" / "results"
DBF = DATA / "db.json"
for d in (ITEMS, RESULTS):
    d.mkdir(parents=True, exist_ok=True)


def load_env():
    p = ROOT / ".env"
    if not p.exists():
        return
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_env()
FAL_KEY = os.environ.get("FAL_KEY", "").strip()
MOCK = not FAL_KEY or os.environ.get("MOCK") == "1"
EDIT_MODEL = os.environ.get("EDIT_MODEL", "fal-ai/nano-banana-pro/edit")
VTON_MODEL = os.environ.get("VTON_MODEL", "fal-ai/kling/v1-5/kolors-virtual-try-on")
RESOLUTION = os.environ.get("RESOLUTION", "1K")  # 1K / 2K / 4K
EDIT_PRICE = float(os.environ.get("EDIT_PRICE", "0.15"))  # 美元/张，仅用于界面估价
VTON_PRICE = float(os.environ.get("VTON_PRICE", "0.07"))

CATEGORIES = {"person", "shirt", "pants", "shoes", "accessory"}
MAX_ACCESSORIES = 3

# ---------------------------------------------------------------- 存储
_lock = threading.Lock()


def db_load():
    if DBF.exists():
        return json.loads(DBF.read_text(encoding="utf-8"))
    return {"items": {}, "looks": {}}


def db_save(db):
    tmp = DBF.with_suffix(".tmp")
    tmp.write_text(json.dumps(db, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(DBF)


def normalize_image(raw: bytes) -> Image.Image:
    """统一成 RGB JPEG：修正手机旋转、透明底铺白、短边不小于 640、长边不超过 1600。"""
    im = Image.open(io.BytesIO(raw))
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, "white")
        bg.paste(im, mask=im.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    w, h = im.size
    if min(w, h) < 640:
        s = 640 / min(w, h)
        im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
        w, h = im.size
    if max(w, h) > 1600:
        s = 1600 / max(w, h)
        im = im.resize((round(w * s), round(h * s)), Image.LANCZOS)
    return im


# ---------------------------------------------------------------- 提示词
PIECE_EN = {"shirt": "shirt", "pants": "trousers", "shoes": "shoes"}
WEAR_EN = {
    "tuck": {"in": "The shirt is tucked neatly into the trousers.",
             "out": "The shirt is worn untucked, hem hanging outside the trousers."},
    "sleeves": {"down": "Shirt sleeves are worn full length with cuffs buttoned.",
                "rolled": "Shirt sleeves are rolled up to just below the elbow."},
    "collar": {"closed": "The shirt is buttoned all the way up to the collar.",
               "open": "The top shirt button is undone, collar open, no tie."},
}


def named(item, fallback):
    n = (item.get("name") or "").strip()
    return f"{fallback} ({n})" if n else fallback


def clothes_prompts(pieces, wear):
    """pieces: [(category, item)]，图 1 是人，图 2.. 依次是各单品。返回 [正常措辞, 备用措辞]。"""
    refs = ", ".join(f"the {named(it, PIECE_EN[c])} from image {i + 2}" for i, (c, it) in enumerate(pieces))
    has_shirt = any(c == "shirt" for c, _ in pieces)
    wear_txt = " ".join(WEAR_EN[k][wear[k]] for k in ("tuck", "sleeves", "collar")) if has_shirt else ""
    keep = ("Keep the person's face, hair, skin tone, body shape, pose, camera framing, background and lighting "
            "exactly as in image 1. Anything not being replaced stays as it is in image 1.")
    fidelity = ("Reproduce every garment faithfully: same colour, fabric texture, pattern and stripe scale, collar "
                "shape, buttons, stitching and logos. Do not invent details. Fabric should drape, crease and cast "
                "shadows naturally under the scene's lighting. Output one photorealistic photo.")
    a = f"Image 1 is a photo of a person. Dress this exact person in {refs}. {wear_txt} {keep} {fidelity}"
    b = (f"Editorial menswear photograph: the same person from image 1, same pose and setting, now wearing {refs}. "
         f"{wear_txt} {keep} {fidelity}")
    return [a, b]


def accessory_prompts(accs):
    refs = ", ".join(f"the {(it.get('name') or '').strip() or 'accessory'} from image {i + 2}" for i, it in enumerate(accs))
    a = (f"Image 1 is a photo of a person. Add these accessories to the person: {refs}. Each one is worn where it "
         "naturally belongs, at realistic scale, matching the reference's colour, material and details. Change "
         "nothing else: face, body, pose, clothing, background and lighting stay identical to image 1. "
         "Output one photorealistic photo.")
    b = (f"Styling photograph: the person from image 1, unchanged, now also wearing {refs}, placed naturally and "
         "at realistic scale. Everything else stays identical to image 1.")
    return [a, b]


# ---------------------------------------------------------------- fal.ai
class FalError(Exception):
    def __init__(self, msg, status=0):
        super().__init__(msg)
        self.status = status


def as_input(x):
    """本地文件转 data URI；已经是 URL 的原样传。"""
    if isinstance(x, Path):
        return "data:image/jpeg;base64," + base64.b64encode(x.read_bytes()).decode()
    return x


async def fal_run(client, model, payload):
    headers = {"Authorization": f"Key {FAL_KEY}", "Content-Type": "application/json"}
    r = await client.post(f"https://queue.fal.run/{model}", json=payload, headers=headers)
    if r.status_code in (401, 403):
        raise FalError("fal.ai 拒绝了这个 FAL_KEY（无效或余额不足）。检查 .env 里的 FAL_KEY 和 fal.ai 账户余额。", r.status_code)
    if r.status_code >= 400:
        raise FalError(f"{model} 提交失败（{r.status_code}）：{r.text[:300]}", r.status_code)
    j = r.json()
    status_url, response_url = j["status_url"], j["response_url"]
    deadline = time.time() + 300
    while True:
        st = (await client.get(status_url, headers=headers)).json().get("status")
        if st == "COMPLETED":
            break
        if st not in ("IN_QUEUE", "IN_PROGRESS"):
            raise FalError(f"{model} 任务状态异常：{st}")
        if time.time() > deadline:
            raise FalError(f"{model} 超过 5 分钟没有出图，已放弃。稍后再试。")
        await asyncio.sleep(1.5)
    res = await client.get(response_url, headers=headers)
    if res.status_code >= 400:
        raise FalError(f"{model} 没有出图（{res.status_code}）：{res.text[:300]}", res.status_code)
    out = res.json()
    img = (out.get("images") or [None])[0] or out.get("image")
    if not img or not img.get("url"):
        raise FalError(f"{model} 的返回里没有图片：{json.dumps(out)[:300]}")
    return img["url"]


async def fal_edit(client, images, prompts):
    last = None
    for p in prompts:  # 第一种措辞被内容过滤误拦时，换一种措辞再试一次
        try:
            return await fal_run(client, EDIT_MODEL, {
                "prompt": p, "image_urls": [as_input(x) for x in images], "num_images": 1,
                "aspect_ratio": "auto", "resolution": RESOLUTION, "output_format": "jpeg"})
        except FalError as e:
            last = e
            if e.status in (401, 403):
                break
    raise last


async def fetch_image(client, src) -> Image.Image:
    if isinstance(src, Path):
        return Image.open(src).convert("RGB")
    if src.startswith("data:"):
        return Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1]))).convert("RGB")
    r = await client.get(src, follow_redirects=True)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


def mock_step(current: Path, refs: List[Path], n: int) -> Path:
    """演示模式：不换装，只把参考图贴在原照左侧，让流程能走通。"""
    im = Image.open(current).convert("RGB")
    w, h = im.size
    t = max(80, w // 5)
    d = ImageDraw.Draw(im)
    for i, r in enumerate(refs):
        th = ImageOps.fit(Image.open(r).convert("RGB"), (t, t))
        x, y = 16 + (n * (t + 12)), 16 + i * (t + 12)
        d.rectangle([x - 4, y - 4, x + t + 3, y + t + 3], fill="white")
        im.paste(th, (x, y))
    d.rectangle([0, h - 36, w, h], fill=(21, 33, 58))
    d.text((14, h - 26), "DEMO MODE - add FAL_KEY to .env for real try-on", fill="white")
    out = RESULTS / f"_tmp_{uuid.uuid4().hex}.jpg"
    im.save(out, quality=90)
    return out


# ---------------------------------------------------------------- 试穿流程
class TryOn(BaseModel):
    person_id: str
    shirt_id: Optional[str] = None
    pants_id: Optional[str] = None
    shoes_id: Optional[str] = None
    accessory_ids: List[str] = []
    tuck: str = "in"
    sleeves: str = "down"
    collar: str = "open"
    refine: bool = False
    force: bool = False


def build_plan(req: TryOn, items):
    def get(i, cat):
        it = items.get(i)
        if not it or it["category"] != cat:
            raise HTTPException(400, "选中的单品已经不在衣橱里了，刷新页面后重选。")
        return it

    person = get(req.person_id, "person")
    wear = {"tuck": req.tuck, "sleeves": req.sleeves, "collar": req.collar}
    for k, v in wear.items():
        if v not in WEAR_EN[k]:
            raise HTTPException(400, f"穿法参数不对：{k}={v}")
    pieces = [(c, get(i, c)) for c, i in (("shirt", req.shirt_id), ("pants", req.pants_id), ("shoes", req.shoes_id)) if i]
    accs = [get(i, "accessory") for i in sorted(set(req.accessory_ids))]
    if len(accs) > MAX_ACCESSORIES:
        raise HTTPException(400, f"配饰一次最多 {MAX_ACCESSORIES} 件，太多会明显掉质量。")
    if not pieces and not accs:
        raise HTTPException(400, "还没选任何单品。至少选一件衬衫、裤子、鞋子或配饰。")

    steps = []
    if pieces:
        label = "换上" + "、".join({"shirt": "衬衫", "pants": "裤子", "shoes": "鞋子"}[c] for c, _ in pieces)
        steps.append({"kind": "edit", "label": label, "refs": [it["file"] for _, it in pieces],
                      "prompts": clothes_prompts(pieces, wear)})
    if req.refine and req.shirt_id:
        steps.append({"kind": "vton", "label": "加强衬衫细节", "refs": [items[req.shirt_id]["file"]]})
    if accs:
        steps.append({"kind": "edit", "label": "戴上配饰", "refs": [it["file"] for it in accs],
                      "prompts": accessory_prompts(accs)})
    sig = json.dumps([person["file"], steps, EDIT_MODEL, VTON_MODEL, RESOLUTION, MOCK], ensure_ascii=False, sort_keys=True)
    cost = sum(EDIT_PRICE if s["kind"] == "edit" else VTON_PRICE for s in steps)
    return {"person": person, "steps": steps, "key": hashlib.sha1(sig.encode()).hexdigest(),
            "cost": 0 if MOCK else round(cost, 2)}


JOBS = {}


async def run_job(job, req: TryOn, plan):
    tmp = []
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            current = ITEMS / plan["person"]["file"]
            for i, step in enumerate(plan["steps"]):
                job.update(step_index=i, step=step["label"])
                refs = [ITEMS / f for f in step["refs"]]
                if MOCK:
                    await asyncio.sleep(0.8)
                    current = mock_step(current, refs, i)
                    tmp.append(current)
                elif step["kind"] == "edit":
                    current = await fal_edit(client, [current] + refs, step["prompts"])
                else:
                    current = await fal_run(client, VTON_MODEL, {
                        "human_image_url": as_input(current), "garment_image_url": as_input(refs[0])})
            job.update(step="保存结果")
            look_id = uuid.uuid4().hex[:12]
            (await fetch_image(client, current)).save(RESULTS / f"{look_id}.jpg", quality=95)
        look = {"id": look_id, "key": plan["key"], "file": f"{look_id}.jpg", "created": time.time(),
                "cost": plan["cost"], "demo": MOCK, "sel": req.model_dump(exclude={"force"})}
        with _lock:
            db = db_load()
            for old in [k for k, v in db["looks"].items() if v["key"] == plan["key"]]:  # 重新生成时替换旧图
                (RESULTS / db["looks"][old]["file"]).unlink(missing_ok=True)
                del db["looks"][old]
            db["looks"][look_id] = look
            db_save(db)
        job.update(status="done", look=look)
    except FalError as e:
        job.update(status="error", error=str(e))
    except Exception as e:  # 网络断开等
        job.update(status="error", error=f"生成中断：{type(e).__name__}: {e}")
    finally:
        for t in tmp:
            t.unlink(missing_ok=True)


# ---------------------------------------------------------------- 接口
app = FastAPI(title="衬衫衣橱")
app.mount("/media", StaticFiles(directory=DATA), name="media")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def state():
    db = db_load()
    return {"items": sorted(db["items"].values(), key=lambda x: -x["created"]),
            "looks": sorted(db["looks"].values(), key=lambda x: -x["created"]),
            "config": {"demo": MOCK, "edit_model": EDIT_MODEL, "vton_model": VTON_MODEL,
                       "edit_price": EDIT_PRICE, "vton_price": VTON_PRICE, "max_accessories": MAX_ACCESSORIES}}


@app.post("/api/items")
async def add_item(file: UploadFile = File(...), category: str = Form(...), name: str = Form("")):
    if category not in CATEGORIES:
        raise HTTPException(400, "不认识的分类。")
    try:
        im = normalize_image(await file.read())
    except Exception:
        raise HTTPException(400, f"“{file.filename}” 不是能打开的图片。支持 JPG、PNG、WebP、HEIC。")
    item_id = uuid.uuid4().hex[:12]
    im.save(ITEMS / f"{item_id}.jpg", quality=92)
    item = {"id": item_id, "category": category, "name": name.strip()[:40], "file": f"{item_id}.jpg",
            "created": time.time()}
    with _lock:
        db = db_load()
        db["items"][item_id] = item
        db_save(db)
    return item


class Rename(BaseModel):
    name: str


@app.patch("/api/items/{item_id}")
def rename_item(item_id: str, body: Rename):
    with _lock:
        db = db_load()
        if item_id not in db["items"]:
            raise HTTPException(404, "这件单品已经被删除。")
        db["items"][item_id]["name"] = body.name.strip()[:40]
        db_save(db)
        return db["items"][item_id]


@app.delete("/api/items/{item_id}")
def delete_item(item_id: str):
    with _lock:
        db = db_load()
        it = db["items"].pop(item_id, None)
        if it:
            (ITEMS / it["file"]).unlink(missing_ok=True)
            db_save(db)
    return {"ok": True}


@app.delete("/api/looks/{look_id}")
def delete_look(look_id: str):
    with _lock:
        db = db_load()
        lk = db["looks"].pop(look_id, None)
        if lk:
            (RESULTS / lk["file"]).unlink(missing_ok=True)
            db_save(db)
    return {"ok": True}


@app.post("/api/tryon")
async def tryon(req: TryOn):
    db = db_load()
    plan = build_plan(req, db["items"])
    if not req.force:
        for lk in db["looks"].values():
            if lk["key"] == plan["key"]:
                return {"cached": True, "look": lk}
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "step": "排队中", "step_index": 0,
           "steps": [s["label"] for s in plan["steps"]]}
    JOBS[job["id"]] = job
    asyncio.create_task(run_job(job, req, plan))
    return {"cached": False, "job": job}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    if job_id not in JOBS:
        raise HTTPException(404, "找不到这个任务（服务可能重启过），重新点一次“生成试穿”。")
    return JOBS[job_id]


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  衬衫衣橱已启动: http://127.0.0.1:{port}   {'（演示模式：未填 FAL_KEY）' if MOCK else '（已连接 fal.ai）'}\n")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

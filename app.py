"""衬衫衣橱 —— 本地运行的电子衣橱 + 真人试穿。

启动:  python app.py   然后浏览器打开 http://127.0.0.1:8000
换装走云端模型（阿里云百炼 / 可灵 / fal.ai，见 providers.py），没填 key 时自动进入演示模式（不花钱）。
"""
import asyncio, hashlib, io, json, os, threading, time, uuid, webbrowser
from pathlib import Path
from typing import List, Optional

import httpx
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageOps
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
            os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip('"').strip("'"))


load_env()
import providers as P  # noqa: E402  读完 .env 再导入

PIPELINES = ("seedream", "bailian", "kling", "fal")


def default_pipeline():
    p = P.env("PIPELINE")
    if p in PIPELINES:
        return p
    return next((x for x in PIPELINES if P.ready(x)), "seedream")


def default_editor():
    e = P.env("EDIT_PROVIDER")
    if e in P.EDIT:
        return e
    return next((x for x in ("seedream", "bailian", "fal") if P.ready(x)), "seedream")


PIPELINE, EDITOR = default_pipeline(), default_editor()
MOCK = os.environ.get("MOCK") == "1" or not P.ready(PIPELINE)

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


GARMENTS = {"shirt", "pants", "shoes", "accessory"}


def cutout(im: Image.Image, holes=False):
    """去掉和边缘连通的白底，得到透明背景的单品图，只用于界面展示（给模型的仍是原图）。
    holes=True 时把被包住的白底也去掉（项链、手链中间的空白）。去不干净（比如白衬衫放白底上）返回 None。"""
    rgb = im.convert("RGB")
    small = rgb.copy()
    small.thumbnail((360, 360))
    r, g, b = small.split()
    near = ImageChops.darker(r, ImageChops.darker(g, b)).point(lambda v: 255 if v >= 232 else 0)
    w, h = near.size
    for xy in [(0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1), (w // 2, 0), (w // 2, h - 1), (0, h // 2), (w - 1, h // 2)]:
        if near.getpixel(xy) == 255:
            ImageDraw.floodfill(near, xy, 128)
    bg = near.point(lambda v: 255 if v in (128, 255) else 0) if holes else near.point(lambda v: 255 if v == 128 else 0)
    keep = 1 - bg.histogram()[255] / (w * h)
    if keep < 0.012 or keep > 0.97:  # 几乎全被去掉，或者根本不是白底
        return None
    alpha = ImageChops.invert(bg).resize(rgb.size, Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.5))
    out = rgb.copy()
    out.putalpha(alpha)
    box = alpha.point(lambda v: 255 if v > 24 else 0).getbbox()
    if box:
        m = 12
        out = out.crop((max(box[0] - m, 0), max(box[1] - m, 0), min(box[2] + m, out.width), min(box[3] + m, out.height)))
    return out


CUT_VERSION = 4
_rembg = {}


def _session(name):
    from rembg import new_session
    if name not in _rembg:
        _rembg[name] = new_session(name)
    return _rembg[name]


def skin_share(rgb: Image.Image, mask: Image.Image) -> float:
    """抠出来的部分里，肤色像素占多少（YCbCr 经验范围）。"""
    small, m = rgb.copy(), mask.copy()
    small.thumbnail((256, 256))
    m = m.resize(small.size)
    y, cb, cr = small.convert("YCbCr").split()
    skin = ImageChops.multiply(ImageChops.multiply(cb.point(lambda v: 255 if 80 <= v <= 125 else 0),
                                                   cr.point(lambda v: 255 if 137 <= v <= 170 else 0)),
                               y.point(lambda v: 255 if v > 60 else 0))
    on = m.point(lambda v: 255 if v > 128 else 0)
    total = on.histogram()[255]
    return ImageChops.multiply(skin, on).histogram()[255] / max(total, 1)


def ai_cutout(im: Image.Image, category):
    """用本地抠图模型（rembg）去背景；没装 rembg 时返回 None，退回白底算法。
    衬衫、裤子如果是模特穿着拍的，再用衣服分割模型去掉头、手和身体。返回 (图, 照片里是否有人)。"""
    try:
        from rembg import remove
    except Exception:
        return None
    rgb = im.convert("RGB")
    mask = remove(rgb, session=_session(P.env("REMBG_MODEL", "u2net")), only_mask=True)
    person = False
    if category in ("shirt", "pants") and 0.03 < skin_share(rgb, mask) < 0.35:  # 太高说明衣服本身是米色、肤色
        # 抠出来的东西里有不少皮肤（脸、脖子、手）：是模特穿着拍的，再用衣服分割模型只留下衣服
        cloth = _session("u2net_cloth_seg").predict(rgb, cc="upper" if category == "shirt" else "lower")[0]
        cloth = cloth.resize(rgb.size).filter(ImageFilter.MaxFilter(5))
        both = ImageChops.multiply(mask, cloth)
        area = lambda m: m.point(lambda v: 255 if v > 128 else 0).histogram()[255]
        person = True
        if 0.25 < area(both) / max(area(mask), 1) < 0.92:
            mask = both.filter(ImageFilter.GaussianBlur(1))
    box = mask.point(lambda v: 255 if v > 24 else 0).getbbox()
    if not box or (box[2] - box[0]) * (box[3] - box[1]) < 0.02 * rgb.width * rgb.height:
        return None
    out = rgb.copy()
    out.putalpha(mask)
    m = 10
    return out.crop((max(box[0] - m, 0), max(box[1] - m, 0), min(box[2] + m, out.width), min(box[3] + m, out.height))), person


def ensure_cut(it):
    """给单品补一张透明底展示图（只用于界面，给模型的仍是原图）。返回是否有改动。"""
    if it["category"] not in GARMENTS or it.get("cutv") == CUT_VERSION:
        return False
    src = ITEMS / it["file"]
    if not src.exists():
        return False
    im = Image.open(src)
    got = ai_cutout(im, it["category"])
    out, it["person"] = got if got else (cutout(im, holes=it["category"] == "accessory"), False)
    if it.get("cut"):
        (ITEMS / it["cut"]).unlink(missing_ok=True)
    it["cut"], it["cutv"] = None, CUT_VERSION
    if out is not None:
        it["cut"] = f"{it['id']}_cut{CUT_VERSION}.png"
        out.save(ITEMS / it["cut"])
    return True


def tidy_prompts(category, name):
    what = {"shirt": "shirt", "pants": "trousers", "shoes": "pair of shoes", "accessory": name or "accessory"}[category]
    a = (f"Extract only the {what} from image 1 and show it alone as a clean e-commerce product photo: "
         "front view, laid flat and neatly shaped (ghost mannequin style for clothing), centred, on a pure white "
         "background, soft even light. Keep its exact colour, fabric texture, pattern and stripe/check scale, collar, "
         "buttons, pockets, logos and details. No person, no hanger, no props, no text.")
    b = (f"Product photo of the {what} from image 1 on a plain white background, front view, every detail, colour "
         "and pattern exactly the same. Nothing else in the picture.")
    return [a, b]


# ---------------------------------------------------------------- 提示词
PIECE_EN = {"shirt": "shirt", "pants": "trousers", "shoes": "shoes"}
WEAR_EN = {
    "tuck": {"in": "The shirt is tucked neatly into the trousers, waistband visible.",
             "out": "The shirt is worn untucked, hem hanging outside over the trousers."},
    "sleeves": {"down": "Shirt sleeves are worn full length with cuffs buttoned.",
                "rolled": "Shirt sleeves are rolled up to just below the elbow."},
    "collar": {"closed": "The shirt is buttoned all the way up to the collar.",
               "open": "The top shirt button is undone, collar open, no tie."},
}
KEEP = ("Keep the person's face, hair, skin tone, body shape, pose, camera framing, background and lighting "
        "exactly as in image 1.")


def named(item, fallback):
    n = (item.get("name") or "").strip()
    return f"{fallback} ({n})" if n else fallback


def wear_text(wear):
    return " ".join(WEAR_EN[k][wear[k]] for k in ("tuck", "sleeves", "collar"))


def clothes_prompts(pieces, wear):
    """一次换上全部单品（即梦 / fal 流程）。图 1 是人，图 2.. 依次是各单品；配饰写成 ("accessory", item)。"""
    label = lambda c, it: named(it, PIECE_EN[c]) if c != "accessory" else (it.get("name") or "").strip() or "accessory"
    refs = ", ".join(f"the {label(c, it)} from image {i + 2}" + (" (worn where it naturally belongs, at realistic scale)"
                                                                  if c == "accessory" else "")
                     for i, (c, it) in enumerate(pieces))
    wear_txt = wear_text(wear) if any(c == "shirt" for c, _ in pieces) else ""
    fidelity = ("Reproduce every garment faithfully: same colour, fabric texture, pattern and stripe scale, collar "
                "shape, buttons, stitching and logos. Do not invent details. Fabric should drape, crease and cast "
                "shadows naturally under the scene's lighting. Output one photorealistic photo.")
    a = f"Image 1 is a photo of a person. Dress this exact person in {refs}. {wear_txt} {KEEP} " \
        f"Anything not being replaced stays as it is in image 1. {fidelity}"
    b = (f"Editorial fashion photograph: the same person from image 1, same pose and setting, now wearing {refs}. "
         f"{wear_txt} {KEEP} {fidelity}")
    return [a, b]


def finish_prompts(shoes, wear, has_shirt):
    """试穿模型换好衬衫裤子之后：换鞋 + 把穿法（塞/放、袖子、领口）调到位。"""
    todo = []
    if shoes:
        todo.append(f"Replace only the person's shoes with the {named(shoes, 'shoes')} from image 2, matching "
                    "its colour, material and shape at realistic scale.")
    if has_shirt:
        todo.append(f"Adjust only how the shirt is worn: {wear_text(wear)} Keep the shirt's colour, fabric, "
                    "stripe or check scale, collar and buttons exactly as in image 1.")
    rest = ("The trousers stay exactly as in image 1. " if has_shirt else "") + KEEP
    a = f"Image 1 is a photo of a person. {' '.join(todo)} {rest} Output one photorealistic photo."
    b = f"Fashion styling photograph of the same person from image 1, unchanged. {' '.join(todo)} {rest}"
    return [a, b]


def portrait_prompts():
    """把一张随手拍的全身照整理成标准形象照：同一个人、正面站姿、贴身基础款、干净背景。"""
    who = "woman" if P.env("GENDER", "woman") == "woman" else "man"
    base = ("a plain fitted white short-sleeve crew-neck T-shirt tucked into slim straight light-grey trousers, "
            "and plain white low sneakers")
    keep = (f"Exactly the same {who} as image 1: identical face and facial features, hairstyle and hair colour, "
            "skin tone, body shape, height and proportions. Do not beautify, slim or alter the person's identity.")
    pose = ("Full body from head to feet, standing upright facing the camera, arms relaxed and slightly away from "
            "the body, hands visible, feet slightly apart. Long hair placed behind the shoulders so the neck, "
            "collarbones and shoulders are visible.")
    scene = ("Plain light warm-grey seamless studio background, soft even daylight from the front, no props, "
             "photorealistic, sharp focus, natural skin texture.")
    a = f"Image 1 is a photo of a {who}. Create a clean full-body studio photo. {keep} {pose} " \
        f"Wearing {base}. {scene}"
    b = f"Studio lookbook base photo of the {who} from image 1, unchanged identity. {pose} Wearing {base}. {scene}"
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


def build_plan(req: TryOn, items, pipeline=None, editor=None):
    """把一身搭配拆成几步模型调用。每步: kind / label / provider / 参考图文件名 / 提示词。"""
    pipeline, editor = pipeline or PIPELINE, editor or EDITOR

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
    shirt = get(req.shirt_id, "shirt") if req.shirt_id else None
    pants = get(req.pants_id, "pants") if req.pants_id else None
    shoes = get(req.shoes_id, "shoes") if req.shoes_id else None
    accs = [get(i, "accessory") for i in sorted(set(req.accessory_ids))]
    if len(accs) > MAX_ACCESSORIES:
        raise HTTPException(400, f"配饰一次最多 {MAX_ACCESSORIES} 件，太多会明显掉质量。")
    if not (shirt or pants or shoes or accs):
        raise HTTPException(400, "还没选任何单品。至少选一件衬衫、裤子、鞋子或配饰。")

    steps = []
    if pipeline == "seedream":  # 一个模型一次出图：人 + 衬衫 + 裤子 + 鞋 + 配饰
        pieces = [(c, it) for c, it in (("shirt", shirt), ("pants", pants), ("shoes", shoes)) if it] + \
                 [("accessory", it) for it in accs]
        steps.append({"kind": "edit", "provider": "seedream", "label": "换上这一身",
                      "refs": [it["file"] for _, it in pieces], "prompts": clothes_prompts(pieces, wear)})
        accs = []
    elif pipeline == "fal":
        pieces = [(c, it) for c, it in (("shirt", shirt), ("pants", pants), ("shoes", shoes)) if it]
        if pieces:
            steps.append({"kind": "edit", "provider": "fal", "label": "换上" + "、".join(
                {"shirt": "衬衫", "pants": "裤子", "shoes": "鞋子"}[c] for c, _ in pieces),
                "refs": [it["file"] for _, it in pieces], "prompts": clothes_prompts(pieces, wear)})
        if req.refine and shirt:  # 注意：专用试穿模型可能把塞进去的下摆重新放出来
            steps.append({"kind": "tryon1", "provider": "fal", "label": "加强衬衫细节", "refs": [shirt["file"]]})
    else:
        if pipeline == "bailian" and (shirt or pants):
            steps.append({"kind": "tryon2", "provider": "bailian",
                          "label": "换上" + "和".join(x for x, it in (("衬衫", shirt), ("裤子", pants)) if it),
                          "top": shirt and shirt["file"], "bottom": pants and pants["file"]})
            if req.refine:
                steps.append({"kind": "refine", "provider": "bailian", "label": "精修衣服细节",
                              "top": shirt and shirt["file"], "bottom": pants and pants["file"]})
        if pipeline == "kling":
            # 叠穿顺序决定塞法：先衬衫后裤子 = 裤腰压住下摆；先裤子后衬衫 = 下摆盖住裤腰
            order = [("衬衫", shirt), ("裤子", pants)] if req.tuck == "in" else [("裤子", pants), ("衬衫", shirt)]
            for label, it in order:
                if it:
                    steps.append({"kind": "tryon1", "provider": "kling", "label": f"换上{label}", "refs": [it["file"]]})
        if shirt or shoes:
            steps.append({"kind": "edit", "provider": editor,
                          "label": "换鞋、调整穿法" if shoes and shirt else ("换鞋" if shoes else "调整穿法"),
                          "refs": [shoes["file"]] if shoes else [], "prompts": finish_prompts(shoes, wear, bool(shirt))})
    if accs:
        editor_acc = "fal" if pipeline == "fal" else editor
        n = P.EDIT_MAX_IMAGES[editor_acc] - 1
        for k in range(0, len(accs), n):
            chunk = accs[k:k + n]
            steps.append({"kind": "edit", "provider": editor_acc, "label": "戴上配饰" if len(accs) <= n else
                          f"戴上配饰（{k // n + 1}）", "refs": [it["file"] for it in chunk],
                          "prompts": accessory_prompts(chunk)})

    price_kind = {"edit": "edit", "tryon1": "tryon", "tryon2": "tryon", "refine": "refine"}
    cost = sum(P.price(price_kind[s["kind"]], s["provider"]) for s in steps)
    models = {k: P.env(k) for k in ("BL_TRYON_MODEL", "BL_EDIT_MODEL", "KLING_TRYON_MODEL", "FAL_EDIT_MODEL",
                                    "FAL_VTON_MODEL", "RESOLUTION")}
    sig = json.dumps([person["file"], steps, pipeline, models, MOCK], ensure_ascii=False, sort_keys=True)
    return {"person": person, "steps": steps, "pipeline": pipeline, "key": hashlib.sha1(sig.encode()).hexdigest(),
            "cost": 0 if MOCK else round(cost, 2)}


def mock_step(current: Path, refs: List[Path], n: int, out_dir: Path) -> Path:
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
    out = out_dir / f"_tmp_{uuid.uuid4().hex}.jpg"
    im.save(out, quality=90)
    return out


async def fetch_image(client, src) -> Image.Image:
    if isinstance(src, Path):
        return Image.open(src).convert("RGB")
    if src.startswith("data:"):
        import base64
        return Image.open(io.BytesIO(base64.b64decode(src.split(",", 1)[1]))).convert("RGB")
    r = await client.get(src, follow_redirects=True)
    r.raise_for_status()
    return Image.open(io.BytesIO(r.content)).convert("RGB")


async def execute(plan, client, on_step=lambda i, s: None, mock=None, item_dir=ITEMS):
    """按计划逐步调用模型，返回最后一张图（URL 或本地 Path）。"""
    mock = MOCK if mock is None else mock
    person = item_dir / plan["person"]["file"]
    f = lambda name: item_dir / name if name else None
    current, tmp = person, []
    try:
        for i, s in enumerate(plan["steps"]):
            on_step(i, s)
            refs = [f(x) for x in s.get("refs", [])] + [f(s[k]) for k in ("top", "bottom") if s.get(k)]
            if mock:
                await asyncio.sleep(0.6)
                current = mock_step(current, refs, i, RESULTS)
                tmp.append(current)
                continue
            if s["provider"] in P.EDIT and not P.ready(s["provider"]):
                raise P.GenError(f"“{s['label']}”这一步要用{P.NAMES[s['provider']]}，但 .env 里还没填它的 key。")
            if s["kind"] == "edit":
                current = await P.EDIT[s["provider"]](client, [current] + refs, s["prompts"])
            elif s["kind"] == "tryon2":
                current = await P.bailian_tryon(client, current, f(s.get("top")), f(s.get("bottom")))
            elif s["kind"] == "refine":
                current = await P.bailian_refine(client, current, person, f(s.get("top")), f(s.get("bottom")))
            elif s["provider"] == "kling":
                current = await P.kling_tryon(client, current, refs[0])
            else:
                current = await P.fal_tryon(client, current, refs[0])
        return await fetch_image(client, current)
    finally:
        for t in tmp:
            t.unlink(missing_ok=True)


JOBS = {}


TRIES = max(1, int(P.env("AUTO_RETRY", "2")))  # 一身最多生成几次（自动检查不合格时重来）


def check_refs(req: TryOn):
    """自动检查用的对照图：原照 + 这一身的每件单品。"""
    items = db_load()["items"]
    refs = [("person", ITEMS / items[req.person_id]["file"])]
    for label, i in (("shirt", req.shirt_id), ("trousers", req.pants_id), ("shoes", req.shoes_id)):
        if i in items:
            refs.append((label, ITEMS / items[i]["file"]))
    refs += [(items[i].get("name") or "accessory", ITEMS / items[i]["file"]) for i in req.accessory_ids if i in items]
    return refs


async def run_job(job, req: TryOn, plan):
    try:
        checking = not MOCK and P.ready("seedream") and P.env("AUTO_CHECK", "1") != "0"
        wear = wear_text({"tuck": req.tuck, "sleeves": req.sleeves, "collar": req.collar}) if req.shirt_id else ""
        best, qa = None, None
        async with httpx.AsyncClient(timeout=180) as client:
            for attempt in range(TRIES if checking else 1):
                im = await execute(plan, client, lambda i, s: job.update(
                    step_index=i, step=s["label"] if attempt == 0 else "效果不理想，重新生成"))
                if not checking:
                    best = im
                    break
                job.update(step_index=len(plan["steps"]), step="检查效果")
                buf = io.BytesIO()
                im.save(buf, "JPEG", quality=90)
                r = await P.ark_judge(client, check_refs(req), "data:image/jpeg;base64," +
                                      __import__("base64").b64encode(buf.getvalue()).decode(), wear)
                score = -1 if r is None else sum(r[k] for k in P.CHECKS)
                if best is None or score > (-1 if qa is None else sum(qa[k] for k in P.CHECKS)):
                    best, qa = im, r
                if r is None or score == len(P.CHECKS):
                    break
        im = best
        job.update(step="保存结果")
        look_id = uuid.uuid4().hex[:12]
        im.save(RESULTS / f"{look_id}.jpg", quality=95)
        look = {"id": look_id, "key": plan["key"], "file": f"{look_id}.jpg", "created": time.time(),
                "cost": plan["cost"], "demo": MOCK, "pipeline": plan["pipeline"], "check": qa,
                "sel": req.model_dump(exclude={"force"})}
        with _lock:
            db = db_load()
            for old in [k for k, v in db["looks"].items() if v["key"] == plan["key"]]:  # 重新生成时替换旧图
                for f in {db["looks"][old]["file"], *db["looks"][old].get("turn", [])}:
                    (RESULTS / f).unlink(missing_ok=True)
                del db["looks"][old]
            db["looks"][look_id] = look
            db_save(db)
        job.update(status="done", look=look)
    except P.GenError as e:
        job.update(status="error", error=str(e))
    except Exception as e:  # 网络断开等
        job.update(status="error", error=f"生成中断：{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 接口
app = FastAPI(title="衬衫衣橱")
app.mount("/media", StaticFiles(directory=DATA), name="media")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


@app.get("/api/state")
def state():
    with _lock:
        db = db_load()
        if [it for it in db["items"].values() if ensure_cut(it)]:  # 旧单品补上透明底展示图
            db_save(db)
    return {"items": sorted(db["items"].values(), key=lambda x: -x["created"]),
            "looks": sorted(db["looks"].values(), key=lambda x: -x["created"]),
            "config": {"demo": MOCK, "pipeline": PIPELINE, "pipeline_name": P.NAMES[PIPELINE],
                       "editor_name": P.NAMES[EDITOR], "max_accessories": MAX_ACCESSORIES,
                       "portrait_cost": 0 if MOCK else P.price("edit", EDITOR),
                       "tidy_cost": 0 if MOCK else P.price("edit", EDITOR),
                       "turn_cost": 0 if MOCK else round(P.price("edit", "seedream") * TURN_VIEWS, 2)}}


@app.post("/api/items")
async def add_item(file: UploadFile = File(...), category: str = Form(...), name: str = Form(""),
                   tidy: str = Form("")):
    """tidy=1：照片里有模特或背景乱时，先用即梦整理成干净的单品图，再放进衣橱（换装也更准）。"""
    if category not in CATEGORIES:
        raise HTTPException(400, "不认识的分类。")
    try:
        im = normalize_image(await file.read())
    except Exception:
        raise HTTPException(400, f"“{file.filename}” 不是能打开的图片。支持 JPG、PNG、WebP、HEIC。")
    item_id = uuid.uuid4().hex[:12]
    item = {"id": item_id, "category": category, "name": name.strip()[:40], "file": f"{item_id}.jpg",
            "created": time.time()}
    if tidy == "1" and category in GARMENTS and not MOCK:
        if not P.ready(EDITOR):
            raise HTTPException(400, f"整理单品图要用{P.NAMES[EDITOR]}，.env 里还没填它的 key。")
        im.save(ITEMS / f"{item_id}_orig.jpg", quality=92)
        item["orig"] = f"{item_id}_orig.jpg"
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                im = normalize_image(_jpeg(await fetch_image(client, await P.EDIT[EDITOR](
                    client, [ITEMS / item["orig"]], tidy_prompts(category, item["name"])))))
        except P.GenError as e:
            raise HTTPException(502, str(e))
    im.save(ITEMS / item["file"], quality=92)
    await asyncio.to_thread(ensure_cut, item)
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
            for f in (it["file"], it.get("cut"), it.get("orig")):
                if f:
                    (ITEMS / f).unlink(missing_ok=True)
            db_save(db)
    return {"ok": True}


TURN_VIEWS = 5  # 正面之外再生成 5 个角度，连同正面一共 6 帧


def turn_prompt():
    she = "she" if P.env("GENDER", "woman") == "woman" else "he"
    her = "her" if she == "she" else "his"
    return (f"Image 1 shows a person wearing an outfit. Generate {TURN_VIEWS} separate photos of exactly the same "
            f"person in exactly the same outfit, as if {she} slowly turns around on a turntable: same face, hair, "
            "body shape, same shirt with identical colour and pattern scale, worn the same way, same trousers, shoes "
            "and accessories, same plain studio background and lighting, same relaxed standing pose. The camera stays "
            "at the same height and distance; full body from head to feet in every photo. In order: "
            f"1) three-quarter view turned to {her} right, 2) {her} right side profile, 3) back view, "
            f"4) {her} left side profile, 5) three-quarter view turned to {her} left. Photorealistic.")


async def run_turn(job, look_id):
    try:
        lk = db_load()["looks"][look_id]
        src = RESULTS / lk["file"]
        frames = []
        if MOCK:
            base = Image.open(src).convert("RGB")
            for i in range(TURN_VIEWS):
                job.update(step=f"生成角度 {i + 1}/{TURN_VIEWS}")
                await asyncio.sleep(0.3)
                im = ImageOps.mirror(base) if i % 2 == 0 else base.copy()
                ImageDraw.Draw(im).text((20, 20), f"DEMO VIEW {i + 1}", fill="white")
                frames.append(im)
        else:
            if not P.ready("seedream"):
                raise P.GenError("转一圈要用即梦，.env 里还没填 ARK_API_KEY。")
            job.update(step="生成多个角度")
            refs = [src]
            shirt = db_load()["items"].get((lk.get("sel") or {}).get("shirt_id") or "")
            if shirt:
                refs.append(ITEMS / shirt["file"])
            async with httpx.AsyncClient(timeout=600) as client:
                urls = await P.seedream_views(client, refs, turn_prompt(), TURN_VIEWS)
                frames = [await fetch_image(client, u) for u in urls]
        names = []
        for i, im in enumerate(frames):
            names.append(f"{look_id}_v{i + 1}.jpg")
            im.save(RESULTS / names[-1], quality=92)
        with _lock:
            db = db_load()
            if look_id in db["looks"]:
                db["looks"][look_id]["turn"] = [lk["file"]] + names
                db_save(db)
                job.update(status="done", look=db["looks"][look_id])
            else:
                job.update(status="error", error="这套搭配已经被删除。")
    except P.GenError as e:
        job.update(status="error", error=str(e))
    except Exception as e:
        job.update(status="error", error=f"生成中断：{type(e).__name__}: {e}")


@app.post("/api/looks/{look_id}/turn")
async def make_turn(look_id: str):
    """给一套已生成的搭配补上多个角度，界面上就能自然转动。"""
    lk = db_load()["looks"].get(look_id)
    if not lk:
        raise HTTPException(404, "这套搭配已经被删除。")
    if lk.get("turn"):
        return {"cached": True, "look": lk}
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "step": "排队中", "step_index": 0, "steps": ["生成多个角度"]}
    JOBS[job["id"]] = job
    asyncio.create_task(run_turn(job, look_id))
    return {"cached": False, "job": job}


@app.delete("/api/looks/{look_id}")
def delete_look(look_id: str):
    with _lock:
        db = db_load()
        lk = db["looks"].pop(look_id, None)
        if lk:
            for f in {lk["file"], *lk.get("turn", [])}:
                (RESULTS / f).unlink(missing_ok=True)
            db_save(db)
    return {"ok": True}


@app.post("/api/items/{item_id}/portrait")
async def make_portrait(item_id: str):
    """用一张自己的照片生成标准形象照，存成一张新的“照片”。原照保留，两张都能用来试穿。"""
    it = db_load()["items"].get(item_id)
    if not it or it["category"] != "person":
        raise HTTPException(404, "这张照片已经被删除。")
    src = ITEMS / it["file"]
    if MOCK:
        await asyncio.sleep(0.8)
        out = mock_step(src, [], 0, RESULTS)
        im = Image.open(out).convert("RGB")
        out.unlink(missing_ok=True)
    else:
        if not P.ready(EDITOR):
            raise HTTPException(400, f"生成形象照要用{P.NAMES[EDITOR]}的图像编辑，.env 里还没填它的 key。")
        try:
            async with httpx.AsyncClient(timeout=180) as client:
                im = await fetch_image(client, await P.EDIT[EDITOR](client, [src], portrait_prompts()))
        except P.GenError as e:
            raise HTTPException(502, str(e))
    new_id = uuid.uuid4().hex[:12]
    normalize_image(_jpeg(im)).save(ITEMS / f"{new_id}.jpg", quality=92)
    item = {"id": new_id, "category": "person", "name": "形象照" + (f"（{it['name']}）" if it.get("name") else ""),
            "file": f"{new_id}.jpg", "created": time.time(), "from": item_id}
    with _lock:
        db = db_load()
        db["items"][new_id] = item
        db_save(db)
    return item


def _jpeg(im):
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=95)
    return buf.getvalue()


@app.post("/api/items/{item_id}/tidy")
async def tidy_item(item_id: str):
    """把已有单品（模特图、背景乱的图）整理成干净的白底单品图，原图保留。"""
    it = db_load()["items"].get(item_id)
    if not it or it["category"] not in GARMENTS:
        raise HTTPException(404, "这件单品已经被删除。")
    if MOCK:
        raise HTTPException(400, "演示模式不会真的整理。在 .env 里填好 ARK_API_KEY 后再试。")
    if not P.ready(EDITOR):
        raise HTTPException(400, f"整理单品图要用{P.NAMES[EDITOR]}，.env 里还没填它的 key。")
    orig = it.get("orig") or it["file"]
    try:
        async with httpx.AsyncClient(timeout=180) as client:
            im = normalize_image(_jpeg(await fetch_image(client, await P.EDIT[EDITOR](
                client, [ITEMS / orig], tidy_prompts(it["category"], it.get("name", ""))))))
    except P.GenError as e:
        raise HTTPException(502, str(e))
    new_file = f"{item_id}_t{int(time.time())}.jpg"
    im.save(ITEMS / new_file, quality=92)
    upd = {**it, "orig": orig, "file": new_file, "cutv": 0}
    await asyncio.to_thread(ensure_cut, upd)
    with _lock:
        db = db_load()
        cur = db["items"].get(item_id)
        if not cur:
            raise HTTPException(404, "这件单品已经被删除。")
        for f in {cur["file"], cur.get("cut")} - {orig, None}:
            (ITEMS / f).unlink(missing_ok=True)
        cur.update({k: upd.get(k) for k in ("orig", "file", "cut", "cutv", "person")})
        db_save(db)
        return cur


@app.post("/api/plan")
def preview_plan(req: TryOn):
    """界面上显示要走哪几步、大概多少钱、是否已经生成过。"""
    db = db_load()
    plan = build_plan(req, db["items"])
    cached = any(lk["key"] == plan["key"] for lk in db["looks"].values())
    return {"steps": [s["label"] for s in plan["steps"]], "cost": plan["cost"], "cached": cached}


@app.post("/api/tryon")
async def tryon(req: TryOn):
    db = db_load()
    plan = build_plan(req, db["items"])
    if not req.force:
        for lk in db["looks"].values():
            if lk["key"] == plan["key"]:
                return {"cached": True, "look": lk}
    checking = not MOCK and P.ready("seedream") and P.env("AUTO_CHECK", "1") != "0"
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "step": "排队中", "step_index": 0,
           "steps": [s["label"] for s in plan["steps"]] + (["检查效果"] if checking else [])}
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
    mode = "演示模式：还没填 key" if MOCK else f"换装：{P.NAMES[PIPELINE]}，编辑：{P.NAMES[EDITOR]}"
    print(f"\n  衬衫衣橱已启动: http://127.0.0.1:{port}   （{mode}）\n")
    if os.environ.get("NO_BROWSER") != "1":
        threading.Timer(1.0, lambda: webbrowser.open(f"http://127.0.0.1:{port}")).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

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

CATEGORIES = {"person", "shirt", "pants", "belt", "shoes", "hat", "accessory"}
MAX_ACCESSORIES = 3

# ---------------------------------------------------------------- 出错记录
def log_error(where, e):
    """出错时打印到终端，并追加到 data/errors.log，方便把原因发出来排查（不会记录 key）。"""
    import traceback
    msg = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {where}: {type(e).__name__}: {e}"
    detail = "" if isinstance(e, P.GenError) else traceback.format_exc()
    print("\n  ✗ " + msg + ("\n" + detail if detail else ""), flush=True)
    try:
        with open(DATA / "errors.log", "a", encoding="utf-8") as f:
            f.write(msg + "\n" + detail + "\n")
    except Exception:
        pass


def friendly(e):
    try:  # 连的是哪个地址：方舟接口，还是生成好的视频/图片的下载地址
        host = e.request.url.host
    except Exception:
        host = ""
    where = "" if not host else ("（下载生成结果的地址 " + host + "）" if "ark." not in host else "（" + host + "）")
    if isinstance(e, (httpx.ConnectError, httpx.ProxyError, httpx.ConnectTimeout)):
        return (f"连不上即梦（火山方舟）的服务器{where}：{type(e).__name__}。检查网络；开着 VPN/代理的话先关掉再试。"
                "可以运行 python check.py 诊断。")
    if isinstance(e, httpx.TimeoutException):
        return "即梦那边太久没出图，已放弃。稍后再试一次；一直这样的话运行 python check.py 诊断。"
    return f"生成中断：{type(e).__name__}: {e}"


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


GARMENTS = {"shirt", "pants", "belt", "shoes", "hat", "accessory"}


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


CUT_VERSION = 5
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
    if category in ("accessory", "belt"):  # 项链、手链、卷起来的腰带中间的白底也去掉
        r, g, bl = rgb.split()
        white = ImageChops.darker(r, ImageChops.darker(g, bl)).point(lambda v: 0 if v >= 240 else 255)
        mask = ImageChops.multiply(mask, white.filter(ImageFilter.MinFilter(3)))
    box = mask.point(lambda v: 255 if v > 24 else 0).getbbox()
    if not box or (box[2] - box[0]) * (box[3] - box[1]) < 0.02 * rgb.width * rgb.height:
        return None
    out = rgb.copy()
    out.putalpha(mask)
    m = 10
    return out.crop((max(box[0] - m, 0), max(box[1] - m, 0), min(box[2] + m, out.width), min(box[3] + m, out.height))), person


def hang_fit(path, category, own=False):
    """量一下透明底单品图，界面按这个把衣服对准衣架。
    衬衫：从上往下找到领口真正开始的地方（宽度突然变大），把上面露出来的衣架钩、人台头裁掉；
    再在肩部那一段找最宽处当肩宽。返回 肩宽/图宽、肩部中心、领口中心、高宽比。"""
    try:
        import numpy as np
    except Exception:
        return None
    im = Image.open(path)
    a = np.asarray(im.split()[-1]) > 128
    h, w = a.shape
    cols = np.arange(w)
    rows = []
    for y in range(h):
        xs = cols[a[y]]
        rows.append((xs[0], xs[-1]) if xs.size else None)
    width = np.array([(r[1] - r[0]) if r else 0 for r in rows], dtype=float)
    if width.max() <= 0:
        return None
    fill = round(float(a.mean()), 3)  # 实心部分占整张图多少，鞋架上按这个把大小调一致
    if own:  # AI 整理过、自带衣架的：保留木衣架；它自己的挂钩（如果有）裁掉，统一用界面画的挂钩
        # 挂钩：最上面一段宽度都不到衣服最大宽度 13% 的部分（钩子的圈也很窄），整段裁掉，从衣架顶端开始。
        # 细铁丝钩抠出来常常是半透明的（看得见，但不到一半不透明），所以找“最上面有东西”时连淡的也算上
        bar = int(np.argmax(width >= 0.13 * width.max()))
        faint = np.asarray(im.split()[-1]) > 24
        top = int(np.argmax(faint.any(axis=1)))
        if bar - top >= 0.005 * h and bar > 1:
            im.crop((0, max(bar - 1, 0), w, h)).save(path)
            return hang_fit(path, category, own=True)
        y0 = int(np.argmax(width > 0))
        ys = [y for y in range(h) if rows[y]][:max(3, int(h * 0.02))]
        hx = sum((rows[y][0] + rows[y][1]) / 2 for y in ys) / len(ys) / w
        return {"own": True, "hx": round(hx, 3), "ty": round(y0 / h, 4), "ar": round(h / w, 3), "fill": fill, "v": 8}
    # 自带衣架（你自己挂着拍的）：最上面是一截很细、居中的挂钩。
    # 这种不裁、不画我们的衣架，直接把它自己的挂钩挂到衣杆上。
    if category in ("shirt", "pants"):
        y0 = int(np.argmax(width > 0))
        thin = 0
        while y0 + thin < h and 0 < width[y0 + thin] < 0.08 * width.max():
            thin += 1
        if thin >= 0.02 * h and rows[y0]:
            hx = (rows[y0][0] + rows[y0][1]) / 2 / w
            if 0.3 < hx < 0.7:
                return {"own": True, "hx": round(hx, 3), "ar": round(h / w, 3), "fill": fill, "v": 4}
    top = 0
    if category == "shirt":
        # 领口：宽度第一次超过最大宽度的 30%，并且之后几行都保持住（避开细的挂钩、人台脖子）
        big = width >= 0.3 * width.max()
        for y in range(h - 6):
            if big[y:y + 6].all():
                top = y
                break
        # 人台/模特的头：上面先宽（头）再窄（脖子）再变宽（肩），在脖子最细处裁掉
        sm = np.convolve(width, np.ones(5) / 5, mode="same")
        lo, hi = top, top + int(h * 0.4)
        if hi - lo > 20:
            y_neck = lo + 5 + int(np.argmin(sm[lo + 5:hi]))
            head = sm[lo:y_neck].max() if y_neck > lo else 0
            below = sm[y_neck:hi].max()
            if head > 1.25 * sm[y_neck] and below > 1.8 * sm[y_neck]:
                top = y_neck
        if top > 4:
            im.crop((0, max(top - 3, 0), w, h)).save(path)
            return hang_fit(path, "shirt-trimmed")
    if not rows[top]:  # 上面是空白（抠图没裁边）：从第一行有东西的地方算
        top = int(np.argmax(width > 0))
    seg = slice(top, top + max(int(h * 0.25), 1))
    y_sh = top + int(np.argmax(width[seg]))
    l, r = rows[y_sh]
    t = rows[top] or rows[y_sh]
    return {"sw": round((r - l) / w, 3), "cx": round((l + r) / 2 / w, 3), "nx": round((t[0] + t[1]) / 2 / w, 3),
            "ar": round(h / w, 3), "fill": fill, "v": 4}


def person_box(path):
    """照片里人的位置（头顶、脚底、中心线、宽度，都是 0–1），镜子里按这个把人放到和试穿图一样大。
    有 rembg 就用抠图模型找人（背景再乱也行），没有就当白底找非白色的部分。"""
    try:
        import numpy as np
        im = Image.open(path).convert("RGB")
        im.thumbnail((512, 512))
        try:
            from rembg import remove
            m = np.asarray(remove(im, session=_session(P.env("REMBG_MODEL", "u2net")), only_mask=True)) > 128
        except ImportError:
            a = np.asarray(im).astype(np.int16)
            m = (a.min(axis=2) < 205) | (a.max(axis=2) - a.min(axis=2) > 40)
        ys, xs = np.nonzero(m)
        if len(ys) < 0.01 * m.size:
            return None
        h, w = m.shape
        y0, y1 = ys.min() / h, (ys.max() + 1) / h
        if y1 - y0 < 0.2:
            return None
        return {"y0": round(y0, 4), "y1": round(y1, 4), "cx": round(float(xs.mean()) / w, 4),
                "w": round((xs.max() - xs.min() + 1) / w, 4), "cut": bool(y1 > 0.985), "v": 1}
    except Exception as e:
        log_error("person_box", e)
        return None


def ensure_cut(it):
    """给单品补一张透明底展示图（只用于界面，给模型的仍是原图）。返回是否有改动。"""
    if it["category"] not in GARMENTS:
        return False
    if it.get("cutv") == CUT_VERSION:
        if it.get("cut") and (it.get("fit") or {}).get("v") not in (4, 8) or (it.get("hanger") and (it.get("fit") or {}).get("v") != 8) and (ITEMS / it["cut"]).exists():
            it["fit"] = hang_fit(ITEMS / it["cut"], it["category"], bool(it.get("hanger")))
            return True
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
    it.pop("fit", None)
    if out is not None:
        it["cut"] = f"{it['id']}_cut{CUT_VERSION}.png"
        out.save(ITEMS / it["cut"])
        it["fit"] = hang_fit(ITEMS / it["cut"], it["category"], bool(it.get("hanger")))
    return True


def tidy_prompts(category, name):
    """整理成单品图：只要衣服本身，像挂在一个看不见的衣架上；不要模特、人台、假人、衣架。"""
    hanger = category in ("shirt", "pants")
    nobody = ("Absolutely no person, no mannequin, no dress form, no ghost-mannequin neck, no body parts, "
              + ("" if hanger else "no hanger, no clip, ") + "no props, no text, no shadow box.")
    keep = ("Keep its exact colour, fabric texture, pattern and stripe/check scale and spacing, collar, buttons, "
            "pockets, logos and details.")
    look = {
        "shirt": "front view, hanging on a slim natural wooden hanger with a thin silver hook at the top centre: "
                 "the shoulders rest on the hanger, collar standing around the empty neck opening, sleeves hanging "
                 "straight down beside the body, natural soft folds, hem straight; the whole hanger and hook visible",
        "pants": "front view, hanging from a wooden trouser hanger with two small clips holding the waistband and a "
                 "thin silver hook at the top centre, legs falling naturally side by side",
        "shoes": "the pair side by side, three-quarter front view",
        "belt": "the belt laid out straight and flat, buckle at one end, front view",
        "hat": "three-quarter front view, sitting on nothing",
        "accessory": "front view, laid out neatly",
    }[category]
    what = {"shirt": "shirt", "pants": "trousers", "shoes": "pair of shoes", "belt": "belt", "hat": "hat",
            "accessory": name or "accessory"}[category]
    a = (f"Extract only the {what} from image 1 and show it alone as a clean e-commerce product photo on a pure white "
         f"background, soft even light, centred, {look}. {keep} {nobody}")
    b = (f"Product photo of only the {what} from image 1 on a plain white background, {look}. Every detail, colour "
         f"and pattern exactly the same. {nobody}")
    return [a, b]


# ---------------------------------------------------------------- 提示词
PIECE_EN = {"shirt": "shirt", "pants": "trousers", "shoes": "shoes", "belt": "belt", "hat": "hat"}
WEAR_EN = {
    "tuck": {"in": "The shirt is tucked neatly into the trousers, waistband visible.",
             "out": "The shirt is worn untucked, hem hanging outside over the trousers."},
    "sleeves": {"down": "Shirt sleeves are worn full length with cuffs buttoned.",
                "rolled": "Shirt sleeves are rolled up to just below the elbow."},
    "collar": {"closed": "The shirt is buttoned all the way up to the collar.",
               "open": "The top shirt button is undone, collar open, no tie."},
    # 姿势写死：不写的话模型每次随手换姿势（插兜、叉腰…），几套搭配放在一起不统一，转身视频也更容易乱动
    "pose": {"down": "Pose: standing upright facing the camera, weight on both feet, both arms hanging naturally "
                     "relaxed at the sides, hands open and relaxed next to the thighs, not in pockets.",
             "open": "Pose: standing upright facing the camera, weight on both feet, both arms held slightly away "
                     "from the body with a small gap between arms and torso, hands relaxed and open, not in pockets.",
             "pocket": "Pose: standing upright facing the camera, weight on both feet, the right hand casually in "
                       "the trouser pocket, the left arm hanging naturally relaxed at the side."},
}
WHITE = ("Background: a pure white seamless studio backdrop, exactly #FFFFFF, filling the whole frame edge to edge; "
         "no grey gradient, no vignette, no visible floor line or horizon, no texture; soft even light, "
         "only a faint natural shadow under the feet; nothing else in the background.")


def person_mask(im, fill=False):
    """人（连同身上的衣服、鞋）在哪：用本地抠图模型。没装 rembg 或抠不出来时返回 None。
    fill=True：把人形里的空缺补满。白裤子配白背景时抠图模型常常认不出裤子，但它上面是上衣、下面是鞋，
    按“每一列从最上到最下、每一行从最左到最右”填满，裤子所在的位置就被保护住了。"""
    try:
        from rembg import remove
    except ImportError:
        return None
    try:
        small = im.copy()
        small.thumbnail((640, 640))
        m = remove(small, session=_session(P.env("REMBG_MODEL", "u2net")), only_mask=True).point(lambda v: 255 if v > 40 else 0)
        if m.histogram()[255] < 0.03 * m.width * m.height:  # 几乎没抠到人：不可信
            return None
        if fill:
            import numpy as np
            a = np.asarray(m) > 0
            ys, xs = np.arange(a.shape[0])[:, None], np.arange(a.shape[1])[None, :]
            cols = a.any(axis=0)
            top = np.where(cols, a.argmax(axis=0), a.shape[0])
            bot = np.where(cols, a.shape[0] - 1 - a[::-1].argmax(axis=0), -1)
            rows = a.any(axis=1)
            left = np.where(rows, a.argmax(axis=1), a.shape[1])
            right = np.where(rows, a.shape[1] - 1 - a[:, ::-1].argmax(axis=1), -1)
            a = a | ((ys >= top) & (ys <= bot)) | ((xs >= left[:, None]) & (xs <= right[:, None]))
            m = Image.fromarray(a.astype(np.uint8) * 255, "L")
        # 往外多留一小圈（约图宽 1.2%），白衣服柔和的边缘、脚下的影子都算人的一部分，不去碰
        grow = max(3, round(m.width * 0.012)) | 1
        return m.filter(ImageFilter.MaxFilter(grow)).resize(im.size, Image.BILINEAR)
    except Exception as e:
        log_error("person_mask", e)
        return None


def whiten_bg(im, light=False):
    """把生成图里接近白色、并且和画面边缘连在一起的背景统一成纯白 #FFFFFF。
    模型说是白底，实际常常带一点灰或暗角；页面背景是固定的纯白，这样照片和背景就没有分界线。
    人（连同衣服和脚下的影子）先用抠图模型圈出来，圈里一个像素都不动，白裤子、白衬衫才不会被当成背景刷掉。
    没装 rembg 时：搭配里有白色/浅色单品（light=True）就不洗，宁可背景带点灰也不能毁衣服。"""
    try:
        import numpy as np
    except Exception:
        return im
    im = im.convert("RGB")
    keep = person_mask(im, fill=light)
    if keep is None and light:
        return im
    a = np.asarray(im).astype(np.int16)
    lo, hi = a.min(axis=2), a.max(axis=2)
    near = ((lo > 222) & (hi - lo < 16))
    if keep is not None:
        near &= np.asarray(keep) < 128
    near = near.astype(np.uint8) * 255
    full = Image.fromarray(near, "L")
    k = max(1, max(full.size) // 512)  # 在小图上找“和边缘连着的”区域，再放大回原图，快很多
    m = full.resize((full.width // k, full.height // k), Image.NEAREST).copy()  # copy：直接从数组来的图改不动
    w, h = m.size
    for x, y in [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]:
        if m.getpixel((x, y)) == 255:
            ImageDraw.floodfill(m, (x, y), 128)
    edge = np.asarray(m.point(lambda v: 255 if v == 128 else 0).resize(full.size, Image.NEAREST)
                      .filter(ImageFilter.MaxFilter(2 * k + 1))) > 0
    soft = max(1.5, im.width / 500)  # 洗白区域的边缘柔一点，人身边不留一圈硬边
    bg = Image.fromarray(((edge & (near > 0)) * 255).astype(np.uint8), "L").filter(ImageFilter.GaussianBlur(soft))
    return Image.composite(Image.new("RGB", im.size, (255, 255, 255)), im, bg)


_light_cache = {}


def is_light(item):
    """白色、米白这类很浅的单品：抠图里实心部分的平均亮度很高、颜色很淡。"""
    f = item.get("cut") or item.get("file")
    if not f:
        return False
    if f not in _light_cache:
        try:
            import numpy as np
            im = Image.open(ITEMS / f).convert("RGBA")
            im.thumbnail((200, 200))
            a = np.asarray(im).astype(float)
            on = a[..., 3] > 128
            if not on.any():
                on = np.ones(on.shape, bool)
            rgb = a[..., :3][on]
            lum = (rgb @ [0.299, 0.587, 0.114]).mean()
            chroma = (rgb.max(axis=1) - rgb.min(axis=1)).mean()
            _light_cache[f] = bool(lum > 218 and chroma < 32)
        except Exception:
            _light_cache[f] = False
    return _light_cache[f]


KEEP = ("Keep the person's face, hair, skin tone, body shape and camera framing exactly as in image 1; "
        "full body from head to feet. " + WHITE)


def named(item, fallback):
    n = (item.get("name") or "").strip()
    return f"{fallback} ({n})" if n else fallback


def wear_text(wear):
    return " ".join(WEAR_EN[k][wear[k]] for k in ("tuck", "sleeves", "collar"))


def pose_text(wear):
    return WEAR_EN["pose"][wear.get("pose") or "down"]


def clothes_prompts(pieces, wear):
    """一次换上全部单品（即梦 / fal 流程）。图 1 是人，图 2.. 依次是各单品；配饰写成 ("accessory", item)。"""
    label = lambda c, it: named(it, PIECE_EN[c]) if c != "accessory" else (it.get("name") or "").strip() or "accessory"
    where = {"accessory": " (worn where it naturally belongs, at realistic scale)",
             "belt": " (threaded through the trouser belt loops at the waist, buckle centred and clearly visible)",
             "hat": " (worn on the head naturally, hair adjusted around it)"}
    desc = lambda it: (f" — {it['desc']}" if it.get("desc") else "") + \
        (" (its photo shows it on a hanger; ignore the hanger)" if (it.get("fit") or {}).get("own") else "")
    refs = "; ".join(f"the {label(c, it)} from image {i + 2}{desc(it)}{where.get(c, '')}"
                     for i, (c, it) in enumerate(pieces))
    wear_txt = wear_text(wear) if any(c == "shirt" for c, _ in pieces) else ""
    fidelity = ("Reproduce every item faithfully from its own reference image: same colour, fabric texture, pattern "
                "and stripe scale, collar shape, buttons, stitching and logos; the shoes must be exactly the referenced "
                "shoes (same type, colours, laces and sole), not a generic pair. Do not invent details. Fabric should drape, crease and cast "
                "shadows naturally under the scene's lighting. Output one photorealistic photo.")
    pose = pose_text(wear)
    lights = [label(c, it) for c, it in pieces if c != "accessory" and is_light(it)]
    light = (f" The {' and the '.join(lights)} {'is' if len(lights) == 1 else 'are'} white or very light: keep "
             f"{'it' if len(lights) == 1 else 'them'} clearly separated from the white background with soft natural "
             "shading in the folds, a gentle shadow along the edges and a soft contact shadow at the feet; expose "
             "correctly, do not blow out the highlights, keep the fabric texture visible.") if lights else ""
    a = f"Image 1 is a photo of a person. Dress this exact person in {refs}. {wear_txt} {pose} {KEEP}{light} " \
        f"Anything not being replaced stays as it is in image 1. {fidelity}"
    b = (f"Editorial fashion photograph: the same person from image 1, same setting, now wearing {refs}. "
         f"{wear_txt} {pose} {KEEP}{light} {fidelity}")
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
    todo.append(pose_text(wear))
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
    scene = (WHITE + " Soft even daylight from the front, no props, "
             "photorealistic, sharp focus, natural skin texture.")
    a = f"Image 1 is a photo of a {who}. Create a clean full-body studio photo. {keep} {pose} " \
        f"Wearing {base}. {scene}"
    b = f"Studio lookbook base photo of the {who} from image 1, unchanged identity. {pose} Wearing {base}. {scene}"
    return [a, b]


def accessory_prompts(accs):
    refs = ", ".join(f"the {(it.get('name') or '').strip() or 'accessory'} from image {i + 2}" for i, it in enumerate(accs))
    a = (f"Image 1 is a photo of a person. Add these accessories to the person: {refs}. Each one is worn where it "
         "naturally belongs, at realistic scale, matching the reference's colour, material and details. Change "
         "nothing else: face, body, pose, clothing and lighting stay identical to image 1, on a pure white background. "
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
    belt_id: Optional[str] = None
    hat_id: Optional[str] = None
    accessory_ids: List[str] = []
    tuck: str = "in"
    sleeves: str = "down"
    collar: str = "open"
    pose: str = "down"  # down 双手自然下垂 / open 双手微张 / pocket 单手插兜
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
    wear = {"tuck": req.tuck, "sleeves": req.sleeves, "collar": req.collar, "pose": req.pose}
    for k, v in wear.items():
        if v not in WEAR_EN[k]:
            raise HTTPException(400, f"穿法参数不对：{k}={v}")
    shirt = get(req.shirt_id, "shirt") if req.shirt_id else None
    pants = get(req.pants_id, "pants") if req.pants_id else None
    shoes = get(req.shoes_id, "shoes") if req.shoes_id else None
    belt = get(req.belt_id, "belt") if req.belt_id else None
    hat = get(req.hat_id, "hat") if req.hat_id else None
    accs = [get(i, "accessory") for i in sorted(set(req.accessory_ids))]
    if len(accs) > MAX_ACCESSORIES:
        raise HTTPException(400, f"配饰一次最多 {MAX_ACCESSORIES} 件，太多会明显掉质量。")
    if not (shirt or pants or shoes or belt or hat or accs):
        raise HTTPException(400, "还没选任何单品。至少选一件衬衫、裤子、鞋子或配饰。")

    steps = []
    if pipeline != "seedream":
        accs = accs + [it for it in (belt, hat) if it]
    if pipeline == "seedream":  # 一个模型一次出图：人 + 衬衫 + 裤子 + 鞋 + 配饰
        pieces = [(c, it) for c, it in (("shirt", shirt), ("pants", pants), ("belt", belt), ("shoes", shoes),
                                        ("hat", hat)) if it] + [("accessory", it) for it in accs]
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
    bare = [{k: v for k, v in st.items() if k != "prompts"} for st in steps]  # 文字描述变了不算新搭配
    sig = json.dumps([person["file"], bare, wear, pipeline, models, MOCK], ensure_ascii=False, sort_keys=True)
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


def snapshot(look_id, req: TryOn):
    """把这身用到的原照和单品图各存一份，删了衣服以后这套搭配照样能看。"""
    import shutil
    items = db_load()["items"]
    out = {"pieces": [], "person_file": None}
    picks = [("person", req.person_id), ("shirt", req.shirt_id), ("pants", req.pants_id), ("belt", req.belt_id),
             ("shoes", req.shoes_id), ("hat", req.hat_id)] + \
            [("accessory", i) for i in req.accessory_ids]
    for n, (cat, iid) in enumerate(picks):
        it = items.get(iid or "")
        if not it:
            continue
        src = ITEMS / (it.get("cut") or it["file"])
        if not src.exists():
            continue
        name = f"{look_id}_{n}{src.suffix}"
        shutil.copyfile(src, RESULTS / name)
        if cat == "person":
            out["person_file"] = name
            out["person_pbox"] = it.get("pbox")
        else:
            out["pieces"].append({"category": cat, "name": it.get("name", ""), "file": name})
    return out


async def ensure_descs(ids):
    """给这身用到、还没有文字描述的单品补上描述（每件只写一次，存进衣橱）。"""
    if MOCK or not P.ready("seedream"):
        return
    items = db_load()["items"]
    todo = [items[i] for i in ids if i in items and items[i]["category"] in GARMENTS and not items[i].get("desc")]
    if not todo:
        return
    async with httpx.AsyncClient(timeout=P.timeout(60)) as client:
        got = await asyncio.gather(*(P.ark_describe(client, ITEMS / it["file"], it["category"]) for it in todo))
    with _lock:
        db = db_load()
        for it, d in zip(todo, got):
            if d and it["id"] in db["items"]:
                db["items"][it["id"]]["desc"] = d
        db_save(db)


PART_ZH = {"shirt": "衬衫", "pants": "裤子", "belt": "腰带", "shoes": "鞋子", "hat": "帽子", "accessory": "配饰",
           "face": "长相", "wear": "穿法和姿势", "background": "白底", "light": "白色单品的轮廓"}


def fix_inputs(sel, parts, note="", snap=None):
    """哪里不对修哪里：按要修的部分挑参考图、写指令。图 1 是要修的结果图，后面依次是参考图。
    sel 是这身的选择（衬衫/裤子…的 id、穿法）；单品已被删掉时用搭配里存的那份图。"""
    items = db_load()["items"]
    saved = {p["category"]: p for p in (snap or {}).get("pieces", [])}
    imgs, todo = [], []

    def ref(cat, iid):
        it = items.get(iid or "")
        if it:
            return ITEMS / it["file"], it
        if cat in saved:
            return RESULTS / saved[cat]["file"], saved[cat]
        return None

    for part in parts:
        if part in ("shirt", "pants", "belt", "shoes", "hat"):
            got = ref(part, sel.get(f"{part}_id"))
            if got:
                imgs.append(got[0])
                d = f" ({got[1]['desc']})" if got[1].get("desc") else ""
                keep = " Keep how it is worn (tucked/untucked, sleeves, collar) as it is now." if part == "shirt" else ""
                todo.append(f"the {PIECE_EN[part]} must become exactly the one in image {len(imgs) + 1}{d}, same colour, "
                            f"pattern scale and details.{keep}")
        elif part == "accessory":
            for iid in sel.get("accessory_ids") or []:
                it = items.get(iid)
                if it:
                    imgs.append(ITEMS / it["file"])
                    todo.append(f"the {(it.get('name') or 'accessory')} must look exactly like image {len(imgs) + 1}.")
        elif part == "face":
            person = items.get(sel.get("person_id") or "")
            path = ITEMS / person["file"] if person else (RESULTS / snap["person_file"] if snap and snap.get("person_file")
                                                          else None)
            if path:
                imgs.append(path)
                todo.append(f"the face, hairstyle, skin tone and body shape must be exactly those of the person in "
                            f"image {len(imgs) + 1}.")
        elif part == "wear":
            if sel.get("shirt_id"):
                todo.append("fix how the shirt is worn: " + wear_text({k: sel.get(k) or d for k, d in
                                                                      (("tuck", "in"), ("sleeves", "down"), ("collar", "open"))}))
            if sel.get("pose"):  # 早期的搭配没有记姿势，就不改姿势
                todo.append("fix the pose. " + pose_text(sel))
        elif part == "light":
            todo.append("make the white or very light garments clearly visible against the white background: add "
                        "soft natural shading in their folds and a gentle shadow along their edges, keep their colour "
                        "and fabric texture, do not overexpose.")
        elif part == "background":
            todo.append("replace the background with a pure white seamless studio backdrop (#FFFFFF), keeping only a "
                        "faint natural shadow under the feet.")
    if note.strip():
        todo.append(f"Also: {note.strip()[:200]}")
    what = " ".join(todo)
    a = (f"Image 1 is a finished outfit photo. Correct only the following, nothing else: {what} Keep everything else "
         "in image 1 pixel-identical: the person, face, pose, all other clothes and lighting; the background stays pure white. "
         "Output one photorealistic photo.")
    b = f"Same photo as image 1 with only these corrections: {what} Nothing else changes."
    return imgs, [a, b], bool(todo)


PALETTE_WEIGHT = {"shirt": 1.0, "pants": 0.8, "shoes": 0.3, "hat": 0.2, "belt": 0.15, "accessory": 0.15}


def look_palette(lk):
    """这身穿搭的色卡：用存下来的单品抠图提色（3–5 个），没有就用衣橱里现有的单品。"""
    import palette
    srcs = [(RESULTS / p["file"], PALETTE_WEIGHT.get(p["category"], .15)) for p in lk.get("pieces", [])
            if (RESULTS / p["file"]).exists()]
    if not srcs:
        items, q = db_load()["items"], lk.get("sel") or {}
        for cat in ("shirt", "pants", "belt", "shoes", "hat"):
            it = items.get(q.get(f"{cat}_id") or "")
            if it:
                srcs.append((ITEMS / (it.get("cut") or it["file"]), PALETTE_WEIGHT[cat]))
    try:
        return palette.palette(srcs) if srcs else []
    except Exception as e:
        log_error("palette", e)
        return []


JOBS = {}


TRIES = max(1, int(P.env("AUTO_RETRY", "2")))  # 一身最多生成几次（自动检查不合格时重来）


def check_refs(req: TryOn):
    """自动检查用的对照图：原照 + 这一身的每件单品。"""
    items = db_load()["items"]
    refs = [("person", ITEMS / items[req.person_id]["file"])]
    for label, i in (("shirt", req.shirt_id), ("trousers", req.pants_id), ("belt", req.belt_id), ("shoes", req.shoes_id),
                     ("hat", req.hat_id)):
        if i in items:
            refs.append((label, ITEMS / items[i]["file"]))
    refs += [(items[i].get("name") or "accessory", ITEMS / items[i]["file"]) for i in req.accessory_ids if i in items]
    return refs


async def run_job(job, req: TryOn, plan):
    try:
        checking = not MOCK and P.ready("seedream") and P.env("AUTO_CHECK", "1") != "0"
        wear = (wear_text({"tuck": req.tuck, "sleeves": req.sleeves, "collar": req.collar}) + " " if req.shirt_id else "") + \
            pose_text({"pose": req.pose})
        items = db_load()["items"]
        try:  # 先给还没有文字描述的单品补上描述，再按最新描述写提示词（缓存键不受影响）
            job.update(step="读懂每件单品")
            await ensure_descs([req.shirt_id, req.pants_id, req.belt_id, req.shoes_id, req.hat_id, *req.accessory_ids])
            plan = {**build_plan(req, db_load()["items"]), "key": plan["key"]}
            items = db_load()["items"]
        except HTTPException:
            raise
        except Exception as e:
            log_error("ensure_descs", e)
        best, qa = None, None
        total = lambda r: -1 if r is None else sum(r[k] for k in P.CHECKS)

        async def judge(client, im):
            job.update(step_index=len(plan["steps"]), step="检查效果")
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=90)
            return await P.ark_judge(client, check_refs(req), "data:image/jpeg;base64," +
                                     __import__("base64").b64encode(buf.getvalue()).decode(), wear)

        async with httpx.AsyncClient(timeout=P.timeout()) as client:
            for attempt in range(TRIES if checking else 1):
                im = await execute(plan, client, lambda i, s: job.update(
                    step_index=i, step=s["label"] if attempt == 0 else "效果不理想，重新生成"))
                if not checking:
                    best = im
                    break
                r = await judge(client, im)
                if best is None or total(r) > total(qa):
                    best, qa = im, r
                if r is None or total(r) == len(P.CHECKS):
                    break
                bad = [k for k in P.CHECKS if not r[k]]
                if all(k in P.FIXABLE for k in bad):
                    # 只是某几处不对（衬衫/裤子/鞋/腰带/帽子/配饰/长相/穿法）：只修这几处，不重做整身
                    parts = list(dict.fromkeys(P.FIXABLE[k] for k in bad))
                    refs, prompts, ok = fix_inputs(req.model_dump(), parts)
                    if ok:
                        job.update(step="只修正" + "、".join(PART_ZH[x] for x in parts))
                        tmp = RESULTS / f"_fix_{uuid.uuid4().hex}.jpg"
                        im.save(tmp, quality=95)
                        try:
                            fixed = await fetch_image(client, await P.EDIT["seedream"](client, [tmp] + refs, prompts))
                        finally:
                            tmp.unlink(missing_ok=True)
                        r2 = await judge(client, fixed)
                        if total(r2) >= total(qa):
                            best, qa = fixed, r2
                        break
        light = any(is_light(it) for it in (items.get(x or "") for x in [req.shirt_id, req.pants_id, req.shoes_id,
                                                                        req.belt_id, req.hat_id]) if it)
        im = await asyncio.to_thread(whiten_bg, best, light)
        job.update(step="保存结果")
        look_id = uuid.uuid4().hex[:12]
        im.save(RESULTS / f"{look_id}.jpg", quality=95)
        best.save(RESULTS / f"{look_id}_raw.jpg", quality=92)  # 洗白前的原图留一份，万一洗坏了能找回
        look = {"id": look_id, "key": plan["key"], "file": f"{look_id}.jpg", "created": time.time(),
                "cost": plan["cost"], "demo": MOCK, "pipeline": plan["pipeline"], "check": qa,
                "sel": req.model_dump(exclude={"force"}), "raws": {f"{look_id}.jpg": f"{look_id}_raw.jpg"},
                "profile": profile_of(items.get(req.person_id), items),  # 照片以后删了，这套搭配也知道是谁的
                **snapshot(look_id, req)}
        look["palette"] = look_palette(look)
        with _lock:
            db = db_load()
            for old in [k for k, v in db["looks"].items() if v["key"] == plan["key"]]:  # 重新生成时替换旧图
                o = db["looks"][old]
                for f in {o["file"], *o.get("history", []), *o.get("turn", []), o.get("video"), o.get("person_file"), *[p["file"] for p in o.get("pieces", [])], *o.get("raws", {}).values()} - {None}:
                    (RESULTS / f).unlink(missing_ok=True)
                del db["looks"][old]
            db["looks"][look_id] = look
            db_save(db)
        job.update(status="done", look=look)
    except P.GenError as e:
        log_error("run_job", e)
        job.update(status="error", error=str(e))
    except Exception as e:  # 网络断开等
        log_error("run_job", e)
        job.update(status="error", error=friendly(e))


# ---------------------------------------------------------------- 接口
app = FastAPI(title="衬衫衣橱")
app.mount("/media", StaticFiles(directory=DATA), name="media")


@app.get("/")
def index():
    return FileResponse(ROOT / "static" / "index.html")


def strip_demo_bar(path):
    """早期演示模式在图片底部加过一条黑条（DEMO MODE…），换装时会被照搬。检测到就裁掉。"""
    try:
        im = Image.open(path).convert("RGB")
        w, h = im.size
        bar = im.crop((0, h - 30, w, h - 6)).convert("L")
        above = im.crop((0, h - 60, w, h - 40)).convert("L")
        dark = sum(bar.getdata()) / (bar.width * bar.height)
        light = sum(above.getdata()) / (above.width * above.height)
        if dark < 60 and light > 150:
            ys = [y for y in range(h - 80, h) if sum(im.crop((0, y, w, y + 1)).convert("L").getdata()) / w < 70]
            im.crop((0, 0, w, min(ys) if ys else h - 40)).save(path, quality=95)
            return True
    except Exception:
        pass
    return False


@app.get("/api/state")
def state():
    with _lock:
        db = db_load()
        todo = [lk for lk in db["looks"].values() if "palette" not in lk]
        for lk in todo:  # 旧搭配补上色卡
            lk["palette"] = look_palette(lk)
        if todo:
            db_save(db)
        if not db.get("bar_fixed"):
            for it in db["items"].values():
                if it["category"] == "person":
                    strip_demo_bar(ITEMS / it["file"])
            for lk in db["looks"].values():
                for f in [lk["file"], *lk.get("history", []), lk.get("person_file")]:
                    if f:
                        strip_demo_bar(RESULTS / f)
            db["bar_fixed"] = True
            db_save(db)
        if [it for it in db["items"].values() if ensure_cut(it)]:  # 旧单品补上透明底展示图
            db_save(db)
        boxes = False  # 照片里人的位置：镜子里把原照和试穿图摆成一样大
        for it in db["items"].values():
            if it["category"] == "person" and "pbox" not in it and (ITEMS / it["file"]).exists():
                it["pbox"], boxes = person_box(ITEMS / it["file"]), True
        for lk in db["looks"].values():
            if lk.get("person_file") and "person_pbox" not in lk and (RESULTS / lk["person_file"]).exists():
                lk["person_pbox"], boxes = person_box(RESULTS / lk["person_file"]), True
        if boxes:
            db_save(db)
    return {"items": sorted(db["items"].values(), key=lambda x: -x["created"]),
            "looks": sorted(db["looks"].values(), key=lambda x: -x["created"]),
            "profiles": db.get("profiles", {}),
            "config": {"demo": MOCK, "pipeline": PIPELINE, "pipeline_name": P.NAMES[PIPELINE],
                       "editor_name": P.NAMES[EDITOR], "max_accessories": MAX_ACCESSORIES,
                       "portrait_cost": 0 if MOCK else P.price("edit", EDITOR),
                       "tidy_cost": 0 if MOCK else P.price("edit", EDITOR),
                       "fix_cost": 0 if MOCK else P.price("edit", "seedream"),
                       "turn_cost": 0 if MOCK else P.price("video", "seedance")}}


@app.post("/api/items")
async def add_item(file: UploadFile = File(...), category: str = Form(...), name: str = Form(""),
                   tidy: str = Form(""), profile: str = Form("")):
    """tidy=1：照片里有模特或背景乱时，先用即梦整理成干净的单品图，再放进衣橱（换装也更准）。
    profile：上传照片时归到哪个形象（不填就是一个新形象）。"""
    if category not in CATEGORIES:
        raise HTTPException(400, "不认识的分类。")
    try:
        im = normalize_image(await file.read())
    except Exception:
        raise HTTPException(400, f"“{file.filename}” 不是能打开的图片。支持 JPG、PNG、WebP、HEIC。")
    item_id = uuid.uuid4().hex[:12]
    item = {"id": item_id, "category": category, "name": name.strip()[:40], "file": f"{item_id}.jpg",
            "created": time.time()}
    if category == "person" and profile.strip():
        item["profile"] = profile.strip()[:40]
    if tidy == "1" and category in GARMENTS and not MOCK:
        if not P.ready(EDITOR):
            raise HTTPException(400, f"整理单品图要用{P.NAMES[EDITOR]}，.env 里还没填它的 key。")
        im.save(ITEMS / f"{item_id}_orig.jpg", quality=92)
        item["orig"] = f"{item_id}_orig.jpg"
        item["hanger"] = category in ("shirt", "pants")
        try:
            async with httpx.AsyncClient(timeout=P.timeout()) as client:
                im = normalize_image(_jpeg(await fetch_image(client, await P.EDIT[EDITOR](
                    client, [ITEMS / item["orig"]], tidy_prompts(category, item["name"])))))
        except P.GenError as e:
            log_error("add_item", e)
            raise HTTPException(502, str(e))
        except Exception as e:
            log_error("add_item", e)
            raise HTTPException(502, friendly(e))
    im.save(ITEMS / item["file"], quality=92)
    await asyncio.to_thread(ensure_cut, item)
    with _lock:
        db = db_load()
        db["items"][item_id] = item
        db_save(db)
    if category in GARMENTS:
        asyncio.create_task(ensure_descs([item_id]))
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


def profile_of(it, items):
    """照片属于哪个形象：手动归过的按手动的；形象照跟着它的原照；都没有就是这张照片自己。"""
    seen = set()
    while it and it["id"] not in seen:
        seen.add(it["id"])
        if it.get("profile"):
            return it["profile"]
        if not it.get("from"):
            return it["id"]
        parent = items.get(it["from"])
        if not parent:
            return it["from"]
        it = parent
    return it["id"] if it else None


class ProfileSet(BaseModel):
    profile: Optional[str] = None  # 归到哪个形象；空 = 自己单独成一个新形象


@app.post("/api/items/{item_id}/profile")
def set_profile(item_id: str, body: ProfileSet):
    """把一张照片归到某个形象（同一个人的几张照片放在一起，搭配也就合在一起显示）。"""
    with _lock:
        db = db_load()
        it = db["items"].get(item_id)
        if not it or it["category"] != "person":
            raise HTTPException(404, "这张照片已经被删除。")
        it["profile"] = body.profile or item_id
        db_save(db)
        return it


class ProfileName(BaseModel):
    name: str


@app.patch("/api/profiles/{pid}")
def rename_profile(pid: str, body: ProfileName):
    with _lock:
        db = db_load()
        db.setdefault("profiles", {}).setdefault(pid, {})["name"] = body.name.strip()[:20]
        db_save(db)
        return db["profiles"][pid]


class Order(BaseModel):
    ids: List[str]


@app.post("/api/items/order")
def order_items(body: Order):
    """整理衣柜里拖动换位置：按给的顺序记下每件的位置（只排给到的这几件）。"""
    with _lock:
        db = db_load()
        for n, iid in enumerate(body.ids):
            if iid in db["items"]:
                db["items"][iid]["order"] = n
        db_save(db)
    return {"ok": True}


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


def turn_prompt():
    """转一圈的指令（中文，即梦视频模型对中文理解更好）。
    TURN_MODE=turntable（默认）：360° 转台展示，人像雕塑一样定住姿势、整体匀速转一圈；
    TURN_MODE=orbit：人完全不动，镜头环绕人物一圈（环绕运镜），有些模型这样更不容易变形。"""
    keep = ("人物全程保持同一个站姿，像静止的雕塑：手臂、手、腿、脚、头和表情都完全不动，不迈步、不摆手、不扭腰、不转头，"
            "身体比例和五官不变形；服装的颜色、条纹格纹、版型、塞法和配饰全程保持一致，布料只随旋转轻微自然摆动。"
            "纯白无缝影棚背景（严格的 #FFFFFF，全程不变，没有灰色渐变、暗角和地平线），柔和均匀的光线，人物全身始终完整在画面中央，脚下只有淡淡的自然阴影。"
            "不切镜头、不推拉变焦：人物在画面中的大小、位置、头顶和脚底的高度全程和第一帧完全相同；不加黑边、不出现其他人物或文字。")
    if P.env("TURN_MODE", "turntable") == "orbit":
        return ("360度环绕运镜（orbit shot）：画面中的人物完全静止不动，相机以人物为圆心、保持同样的高度和距离，"
                "匀速水平环绕人物一整圈，依次拍到正面、侧面、背面、另一侧，最后回到正面停住，之后画面完全静止直到结束。" + keep)
    # 默认「转台」模式：严格按这几条写，顺序也照这个来
    return ("360度转台展示，相机固定。"
            "人物站在看不见的旋转台上，以身体中轴为轴心整体匀速旋转一整圈，依次转到侧面、背面、另一侧，最后回到正面停住。"
            "人物像静止的雕塑：手臂、腿、脚、头、表情都不动，不迈步、不摆手、不扭腰。"
            "身体比例和五官不变形。"
            "回到正面停住以后直到视频结束都完全静止。"
            "服装的颜色、图案、版型和配饰全程不变。"
            "纯白背景 #FFFFFF，人物全身始终完整在画面中央，大小和位置和第一帧相同，不推拉、不切镜头、不加黑边和文字。")


async def _video(client, job, image, prompt, label):
    url, anchored = await P.seedance_turn(client, image, prompt, lambda s: job.update(step=f"{label}（通常 1–3 分钟）"))
    job.update(step="下载视频")
    r = await client.get(url, follow_redirects=True, timeout=P.timeout(300))
    r.raise_for_status()
    return r.content, anchored


async def run_turn(job, look_id):
    """转一圈：用 Seedance 把正面试穿图做成“原地转身一周、回到正面站定”的短视频。"""
    try:
        lk = db_load()["looks"][look_id]
        if MOCK:
            raise P.GenError("演示模式不会生成视频。在 .env 里填好 ARK_API_KEY 后再试。")
        if not P.ready("seedream"):
            raise P.GenError("转一圈要用即梦（火山方舟），.env 里还没填 ARK_API_KEY。")
        job.update(step="生成转身视频")
        name = f"{look_id}_turn.mp4"
        out = RESULTS / name
        async with httpx.AsyncClient(timeout=P.timeout(120)) as client:
            data, anchored = await _video(client, job, RESULTS / lk["file"], turn_prompt(), "生成转身视频")
            out.write_bytes(data)
        with _lock:
            db = db_load()
            if look_id in db["looks"]:
                db["looks"][look_id]["video"] = name
                db["looks"][look_id]["turn_anchored"] = anchored  # 照片有没有同时当尾帧（转回来脸更稳）
                db["looks"][look_id]["video_at"] = time.time()  # 重转以后文件名不变，界面靠这个刷新缓存
                db_save(db)
                job.update(status="done", look=db["looks"][look_id])
            else:
                out.unlink(missing_ok=True)
                job.update(status="error", error="这套搭配已经被删除。")
    except P.GenError as e:
        log_error("run_turn", e)
        job.update(status="error", error=str(e))
    except Exception as e:
        log_error("run_turn", e)
        job.update(status="error", error=friendly(e))


@app.post("/api/looks/{look_id}/turn")
async def make_turn(look_id: str, force: bool = False):
    """给一套已生成的搭配做一段原地转身一周的视频。force=1：不满意，重新生成一段（覆盖旧的）。"""
    lk = db_load()["looks"].get(look_id)
    if not lk:
        raise HTTPException(404, "这套搭配已经被删除。")
    if not force and lk.get("video") and (RESULTS / lk["video"]).exists():
        return {"cached": True, "look": lk}
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "step": "排队中", "step_index": 0, "steps": ["生成转身视频"]}
    JOBS[job["id"]] = job
    asyncio.create_task(run_turn(job, look_id))
    return {"cached": False, "job": job}


class Fix(BaseModel):
    parts: List[str] = []
    note: str = ""


async def run_fix(job, look_id, fix: Fix):
    try:
        lk = db_load()["looks"][look_id]
        refs, prompts, ok = fix_inputs(lk.get("sel") or {}, fix.parts, fix.note, snap=lk)
        if not ok:
            raise P.GenError("没说要修哪里。勾选不对的部分，或者写一句哪里不对。")
        job.update(step="修正" + "、".join(PART_ZH.get(x, x) for x in fix.parts) if fix.parts else "按说明修正")
        async with httpx.AsyncClient(timeout=P.timeout()) as client:
            raw = await fetch_image(client, await P.EDIT["seedream"](client, [RESULTS / lk["file"]] + refs, prompts))
        items = db_load()["items"]
        q = lk.get("sel") or {}
        light = any(is_light(it) for it in (items.get(q.get(f"{c}_id") or "") for c in ("shirt", "pants", "shoes", "belt", "hat")) if it)
        im = await asyncio.to_thread(whiten_bg, raw, light)
        name = f"{look_id}_f{int(time.time())}.jpg"
        im.save(RESULTS / name, quality=95)
        raw.save(RESULTS / f"{name[:-4]}_raw.jpg", quality=92)
        with _lock:
            db = db_load()
            cur = db["looks"].get(look_id)
            if not cur:
                (RESULTS / name).unlink(missing_ok=True)
                raise P.GenError("这套搭配已经被删除。")
            cur.setdefault("history", []).append(cur["file"])  # 留着上一版，修坏了可以撤销
            if cur.get("video"):
                (RESULTS / cur.pop("video")).unlink(missing_ok=True)  # 旧的转身视频对不上了
            cur["file"], cur["created"] = name, time.time()
            cur.setdefault("raws", {})[name] = f"{name[:-4]}_raw.jpg"
            db_save(db)
            job.update(status="done", look=cur)
    except P.GenError as e:
        log_error("run_fix", e)
        job.update(status="error", error=str(e))
    except Exception as e:
        log_error("run_fix", e)
        job.update(status="error", error=friendly(e))


@app.post("/api/looks/{look_id}/fix")
async def fix_look(look_id: str, fix: Fix):
    """你发现哪里不对，就只修哪里：勾选部分 + 一句说明。"""
    if not db_load()["looks"].get(look_id):
        raise HTTPException(404, "这套搭配已经被删除。")
    if MOCK or not P.ready("seedream"):
        raise HTTPException(400, "修一下要用即梦，.env 里还没填 ARK_API_KEY。")
    job = {"id": uuid.uuid4().hex[:12], "status": "running", "step": "排队中", "step_index": 0, "steps": ["修正"]}
    JOBS[job["id"]] = job
    asyncio.create_task(run_fix(job, look_id, fix))
    return {"cached": False, "job": job}


@app.post("/api/looks/{look_id}/undo")
def undo_fix(look_id: str):
    with _lock:
        db = db_load()
        lk = db["looks"].get(look_id)
        if not lk or not lk.get("history"):
            raise HTTPException(400, "没有可以撤销的修改。")
        (RESULTS / lk["file"]).unlink(missing_ok=True)
        if lk.get("video"):
            (RESULTS / lk.pop("video")).unlink(missing_ok=True)
        lk["file"], lk["created"] = lk["history"].pop(), time.time()
        db_save(db)
        return lk


@app.delete("/api/looks/{look_id}")
def delete_look(look_id: str):
    with _lock:
        db = db_load()
        lk = db["looks"].pop(look_id, None)
        if lk:
            for f in {lk["file"], *lk.get("history", []), *lk.get("turn", []), lk.get("video"), lk.get("person_file"), *[p["file"] for p in lk.get("pieces", [])], *lk.get("raws", {}).values()} - {None}:
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
            async with httpx.AsyncClient(timeout=P.timeout()) as client:
                im = await fetch_image(client, await P.EDIT[EDITOR](client, [src], portrait_prompts()))
        except P.GenError as e:
            log_error("make_portrait", e)
            raise HTTPException(502, str(e))
        except Exception as e:
            log_error("make_portrait", e)
            raise HTTPException(502, friendly(e))
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

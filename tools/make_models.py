"""生成内置的默认模特图（中性的白色人台，正面站姿、手臂微离身体、纯白背景）。

    python tools/make_models.py

输出到 assets/models/。这是占位级素材：干净、比例标准、没有脸，保证流程开箱即用。
想要写实模特，在 App 的“服装库 → 模特”里上传你有授权的模特照片即可，随时替换。
"""
from pathlib import Path
from PIL import Image, ImageChops, ImageDraw, ImageFilter

W, H, S = 1024, 1536, 2          # 成图尺寸；S 倍超采样让边缘平滑
OUT = Path(__file__).resolve().parent.parent / "assets" / "models"


def smooth(pts, n=14):
    """Catmull-Rom 曲线，把关键点连成圆润的轮廓。"""
    out = []
    p = [pts[0], *pts, pts[-1]]
    for i in range(1, len(p) - 2):
        for k in range(n):
            t = k / n
            out.append(tuple(0.5 * ((2 * p[i][d]) + (-p[i - 1][d] + p[i + 1][d]) * t +
                                    (2 * p[i - 1][d] - 5 * p[i][d] + 4 * p[i + 1][d] - p[i + 2][d]) * t ** 2 +
                                    (-p[i - 1][d] + 3 * p[i][d] - 3 * p[i + 1][d] + p[i + 2][d]) * t ** 3) for d in (0, 1)))
    out.append(pts[-1])
    return out


def mirror(left, cx=512):
    return left + [(2 * cx - x, y) for x, y in reversed(left)]


def limb(d, a, b, w0, w1):
    """a→b 一段渐细的肢体（圆头）。"""
    import math
    (x0, y0), (x1, y1) = a, b
    ang = math.atan2(y1 - y0, x1 - x0) + math.pi / 2
    dx0, dy0, dx1, dy1 = (math.cos(ang) * w0 / 2, math.sin(ang) * w0 / 2, math.cos(ang) * w1 / 2, math.sin(ang) * w1 / 2)
    d.polygon([(x0 + dx0, y0 + dy0), (x1 + dx1, y1 + dy1), (x1 - dx1, y1 - dy1), (x0 - dx0, y0 - dy0)], fill=255)
    d.ellipse([x0 - w0 / 2, y0 - w0 / 2, x0 + w0 / 2, y0 + w0 / 2], fill=255)
    d.ellipse([x1 - w1 / 2, y1 - w1 / 2, x1 + w1 / 2, y1 + w1 / 2], fill=255)


SHAPES = {
    # 躯干左半边轮廓（从颈根到髋），肩宽、胸、腰、髋
    "man": dict(torso=[(468, 318), (400, 336), (330, 352), (322, 440), (352, 560), (376, 650), (378, 740), (376, 800), (392, 850)],
                head=(512, 200, 66, 88), neck=(512, 290, 62), arm_w=(84, 54, 44), shoulder=(336, 372), hand=(262, 800),
                hip=(452, 810), foot=(436, 1432), leg_w=(150, 96, 70), knee=(446, 1130), ankle=(440, 1400)),
    "woman": dict(torso=[(474, 322), (420, 338), (368, 356), (362, 440), (390, 540), (402, 640), (366, 735), (360, 790), (378, 850)],
                  head=(512, 204, 60, 82), neck=(512, 290, 52), arm_w=(70, 46, 38), shoulder=(372, 376), hand=(296, 790),
                  hip=(454, 820), foot=(438, 1432), leg_w=(140, 88, 62), knee=(448, 1134), ankle=(442, 1400)),
}


def draw_model(kind):
    P = SHAPES[kind]
    sc = lambda v: tuple(c * S for c in v) if isinstance(v, tuple) else v * S
    mask = Image.new("L", (W * S, H * S), 0)
    d = ImageDraw.Draw(mask)
    d.polygon([sc(p) for p in smooth(mirror(P["torso"]))], fill=255)
    x, y, rx, ry = P["head"]
    d.ellipse([(x - rx) * S, (y - ry) * S, (x + rx) * S, (y + ry) * S], fill=255)
    nx, ny, nw = P["neck"]
    d.rectangle([(nx - nw / 2) * S, (ny - 40) * S, (nx + nw / 2) * S, (ny + 50) * S], fill=255)
    for sgn in (-1, 1):
        sx, sy = P["shoulder"]; hx, hy = P["hand"]
        a = (512 + sgn * (512 - sx), sy)
        b = (512 + sgn * (512 - hx), hy)
        m = (512 + sgn * (512 - (sx + hx) / 2 - 6), (sy + hy) / 2)       # 手肘
        limb(d, tuple(c * S for c in a), tuple(c * S for c in m), P["arm_w"][0] * S, P["arm_w"][1] * S)
        limb(d, tuple(c * S for c in m), tuple(c * S for c in b), P["arm_w"][1] * S, P["arm_w"][2] * S)
        hipx, hipy = P["hip"]; kx, ky = P["knee"]; ax, ay = P["ankle"]; fx, fy = P["foot"]
        lx = lambda v: 512 + sgn * (512 - v)
        limb(d, (lx(hipx) * S, hipy * S), (lx(kx) * S, ky * S), P["leg_w"][0] * S, P["leg_w"][1] * S)
        limb(d, (lx(kx) * S, ky * S), (lx(ax) * S, ay * S), P["leg_w"][1] * S, P["leg_w"][2] * S)
        d.ellipse([(lx(fx) - 52) * S, (fy - 18) * S, (lx(fx) + 52) * S, (fy + 26) * S], fill=255)   # 脚
    mask = mask.filter(ImageFilter.GaussianBlur(1.2 * S))
    # 圆润的明暗：蒙版越靠边越暗，像光从正面照在白色人台上
    inner = mask.filter(ImageFilter.GaussianBlur(26 * S))
    shade = ImageChops.multiply(inner, inner)
    lo, hi = (150, 150, 152), (238, 238, 240)
    body = Image.merge("RGB", [Image.eval(shade, lambda v, a=a, b=b: int(a + (b - a) * v / 255)) for a, b in zip(lo, hi)])
    img = Image.new("RGB", (W * S, H * S), "white")
    # 地面软影
    sh = Image.new("L", (W * S, H * S), 0)
    ImageDraw.Draw(sh).ellipse([300 * S, 1405 * S, 724 * S, 1470 * S], fill=70)
    sh = sh.filter(ImageFilter.GaussianBlur(22 * S))
    img = Image.composite(Image.new("RGB", img.size, (120, 120, 124)), img, sh)
    img.paste(body, mask=mask)
    return img.resize((W, H), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for kind in SHAPES:
        draw_model(kind).save(OUT / f"{kind}.jpg", quality=92)
        print("wrote", OUT / f"{kind}.jpg")

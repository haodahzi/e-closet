"""从一身穿搭的单品图里提取主色，做成色卡（本地计算，不调用模型、不花钱）。

颜色数量 3–5 个：按面积加权聚类，相近的颜色合并，按占比从大到小排。
色名是自带的服装常用色表里最接近的那个（不是 Pantone 官方色号）。
"""
from PIL import Image

# 服装常用色：(中文, English, RGB)
NAMED = [
    ("纯白", "White", (246, 246, 244)), ("象牙白", "Ivory", (240, 234, 218)), ("奶油", "Cream", (238, 226, 196)),
    ("燕麦", "Oatmeal", (216, 202, 178)), ("沙色", "Sand", (201, 180, 146)), ("卡其", "Khaki", (176, 158, 116)),
    ("驼色", "Camel", (184, 140, 92)), ("焦糖", "Caramel", (160, 104, 58)), ("姜黄", "Ginger", (196, 132, 52)),
    ("锈红", "Rust", (158, 72, 42)), ("砖红", "Brick", (150, 60, 50)), ("正红", "Red", (190, 30, 40)),
    ("酒红", "Burgundy", (110, 28, 40)), ("巧克力", "Chocolate", (92, 58, 40)), ("深咖", "Espresso", (58, 40, 32)),
    ("灰褐", "Taupe", (140, 124, 112)), ("珊瑚", "Coral", (232, 120, 104)), ("雾粉", "Dusty Pink", (214, 168, 166)),
    ("樱花粉", "Blush", (240, 200, 204)), ("藕紫", "Orchid", (206, 186, 214)), ("薰衣草", "Lavender", (180, 164, 210)),
    ("葡萄紫", "Plum", (98, 54, 86)), ("鹅黄", "Butter", (244, 232, 170)), ("芥末黄", "Mustard", (206, 166, 48)),
    ("橄榄绿", "Olive", (110, 110, 60)), ("鼠尾草", "Sage", (160, 172, 140)), ("薄荷", "Mint", (186, 222, 200)),
    ("森林绿", "Forest", (40, 76, 52)), ("墨绿", "Bottle Green", (30, 56, 44)), ("冰蓝", "Ice Blue", (206, 222, 236)),
    ("天蓝", "Sky Blue", (150, 190, 226)), ("雾霾蓝", "Dusty Blue", (124, 148, 172)), ("牛仔蓝", "Denim", (78, 108, 146)),
    ("宝蓝", "Royal Blue", (40, 70, 160)), ("藏青", "Navy", (32, 40, 70)), ("午夜蓝", "Midnight", (24, 28, 44)),
    ("浅灰", "Light Grey", (200, 200, 200)), ("中灰", "Grey", (140, 140, 142)), ("炭灰", "Charcoal", (66, 66, 70)),
    ("石墨", "Graphite", (46, 46, 50)), ("黑", "Black", (20, 20, 22)),
]


def _lab(rgb):
    def lin(c):
        c /= 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(float(v)) for v in rgb)
    x, y, z = (r * .4124 + g * .3576 + b * .1805) / .9505, r * .2126 + g * .7152 + b * .0722, \
        (r * .0193 + g * .1192 + b * .9505) / 1.089
    f = lambda t: t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def _dist(a, b):
    return sum((p - q) ** 2 for p, q in zip(_lab(a), _lab(b))) ** .5


def name_of(rgb):
    zh, en, _ = min(NAMED, key=lambda n: _dist(rgb, n[2]))
    return zh, en


def palette(sources, max_colors=5):
    """sources: [(图片路径, 权重)]，透明底 PNG 只取实心部分。返回 [{"hex","zh","en","share"}]，3–5 个。"""
    import numpy as np
    pts, wts = [], []
    for path, weight in sources:
        try:
            im = Image.open(path).convert("RGBA")
        except Exception:
            continue
        im.thumbnail((120, 120))
        a = np.asarray(im, dtype=float).reshape(-1, 4)
        a = a[a[:, 3] > 200][:, :3]
        if not len(a):
            continue
        pts.append(a)
        wts.append(np.full(len(a), weight / len(a)))
    if not pts:
        return []
    X, W = np.concatenate(pts), np.concatenate(wts)
    # 加权 k-means（k=6），再把看起来差不多的颜色合并
    rng = np.random.default_rng(0)
    C = X[rng.choice(len(X), size=min(6, len(X)), replace=False, p=W / W.sum())]
    for _ in range(20):
        lab = ((X[:, None, :] - C[None]) ** 2).sum(-1).argmin(1)
        C = np.array([np.average(X[lab == k], axis=0, weights=W[lab == k]) if (lab == k).any() else C[k]
                      for k in range(len(C))])
    share = np.array([W[lab == k].sum() for k in range(len(C))]) / W.sum()
    groups = []
    for k in np.argsort(-share):
        rgb = tuple(int(round(v)) for v in C[k])
        for g in groups:
            if _dist(rgb, g["rgb"]) < 14:
                g["share"] += share[k]
                break
        else:
            groups.append({"rgb": rgb, "share": float(share[k])})
    out = {}
    for g in sorted(groups, key=lambda g: -g["share"]):
        zh, en = name_of(g["rgb"])
        if zh in out:  # 同一个色名只留一张卡（面积大的那种深浅），占比合并
            out[zh]["share"] += g["share"]
            continue
        out[zh] = {"hex": "#%02X%02X%02X" % g["rgb"], "zh": zh, "en": en, "share": g["share"]}
    cards = sorted((c for c in out.values() if c["share"] >= 0.03), key=lambda c: -c["share"])[:max_colors]
    return [{**c, "share": round(float(c["share"]), 3)} for c in cards]

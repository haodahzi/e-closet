"""转一圈视频的拼接：两段半圈拼成一整圈。

为什么要拼：视频模型离首帧越远越容易走样，一整圈转回正面（270°→360°）时，
脸已经离首帧很远，常常变形。拆成两段，每段都从同一张正面照片开始只转半圈：

  A：照片 → 背面（正常生成）
  B：镜像照片 → 背面（同一句指令，和 A 同方向转），再把画面镜像回来，就成了“往另一边转”

播放 A，再倒着播 B：正面 → 背面 → 正面，方向连续。最后转回正面那一段是 B 的开头几帧，
离首帧很近，脸和照片一致，最后一帧就是照片本身。
"""
import io

import numpy as np
from PIL import Image, ImageOps

THUMB = (72, 96)


def read_frames(path, mirror=False, size=None):
    """解码视频。返回 (JPEG 帧列表, 小灰度图列表, 尺寸, fps)。帧存成 JPEG，不占太多内存。"""
    import imageio_ffmpeg
    gen = imageio_ffmpeg.read_frames(str(path))
    meta = next(gen)
    w, h = meta["size"]
    fps = meta.get("fps") or 24
    frames, thumbs = [], []
    for raw in gen:
        im = Image.frombytes("RGB", (w, h), raw)
        if mirror:
            im = ImageOps.mirror(im)
        if size and im.size != tuple(size):
            im = im.resize(size)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=95)
        frames.append(buf.getvalue())
        thumbs.append(np.asarray(im.convert("L").resize(THUMB), dtype=np.float32))
    return frames, thumbs, (size or (w, h)), fps


def _mask(t):
    return t < 200  # 白底上的人


def opposite_ways(ta, tb):
    """A、B 是不是往相反方向转（B 已经镜像回来）。看转到侧面那一刻（大约 45% 处）的剪影：
    方向相反时，B 的侧影是 A 的侧影左右翻过来的样子。分不清（剪影太对称）时算通过。"""
    if len(ta) < 10 or len(tb) < 10:
        return True
    cx = np.nonzero(_mask(ta[0]))[1].mean() if _mask(ta[0]).any() else THUMB[0] / 2
    cols = np.arange(THUMB[0])
    src = np.clip(np.round(2 * cx - cols).astype(int), 0, THUMB[0] - 1)
    iou = lambda a, b: (a & b).sum() / max((a | b).sum(), 1)
    best_same = best_flip = 0
    for f in (0.35, 0.45, 0.55):
        a, b = _mask(ta[int(len(ta) * f)]), _mask(tb[int(len(tb) * f)])
        best_same += iou(a, b)
        best_flip += iou(a, b[:, src])
    return not (best_same > best_flip + 0.06)


def seam(ta, tb):
    """找接缝：A 后半段和 B 后半段里最像的一对帧（都应该是背面）。返回 (i, j)。"""
    best, ij = None, (len(ta) - 1, len(tb) - 1)
    for i in range(len(ta) // 2, len(ta)):
        for j in range(len(tb) // 2, len(tb)):
            # 差不多像的时候取更靠后的帧：尽量转到正背面再接
            e = float(((ta[i] - tb[j]) ** 2).mean()) + 3.0 * ((len(ta) - 1 - i) / len(ta) + (len(tb) - 1 - j) / len(tb))
            if best is None or e < best:
                best, ij = e, (i, j)
    return ij


def stitch(fa, ta, fb, tb, size, fps, out, speed=1.4, fade=5):
    """A[0..i] 接 B[j..0]，接缝处交叉淡化几帧，写成 H.264 mp4。返回帧数。"""
    import imageio_ffmpeg
    i, j = seam(ta, tb)
    a = fa[:i + 1]
    b = fb[:j + 1][::-1]
    k = min(fade, len(a) - 1, len(b) - 1)
    dec = lambda x: Image.open(io.BytesIO(x)).convert("RGB")
    w = imageio_ffmpeg.write_frames(str(out), size, fps=fps * speed, codec="libx264", pix_fmt_out="yuv420p",
                                    quality=8, macro_block_size=2, output_params=["-movflags", "+faststart"])
    w.send(None)
    count = 0
    for x in a[:len(a) - k]:  # 一帧一帧写，不把整段解码进内存
        w.send(dec(x).tobytes())
        count += 1
    for n in range(k):
        t = (n + 1) / (k + 1)
        w.send(Image.blend(dec(a[len(a) - k + n]), dec(b[n]), t).tobytes())
        count += 1
    for x in b[k:]:
        w.send(dec(x).tobytes())
        count += 1
    w.close()
    return count

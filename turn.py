"""转一圈视频：只生成半圈，另外半圈用镜像拼出来。

为什么：视频模型离首帧越远越容易走样，一整圈转回正面（270°→360°）时脸已经离首帧很远，常常变形，
停下来的姿势也和照片对不上。

做法（只花一段视频的钱）：
  A = 模型生成的半圈：照片 → 背面（离首帧最远也只有 180°）
  后半圈 = A 的每一帧沿人的中轴左右镜像，倒着播：背面 → 正面，旋转方向和 A 连续
最后转回正面那几帧来自 A 的开头（离照片最近），脸不走样；最后几帧再淡到 A 的第一帧，
所以视频以照片本身结束，左右镜像带来的细节差异（头发分缝、单边口袋）也被淡掉。
"""
import io

import numpy as np
from PIL import Image, ImageOps

THUMB = (72, 96)


def read_frames(path):
    """解码视频。返回 (JPEG 帧列表, 小灰度图列表, 尺寸, fps)。帧存成 JPEG，不占太多内存。"""
    import imageio_ffmpeg
    gen = imageio_ffmpeg.read_frames(str(path))
    meta = next(gen)
    w, h = meta["size"]
    fps = meta.get("fps") or 24
    frames, thumbs = [], []
    for raw in gen:
        im = Image.frombytes("RGB", (w, h), raw)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=95)
        frames.append(buf.getvalue())
        thumbs.append(np.asarray(im.convert("L").resize(THUMB), dtype=np.float32))
    return frames, thumbs, (w, h), fps


def axis(thumb):
    """人的中轴（0–1）：白底上非白像素的平均横坐标。"""
    xs = np.nonzero(thumb < 200)[1]
    return float(xs.mean() + 0.5) / thumb.shape[1] if xs.size else 0.5


def mirror_about(im, cx):
    """沿人的中轴（cx，0–1）左右镜像，人留在原地。空出来的地方补白。"""
    w = im.width
    m = ImageOps.mirror(im)
    shift = round((2 * cx - 1) * w)  # 图片中线镜像后人跑到 1-cx，挪回 cx
    out = Image.new("RGB", im.size, (255, 255, 255))
    out.paste(m, (shift, 0))
    return out


def _mirror_thumb(t, cx):
    w = t.shape[1]
    cols = np.arange(w)
    src = np.clip(np.round(2 * cx * w - 1 - cols).astype(int), 0, w - 1)
    return t[:, src]


def turn_point(thumbs, cx):
    """后半段里最左右对称的一帧（正背面），从这里接上镜像的后半圈。"""
    best, k = None, len(thumbs) - 1
    for i in range(len(thumbs) // 2, len(thumbs)):
        # 差不多对称时取更靠后的帧：尽量转到正背面
        e = float(((thumbs[i] - _mirror_thumb(thumbs[i], cx)) ** 2).mean()) + 3.0 * (len(thumbs) - 1 - i) / len(thumbs)
        if best is None or e < best:
            best, k = e, i
    return k


def full_turn(frames, thumbs, size, fps, out, speed=1.25, fade=5, settle=8):
    """半圈 → 一整圈，写成 H.264 mp4。返回帧数。"""
    import imageio_ffmpeg
    dec = lambda x: Image.open(io.BytesIO(x)).convert("RGB")
    cx = axis(thumbs[0])
    i = turn_point(thumbs, cx)
    first = dec(frames[0])
    w = imageio_ffmpeg.write_frames(str(out), size, fps=fps * speed, codec="libx264", pix_fmt_out="yuv420p",
                                    quality=8, macro_block_size=2, output_params=["-movflags", "+faststart"])
    w.send(None)
    count = 0

    def send(im):
        nonlocal count
        w.send(im.tobytes())
        count += 1

    k = min(fade, i)
    for n in range(i + 1 - k):  # 前半圈：原样
        send(dec(frames[n]))
    for n in range(k):  # 背面：从原画面淡到镜像画面
        j = i + 1 - k + n
        send(Image.blend(dec(frames[j]), mirror_about(dec(frames[i - n]), cx), (n + 1) / (k + 1)))
    back = list(range(i - k, -1, -1))  # 后半圈：镜像倒放
    for n, j in enumerate(back):
        im = mirror_about(dec(frames[j]), cx)
        left = len(back) - 1 - n
        if left < settle:  # 最后几帧淡到第一帧（就是照片），以照片结束
            im = Image.blend(im, first, 1 - left / settle)
        send(im)
    w.close()
    return count

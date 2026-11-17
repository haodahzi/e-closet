"""生成应用图标（四眼衬衫扣）：python tools/make_icon.py → assets/icon.png / icon.ico"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter

OUT = Path(__file__).resolve().parent.parent / "assets"
N = 1024
im = Image.new("RGBA", (N, N), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle([40, 40, N - 40, N - 40], radius=220, fill=(27, 27, 26, 255))          # 炭黑底
shadow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
ImageDraw.Draw(shadow).ellipse([226, 250, N - 226, N - 190], fill=(0, 0, 0, 120))
im = Image.alpha_composite(im, shadow.filter(ImageFilter.GaussianBlur(24)))
d = ImageDraw.Draw(im)
d.ellipse([212, 212, N - 212, N - 212], fill=(241, 236, 228, 255), outline=(185, 176, 162, 255), width=10)  # 纽扣
d.ellipse([290, 290, N - 290, N - 290], outline=(207, 198, 184, 255), width=8)
for dx, dy in ((-70, -70), (70, -70), (-70, 70), (70, 70)):                                  # 四个孔
    d.ellipse([N / 2 + dx - 30, N / 2 + dy - 30, N / 2 + dx + 30, N / 2 + dy + 30], fill=(167, 156, 140, 255))
d.line([(N / 2 - 70, N / 2 - 70), (N / 2 + 70, N / 2 + 70)], fill=(201, 137, 91, 255), width=14)  # 缝线（强调色）
d.line([(N / 2 + 70, N / 2 - 70), (N / 2 - 70, N / 2 + 70)], fill=(201, 137, 91, 255), width=14)
im.save(OUT / "icon.png")
im.save(OUT / "icon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", OUT / "icon.png", OUT / "icon.ico")

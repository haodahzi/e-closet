"""把 SENDI 打包成桌面应用（在哪个系统上跑，就打出哪个系统的版本）。

    pip install -r requirements.txt pywebview pyinstaller
    python build_app.py

输出：dist/SENDI/（Windows，里面的 SENDI.exe）或 dist/SENDI.app（macOS）。
"""
import importlib.util, os, subprocess, sys
from pathlib import Path

ROOT = Path(__file__).parent
sep = os.pathsep
args = [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed", "--name", "SENDI",
        "--add-data", f"static{sep}static", "--add-data", f"assets{sep}assets",
        "--icon", str(ROOT / "assets" / ("icon.ico" if sys.platform == "win32" else "icon.png")),
        "--paths", str(ROOT),
        "--hidden-import", "multipart", "--hidden-import", "pillow_heif",
        # 项目自己的模块：app 是在函数里才 import 的，明确告诉 PyInstaller 一并打进去
        "--hidden-import", "dcloset_app", "--hidden-import", "db", "--hidden-import", "providers",
        "--hidden-import", "settings", "--hidden-import", "palette",
        "--collect-submodules", "uvicorn", "--collect-submodules", "webview"]
if importlib.util.find_spec("rembg"):              # 本地抠图（可选）；装了就一起打进去
    args += ["--collect-all", "rembg", "--collect-all", "onnxruntime", "--collect-all", "pymatting"]
else:
    print("没装 rembg：打出来的应用没有本地抠图（单品会用简单的去白底），其余功能不受影响。")
args.append("desktop.py")
sys.exit(subprocess.call(args, cwd=ROOT))

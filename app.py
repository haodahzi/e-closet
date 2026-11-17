"""入口：python app.py（等同于 python dcloset_app.py）。

主程序改名成了 dcloset_app.py：打包成桌面应用时，名字叫 app 的模块在 Windows / macOS 上会被 PyInstaller 漏掉。
"""
import runpy

if __name__ == "__main__":
    runpy.run_module("dcloset_app", run_name="__main__")

"""D-Closet 桌面客户端：双击打开，原生窗口，不用开终端、不用开浏览器。

    python desktop.py            # 源码运行（需要 pip install pywebview；没装就退回用浏览器打开）
    打包后的 D-Closet.exe / D-Closet.app 就是它

数据放在：Windows %APPDATA%\\D-Closet、macOS ~/Library/Application Support/D-Closet、Linux ~/.local/share/d-closet
（源码运行时仍放在项目目录里，和以前一样）。想换位置：先设置环境变量 DCLOSET_HOME。
"""
import os, socket, sys, threading, time, urllib.request, webbrowser

TITLE = "D-Closet · 定制销售"


def free_port():
    """优先用 8000（书签、老习惯），被占了就让系统挑一个空闲端口。"""
    for port in (int(os.environ.get("PORT", "8000")), 0):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
                return s.getsockname()[1]
            except OSError:
                continue
    raise RuntimeError("找不到空闲端口")


def home_dir():
    home = os.environ.get("DCLOSET_HOME")
    if not home:
        if sys.platform == "win32":
            home = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "D-Closet")
        elif sys.platform == "darwin":
            home = os.path.expanduser("~/Library/Application Support/D-Closet")
        else:
            home = os.path.expanduser("~/.local/share/d-closet")
    os.makedirs(home, exist_ok=True)
    return home


def log_crash():
    """启动失败时把完整原因写进 app.log（连同模块搜索路径，方便排查打包问题）。"""
    import traceback
    try:
        with open(os.path.join(home_dir(), "app.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("[%Y-%m-%d %H:%M:%S] 启动失败\n") + traceback.format_exc())
            f.write(f"frozen={getattr(sys, 'frozen', False)} _MEIPASS={getattr(sys, '_MEIPASS', None)}\nsys.path={sys.path}\n")
            base = getattr(sys, "_MEIPASS", None)
            if base and os.path.isdir(base):
                f.write("bundle files: " + ", ".join(sorted(os.listdir(base))[:80]) + "\n")
    except Exception:
        pass
    traceback.print_exc()


def main():
    os.environ.setdefault("NO_BROWSER", "1")
    if sys.stdout is None or sys.stderr is None:      # 无控制台的窗口程序
        log = open(os.path.join(home_dir(), "app.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    import uvicorn
    import app as appmod

    port = free_port()
    server = uvicorn.Server(uvicorn.Config(appmod.app, host="127.0.0.1", port=port, log_level="warning", log_config=None))
    threading.Thread(target=server.run, daemon=True).start()
    url = f"http://127.0.0.1:{port}"
    for _ in range(300):                                # 最多等 30 秒服务起来
        try:
            urllib.request.urlopen(url + "/api/settings", timeout=1)
            break
        except Exception:
            time.sleep(0.1)
    else:
        raise SystemExit("服务没有启动成功，详见 data/errors.log 或 app.log")

    try:
        if os.environ.get("DCLOSET_NO_WINDOW") == "1":
            raise ImportError("按要求不开窗口")
        import webview
        try:
            webview.settings["ALLOW_DOWNLOADS"] = True   # “导出表格”要能下载
        except Exception:
            pass
        webview.create_window(TITLE, url, width=1440, height=900, min_size=(1100, 720))
        webview.start()
    except Exception as e:                              # 没有 pywebview / 系统缺 WebView 组件：用默认浏览器
        print(f"  （没能打开原生窗口：{type(e).__name__}: {e}）改用浏览器打开 {url}", flush=True)
        if os.environ.get("NO_BROWSER_FALLBACK") != "1":
            webbrowser.open(url)
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            pass
    server.should_exit = True


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        log_crash()
        raise

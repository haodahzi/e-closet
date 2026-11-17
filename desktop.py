"""SENDI 桌面客户端：双击打开，原生窗口，不用开终端、不用开浏览器。

    python desktop.py            # 源码运行（需要 pip install pywebview；没装就退回用浏览器打开）
    打包后的 SENDI.exe / SENDI.app 就是它

数据放在：Windows %APPDATA%\\SENDI、macOS ~/Library/Application Support/SENDI、Linux ~/.local/share/sendi
（源码运行时仍放在项目目录里，和以前一样）。想换位置：先设置环境变量 DCLOSET_HOME。
"""
import os, socket, sys, threading, time, urllib.request, webbrowser

TITLE = "SENDI · 定制销售"


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
            home = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "SENDI")
        elif sys.platform == "darwin":
            home = os.path.expanduser("~/Library/Application Support/SENDI")
        else:
            home = os.path.expanduser("~/.local/share/sendi")
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


def find_chromium():
    """系统里的 Edge / Chrome（Windows 10/11 自带 Edge）。用它的“应用模式”开独立窗口：没有地址栏、没有标签页。"""
    import shutil
    c = [os.environ.get("DCLOSET_APP_BROWSER", "")]
    if sys.platform == "win32":
        for env in ("ProgramFiles(x86)", "ProgramFiles", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                c += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    elif sys.platform == "darwin":
        c += [f"/Applications/{n}.app/Contents/MacOS/{n}" for n in
              ("Microsoft Edge", "Google Chrome", "Chromium", "Brave Browser")]
    else:
        c += [shutil.which(n) or "" for n in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser")]
    return next((p for p in c if p and os.path.exists(p)), None)


def run_app_window(url):
    """用 Edge / Chrome 的应用模式打开独立窗口，等窗口关了再返回 True；起不来返回 False。"""
    import shutil, subprocess
    exe = find_chromium()
    if not exe:
        return False
    profile = os.path.join(home_dir(), "window-profile")     # 单独的用户目录：不和平时的浏览器混在一起，关窗口进程就结束
    args = [exe, f"--app={url}", f"--user-data-dir={profile}", "--window-size=1440,900", "--no-first-run",
            "--no-default-browser-check", "--disable-features=Translate", *os.environ.get("DCLOSET_APP_FLAGS", "").split()]
    t0 = time.time()
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    proc.wait()
    return time.time() - t0 > 3                                # 几秒内就退出多半是没真正开成窗口


def main():
    os.environ.setdefault("NO_BROWSER", "1")
    if sys.stdout is None or sys.stderr is None:      # 无控制台的窗口程序
        log = open(os.path.join(home_dir(), "app.log"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = log

    if os.environ.get("DCLOSET_SELFTEST_WEBVIEW") == "1":   # 打包后自检：原生窗口组件能不能初始化
        import traceback
        try:
            import importlib
            import webview
            gui = importlib.import_module("webview.guilib").initialize(None)
            print("WEBVIEW_OK", getattr(webview, "__version__", "?"), getattr(gui, "__name__", gui), flush=True)
        except BaseException:
            traceback.print_exc()
            print("WEBVIEW_FAIL", flush=True)
        return

    import uvicorn
    import dcloset_app as appmod

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
        # Windows 上必须用 WebView2（Edge）：它不在的话 pywebview 会悄悄退到老旧的 IE 内核，页面会坏。宁可明确报错
        webview.start(gui="edgechromium" if sys.platform == "win32" else None)
    except Exception as e:                              # 没有 pywebview / 系统缺 WebView 组件：用默认浏览器
        reason = f"{type(e).__name__}: {e}"
        if sys.platform == "win32" and "ImportError" not in reason[:11]:
            reason += "（Windows 需要 Microsoft Edge WebView2 运行时：https://developer.microsoft.com/microsoft-edge/webview2 ，装好后重开）"
        print(f"  （原生窗口没能打开：{reason}）改用 Edge / Chrome 的应用窗口", flush=True)
        shown = False
        try:
            shown = run_app_window(url)                    # 第二级：系统浏览器的“应用模式”，仍是独立窗口
        except Exception as e2:
            print(f"  （应用窗口也没能打开：{type(e2).__name__}: {e2}）", flush=True)
        if not shown:                                      # 最后一级：普通浏览器，页面顶部说明原因
            appmod.NATIVE_ERROR = reason
            print(f"  改用浏览器打开 {url}", flush=True)
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

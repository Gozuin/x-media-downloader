# -*- coding: utf-8 -*-
"""X 媒体下载器 —— 程序入口。

    X媒体下载器.exe                           双击启动图形界面
    X媒体下载器.exe --selftest                自检并把结果写到 selftest-report.txt
    X媒体下载器.exe --probe <推文链接>        诊断某条推文的媒体地址（为什么下不下来）
"""
from __future__ import annotations

import os
import sys
import traceback


def _base_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


BASE_DIR = _base_dir()
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)


def _alert(title: str, message: str) -> None:
    """出错时弹窗提示（打包成无控制台 exe 后没有 stdout，只能靠弹窗）。"""
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror(title, message[:3000])
        root.destroy()
    except Exception:
        pass


def _run_selftest() -> int:
    """无控制台时把自检输出写进文件，并弹窗汇报结果。"""
    report = os.path.join(BASE_DIR, "selftest-report.txt")
    stream = open(report, "w", encoding="utf-8")
    old_stdout, old_stderr = sys.stdout, sys.stderr
    sys.stdout = sys.stderr = stream
    try:
        from xdl.selftest import run_selftest

        code = run_selftest()
    except Exception:
        stream.write(traceback.format_exc())
        code = 1
    finally:
        stream.flush()
        sys.stdout, sys.stderr = old_stdout, old_stderr
        stream.close()

    with open(report, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    tail = [line for line in text.strip().splitlines() if line.strip()][-3:]
    verdict = "全部通过 ✅" if code == 0 else "有失败项 ❌"
    summary = f"{verdict}\n\n" + "\n".join(tail) + f"\n\n完整报告：{report}"
    # 有控制台就直接打印（避免弹窗把批处理卡住）；无控制台的 windowed 版才弹窗
    if old_stdout is not None:
        try:
            print(text)
            print(summary)
        except Exception:
            _alert("自检结果", summary)
    else:
        _alert("自检结果", summary)
    return code


def _run_probe(argv: list) -> int:
    """诊断模式：给一条推文链接，看媒体地址到底哪里不行。

    结果同时打印到控制台并写入 probe-report.txt（双击运行的兄弟也好复制粘贴）。
    """
    args = [a for a in argv[1:] if not a.startswith("--")]
    if not args:
        print("用法：X媒体下载器.exe --probe https://x.com/用户名/status/推文ID")
        return 2
    tweet_url = args[0]
    report = os.path.join(BASE_DIR, "probe-report.txt")
    lines: list = []

    def emit(text: str) -> None:
        lines.append(text)
        print(text, flush=True)

    controller = None
    try:
        from xdl.browser import BrowserController, detect_working_proxy, find_browser
        from xdl.probe import probe_tweet
        from xdl.util import load_json

        cfg = load_json(os.path.join(BASE_DIR, "config.json"), {})
        proxy = ""
        if cfg.get("auto_proxy", True):
            proxy = detect_working_proxy(log=emit)
        else:
            proxy = str(cfg.get("proxy") or "").strip()
        cookies = load_json(os.path.join(BASE_DIR, "cookies.json"), {})
        jar = cookies.get("cookies") if isinstance(cookies.get("cookies"), dict) else {}
        cookie_header = "; ".join(f"{k}={v}" for k, v in (jar or {}).items() if v)

        profile = os.path.join(BASE_DIR, "_browser_profile")
        controller = BrowserController(profile_dir=profile, proxy=proxy, exe=find_browser(""), log=emit)
        try:
            controller.start("https://x.com/home")
            controller.cookie_header = cookie_header
            controller.fetch_session()
        except Exception as exc:  # noqa: BLE001
            emit(f"浏览器准备失败：{type(exc).__name__}: {exc}（继续做离线候选测试）")

        result = probe_tweet(controller, tweet_url, proxy=proxy, cookie_header=cookie_header,
                             log=emit, report_path=report)
        emit("")
        emit("=== 总体结论 ===")
        emit(str(result.get("verdict") or ""))
    except Exception:
        emit(traceback.format_exc())
        return 1
    finally:
        try:
            if controller:
                controller.close_browser()
        except Exception:
            pass
    try:
        with open(report, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")
    except OSError:
        pass
    return 0


def _hide_console_if_gui() -> None:
    """GUI 模式把黑色控制台窗口藏起来（控制台子系统的 exe 兼容性最好）。

    之所以不用 --windowed 打包：某些受限/加固环境会直接杀掉 GUI 子系统的进程，
    控制台子系统最稳；藏掉窗口后外观与普通窗口程序一致。
    """
    try:
        import ctypes

        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)   # SW_HIDE
    except Exception:
        pass


def main() -> int:
    if "--selftest" in sys.argv:
        return _run_selftest()
    if "--probe" in sys.argv:
        return _run_probe(sys.argv)
    _hide_console_if_gui()
    try:
        from xdl.gui import main as gui_main

        gui_main()
        return 0
    except Exception:
        detail = traceback.format_exc()
        try:
            with open(os.path.join(BASE_DIR, "error.log"), "a", encoding="utf-8") as fh:
                fh.write(detail + "\n")
        except Exception:
            pass
        _alert("X 媒体下载器启动失败", detail)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

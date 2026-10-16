# -*- coding: utf-8 -*-
"""最小探针：验证「打包后的 exe」能不能拉起 Chrome 的调试端口，并把 Chrome 的输出打出来。"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

CHROME = r"C:\Users\GOZ\AppData\Local\Google\Chrome\Application\chrome.exe"


def try_port(port: int, timeout: float = 12.0) -> tuple:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return True, r.read().decode("utf-8", "replace")[:120]
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.4)
    return False, last


def attempt(label: str, profile: str, port: int) -> None:
    os.makedirs(profile, exist_ok=True)
    args = [
        CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
        "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "about:blank",
    ]
    print(f"\n=== {label} ===", flush=True)
    print("profile:", profile, flush=True)
    print("frozen:", getattr(sys, "frozen", False), " exe:", sys.executable, flush=True)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    ok, detail = try_port(port, timeout=12.0)
    print("端口就绪:", ok, "|", detail, flush=True)
    print("Chrome 退出码:", proc.poll(), flush=True)
    if not ok:
        try:
            proc.terminate()
            out = proc.communicate(timeout=5)[0]
            print("Chrome 输出:", out.decode("utf-8", "replace")[:800] if out else "(空)", flush=True)
        except Exception as exc:
            print("取输出失败:", exc, flush=True)
    try:
        proc.terminate()
    except Exception:
        pass


def main() -> int:
    base = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.abspath(__file__))
    print("TEMP =", os.environ.get("TEMP"), flush=True)
    attempt("A. 用 %TEMP% 下的 profile", tempfile.mkdtemp(prefix="probe-tmp-"), 9611)
    attempt("B. 用 exe 同级目录下的 profile", os.path.join(base, "_probe_profile"), 9612)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

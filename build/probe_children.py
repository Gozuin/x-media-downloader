# -*- coding: utf-8 -*-
"""隔离探针：打包后的 exe 到底能不能拉起子进程 / 拉起 Chrome。

分成四种情况，逐条打结果，用来区分「沙箱拦了子进程」还是「程序有 bug」。
"""
import os
import subprocess
import sys
import tempfile
import time
import urllib.request

CHROME = r"C:\Users\GOZ\AppData\Local\Google\Chrome\Application\chrome.exe"


def port_ready(port: int, timeout: float = 10.0) -> tuple:
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as resp:
                return True, resp.read().decode("utf-8", "replace")[:60].replace("\n", " ")
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(0.4)
    return False, last


def show(label: str, ok: bool, detail: str) -> None:
    line = f"[{'通' if ok else '隔'}] {label} —— {detail}"
    print(line, flush=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_result.txt")


def main() -> int:
    base = os.path.dirname(os.path.abspath(sys.executable)) if getattr(sys, "frozen", False) \
        else os.path.dirname(os.path.abspath(__file__))
    global LOG_PATH
    LOG_PATH = os.path.join(base, "probe_result.txt")
    try:
        if os.path.exists(LOG_PATH):
            os.remove(LOG_PATH)
    except OSError:
        pass
    work = os.path.join(base, "_probe_work")
    os.makedirs(work, exist_ok=True)
    print("frozen =", getattr(sys, "frozen", False), "| exe =", sys.executable, flush=True)
    print("CWD =", os.getcwd(), "| TEMP =", os.environ.get("TEMP"), flush=True)

    # 1. 最普通的子进程：cmd
    try:
        out = subprocess.run(["cmd", "/c", "echo child-ok"], capture_output=True, timeout=15)
        show("1) 拉起 cmd 子进程", b"child-ok" in out.stdout, repr(out.stdout[:40]))
    except Exception as exc:
        show("1) 拉起 cmd 子进程", False, f"{type(exc).__name__}: {exc}")

    # 2. 子进程能否写文件
    target = os.path.join(work, "child_write.txt")
    if os.path.exists(target):
        os.remove(target)
    try:
        subprocess.run(["cmd", "/c", f'echo hi > "{target}"'], capture_output=True, timeout=15)
        show("2) 子进程写文件", os.path.exists(target), target)
    except Exception as exc:
        show("2) 子进程写文件", False, f"{type(exc).__name__}: {exc}")

    # 3. Chrome（普通模式，带调试端口）
    profile = os.path.join(work, "profile_normal")
    try:
        proc = subprocess.Popen(
            [CHROME, "--remote-debugging-port=9631", f"--user-data-dir={profile}",
             "--no-first-run", "--no-default-browser-check", "--window-size=600,400", "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        ok, detail = port_ready(9631, 12.0)
        show("3) Chrome 普通模式（调试端口）", ok, f"{detail} | Chrome退出码={proc.poll()}")
        try:
            proc.terminate()
        except Exception:
            pass
    except Exception as exc:
        show("3) Chrome 普通模式（调试端口）", False, f"{type(exc).__name__}: {exc}")

    # 4. Chrome（headless，带调试端口）
    profile = os.path.join(work, "profile_headless")
    try:
        proc = subprocess.Popen(
            [CHROME, "--remote-debugging-port=9632", f"--user-data-dir={profile}",
             "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        ok, detail = port_ready(9632, 12.0)
        show("4) Chrome headless（调试端口）", ok, f"{detail} | Chrome退出码={proc.poll()}")
        try:
            proc.terminate()
        except Exception:
            pass
    except Exception as exc:
        show("4) Chrome headless（调试端口）", False, f"{type(exc).__name__}: {exc}")

    # 5. Chrome + 干净环境变量（只留系统必需项）
    profile = os.path.join(work, "profile_cleanenv")
    clean = {
        "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
        "windir": os.environ.get("windir", r"C:\Windows"),
        "TEMP": os.environ.get("TEMP", r"C:\Windows\Temp"),
        "TMP": os.environ.get("TMP", r"C:\Windows\Temp"),
        "USERPROFILE": os.environ.get("USERPROFILE", ""),
        "APPDATA": os.environ.get("APPDATA", ""),
        "LOCALAPPDATA": os.environ.get("LOCALAPPDATA", ""),
        "PATH": os.environ.get("SystemRoot", r"C:\Windows") + r"\System32;" + os.environ.get("SystemRoot", r"C:\Windows"),
        "NUMBER_OF_PROCESSORS": os.environ.get("NUMBER_OF_PROCESSORS", "4"),
    }
    try:
        proc = subprocess.Popen(
            [CHROME, "--remote-debugging-port=9633", f"--user-data-dir={profile}",
             "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=clean,
        )
        ok, detail = port_ready(9633, 12.0)
        show("5) Chrome + 干净环境变量", ok, f"{detail} | Chrome退出码={proc.poll()}")
        try:
            proc.terminate()
        except Exception:
            pass
    except Exception as exc:
        show("5) Chrome + 干净环境变量", False, f"{type(exc).__name__}: {exc}")

    # 6. Chrome + 当前环境，但剔除打包器注入的变量
    profile = os.path.join(work, "profile_noemipass")
    trimmed = dict(os.environ)
    removed = []
    for key in list(trimmed):
        if key.upper().startswith("_MEI") or key.upper() in {"PYTHONHOME", "PYTHONPATH", "PYTHONUTF8"}:
            removed.append(key)
            trimmed.pop(key, None)
    try:
        proc = subprocess.Popen(
            [CHROME, "--remote-debugging-port=9634", f"--user-data-dir={profile}",
             "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check", "about:blank"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=trimmed,
        )
        ok, detail = port_ready(9634, 12.0)
        show(f"6) Chrome + 剔除[{','.join(removed) or '无'}]后的环境", ok, f"{detail} | Chrome退出码={proc.poll()}")
        try:
            proc.terminate()
        except Exception:
            pass
    except Exception as exc:
        show("6) Chrome + 剔除打包器变量后的环境", False, f"{type(exc).__name__}: {exc}")

    # 7. 用 ShellExecute（os.startfile）启动 Chrome —— 父进程变成 explorer
    profile = os.path.join(work, "profile_shellexec")
    try:
        os.startfile(f'"{CHROME}" --remote-debugging-port=9635 --user-data-dir="{profile}" '
                     f'--headless=new --disable-gpu --no-first-run --no-default-browser-check about:blank')
        ok, detail = port_ready(9635, 12.0)
        show("7) Chrome 经 ShellExecute 启动", ok, detail)
    except Exception as exc:
        show("7) Chrome 经 ShellExecute 启动", False, f"{type(exc).__name__}: {exc}")

    # 8. 经 cmd（shell=True）启动 Chrome —— 父进程变成 cmd.exe
    profile = os.path.join(work, "profile_shellcmd")
    try:
        subprocess.Popen(
            f'start "" "{CHROME}" --remote-debugging-port=9636 --user-data-dir="{profile}" '
            f'--headless=new --disable-gpu --no-first-run --no-default-browser-check about:blank',
            shell=True,
        )
        ok, detail = port_ready(9636, 12.0)
        show("8) Chrome 经 cmd start 启动", ok, detail)
    except Exception as exc:
        show("8) Chrome 经 cmd start 启动", False, f"{type(exc).__name__}: {exc}")

    # 9~11. 不同启动参数（定位 Chrome 为什么秒退）
    variants = [
        ("9)  --no-sandbox", ["--no-sandbox"], 9637),
        ("10) --disable-crashpad --no-first-run", ["--disable-crashpad", "--disable-breakpad"], 9638),
        ("11) 去掉 --headless（普通窗口）", [], 9639),
    ]
    for label, extra, port in variants:
        profile = os.path.join(work, f"profile_{port}")
        args = [CHROME, f"--remote-debugging-port={port}", f"--user-data-dir={profile}"] + extra
        if "去掉" not in label:
            args.append("--headless=new")
        args += ["--disable-gpu", "--no-first-run", "--no-default-browser-check", "about:blank"]
        try:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            ok, detail = port_ready(port, 10.0)
            show(label, ok, f"{detail} | Chrome退出码={proc.poll()}")
            try:
                proc.terminate()
            except Exception:
                pass
        except Exception as exc:
            show(label, False, f"{type(exc).__name__}: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())

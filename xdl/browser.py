# -*- coding: utf-8 -*-
"""浏览器控制：启动真实 Chrome / Edge，让用户登录 X，然后从页面里取登录 Cookie。

设计要点
  · 用独立 user-data-dir（不动用户日常浏览器配置），登录态可长期保留
  · 通过 --remote-debugging-port 暴露调试端口，用 CDP 读写页面与 Cookie
  · Cookie 与"是否真的登录成功"都用页面实测确认（不是只看有没有 auth_token）
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Callable, Dict, Optional, Tuple

from .cdp import CDP, CDPError, http_json, list_pages, pick_free_port, port_in_use, wait_devtools_ready
from .util import Logger

LOGIN_URL = "https://x.com/home"
LOGIN_ENTRY = "https://x.com/i/flow/login"
SEARCH_ENTRY = "https://x.com/explore"

# 常见浏览器安装位置（按优先级）
BROWSER_CANDIDATES = [
    os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe"),
    os.path.expandvars(r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe"),
    os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\Application\msedge.exe"),
]

# 需要重点关注的 Cookie（其余一并保存）
KEY_COOKIES = ("auth_token", "ct0", "twid", "kdt", "guest_id", "auth_multi", "csrf_token")

UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/152.0.0.0 Safari/537.36"
)

# 判断"是否已登录"的页面标记
_LOGIN_PROBE_JS = r"""
(() => {
  const pick = (sel) => document.querySelector(sel);
  const profileLink = pick('[data-testid="AppTabBar_Profile_Link"]');
  const switcher = pick('[data-testid="SideNav_AccountSwitcher_Button"]');
  const loginForm = pick('[data-testid="LoginForm"]') || pick('[data-testid="loginButton"]');
  let handle = null;
  if (profileLink) handle = profileLink.getAttribute('href');
  if (!handle && switcher) {
    const m = (switcher.textContent || '').match(/@[A-Za-z0-9_]{1,15}/);
    if (m) handle = m[0];
  }
  if (!handle) {
    const cand = Array.from(document.querySelectorAll('a[role="link"]'))
      .map(a => a.getAttribute('href') || '')
      .find(h => /^\/[A-Za-z0-9_]{1,15}$/.test(h));
    if (cand) handle = cand;
  }
  const hasCookie = document.cookie.includes('auth_token=');
  const logged = !!(profileLink || switcher) && !loginForm;
  return JSON.stringify({
    logged: logged,
    handle: handle,
    hasCookie: hasCookie,
    url: location.href,
    title: document.title
  });
})()
"""


def build_browser_args(exe: str, port: int, profile_dir: str, url: str, proxy: str = "",
                       headless: bool = False, no_sandbox: bool = False) -> list:
    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,OptimizationHints",
        "--disable-popup-blocking",
        "--window-size=1360,900",
    ]
    if proxy:
        args.append(f"--proxy-server={proxy}")
        args.append("--proxy-bypass-list=localhost;127.0.0.1")
    if headless:
        args += ["--headless=new", "--disable-gpu"]
    if no_sandbox:
        args.append("--no-sandbox")
    args.append(url)
    return args


def launch_browser_process(exe: str, port: int, profile_dir: str, url: str, proxy: str = "",
                           headless: bool = False, no_sandbox: bool = False,
                           log: Optional[Callable[[str], None]] = None,
                           ready_timeout: float = 20.0):
    """启动浏览器并等调试端口就绪。

    有些受限环境（沙箱 / EDR / 某些加固系统）会让 Chrome 自带沙箱初始化失败，
    Chrome 会在启动瞬间以 0x80000003 退出；这种情况下加 --no-sandbox 就能起来。
    这里把进程句柄交给等待逻辑，进程一死立刻抛错，不做无谓等待。
    """
    os.makedirs(profile_dir, exist_ok=True)
    args = build_browser_args(exe, port, profile_dir, url, proxy, headless, no_sandbox)
    logger = log or (lambda _m: None)
    if no_sandbox:
        logger("以 --no-sandbox 方式启动浏览器（受限环境兼容模式）")
    proc = subprocess.Popen(args, close_fds=True)
    try:
        wait_devtools_ready(port, timeout=ready_timeout, proc=proc)
    except CDPError:
        try:
            proc.terminate()
        except Exception:
            pass
        raise
    return proc


# ---------------------------------------------------------------- 代理
# 常见的本地代理端口（Clash / v2rayN / sing-box / Shadowsocks 等默认值）
COMMON_PROXY_PORTS = (7890, 7897, 7891, 7899, 10809, 10808, 1080, 1081, 2080, 8889, 8080, 20171, 33210)


def proxy_candidates() -> list:
    """候选代理列表：系统注册表里的 + 常见本地端口（http 和 socks5 都试）。"""
    found = []
    registry = system_proxy()
    if registry:
        found.append(registry)
    for port in COMMON_PROXY_PORTS:
        found.append(f"http://127.0.0.1:{port}")
    for port in (1080, 10808, 7891, 7890, 7897):
        found.append(f"socks5://127.0.0.1:{port}")
    seen, unique = set(), []
    for item in found:
        if item and item not in seen:
            seen.add(item)
            unique.append(item)
    return unique


def proxy_reachable(proxy: str, timeout: float = 5.0) -> tuple:
    """实测这个代理能不能连上 X。返回 (是否可用, 说明)。"""
    import urllib.error
    import urllib.request

    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy})] if proxy \
        else [urllib.request.ProxyHandler({})]
    opener = urllib.request.build_opener(*handlers)
    try:
        req = urllib.request.Request("https://x.com/robots.txt", headers={"User-Agent": UA_CHROME})
        started = time.time()
        with opener.open(req, timeout=timeout) as resp:
            cost = int((time.time() - started) * 1000)
            return resp.status < 500, f"HTTP {resp.status} · {cost}ms"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def detect_working_proxy(log: Optional[Callable[[str], None]] = None, timeout: float = 5.0) -> str:
    """自动找一个「真的能连上 X」的代理，找不到返回空字符串。"""
    logger = log or (lambda _m: None)
    for candidate in proxy_candidates():
        ok, detail = proxy_reachable(candidate, timeout=timeout)
        if ok:
            logger(f"代理可用：{candidate}（{detail}）")
            return candidate
        logger(f"  试过 {candidate} —— 不可用（{detail.splitlines()[0][:60]}）")
    logger("没找到可用的本地代理：请手动填写，或先启动代理客户端")
    return ""


def find_browser(explicit: str = "") -> str:
    """找一个可用的 Chrome / Edge 可执行文件。"""
    if explicit and os.path.isfile(explicit):
        return explicit
    for path in BROWSER_CANDIDATES:
        if path and os.path.isfile(path):
            return path
    raise FileNotFoundError("没找到 Chrome/Edge，请在界面的「浏览器路径」里手动指定 exe 路径。")


def system_proxy() -> str:
    """读取 Windows 系统代理（注册表），用作默认值。"""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Internet Settings",
        ) as key:
            server, _ = winreg.QueryValueEx(key, "ProxyServer")
            try:
                enabled, _ = winreg.QueryValueEx(key, "ProxyEnable")
            except OSError:
                enabled = 1
        if not enabled:
            return ""
        server = (server or "").strip()
        if not server:
            return ""
        if "=" in server:  # 形如 http=127.0.0.1:7890;https=...
            for part in server.split(";"):
                if part.lower().startswith("http="):
                    server = part.split("=", 1)[1]
                    break
            else:
                server = server.split(";")[0].split("=", 1)[-1]
        if not server.startswith(("http://", "https://", "socks5://", "socks4://")):
            server = "http://" + server
        return server
    except Exception:
        return ""


class BrowserController:
    """管理一个带调试端口的浏览器实例。"""

    def __init__(
        self,
        profile_dir: str,
        proxy: str = "",
        exe: str = "",
        port: int = 0,
        log: Optional[Callable[[str], None]] = None,
        headless: bool = False,
        no_sandbox: bool = False,
    ):
        self.profile_dir = profile_dir
        self.proxy = (proxy or "").strip()
        self.exe = exe
        self.port = port
        self.log = log or Logger()
        self.headless = headless
        self.no_sandbox = no_sandbox          # 记住上次成功的模式，避免每次白等重试
        self.proc: Optional[subprocess.Popen] = None
        self.cdp: Optional[CDP] = None
        self.cookie_header = ""
        # 真实浏览器的 UA（脚本里写死一个版本号，和浏览器实际版本对不上，
        # 在 X 眼里就是很典型的机器人特征；这里改成向浏览器本身要 UA）
        self.user_agent = UA_CHROME
        self._real_ua = ""
        self._login_page: Optional[Tuple[str, str]] = None  # (target_id, session_id)
        # 端口记忆放在该配置目录内部：每个配置目录只认自己的浏览器，避免串到别的实例
        self._port_file = os.path.join(self.profile_dir, "xdl_debug_port.txt")

    # ------------------------------------------------------------ 端口记忆
    def _remember_port(self) -> None:
        try:
            with open(self._port_file, "w", encoding="utf-8") as fh:
                fh.write(str(self.port))
        except OSError:
            pass

    def _recall_port(self) -> int:
        """上次开的浏览器可能还活着（程序关掉但浏览器没关）——直接复用它的调试端口。"""
        try:
            with open(self._port_file, "r", encoding="utf-8") as fh:
                port = int((fh.read() or "0").strip())
        except (OSError, ValueError):
            return 0
        if port and port_in_use(port):
            try:
                http_json(f"http://127.0.0.1:{port}/json/version", timeout=3.0)
                return port
            except Exception:
                return 0
        return 0

    # ------------------------------------------------------------ 占用处理
    def profile_locked(self) -> bool:
        """配置目录是否被别的浏览器实例占着（有锁文件且调试端口不通）。"""
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket", "lockfile"):
            try:
                if os.path.exists(os.path.join(self.profile_dir, name)):
                    return True
            except OSError:
                pass
        return False

    def kill_profile_holders(self) -> int:
        """结束仍在占用本配置目录的 Chrome（只杀命令行里带本目录的，不动用户日常浏览器）。

        只在用户主动点「结束残留浏览器」时调用 —— 平时不做进程扫描，
        以免在受限环境里反复拉起 PowerShell 拖慢程序。
        """
        profile = os.path.abspath(self.profile_dir).lower().replace("/", "\\")
        script = (
            "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe' or Name='msedge.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.ToLower().Contains('" + profile + "') } | "
            "Select-Object -ExpandProperty ProcessId"
        )
        pids = []
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                capture_output=True, timeout=25, text=True,
            )
            pids = [int(line.strip()) for line in (out.stdout or "").splitlines() if line.strip().isdigit()]
        except Exception as exc:
            self.log(f"枚举残留浏览器失败：{type(exc).__name__}: {exc}")
            return 0
        killed = 0
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
                killed += 1
            except Exception:
                pass
        if killed:
            self.log(f"已结束 {killed} 个占用配置目录的旧浏览器进程")
            time.sleep(0.6)
        else:
            self.log("没有发现占用配置目录的浏览器进程")
        return killed

    # ------------------------------------------------------------ 启动/连接
    def start(self, url: str = LOGIN_URL) -> int:
        """启动浏览器（若调试端口已在监听则直接复用）。返回调试端口。"""
        if self.cdp and not self.cdp.closed and port_in_use(self.port):
            return self.port

        # 1) 上次开的浏览器还活着 → 直接复用它的调试端口（秒开）
        if not (self.port and port_in_use(self.port)):
            remembered = self._recall_port()
            if remembered:
                self.port = remembered
                self.log(f"复用上次打开的浏览器（调试端口 {self.port}）")
            else:
                self.port = 0

        if self.port and port_in_use(self.port):
            pass
        else:
            self.port = self.port or pick_free_port()
            self.exe = find_browser(self.exe)
            self.log(f"启动浏览器：{os.path.basename(self.exe)}（调试端口 {self.port}）")
            self.proc = self._launch_with_fallback(url)
            self._remember_port()

        wait_devtools_ready(self.port, timeout=15.0)
        self._ensure_cdp()
        return self.port

    def _launch_with_fallback(self, url: str):
        """先按上次成功的模式启动；失败再换模式重试（进程一死立刻重试，不干等）。"""
        attempts = []
        if self.no_sandbox:
            attempts = [(True, 25.0, "兼容模式（上次可用）")]
        else:
            attempts = [(False, 10.0, "常规模式"), (True, 30.0, "兼容模式")]

        last_error: Optional[Exception] = None
        for index, (no_sandbox, timeout, label) in enumerate(attempts):
            try:
                if index > 0:
                    self.port = pick_free_port(self.port + 1, self.port + 40)
                self.log(f"尝试{label}启动…")
                proc = launch_browser_process(
                    self.exe, self.port, self.profile_dir, url,
                    proxy=self.proxy, headless=self.headless,
                    no_sandbox=no_sandbox, log=self.log, ready_timeout=timeout,
                )
                self.no_sandbox = no_sandbox
                if no_sandbox and index == 0:
                    pass
                elif no_sandbox:
                    self.log("兼容模式启动成功（受限环境，Chrome 自带沙箱被系统拦截）")
                return proc
            except CDPError as exc:
                last_error = exc
                self.log(f"{label}启动没成功：{exc}")

        # 两种模式都不行：多半是上次那个浏览器还占着配置目录
        if self.profile_locked():
            hint = ("浏览器配置目录被占用（上次打开的浏览器还在运行）。请手动关掉那个浏览器窗口，"
                    "或点界面上的「结束残留浏览器」再重试。")
            self.log(hint)
            raise CDPError(f"浏览器启动失败：{last_error}；{hint}")
        raise CDPError(f"浏览器启动失败：{last_error}")

    def _ensure_cdp(self) -> CDP:
        if self.cdp and not self.cdp.closed:
            return self.cdp
        version = http_json(f"http://127.0.0.1:{self.port}/json/version", timeout=5.0)
        self.cdp = CDP(version["webSocketDebuggerUrl"], timeout=90.0)
        self._login_page = None        # 新连接上旧会话 ID 全部失效，必须重新附着页面
        self._real_ua = ""             # UA 也跟着新会话重新取一次
        return self.cdp

    def ensure_ready(self) -> CDP:
        if not self.port or not port_in_use(self.port):
            self.start()
        return self._ensure_cdp()

    # ------------------------------------------------------------ 页面
    def real_user_agent(self) -> str:
        """问浏览器要它真实使用的 UA（拿不到时才退回内置常量）。

        统一的 UA 有两个好处：① 页面里的 UA 与真实浏览器一致，少一个机器人特征；
        ② 下载图片/视频时用的 UA 也和浏览器一致，不会出现"浏览器一个身份、下载器另一个身份"。
        """
        if self._real_ua:
            return self._real_ua
        agent = ""
        try:
            cdp = self._ensure_cdp()
            info = cdp.send("Browser.getVersion", {}, timeout=10.0)
            agent = str(info.get("userAgent") or "").strip()
        except Exception:  # noqa: BLE001
            agent = ""
        if not agent:
            agent = UA_CHROME
        self._real_ua = agent
        self.user_agent = agent
        return agent

    def open_page(self, url: str = LOGIN_URL) -> Tuple[str, str]:
        """新建一个页面并开好域名，返回 (target_id, session_id)。"""
        cdp = self.ensure_ready()
        target_id, session_id = cdp.create_page(url)
        cdp.send("Page.enable", {}, session_id=session_id)
        cdp.send("Network.enable", {}, session_id=session_id)
        agent = self.real_user_agent()
        cdp.send("Emulation.setUserAgentOverride", {"userAgent": agent}, session_id=session_id)
        return target_id, session_id

    def open_login_page(self) -> Tuple[str, str]:
        """打开/复用 X 主页面，返回 (target_id, session_id)。"""
        cdp = self.ensure_ready()
        if self._login_page:
            target_id, session_id = self._login_page
            try:
                if target_id in {p.get("id") for p in list_pages(self.port)}:
                    return self._login_page
            except Exception:
                pass
        self._login_page = self.open_page(LOGIN_URL)
        return self._login_page

    def goto(self, session_id: str, url: str) -> None:
        cdp = self.ensure_ready()
        cdp.send("Page.navigate", {"url": url}, session_id=session_id)

    def wait_dom_ready(self, session_id: str, timeout: float = 25.0) -> bool:
        """等页面 document.readyState 变成 complete/interactive。"""
        cdp = self.ensure_ready()
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                state = cdp.evaluate(session_id, "document.readyState", timeout=6.0)
                if state in ("interactive", "complete"):
                    return True
            except CDPError:
                pass
            time.sleep(0.5)
        return False

    # ------------------------------------------------------------ Cookie
    def read_cookies(self, session_id: str) -> Dict[str, str]:
        """读取该浏览器里 x.com / twitter.com 的全部 Cookie。"""
        cdp = self.ensure_ready()
        try:
            cdp.send("Network.enable", {}, session_id=session_id)
        except CDPError:
            pass
        try:
            raw = cdp.send("Network.getAllCookies", {}, session_id=session_id, timeout=30.0)
            cookies = raw.get("cookies", [])
        except CDPError:
            raw = cdp.send("Storage.getCookies", {}, timeout=30.0)
            cookies = raw.get("cookies", [])
        jar: Dict[str, str] = {}
        for item in cookies:
            domain = (item.get("domain") or "").lstrip(".").lower()
            if domain in ("x.com", "twitter.com") or domain.endswith((".x.com", ".twitter.com")):
                name = item.get("name")
                if name and name not in jar:
                    jar[name] = item.get("value", "")
        return jar

    def verify_login(self, session_id: str, wait_login: float = 0.0) -> Dict[str, object]:
        """到 x.com/home 实测是否登录成功。"""
        cdp = self.ensure_ready()
        self.goto(session_id, LOGIN_URL)
        self.wait_dom_ready(session_id, timeout=25.0)
        deadline = time.time() + max(wait_login, 9.0)
        info: Dict[str, object] = {}
        while time.time() < deadline:
            try:
                payload = cdp.evaluate(session_id, _LOGIN_PROBE_JS, timeout=15.0)
                if payload:
                    import json

                    info = json.loads(payload)
                    if info.get("logged"):
                        break
            except CDPError:
                pass
            time.sleep(1.0)
        return info

    def fetch_session(self, wait_login: float = 0.0) -> Dict[str, object]:
        """一站式：打开页面 → 读 Cookie → 实测登录状态。"""
        target_id, session_id = self.open_login_page()
        cookies = self.read_cookies(session_id)
        info = self.verify_login(session_id, wait_login=wait_login)
        return {
            "target_id": target_id,
            "session_id": session_id,
            "cookies": cookies,
            "cookie_header": cookie_header(cookies),
            "logged": bool(info.get("logged")),
            "handle": info.get("handle") or "",
            "url": info.get("url") or "",
            "info": info,
        }

    # ------------------------------------------------------------ 收尾
    def close_browser(self) -> None:
        """断开 CDP（不杀浏览器，用户可能还在用）。"""
        if self.cdp:
            try:
                self.cdp.close()
            except Exception:
                pass
            self.cdp = None
        self._login_page = None


def cookie_header(jar: Dict[str, str]) -> str:
    return "; ".join(f"{k}={v}" for k, v in jar.items() if v)


def parse_cookie_text(text: str) -> Dict[str, str]:
    """解析用户手动粘贴的 Cookie 串（支持 name=value; name=value 或每行一条）。"""
    jar: Dict[str, str] = {}
    if not text:
        return jar
    normalized = text.replace("\r", "\n").replace(";", "\n")
    for chunk in normalized.split("\n"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        name = name.strip()
        if name:
            jar[name] = value.strip()
    return jar


def probe_connectivity(proxy: str = "", timeout: float = 8.0) -> Dict[str, object]:
    """测试能不能连上 X（程序联网前必须过这一关）。"""
    import json as _json
    import urllib.error
    import urllib.request

    result: Dict[str, object] = {}
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    else:
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    for name, url in (("x.com", "https://x.com/robots.txt"), ("视频CDN", "https://video.twimg.com/")):
        started = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA_CHROME})
            with opener.open(req, timeout=timeout) as resp:
                result[name] = {
                    "ok": resp.status < 500,
                    "status": resp.status,
                    "ms": int((time.time() - started) * 1000),
                }
        except Exception as exc:
            result[name] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    return result

# -*- coding: utf-8 -*-
"""图形界面（tkinter）：登录取 Cookie → 填搜索条件 → 下载 → 按日期归档。"""
from __future__ import annotations

import datetime as dt
import os
import queue
import sys
import threading
import time
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, simpledialog, ttk
from typing import Any, Dict, Optional

from . import __version__
from .browser import (
    LOGIN_URL,
    BrowserController,
    UA_CHROME,
    cookie_header,
    parse_cookie_text,
    probe_connectivity,
    system_proxy,
)
from .pipeline import JobConfig, run_job
from .download import DEFAULT_RATE_PER_SEC
from .probe import probe_tweet
from .util import Logger, ensure_dir, load_json, parse_ymd, save_json
from .theme import C, FONT, Card, apply_theme, log_tag, style_text

APP_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
COOKIE_PATH = os.path.join(APP_DIR, "cookies.json")
PROFILE_DIR = os.path.join(APP_DIR, "_browser_profile")


def _resolve_app_dir() -> str:
    """程序数据目录。

    打包成 exe 后 sys._MEIPASS 是临时解包目录（每次运行都会被删），
    所以配置、Cookie、浏览器 profile 一律放在 exe 同级目录（便携）；
    若该目录不可写（例如装在 Program Files），退回 %APPDATA%。
    """
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    probe = os.path.join(base, ".write_probe")
    try:
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return base
    except OSError:
        fallback = os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "XMediaDownloader")
        os.makedirs(fallback, exist_ok=True)
        return fallback


APP_DIR = _resolve_app_dir()
CONFIG_PATH = os.path.join(APP_DIR, "config.json")
COOKIE_PATH = os.path.join(APP_DIR, "cookies.json")
PROFILE_DIR = os.path.join(APP_DIR, "_browser_profile")

DEFAULT_CONFIG: Dict[str, Any] = {
    "browser_path": "",
    "proxy": "",
    "use_proxy": True,
    "output_dir": os.path.join(os.path.expanduser("~"), "Downloads", "X媒体下载"),
    "queries": "",
    "date_from": "",
    "date_to": "",
    "tab": "live",
    "want_video": True,
    "want_photo": True,
    "want_gif": True,
    "include_retweet": True,
    "auto_date_filter": True,
    "use_dom_fallback": True,
    "max_scrolls": 40,
    "max_tweets": 800,
    "workers": 8,
    "skip_existing": True,
    "tz_mode": "local",
    "pause_min": 1.5,
    "pause_max": 2.8,
}


def enable_dpi_awareness() -> None:
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass


class App(ttk.Frame):
    def __init__(self, master: tk.Tk):
        super().__init__(master, padding=10)
        self.master.title(f"X 媒体下载器 v{__version__} —— 按日期归档下载搜索结果里的视频/图片")
        screen_w, screen_h = master.winfo_screenwidth(), master.winfo_screenheight()
        win_w = max(720, min(1160, screen_w - 80))
        win_h = max(560, min(880, screen_h - 90))
        self.master.geometry(f"{win_w}x{win_h}")
        self.master.minsize(680, 520)
        self.pack(fill="both", expand=True)

        self.cfg = load_json(CONFIG_PATH, DEFAULT_CONFIG)
        # 代理默认值：优先"每次启动读取本机系统代理"，避免把上一台机器的地址带过去
        if self.cfg.get("auto_proxy", True) or not self.cfg.get("proxy"):
            detected = system_proxy()
            if detected:
                self.cfg["proxy"] = detected

        self.logger = Logger(self._log_threadsafe)
        self.queue: "queue.Queue[tuple]" = queue.Queue()
        self.controller: Optional[BrowserController] = None
        self.session_id: str = ""
        self.cookies: Dict[str, str] = {}
        self.cookie_header: str = ""
        self.stop_event = threading.Event()
        self.worker: Optional[threading.Thread] = None
        self.busy = False

        self._build_vars()
        self._build_ui()
        self._load_config_into_ui()
        self._restore_cookies()
        self._pump_job = self.after(120, self._pump)

    # ================================================================ 变量
    def _build_vars(self) -> None:
        cfg = self.cfg
        self.var_browser = tk.StringVar(value=cfg.get("browser_path", ""))
        self.var_proxy = tk.StringVar(value=cfg.get("proxy", ""))
        self.var_use_proxy = tk.BooleanVar(value=bool(cfg.get("use_proxy", True)))
        self.var_auto_proxy = tk.BooleanVar(value=bool(cfg.get("auto_proxy", True)))
        self.var_output = tk.StringVar(value=cfg.get("output_dir", ""))
        self.var_date_from = tk.StringVar(value=cfg.get("date_from", ""))
        self.var_date_to = tk.StringVar(value=cfg.get("date_to", ""))
        self.var_tab = tk.StringVar(value=cfg.get("tab", "live"))
        self.var_want_video = tk.BooleanVar(value=bool(cfg.get("want_video", True)))
        self.var_want_photo = tk.BooleanVar(value=bool(cfg.get("want_photo", True)))
        self.var_want_gif = tk.BooleanVar(value=bool(cfg.get("want_gif", True)))
        self.var_retweet = tk.BooleanVar(value=bool(cfg.get("include_retweet", True)))
        self.var_auto_date = tk.BooleanVar(value=bool(cfg.get("auto_date_filter", True)))
        self.var_dom_fallback = tk.BooleanVar(value=bool(cfg.get("use_dom_fallback", True)))
        self.var_max_scrolls = tk.IntVar(value=int(cfg.get("max_scrolls", 40)))
        self.var_max_tweets = tk.IntVar(value=int(cfg.get("max_tweets", 800)))
        self.var_workers = tk.IntVar(value=int(cfg.get("workers", 8)))
        self.var_skip = tk.BooleanVar(value=bool(cfg.get("skip_existing", True)))
        self.var_tz = tk.StringVar(value=cfg.get("tz_mode", "local"))
        self.var_safe_mode = tk.BooleanVar(value=bool(cfg.get("safe_mode", False)))
        self.var_reuse_cache = tk.BooleanVar(value=bool(cfg.get("reuse_cache", True)))
        self.var_cooldown = tk.IntVar(value=int(cfg.get("cooldown_sec", 20)))
        self.var_manual_cookie = tk.StringVar(value="")
        self.var_open_when_done = tk.BooleanVar(value=bool(cfg.get("open_when_done", False)))
        self.var_status = tk.StringVar(value="就绪：先点「① 打开浏览器登录 X」，再点「② 读取登录 Cookie」")
        self.var_login = tk.StringVar(value="Cookie：未获取 · 登录：未验证")
        self.var_progress = tk.StringVar(value="等待开始")

    # ================================================================ 界面
    def _build_ui(self) -> None:
        apply_theme(self.master)
        self.configure(padding=(18, 14))
        self._ui_header()

        # 上半部分（登录 / 搜索 / 保存）放进可滚动区域，窗口再小也不丢控件
        host = ttk.Frame(self)
        host.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(host, highlightthickness=0, borderwidth=0, bg=C["bg"])
        vbar = ttk.Scrollbar(host, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vbar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        vbar.pack(side="right", fill="y")

        inner = ttk.Frame(self.canvas)
        self._canvas_window = self.canvas.create_window((0, 0), window=inner, anchor="nw")
        inner.bind("<Configure>", lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._canvas_window, width=e.width))
        self.canvas.bind("<Enter>", lambda _e: self.canvas.bind_all("<MouseWheel>", self._on_wheel))
        self.canvas.bind("<Leave>", lambda _e: self.canvas.unbind_all("<MouseWheel>"))
        self.scroll_inner = inner

        self._ui_login_card()
        self._ui_search_card()
        self._ui_advanced_card()
        self._ui_action_bar()
        self._ui_log_card()

    def _on_wheel(self, event) -> None:
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ================================================================ 现代化界面
    def _ui_header(self) -> None:
        head = ttk.Frame(self)
        head.pack(fill="x", pady=(0, 14))
        left = ttk.Frame(head)
        left.pack(side="left")
        ttk.Label(left, text="X 媒体下载器", style="Title.TLabel").pack(anchor="w")
        ttk.Label(left, text=f"v{__version__} · 搜索结果里的视频 / 图片，按日期自动归档",
                  style="Sub.TLabel").pack(anchor="w")
        self.badge_login = tk.Label(head, textvariable=self.var_login, font=(FONT, 9, "bold"),
                                    padx=12, pady=5, bd=0)
        self.badge_login.pack(side="right", anchor="center")
        # 高级设置入口放在顶栏，窗口再矮也能一眼找到
        ttk.Button(head, text="⚙ 高级设置", style="Soft.TButton",
                   command=self._open_advanced).pack(side="right", padx=(0, 10))
        self.var_login.trace_add("write", lambda *_a: self._refresh_login_badge())
        self._refresh_login_badge()

    def _refresh_login_badge(self) -> None:
        text = self.var_login.get()
        if "已登录" in text or "已加载" in text or "手动粘贴" in text:
            fg, bg = C["ok"], C["ok_soft"]
        elif "未获取" in text:
            fg, bg = C["idle"], C["idle_soft"]
        else:
            fg, bg = C["warn"], C["warn_soft"]
        self.badge_login.configure(fg=fg, bg=bg)

    def _ui_login_card(self) -> None:
        card = Card(self.scroll_inner, "① 登录 X 账号", "在独立浏览器里登录一次，长期有效")
        card.pack(fill="x", pady=(0, 12))
        row = ttk.Frame(card.body, style="Card.TFrame")
        row.pack(fill="x")
        self.btn_open = ttk.Button(row, text="打开浏览器登录", style="Soft.TButton", command=self._open_browser)
        self.btn_open.pack(side="left")
        self.btn_cookie = ttk.Button(row, text="读取登录 Cookie", style="Accent.TButton", command=self._read_cookie)
        self.btn_cookie.pack(side="left", padx=(10, 0))
        ttk.Label(row, text="登录完成后点「读取登录 Cookie」，右上角变绿即可开始",
                  style="Muted.TLabel").pack(side="left", padx=(14, 0))

    def _ui_search_card(self) -> None:
        card = Card(self.scroll_inner, "② 搜索条件", "每行一个关键词，支持 from:user / filter:images 等高级语法")
        card.pack(fill="x", pady=(0, 12))
        body = card.body
        self.txt_queries = tk.Text(body, height=3, wrap="word")
        style_text(self.txt_queries, mono=True)
        self.txt_queries.pack(fill="x", pady=(0, 12))

        row = ttk.Frame(body, style="Card.TFrame")
        row.pack(fill="x", pady=(0, 10))
        ttk.Label(row, text="日期", style="Card.TLabel", width=6).pack(side="left")
        self.ent_date_from = ttk.Entry(row, textvariable=self.var_date_from, width=12)
        self.ent_date_from.pack(side="left")
        ttk.Label(row, text="→", style="Muted.TLabel").pack(side="left", padx=6)
        self.ent_date_to = ttk.Entry(row, textvariable=self.var_date_to, width=12)
        self.ent_date_to.pack(side="left")
        for text, cmd in (("最近 7 天", lambda: self._quick_range(7)),
                          ("最近 30 天", lambda: self._quick_range(30)),
                          ("不限", lambda: (self.var_date_from.set(""), self.var_date_to.set("")))):
            ttk.Button(row, text=text, style="Link.TButton", command=cmd).pack(side="left", padx=(8, 0))
        self.lbl_date_hint = ttk.Label(row, text="", style="Muted.TLabel")
        self.lbl_date_hint.pack(side="left", padx=(12, 0))
        for var in (self.var_date_from, self.var_date_to):
            var.trace_add("write", lambda *_a: self._check_dates())
        self._check_dates()

        row = ttk.Frame(body, style="Card.TFrame")
        row.pack(fill="x", pady=(0, 10))
        ttk.Label(row, text="排序", style="Card.TLabel", width=6).pack(side="left")
        ttk.Radiobutton(row, text="最新（推荐）", value="live", variable=self.var_tab,
                        style="Seg.TRadiobutton").pack(side="left")
        ttk.Radiobutton(row, text="热门", value="top", variable=self.var_tab,
                        style="Seg.TRadiobutton").pack(side="left", padx=(6, 0))

        row = ttk.Frame(body, style="Card.TFrame")
        row.pack(fill="x")
        ttk.Label(row, text="内容", style="Card.TLabel", width=6).pack(side="left")
        for text, var in (("视频", self.var_want_video), ("图片", self.var_want_photo),
                          ("动图", self.var_want_gif), ("含转推", self.var_retweet)):
            ttk.Checkbutton(row, text=text, variable=var, style="Chip.TCheckbutton").pack(side="left", padx=(0, 6))

    def _check_dates(self) -> None:
        bad = False
        for var, ent in ((self.var_date_from, self.ent_date_from), (self.var_date_to, self.ent_date_to)):
            text = var.get().strip()
            wrong = bool(text) and parse_ymd(text) is None
            ent.configure(style="Bad.TEntry" if wrong else "TEntry")
            bad = bad or wrong
        self.lbl_date_hint.configure(
            text="日期格式不对，请用 YYYY-MM-DD" if bad else "格式 YYYY-MM-DD，留空表示不限",
            foreground=C["err"] if bad else C["muted"],
        )

    def _ui_advanced_card(self) -> None:
        card = Card(self.scroll_inner, "高级设置", "浏览器、代理、手动 Cookie、抓取参数")
        card.pack(fill="x", pady=(0, 4))
        self._adv_card = card
        self._adv_open = False
        self.btn_adv = ttk.Button(card.head, text="展开 ▾", style="Link.TButton", command=self._toggle_advanced)
        self.btn_adv.pack(side="right")
        body = card.body
        body.pack_forget()

        def field(label: str) -> ttk.Frame:
            line = ttk.Frame(body, style="Card.TFrame")
            line.pack(fill="x", pady=(0, 8))
            ttk.Label(line, text=label, style="Card.TLabel", width=10).pack(side="left")
            return line

        row = field("浏览器")
        ttk.Entry(row, textvariable=self.var_browser).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="自动检测", command=self._auto_detect_browser).pack(side="left", padx=(8, 0))

        row = field("代理")
        ttk.Entry(row, textvariable=self.var_proxy).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="自动检测", command=self._detect_proxy).pack(side="left", padx=(8, 0))
        ttk.Button(row, text="测试连通性", command=self._test_connectivity).pack(side="left", padx=(8, 0))

        row = field("")
        ttk.Checkbutton(row, text="启用代理", variable=self.var_use_proxy).pack(side="left")
        ttk.Checkbutton(row, text="每次启动自动读取系统代理", variable=self.var_auto_proxy).pack(side="left", padx=(12, 0))
        ttk.Button(row, text="结束残留浏览器", style="Danger.TButton",
                   command=self._kill_leftover_browser).pack(side="right")

        row = field("手动 Cookie")
        ttk.Entry(row, textvariable=self.var_manual_cookie, show="•").pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="应用", command=self._apply_manual_cookie).pack(side="left", padx=(8, 0))

        row = field("抓取选项")
        ttk.Checkbutton(row, text="自动补 since/until", variable=self.var_auto_date).pack(side="left")
        ttk.Checkbutton(row, text="网页兜底", variable=self.var_dom_fallback).pack(side="left", padx=(12, 0))
        ttk.Checkbutton(row, text="跳过已下载", variable=self.var_skip).pack(side="left", padx=(12, 0))
        ttk.Label(row, text="时区", style="Card.TLabel").pack(side="left", padx=(16, 4))
        ttk.Combobox(row, values=("local", "utc"), width=6, state="readonly",
                     textvariable=self.var_tz).pack(side="left")

        row = field("数量限制")
        for text, var, low, high, width in (("最多翻页", self.var_max_scrolls, 1, 500, 5),
                                            ("最多推文", self.var_max_tweets, 20, 20000, 7),
                                            ("并发下载", self.var_workers, 1, 32, 4)):
            ttk.Label(row, text=text, style="Card.TLabel").pack(side="left", padx=(0, 4))
            ttk.Spinbox(row, from_=low, to=high, width=width, textvariable=var).pack(side="left", padx=(0, 14))

    def _toggle_advanced(self) -> None:
        body = self._adv_card.body
        if self._adv_open:
            body.pack_forget()
            self.btn_adv.configure(text="展开 ▾")
        else:
            body.pack(fill="both", expand=True, padx=16, pady=(8, 14))
            self.btn_adv.configure(text="收起 ▴")
            self.after(60, self._scroll_to_advanced)
        self._adv_open = not self._adv_open

    def _open_advanced(self) -> None:
        if not self._adv_open:
            self._toggle_advanced()
        else:
            self._scroll_to_advanced()

    def _scroll_to_advanced(self) -> None:
        """展开后自动滚到高级设置，避免它藏在可视区下面看不到。"""
        self.canvas.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self.canvas.yview_moveto(1.0)

    def _ui_action_bar(self) -> None:
        card = Card(self)
        card.pack(fill="x", pady=(12, 12))
        top = ttk.Frame(card.body, style="Card.TFrame")
        top.pack(fill="x")
        self.btn_start = ttk.Button(top, text="▶  开始搜索并下载", style="Accent.TButton", command=self._start_job)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(top, text="■  停止", style="Danger.TButton", command=self._stop_job, state="disabled")
        self.btn_stop.pack(side="left", padx=(8, 0))
        prog = ttk.Frame(top, style="Card.TFrame")
        prog.pack(side="left", fill="x", expand=True, padx=(16, 0))
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(4, 3))
        ttk.Label(prog, textvariable=self.var_progress, style="Muted.TLabel").pack(anchor="w")

        bottom = ttk.Frame(card.body, style="Card.TFrame")
        bottom.pack(fill="x", pady=(12, 0))
        ttk.Label(bottom, text="保存到", style="Card.TLabel").pack(side="left")
        ttk.Label(bottom, textvariable=self.var_output, style="Path.TLabel").pack(side="left", padx=(8, 8))
        ttk.Button(bottom, text="更改…", style="Link.TButton", command=self._choose_output).pack(side="left")
        ttk.Button(bottom, text="打开", style="Link.TButton", command=self._open_output).pack(side="left")
        ttk.Button(bottom, text="恢复默认", style="Link.TButton", command=self._reset_output).pack(side="left")
        ttk.Checkbutton(bottom, text="完成后自动打开文件夹", variable=self.var_open_when_done).pack(side="right")

    def _ui_log_card(self) -> None:
        card = Card(self, "运行日志")
        # 日志区固定高度，多余空间留给上面的设置区，避免搜索条件被挤出可视范围
        card.pack(fill="x")
        ttk.Button(card.head, text="清空", style="Link.TButton", command=self._clear_log).pack(side="right")
        ttk.Button(card.head, text="复制", style="Link.TButton", command=self._copy_log).pack(side="right", padx=(0, 4))
        frame = tk.Frame(card.body, bg=C["log_bg"])
        frame.pack(fill="both", expand=True)
        # 日志区再压低一点，让上方「搜索条件 / 高级设置」在 900 高的窗口里完整露出来
        self.txt_log = tk.Text(frame, height=5, wrap="word", state="disabled")
        style_text(self.txt_log, dark=True)
        self.txt_log.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.txt_log.yview)
        bar.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=bar.set)
        ttk.Label(card.body, textvariable=self.var_status, style="Muted.TLabel").pack(anchor="w", pady=(8, 0))

    # ---------------------------------------------------------- 1 登录区
    def _build_login_section(self) -> None:
        box = ttk.LabelFrame(self.scroll_inner, text=" 1. 浏览器登录与 Cookie ", padding=8)
        box.pack(fill="x", pady=(0, 8))

        row1 = ttk.Frame(box)
        row1.pack(fill="x")
        ttk.Label(row1, text="浏览器：", width=9).pack(side="left")
        ttk.Entry(row1, textvariable=self.var_browser).pack(side="left", fill="x", expand=True)
        ttk.Button(row1, text="自动检测", width=10, command=self._auto_detect_browser).pack(side="left", padx=4)

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Label(row2, text="代理：", width=9).pack(side="left")
        ttk.Entry(row2, textvariable=self.var_proxy).pack(side="left", fill="x", expand=True)

        row2a = ttk.Frame(box)
        row2a.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(row2a, text="启用代理", variable=self.var_use_proxy).pack(side="left")
        ttk.Button(row2a, text="自动检测", width=10, command=self._detect_proxy).pack(side="left", padx=(10, 6))
        ttk.Button(row2a, text="测试连通性", width=12, command=self._test_connectivity).pack(side="left")
        ttk.Button(row2a, text="结束残留浏览器", width=14,
                   command=self._kill_leftover_browser).pack(side="left", padx=(16, 0))

        row2b = ttk.Frame(box)
        row2b.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(row2b, text="每次启动自动读取本机系统代理（换电脑用就勾这个）",
                        variable=self.var_auto_proxy).pack(side="left")
        ttk.Label(row2b, text="留空 = 不指定代理",
                  foreground="#666").pack(side="left", padx=(10, 0))

        row3 = ttk.Frame(box)
        row3.pack(fill="x", pady=(8, 0))
        self.btn_open = ttk.Button(row3, text="① 打开浏览器登录 X", style="Big.TButton",
                                   command=self._open_browser)
        self.btn_open.pack(side="left")
        self.btn_cookie = ttk.Button(row3, text="② 读取登录 Cookie", style="Big.TButton",
                                     command=self._read_cookie)
        self.btn_cookie.pack(side="left", padx=6)

        row4 = ttk.Frame(box)
        row4.pack(fill="x", pady=(6, 0))
        ttk.Label(row4, textvariable=self.var_login, style="Title.TLabel").pack(side="left")

        row5 = ttk.Frame(box)
        row5.pack(fill="x", pady=(6, 0))
        ttk.Label(row5, text="手动 Cookie：", width=9).pack(side="left")
        ttk.Entry(row5, textvariable=self.var_manual_cookie, show="•").pack(side="left", fill="x", expand=True)
        ttk.Button(row5, text="应用", width=8, command=self._apply_manual_cookie).pack(side="left", padx=4)

        ttk.Label(box, text="提示：浏览器用独立配置目录，登录一次长期有效；也可以从 F12 复制整段 Cookie 粘到上一行。",
                  foreground="#666").pack(anchor="w", pady=(4, 0))

    # ---------------------------------------------------------- 2 搜索区
    def _build_search_section(self) -> None:
        box = ttk.LabelFrame(self.scroll_inner, text=" 2. 搜索条件 ", padding=8)
        box.pack(fill="x", pady=(0, 8))

        ttk.Label(box, text="搜索关键词（每行一个，支持 from:user / filter:images 等高级语法）：").pack(anchor="w")
        self.txt_queries = tk.Text(box, height=3, wrap="word", font=("Consolas", 10))
        self.txt_queries.pack(fill="x", pady=(3, 6))

        row_date = ttk.Frame(box)
        row_date.pack(fill="x", pady=(0, 4))
        ttk.Label(row_date, text="日期区间：").pack(side="left")
        ttk.Label(row_date, text="从").pack(side="left")
        ttk.Entry(row_date, textvariable=self.var_date_from, width=12).pack(side="left", padx=(2, 6))
        ttk.Label(row_date, text="到").pack(side="left")
        ttk.Entry(row_date, textvariable=self.var_date_to, width=12).pack(side="left", padx=(2, 6))
        ttk.Button(row_date, text="最近7天", width=8, command=lambda: self._quick_range(7)).pack(side="left", padx=(6, 2))
        ttk.Button(row_date, text="最近30天", width=9, command=lambda: self._quick_range(30)).pack(side="left", padx=2)
        ttk.Button(row_date, text="清空", width=6,
                   command=lambda: (self.var_date_from.set(""), self.var_date_to.set(""))).pack(side="left", padx=2)

        ttk.Label(box, text="格式 YYYY-MM-DD，留空表示不限；日期会同时写进搜索语句和本地过滤。",
                  foreground="#666").pack(anchor="w", pady=(0, 6))

        row_tab = ttk.Frame(box)
        row_tab.pack(fill="x", pady=(0, 4))
        ttk.Label(row_tab, text="结果页签：").pack(side="left")
        ttk.Radiobutton(row_tab, text="最新（推荐）", value="live", variable=self.var_tab).pack(side="left")
        ttk.Radiobutton(row_tab, text="热门", value="top", variable=self.var_tab).pack(side="left", padx=(6, 0))

        row_type = ttk.Frame(box)
        row_type.pack(fill="x", pady=(0, 4))
        ttk.Label(row_type, text="下载内容：").pack(side="left")
        ttk.Checkbutton(row_type, text="视频", variable=self.var_want_video).pack(side="left")
        ttk.Checkbutton(row_type, text="图片", variable=self.var_want_photo).pack(side="left", padx=4)
        ttk.Checkbutton(row_type, text="动图", variable=self.var_want_gif).pack(side="left", padx=4)
        ttk.Checkbutton(row_type, text="含转推", variable=self.var_retweet).pack(side="left", padx=(12, 0))

        row_opt = ttk.Frame(box)
        row_opt.pack(fill="x", pady=(0, 4))
        ttk.Checkbutton(row_opt, text="自动补 since/until", variable=self.var_auto_date).pack(side="left")
        ttk.Checkbutton(row_opt, text="DOM 兜底", variable=self.var_dom_fallback).pack(side="left", padx=(8, 12))
        ttk.Checkbutton(row_opt, text="跳过已下载", variable=self.var_skip).pack(side="left")
        ttk.Label(row_opt, text="时区").pack(side="left", padx=(12, 0))
        ttk.Combobox(row_opt, values=("local", "utc"), width=6, state="readonly",
                     textvariable=self.var_tz).pack(side="left", padx=3)

        row_num = ttk.Frame(box)
        row_num.pack(fill="x")
        ttk.Label(row_num, text="最多翻页").pack(side="left")
        ttk.Spinbox(row_num, from_=1, to=500, width=5, textvariable=self.var_max_scrolls).pack(side="left", padx=3)
        ttk.Label(row_num, text="最多推文").pack(side="left", padx=(10, 0))
        ttk.Spinbox(row_num, from_=20, to=20000, width=7, textvariable=self.var_max_tweets).pack(side="left", padx=3)
        ttk.Label(row_num, text="并发下载").pack(side="left", padx=(10, 0))
        ttk.Spinbox(row_num, from_=1, to=32, width=4, textvariable=self.var_workers).pack(side="left", padx=3)

        # ---- 防封号相关（v1.0.2 新增）----
        row_safe = ttk.Frame(box)
        row_safe.pack(fill="x", pady=(4, 0))
        ttk.Checkbutton(row_safe, text="🛡 低速防封模式（推荐）", variable=self.var_safe_mode,
                        command=self._on_safe_mode).pack(side="left")
        ttk.Checkbutton(row_safe, text="复用已抓取结果（不再请求搜索接口）",
                        variable=self.var_reuse_cache).pack(side="left", padx=(10, 0))
        ttk.Label(row_safe, text="搜索词冷却(秒)").pack(side="left", padx=(10, 0))
        ttk.Spinbox(row_safe, from_=0, to=3600, width=5, textvariable=self.var_cooldown).pack(side="left", padx=3)

        ttk.Label(box, text="「低速防封模式」= 翻页间隔 5~9 秒、并发 3、搜索词之间冷却 60 秒、限速 3 请求/秒；"
                            "想重下一批图时勾上「复用已抓取结果」，整个下载过程完全不碰账号。",
                  foreground="#666").pack(anchor="w", pady=(3, 0))

    def _on_safe_mode(self) -> None:
        """勾上低速模式时，把相关参数一次性调到保守值（用户还可以再手改）。"""
        if not self.var_safe_mode.get():
            self.var_status.set("已关闭低速防封模式")
            return
        try:
            if self._safe_int(self.var_max_scrolls, 40, 1, 500) > 15:
                self.var_max_scrolls.set(15)
            if self._safe_int(self.var_workers, 8, 1, 32) > 3:
                self.var_workers.set(3)
            if self._safe_int(self.var_cooldown, 20, 0, 3600) < 60:
                self.var_cooldown.set(60)
            self.var_reuse_cache.set(True)
        except tk.TclError:
            pass
        self.var_status.set("已启用低速防封模式：翻页 5~9 秒 / 并发 3 / 冷却 60 秒 / 限速 3 请求每秒")

    # ---------------------------------------------------------- 3 输出区
    def _build_output_section(self) -> None:
        box = ttk.LabelFrame(self.scroll_inner, text=" 3. 保存位置（自动按日期建子文件夹） ", padding=8)
        box.pack(fill="x", pady=(0, 8))

        row1 = ttk.Frame(box)
        row1.pack(fill="x")
        ttk.Label(row1, text="保存文件夹：", width=11).pack(side="left")
        ttk.Entry(row1, textvariable=self.var_output).pack(side="left", fill="x", expand=True)

        row2 = ttk.Frame(box)
        row2.pack(fill="x", pady=(6, 0))
        ttk.Button(row2, text="📁 选择文件夹…", style="Big.TButton",
                   command=self._choose_output).pack(side="left")
        ttk.Button(row2, text="打开文件夹", width=11, command=self._open_output).pack(side="left", padx=6)
        ttk.Button(row2, text="恢复默认", width=9, command=self._reset_output).pack(side="left")
        ttk.Checkbutton(row2, text="下载完成后自动打开文件夹",
                        variable=self.var_open_when_done).pack(side="left", padx=(14, 0))

        ttk.Label(box, text="下载结果按推文日期分文件夹，例如：…\\2024-05-01\\alice_1800000000000000001_01.jpg",
                  foreground="#666").pack(anchor="w", pady=(4, 0))

    # ---------------------------------------------------------- 4 执行区
    def _build_action_section(self) -> None:
        box = ttk.Frame(self)
        box.pack(fill="x", pady=(0, 8))
        top = ttk.Frame(box)
        top.pack(fill="x")
        self.btn_start = ttk.Button(top, text="▶ 开始搜索并下载", style="Big.TButton", command=self._start_job)
        self.btn_start.pack(side="left")
        self.btn_stop = ttk.Button(top, text="■ 停止", width=10, command=self._stop_job, state="disabled")
        self.btn_stop.pack(side="left", padx=6)
        self.btn_diag = ttk.Button(top, text="🔍 媒体诊断", width=12, command=self._diagnose_media)
        self.btn_diag.pack(side="left", padx=(0, 6))
        self.progress = ttk.Progressbar(top, mode="determinate", maximum=100, length=240)
        self.progress.pack(side="left", padx=8)
        ttk.Label(top, textvariable=self.var_progress).pack(side="left")

        # 保存文件夹常驻在这里，一眼就能看到、随手就能改
        bottom = ttk.Frame(box)
        bottom.pack(fill="x", pady=(6, 0))
        ttk.Label(bottom, text="下载到：", style="Title.TLabel").pack(side="left")
        ttk.Label(bottom, textvariable=self.var_output, foreground="#0b5cad").pack(side="left", padx=(2, 6))
        ttk.Button(bottom, text="📁 选择文件夹…", command=self._choose_output).pack(side="left")
        ttk.Button(bottom, text="打开", width=6, command=self._open_output).pack(side="left", padx=4)

    # ---------------------------------------------------------- 5 日志区
    def _build_log_section(self) -> None:
        box = ttk.LabelFrame(self, text=" 运行日志 ", padding=6)
        box.pack(fill="both", expand=True)
        frame = ttk.Frame(box)
        frame.pack(fill="both", expand=True)
        self.txt_log = tk.Text(frame, height=8, wrap="word", font=("Consolas", 9), state="disabled")
        self.txt_log.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(frame, orient="vertical", command=self.txt_log.yview)
        bar.pack(side="right", fill="y")
        self.txt_log.configure(yscrollcommand=bar.set)
        bottom = ttk.Frame(box)
        bottom.pack(fill="x", pady=(4, 0))
        ttk.Label(bottom, textvariable=self.var_status).pack(side="left")
        ttk.Button(bottom, text="清空日志", width=9, command=self._clear_log).pack(side="right")

    # ================================================================ 配置
    def _load_config_into_ui(self) -> None:
        self.txt_queries.delete("1.0", "end")
        self.txt_queries.insert("1.0", self.cfg.get("queries", ""))

    def _collect_config(self) -> Dict[str, Any]:
        data = {
            "browser_path": self.var_browser.get().strip(),
            "use_proxy": bool(self.var_use_proxy.get()),
            "auto_proxy": bool(self.var_auto_proxy.get()),
            "output_dir": self.var_output.get().strip(),
            "queries": self.txt_queries.get("1.0", "end").strip(),
            "date_from": self.var_date_from.get().strip(),
            "date_to": self.var_date_to.get().strip(),
            "tab": self.var_tab.get(),
            "want_video": bool(self.var_want_video.get()),
            "want_photo": bool(self.var_want_photo.get()),
            "want_gif": bool(self.var_want_gif.get()),
            "include_retweet": bool(self.var_retweet.get()),
            "auto_date_filter": bool(self.var_auto_date.get()),
            "use_dom_fallback": bool(self.var_dom_fallback.get()),
            "max_scrolls": self._safe_int(self.var_max_scrolls, 40, 1, 500),
            "max_tweets": self._safe_int(self.var_max_tweets, 800, 20, 20000),
            "workers": self._safe_int(self.var_workers, 8, 1, 32),
            "skip_existing": bool(self.var_skip.get()),
            "tz_mode": self.var_tz.get() or "local",
            "open_when_done": bool(self.var_open_when_done.get()),
            "safe_mode": bool(self.var_safe_mode.get()),
            "reuse_cache": bool(self.var_reuse_cache.get()),
            "cooldown_sec": self._safe_int(self.var_cooldown, 20, 0, 3600),
        }
        # 代理：开启"自动读取系统代理"时不写死具体地址，换机器才不会串
        if not self.var_auto_proxy.get():
            data["proxy"] = self.var_proxy.get().strip()
        return data

    def _persist(self) -> None:
        self.cfg.update(self._collect_config())
        save_json(CONFIG_PATH, self.cfg)

    def _restore_cookies(self) -> None:
        saved = load_json(COOKIE_PATH, {})
        jar = saved.get("cookies") if isinstance(saved.get("cookies"), dict) else {}
        jar = {k: str(v) for k, v in (jar or {}).items()}
        if jar and "auth_token" in jar:
            self.cookies = jar
            self.cookie_header = cookie_header(jar)
            handle = saved.get("handle") or ""
            self.var_login.set(f"Cookie：已加载上次保存的（{len(jar)} 项）" + (f" · 账号：@{handle.lstrip('@')}" if handle else ""))
            self._log(f"已加载上次保存的 Cookie（{len(jar)} 项），可直接开始下载。")

    # ================================================================ 工具
    def _log(self, text: str) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.insert("end", text + "\n", log_tag(text))
        lines = int(self.txt_log.index("end-1c").split(".")[0])
        if lines > 2500:                      # 防止日志无限膨胀
            self.txt_log.delete("1.0", f"{lines - 2000}.0")
        self.txt_log.see("end")
        self.txt_log.configure(state="disabled")

    def _log_threadsafe(self, text: str) -> None:
        self.queue.put(("log", text))

    def _clear_log(self) -> None:
        self.txt_log.configure(state="normal")
        self.txt_log.delete("1.0", "end")
        self.txt_log.configure(state="disabled")

    def _copy_log(self) -> None:
        self.clipboard_clear()
        self.clipboard_append(self.txt_log.get("1.0", "end-1c"))
        self.var_status.set("日志已复制到剪贴板")

    def _quick_range(self, days: int) -> None:
        today = dt.date.today()
        self.var_date_to.set(today.strftime("%Y-%m-%d"))
        self.var_date_from.set((today - dt.timedelta(days=days - 1)).strftime("%Y-%m-%d"))

    def _effective_proxy(self) -> str:
        return self.var_proxy.get().strip() if self.var_use_proxy.get() else ""

    @staticmethod
    def _safe_int(var, default: int, low: int, high: int) -> int:
        """数字框乱填（空、字母、小数）时不抛 TclError，回退默认值并限制在合理范围。"""
        try:
            value = int(float(str(var.get()).strip()))
        except (tk.TclError, ValueError, TypeError):
            value = default
        return max(low, min(high, value))

    def _detect_proxy(self) -> None:
        """自动找一个真能连上 X 的本地代理，省得别人拿到软件不知道该填什么。"""
        self._persist()
        self.var_status.set("正在自动检测可用代理（会逐个测试常见端口）…")
        self._log("开始自动检测代理：先看系统设置，再试常见本地端口（7890/7897/10809/10808/1080 等）")

        def work() -> None:
            from .browser import detect_working_proxy

            found = detect_working_proxy(log=self._log_threadsafe, timeout=4.0)
            if found:
                self.queue.put(("proxy", found))
                self.queue.put(("status", f"已自动填入可用代理：{found}"))
            else:
                self.queue.put(("status", "没找到可用代理：请确认代理客户端已启动，或手动填写地址"))

        self._run_bg("代理检测", work)

    def _ensure_controller(self) -> BrowserController:
        if self.controller is None:
            self.controller = BrowserController(
                profile_dir=PROFILE_DIR,
                proxy=self._effective_proxy(),
                exe=self.var_browser.get().strip(),
                log=self.logger,
                no_sandbox=bool(self.cfg.get("browser_no_sandbox", False)),
            )
        else:
            self.controller.proxy = self._effective_proxy()
            if self.var_browser.get().strip():
                self.controller.exe = self.var_browser.get().strip()
        return self.controller

    def _remember_launch_mode(self) -> None:
        """记住这次成功的启动模式，下次直接用它，不再白等重试。"""
        if self.controller is not None:
            self.cfg["browser_no_sandbox"] = bool(self.controller.no_sandbox)
            self._persist()

    def _kill_leftover_browser(self) -> None:
        """用户主动清理：只结束命令行里带本程序配置目录的浏览器，不动日常浏览器。"""
        if not messagebox.askyesno(
            "结束残留浏览器",
            "将结束「用本程序配置目录启动」的浏览器进程（不会影响你平时用的 Chrome）。\n\n继续吗？",
        ):
            return
        self.var_status.set("正在清理残留浏览器…")

        def work() -> None:
            controller = self._ensure_controller()
            killed = controller.kill_profile_holders()
            self.queue.put(("status", f"已结束 {killed} 个残留浏览器进程" if killed else "没有发现残留浏览器"))

        self._run_bg("清理浏览器", work)

    def _auto_detect_browser(self) -> None:
        from .browser import find_browser

        try:
            path = find_browser("")
        except FileNotFoundError as exc:
            messagebox.showwarning("没找到浏览器", str(exc))
            return
        self.var_browser.set(path)
        self._log(f"检测到浏览器：{path}")

    # ================================================================ 动作
    def _run_bg(self, name: str, func, *, block_ui: bool = False) -> None:
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("正在忙", "上一个操作还没结束，请稍候。")
            return

        def wrapper() -> None:
            try:
                func()
            except Exception as exc:  # noqa: BLE001 —— 后台线程异常不能静默
                self._log_threadsafe(f"【{name}出错】{type(exc).__name__}: {exc}")
                self.queue.put(("status", f"{name}失败：{exc}"))
            finally:
                self.queue.put(("idle", name))

        self.worker = threading.Thread(target=wrapper, name=name, daemon=True)
        self.worker.start()

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.btn_start.configure(state=state)
        self.btn_open.configure(state=state if not busy else "disabled")
        self.btn_cookie.configure(state=state)
        self.btn_stop.configure(state="normal" if busy else "disabled")

    # ---------------------------------------------------------- 打开浏览器
    def _open_browser(self) -> None:
        self._persist()
        self._set_busy(True)
        self.var_status.set("正在启动浏览器…")

        def work() -> None:
            controller = self._ensure_controller()
            port = controller.start(LOGIN_URL)
            self._remember_launch_mode()
            target_id, session_id = controller.open_login_page()
            self.session_id = session_id
            self._log_threadsafe(f"浏览器已启动（调试端口 {port}），请在打开的窗口里完成 X 登录。")
            self._log_threadsafe("登录完成后，回到本窗口点「② 读取登录 Cookie」。")
            self.queue.put(("status", "已打开浏览器，请在窗口里登录 X"))

        self._run_bg("打开浏览器", work)

    # ---------------------------------------------------------- 读 Cookie
    def _read_cookie(self) -> None:
        self._persist()
        self._set_busy(True)
        self.var_status.set("正在读取 Cookie 并验证登录状态…")

        def work() -> None:
            controller = self._ensure_controller()
            controller.start(LOGIN_URL)
            self._remember_launch_mode()
            info = controller.fetch_session(wait_login=10.0)
            jar = info.get("cookies") or {}
            self.session_id = str(info.get("session_id") or "")
            self.cookies = jar
            self.cookie_header = cookie_header(jar)
            controller.cookie_header = self.cookie_header
            self.queue.put(("cookies", {"jar": jar, "handle": info.get("handle") or "", "logged": bool(info.get("logged"))}))
            if info.get("logged"):
                self._log_threadsafe(f"登录验证通过：@{str(info.get('handle', '')).lstrip('@')}，已抓到 {len(jar)} 项 Cookie。")
                self.queue.put(("status", "登录状态正常，可以开始搜索下载了"))
            else:
                self._log_threadsafe("没能确认登录状态：请在浏览器窗口里登录 X 后重试。")
                self.queue.put(("status", "未确认登录，请先在浏览器里登录"))

        self._run_bg("读取Cookie", work)

    # ---------------------------------------------------------- 媒体诊断
    def _diagnose_media(self) -> None:
        """诊断一条推文：为什么它的图片/视频下不下来（地址形状问题还是媒体本身没了）。"""
        if self.busy:
            messagebox.showinfo("先等一等", "当前还有任务在跑，等它结束再做诊断。")
            return
        url = simpledialog.askstring(
            "媒体诊断",
            "粘贴一条推文链接（例如 https://x.com/用户名/status/123456789）：",
            parent=self.root,
        )
        if not url:
            return
        url = url.strip()
        if url.isdigit():
            url = f"https://x.com/i/web/status/{url}"
        if "/status/" not in url:
            messagebox.showwarning("链接不对", "请给一条推文链接，形如 https://x.com/用户/status/123456789")
            return
        self._persist()
        self._set_busy(True)
        self.var_status.set("正在诊断媒体地址…")
        self._log("=" * 68)
        self._log(f"媒体诊断：{url}")
        report = os.path.join(APP_DIR, "probe-report.txt")

        def work() -> None:
            controller = self._ensure_controller()
            try:
                controller.start(LOGIN_URL)
                self._remember_launch_mode()
                _target, session_id = controller.open_login_page()
                self.session_id = session_id
                if self.cookie_header:
                    controller.cookie_header = self.cookie_header
            except Exception as exc:  # noqa: BLE001
                self._log_threadsafe(f"浏览器准备失败：{type(exc).__name__}: {exc}（继续做离线测试）")
            result = probe_tweet(
                controller,
                url,
                proxy=self._effective_proxy(),
                cookie_header=self.cookie_header,
                log=self._log_threadsafe,
                report_path=report,
            )
            self._log_threadsafe(f"诊断结论：{result.get('verdict', '')}")
            self._log_threadsafe(f"完整报告：{report}（可以直接把这个文件发我）")
            self.queue.put(("status", "诊断结束，看日志里的结论"))

        self._run_bg("媒体诊断", work)

    # ---------------------------------------------------------- 手动 Cookie
    def _apply_manual_cookie(self) -> None:
        jar = parse_cookie_text(self.var_manual_cookie.get())
        if not jar:
            messagebox.showwarning("Cookie 为空", "请粘贴形如 auth_token=xxx; ct0=yyy 的 Cookie 内容。")
            return
        self.cookies = jar
        self.cookie_header = cookie_header(jar)
        if self.controller:
            self.controller.cookie_header = self.cookie_header
        save_json(COOKIE_PATH, {"cookies": jar, "handle": "", "saved_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        self.var_login.set(f"Cookie：手动粘贴（{len(jar)} 项）")
        self._log(f"已应用手动粘贴的 Cookie（{len(jar)} 项）。")
        self.var_manual_cookie.set("")

    # ---------------------------------------------------------- 连通性
    def _test_connectivity(self) -> None:
        self._persist()
        self.var_status.set("正在测试能不能连上 X…")
        proxy = self._effective_proxy()

        def work() -> None:
            result = probe_connectivity(proxy, timeout=8.0)
            ok_all = all(bool(v.get("ok")) for v in result.values())
            for name, info in result.items():
                if info.get("ok"):
                    self._log_threadsafe(f"连通性 · {name}：正常（HTTP {info.get('status')}，{info.get('ms')}ms）")
                else:
                    self._log_threadsafe(f"连通性 · {name}：失败 —— {info.get('error')}")
            if ok_all:
                self.queue.put(("status", "连通性正常"))
            else:
                self._log_threadsafe("提示：X 连不上通常是代理没开。请确认代理客户端已启动（本机常见端口 7890）。")
                self.queue.put(("status", "连不上 X：请检查代理是否已开启"))

        self._run_bg("连通性测试", work)

    # ---------------------------------------------------------- 输出目录
    def _choose_output(self) -> None:
        current = self.var_output.get().strip()
        initial = current if current and os.path.isdir(current) else os.path.expanduser("~")
        chosen = filedialog.askdirectory(
            title="选择下载保存文件夹（程序会自动在里面按日期建子文件夹）",
            initialdir=initial,
            mustexist=False,
        )
        if chosen:
            self.var_output.set(os.path.normpath(chosen))
            self._persist()
            self._log(f"保存文件夹已设为：{self.var_output.get()}")

    def _reset_output(self) -> None:
        default = os.path.join(os.path.expanduser("~"), "Downloads", "X媒体下载")
        self.var_output.set(default)
        self._persist()
        self._log(f"保存文件夹已恢复默认：{default}")

    def _open_output(self) -> None:
        path = self.var_output.get().strip()
        if not path:
            return
        ensure_dir(path)
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001
            webbrowser.open(path)
            self._log(f"打开目录失败（{exc}），已尝试用资源管理器打开。")

    # ---------------------------------------------------------- 主任务
    def _start_job(self) -> None:
        queries = [line.strip() for line in self.txt_queries.get("1.0", "end").splitlines() if line.strip()]
        if not queries:
            messagebox.showwarning("缺搜索词", "请先填写至少一个搜索关键词。")
            return
        out_dir = self.var_output.get().strip()
        if not out_dir or not os.path.isdir(out_dir):
            # 没选目录就直接弹文件夹选择框，不让用户卡在这一步
            chosen = filedialog.askdirectory(
                title="选择下载保存文件夹（程序会自动在里面按日期建子文件夹）",
                initialdir=out_dir if out_dir and os.path.isdir(out_dir) else os.path.expanduser("~"),
                mustexist=False,
            )
            if not chosen:
                messagebox.showwarning("还没选保存文件夹", "请先选一个文件夹用来存放下载的文件。")
                return
            self.var_output.set(os.path.normpath(chosen))
            self._persist()
            out_dir = self.var_output.get().strip()
        for label, text in (("起始日期", self.var_date_from.get().strip()), ("结束日期", self.var_date_to.get().strip())):
            if text and parse_ymd(text) is None:
                messagebox.showwarning("日期格式不对", f"{label}「{text}」无法识别，请用 YYYY-MM-DD 格式，或留空表示不限。")
                return
        start = parse_ymd(self.var_date_from.get())
        end = parse_ymd(self.var_date_to.get())
        if start and end and start > end:
            start, end = end, start
        if not (self.var_want_photo.get() or self.var_want_video.get() or self.var_want_gif.get()):
            messagebox.showwarning("没选内容", "请至少勾选一种要下载的内容（视频/图片/动图）。")
            return
        if not self.cookies:
            if not messagebox.askyesno("还没取 Cookie", "尚未获取登录 Cookie。\n未登录时 X 搜索通常看不到结果。\n\n仍要继续吗？"):
                return
        self._persist()
        self.stop_event.clear()
        self._set_busy(True)
        self.progress.configure(value=0, maximum=100)
        self.var_status.set("开始执行…")
        self._log("=" * 68)
        self._log(f"新任务：{len(queries)} 个搜索词，输出到 {out_dir}")

        # 低速防封模式：把节奏相关的参数一次性压到保守值
        safe = bool(self.var_safe_mode.get())
        pause_min = float(self.cfg.get("pause_min", 1.5))
        pause_max = float(self.cfg.get("pause_max", 2.8))
        workers = self._safe_int(self.var_workers, 8, 1, 32)
        scrolls = self._safe_int(self.var_max_scrolls, 40, 1, 500)
        cooldown = float(self._safe_int(self.var_cooldown, 20, 0, 3600))
        rate = DEFAULT_RATE_PER_SEC
        if safe:
            pause_min, pause_max = 5.0, 9.0
            workers = min(workers, 3)
            scrolls = min(scrolls, 15)
            cooldown = max(cooldown, 60.0)
            rate = 3.0

        cfg = JobConfig(
            queries=queries,
            date_from=self.var_date_from.get().strip(),
            date_to=self.var_date_to.get().strip(),
            tab=self.var_tab.get(),
            want_photo=bool(self.var_want_photo.get()),
            want_video=bool(self.var_want_video.get()),
            want_gif=bool(self.var_want_gif.get()),
            include_retweet=bool(self.var_retweet.get()),
            auto_date_filter=bool(self.var_auto_date.get()),
            use_dom_fallback=bool(self.var_dom_fallback.get()),
            max_scrolls=scrolls,
            max_tweets=self._safe_int(self.var_max_tweets, 800, 20, 20000),
            workers=workers,
            skip_existing=bool(self.var_skip.get()),
            output_dir=out_dir,
            tz_mode=self.var_tz.get() or "local",
            pause_min=pause_min,
            pause_max=pause_max,
            reuse_cache=bool(self.var_reuse_cache.get()),
            cache_ttl_hours=float(self.cfg.get("cache_ttl_hours", 72.0)),
            cooldown_sec=cooldown,
            dl_rate_per_sec=rate,
            safe_mode=safe,
        )

        def work() -> None:
            controller = self._ensure_controller()
            controller.start(LOGIN_URL)
            self._remember_launch_mode()
            # 浏览器可能重连过，旧会话 ID 会失效：每次开任务都重新取当前有效的页面会话
            _target, session_id = controller.open_login_page()
            self.session_id = session_id
            if self.cookie_header:
                controller.cookie_header = self.cookie_header
            result = run_job(
                controller,
                self.session_id,
                cfg,
                log=self._log_threadsafe,
                on_progress=lambda info: self.queue.put(("progress", info)),
                stop_event=self.stop_event,
            )
            self.queue.put(("result", result))

        self._run_bg("下载任务", work)

    def _stop_job(self) -> None:
        self.stop_event.set()
        self.var_status.set("已请求停止，正在收尾…")
        self._log("已请求停止：抓取会在当前翻页后结束，下载会等已开始的连接收尾。")

    # ================================================================ 队列
    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self.queue.get_nowait()
                if kind == "log":
                    self._log(str(payload))
                elif kind == "status":
                    self.var_status.set(str(payload))
                elif kind == "progress":
                    self._apply_progress(payload)
                elif kind == "cookies":
                    self._on_cookies(payload)
                elif kind == "proxy":
                    self.var_proxy.set(str(payload))
                    self.var_use_proxy.set(True)
                elif kind == "result":
                    self._on_result(payload)
                elif kind == "idle":
                    self._set_busy(False)
        except queue.Empty:
            pass
        except tk.TclError:          # 窗口已销毁
            return
        try:
            self._pump_job = self.after(120, self._pump)
        except tk.TclError:
            pass

    def _apply_progress(self, info: Dict[str, Any]) -> None:
        phase = info.get("phase")
        if phase == "抓取":
            tweets = info.get("tweets", 0)
            self.var_progress.set(
                f"抓取中 · 第 {info.get('scrolls', 0)} 次翻页 · 已收集推文 {tweets} · 页面可见 {info.get('dom_articles', 0)} 条"
            )
            if str(self.progress.cget("mode")) != "indeterminate":
                self.progress.stop()
                self.progress.configure(mode="indeterminate")
                self.progress.start(80)
        elif phase == "下载":
            done, total = info.get("done", 0), max(1, info.get("total", 1))
            if str(self.progress.cget("mode")) != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate")
            self.progress.configure(value=done * 100.0 / total)
            self.var_progress.set(
                f"下载中 {done}/{total} · 成功 {info.get('ok', 0)} · 跳过 {info.get('skip', 0)} · 失败 {info.get('fail', 0)}"
            )

    def _on_cookies(self, payload: Dict[str, Any]) -> None:
        jar = payload.get("jar") or {}
        handle = str(payload.get("handle") or "").lstrip("@")
        logged = bool(payload.get("logged"))
        if jar:
            save_json(COOKIE_PATH, {
                "cookies": jar,
                "handle": handle,
                "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        marks = []
        for key in ("auth_token", "ct0", "twid"):
            marks.append(f"{key}{'✓' if key in jar else '✗'}")
        state = "已登录" if logged else "未确认登录"
        self.var_login.set(f"Cookie：{'/'.join(marks)}（共 {len(jar)} 项） · 登录：{state}" + (f" @{handle}" if handle else ""))

    def _on_result(self, result) -> None:
        try:
            self.progress.stop()
        except Exception:
            pass
        self.progress.configure(mode="determinate", value=100)
        summary = result.summary()
        self._log("任务结束：" + summary)
        if getattr(result, "failures", None):
            for line in result.failures[:15]:
                self._log(f"  失败明细：{line}")
        failed_csv = getattr(result, "failed_csv", "") or ""
        if failed_csv:
            self._log(f"  失败明细（含每个候选地址的 HTTP 状态）：{failed_csv}")
            self._log("  提示：如果失败的图片很多，用「🔍 媒体诊断」查一条，把 probe-report.txt 发我就能定位。")
        if getattr(result, "risk_hits", 0):
            self._log(f"  ⚠ 本轮搜索被限流 {result.risk_hits} 次：建议勾上「🛡 低速防封模式」，"
                      f"或隔几小时再跑；已抓取结果会被缓存，重下不会再次请求搜索接口。")
        self.var_progress.set(summary)
        self.var_status.set("任务结束")
        if self.var_open_when_done.get():
            self._open_output()
        messagebox.showinfo("任务结束", summary + f"\n\n保存位置：{self.var_output.get()}")


def main() -> None:
    enable_dpi_awareness()
    root = tk.Tk()
    try:
        root.iconbitmap(default="")
    except Exception:
        pass
    app = App(root)
    closing = {"done": False}

    def on_close() -> None:
        """关窗口必须"点了就掉"：先立刻销毁界面，收尾放到后台线程，最后强退兜底。"""
        if closing["done"]:
            return
        closing["done"] = True
        app.stop_event.set()
        try:
            app._persist()
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass

        def shutdown() -> None:
            deadline = time.time() + 2.5
            try:
                if app.controller:
                    app.controller.close_browser()
            except Exception:
                pass
            # 最多再等一会儿让 CDP 线程收尾，然后无条件结束进程
            while time.time() < deadline:
                time.sleep(0.1)
            os._exit(0)

        threading.Thread(target=shutdown, name="shutdown", daemon=True).start()

    root.protocol("WM_DELETE_WINDOW", on_close)
    try:
        root.mainloop()
    finally:
        on_close()


if __name__ == "__main__":
    main()

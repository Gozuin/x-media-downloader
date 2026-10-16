# -*- coding: utf-8 -*-
"""下载层：多线程拉取媒体，按日期分文件夹归档，支持断点去重与 HLS 兜底。

v1.0.2 的关键改动（针对"图片大面积 404"和"请求太多容易被封"）：
  1) 图片地址不再只有"猜出来的 5 个"候选，而是由 parse.photo_candidate_urls 生成的
     「分辨率 × 格式(?format=) × 扩展名写法」完整候选梯子；运行中会按实际成败
     **自适应排序 + 剪枝**：在当前网络环境下没用的地址形状试几次后就不再试，
     请求量能降一个数量级（对防封号很重要）。
  2) 403 / 410 / 451 不再中断候选链（老版本遇到一个 403 就直接放弃整张图）。
  3) 404 是确定性失败，不再重试 3 遍；重试只针对网络错误 / 5xx / 429。
  4) 匿名候选全部失败后，再带登录 Cookie 走一遍（部分受限/敏感媒体要求登录）。
  5) 全局限流 + 失败风暴自动降速，429 退避重试。
  6) 每个候选地址的 HTTP 状态都落进 _meta/failed.csv，下次一眼就能看出是谁不行。
"""
from __future__ import annotations

import base64
import csv
import hashlib
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from requests.adapters import HTTPAdapter

from .browser import UA_CHROME
from .parse import MediaItem
from .util import Logger, ensure_dir, human_size, sanitize, to_local_date, unique_path

CHUNK = 256 * 1024
RETRIES = 3                     # 只对"有可能成功"的错误重试（网络错误 / 5xx / 429）
CDN_GATE_STATUSES = (401, 403, 404, 410, 451)     # 这个地址不行 → 换下一个候选，不中断
RETRYABLE_STATUSES = (408, 425, 429, 500, 502, 503, 504)
DEFAULT_RATE_PER_SEC = 8.0      # 全局每秒请求上限，别把 CDN 打爆

# 每种"地址形状"允许连续失败多少次就判定在当前网络环境下没用（剪枝）
_SHAPE_DEAD_AFTER = {"fmt": 6, "ext": 4, "plain": 3, "bare": 2}

_CSV_HEADER = ["下载时间", "日期", "用户名", "推文链接", "类型", "文件名", "大小(字节)", "源地址"]
_FAIL_CSV_HEADER = ["下载时间", "用户名", "推文链接", "类型", "媒体地址", "最终错误", "尝试次数", "尝试明细"]


# ------------------------------------------------------------------ 地址工具
def _ext_of(url: str, default: str) -> str:
    """从路径扩展名或 ?format= 判断文件后缀。"""
    path = urlparse(url).path
    _, dot, tail = path.rpartition(".")
    if dot and 1 <= len(tail) <= 5 and "/" not in tail:
        ext = tail.lower()
        if ext in ("jpg", "jpeg", "png", "webp", "gif", "mp4", "m4v", "mov", "ts"):
            return "jpg" if ext == "jpeg" else ext
    for chunk in (urlparse(url).query or "").split("&"):
        if chunk.lower().startswith("format="):
            fmt = chunk.split("=", 1)[1].lower()
            if fmt in ("jpg", "jpeg", "png", "webp", "gif"):
                return "jpg" if fmt == "jpeg" else fmt
    return default


def _host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def _is_x_site(url: str) -> bool:
    """x.com / twitter.com 本体。"""
    host = _host_of(url)
    return host in ("x.com", "twitter.com") or host.endswith((".x.com", ".twitter.com"))


def _is_twimg(url: str) -> bool:
    """图片/视频 CDN 域名（pbs.twimg.com / video.twimg.com ...）。"""
    host = _host_of(url)
    return host.endswith("twimg.com")


def shape_key(url: str) -> str:
    """把地址归类成"形状"：fmt:orig / ext:large / plain:medium / bare:-。

    同一形状在某台机器上要么好用要么不好用，用它来学习+剪枝。
    """
    query = url.split("?", 1)[1] if "?" in url else ""
    name = "-"
    for chunk in query.split("&"):
        if chunk.lower().startswith("name="):
            name = chunk.split("=", 1)[1] or "-"
    if "format=" in query:
        mode = "fmt"
    elif not query:
        mode = "bare"
    elif url.split("?")[0].rsplit(".", 1)[-1].lower() in ("jpg", "jpeg", "png", "webp"):
        mode = "ext"
    else:
        mode = "plain"
    return f"{mode}:{name}"


class ShapeStats:
    """记录每种地址形状的成败，用来排序与剪枝（一次运行内学习）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.ok: Dict[str, int] = {}
        self.fail: Dict[str, int] = {}

    def note(self, key: str, ok: bool) -> None:
        with self._lock:
            table = self.ok if ok else self.fail
            table[key] = table.get(key, 0) + 1

    def snapshot(self) -> Dict[str, Tuple[int, int]]:
        with self._lock:
            keys = set(self.ok) | set(self.fail)
            return {k: (self.ok.get(k, 0), self.fail.get(k, 0)) for k in keys}

    def _is_dead(self, key: str) -> bool:
        mode = key.split(":", 1)[0]
        limit = _SHAPE_DEAD_AFTER.get(mode, 4)
        ok, fail = self.snapshot().get(key, (0, 0))
        return ok == 0 and fail >= limit

    def order(self, urls: List[str]) -> List[str]:
        """成功过的排前面；试过没用过的排中间；确定没用的直接剪掉。"""
        snapshot = self.snapshot()
        ranked: List[Tuple[int, int, str]] = []
        for index, url in enumerate(urls):
            key = shape_key(url)
            ok, fail = snapshot.get(key, (0, 0))
            if ok:
                rank = 0
            elif fail == 0:
                rank = 1
            elif not self._is_dead(key):
                rank = 2
            else:
                continue
            ranked.append((rank, index, url))
        ranked.sort(key=lambda row: (row[0], row[1]))
        ordered = [url for _r, _i, url in ranked]
        return ordered or list(urls)      # 全被剪了就别挑了，老老实实全试一遍

    def summary(self) -> str:
        rows = []
        for key, (ok, fail) in sorted(self.snapshot().items(), key=lambda kv: (-kv[1][0], kv[1][1])):
            flag = "（已停用）" if self._is_dead(key) else ""
            rows.append(f"{key} 成功{ok}/失败{fail}{flag}")
        return "；".join(rows)


class RateLimiter:
    """全局令牌桶：所有下载线程共享，限制每秒发出的请求数。"""

    def __init__(self, per_sec: float = DEFAULT_RATE_PER_SEC) -> None:
        self._lock = threading.Lock()
        self._min_interval = 1.0 / max(0.2, float(per_sec or DEFAULT_RATE_PER_SEC))
        self._next_at = 0.0
        self._penalty = 1.0

    def wait(self, stop_event: Optional[threading.Event] = None) -> bool:
        with self._lock:
            now = time.monotonic()
            self._next_at = max(self._next_at, now)
            delay = self._next_at - now
            self._next_at += self._min_interval * self._penalty
        if delay <= 0:
            return True
        deadline = time.monotonic() + min(delay, 8.0)
        while time.monotonic() < deadline:
            if stop_event is not None and stop_event.is_set():
                return False
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
        return True

    def slow_down(self, factor: float = 1.5, cap: float = 5.0) -> None:
        with self._lock:
            self._penalty = min(cap, self._penalty * factor)

    @property
    def penalty(self) -> float:
        with self._lock:
            return self._penalty

    @property
    def per_sec(self) -> float:
        with self._lock:
            return 1.0 / max(self._min_interval, 1e-6)


class AllCandidatesFailed(Exception):
    """整条候选链都失败了（通常代表这批媒体确实取不到）。"""

    def __init__(self, message: str, attempts: List[Tuple[str, Any, str]]):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class _FetchResult:
    status: Optional[int]
    size: Optional[int] = None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.size is not None


class Downloader:
    def __init__(
        self,
        out_dir: str,
        proxy: str = "",
        cookie_header: str = "",
        user_agent: str = UA_CHROME,
        workers: int = 8,
        skip_existing: bool = True,
        tz_mode: str = "local",
        log: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        rate_per_sec: float = DEFAULT_RATE_PER_SEC,
        cookie_to_cdn: bool = True,
    ):
        self.out_dir = out_dir
        self.proxy = (proxy or "").strip()
        self.cookie_header = cookie_header or ""
        self.user_agent = user_agent or UA_CHROME
        self.workers = max(1, min(int(workers or 8), 32))
        self.skip_existing = skip_existing
        self.tz_mode = tz_mode
        self.log = log or Logger()
        self.stop_event = stop_event or threading.Event()
        self.on_progress = on_progress or (lambda info: None)
        self.cookie_to_cdn = bool(cookie_to_cdn)

        self._local = threading.local()
        self._csv_lock = threading.Lock()
        self._csv_path = os.path.join(out_dir, "_meta", "downloads.csv")
        self._fail_csv_path = os.path.join(out_dir, "_meta", "failed.csv")
        self._csv_ready = False
        self.shapes = ShapeStats()
        self.limiter = RateLimiter(rate_per_sec)
        self._fail_streak = 0
        self._streak_lock = threading.Lock()
        self.stats = {"ok": 0, "skip": 0, "fail": 0, "bytes": 0}

    # ------------------------------------------------------------ 会话
    def _session(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            session.headers.update({
                "User-Agent": self.user_agent,
                "Referer": "https://x.com/",
                "Accept": "*/*",
                "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            })
            # Cookie 不挂在会话上：按请求域名决定是否携带（见 _cookie_for）
            if self.proxy:
                session.proxies.update({"http": self.proxy, "https": self.proxy})
                if self.proxy.lower().startswith(("socks", "socks4", "socks5")):
                    try:
                        import socks  # noqa: F401  (PySocks)
                    except ImportError:
                        self.log("提示：代理是 SOCKS 但没装 PySocks，请改用 http:// 代理或执行 python -m pip install pysocks")
            session.mount("https://", HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0))
            session.mount("http://", HTTPAdapter(pool_connections=4, pool_maxsize=16, max_retries=0))
            self._local.session = session
        return session

    def _cookie_for(self, url: str, with_cookie: bool) -> Optional[Dict[str, str]]:
        if not (with_cookie and self.cookie_header):
            return None
        if _is_x_site(url) or (self.cookie_to_cdn and _is_twimg(url)):
            return {"Cookie": self.cookie_header}
        return None

    # ------------------------------------------------------------ 目标路径
    def _target_path(self, item: MediaItem) -> Tuple[str, str]:
        day = to_local_date(item.created_at, self.tz_mode)
        folder = os.path.join(self.out_dir, day.strftime("%Y-%m-%d") if day else "未知日期")
        ensure_dir(folder)
        user = sanitize(item.username or "unknown", 40)
        if ".m3u8" in item.url:                      # HLS 兜底：分片合并成 .ts
            ext = "ts"
        else:
            ext = _ext_of(item.url, "mp4" if item.is_video else "jpg")
        if item.tweet_id:
            name = f"{user}_{item.tweet_id}_{item.index:02d}.{ext}"
        else:
            # 没有推文 ID（DOM 兜底）时用 URL 短哈希区分，避免互相覆盖/误跳过
            short = hashlib.md5(item.url.split("?")[0].encode("utf-8")).hexdigest()[:10]
            name = f"{user}_na_{short}_{item.index:02d}.{ext}"
        path = os.path.join(folder, name)
        if os.path.exists(path) and not self.skip_existing:
            path = unique_path(path)
        return folder, path

    # ------------------------------------------------------------ 记录
    def _append_csv(self, item: MediaItem, path: str, size: int) -> None:
        with self._csv_lock:
            try:
                ensure_dir(os.path.dirname(self._csv_path))
                new_file = not os.path.exists(self._csv_path)
                with open(self._csv_path, "a", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh)
                    if new_file:
                        writer.writerow(_CSV_HEADER)
                    writer.writerow([
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        to_local_date(item.created_at, self.tz_mode) or "",
                        item.username,
                        item.tweet_url,
                        item.kind,
                        os.path.basename(path),
                        size,
                        item.url,
                    ])
            except OSError:
                pass

    def _append_fail_csv(self, item: MediaItem, error: str, attempts: List[Tuple[str, Any, str]]) -> None:
        with self._csv_lock:
            try:
                ensure_dir(os.path.dirname(self._fail_csv_path))
                new_file = not os.path.exists(self._fail_csv_path)
                detail = " | ".join(f"{url} => {status if status else err}" for url, status, err in attempts)
                with open(self._fail_csv_path, "a", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh)
                    if new_file:
                        writer.writerow(_FAIL_CSV_HEADER)
                    writer.writerow([
                        time.strftime("%Y-%m-%d %H:%M:%S"),
                        item.username,
                        item.tweet_url,
                        item.kind,
                        item.url,
                        error,
                        len(attempts),
                        detail[:2000],
                    ])
            except OSError:
                pass

    # ------------------------------------------------------------ 单次请求
    def _fetch_once(self, url: str, dest: str, with_cookie: bool) -> _FetchResult:
        """取一个地址。返回状态码；size 非空表示已经落盘。"""
        if not self.limiter.wait(self.stop_event):
            return _FetchResult(None, None, "用户停止")
        session = self._session()
        tmp = dest + ".part"
        headers = self._cookie_for(url, with_cookie)
        try:
            with session.get(url, stream=True, timeout=(15, 90), headers=headers) as resp:
                status = resp.status_code
                if status in CDN_GATE_STATUSES:
                    self._drain(resp)
                    return _FetchResult(status, None, "")
                if status != 200:
                    self._drain(resp)
                    return _FetchResult(status, None, f"HTTP {status}")
                total = 0
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_content(CHUNK):
                        if self.stop_event.is_set():
                            raise InterruptedError("用户停止")
                        if chunk:
                            fh.write(chunk)
                            total += len(chunk)
                if total <= 0:
                    raise requests.RequestException("响应体为空")
            os.replace(tmp, dest)
            return _FetchResult(200, total, "")
        except InterruptedError:
            self._cleanup_part(dest)
            raise
        except requests.RequestException as exc:
            self._cleanup_part(dest)
            return _FetchResult(None, None, f"{type(exc).__name__}: {exc}")

    @staticmethod
    def _drain(resp: requests.Response) -> None:
        try:
            resp.content
        except Exception:  # noqa: BLE001
            pass

    def _stream_to_file(self, url: str, dest: str) -> int:
        """单地址下载（视频用）；失败抛异常。"""
        result = self._fetch_once(url, dest, with_cookie=True)
        if result.ok:
            return int(result.size or 0)
        if result.status:
            raise requests.HTTPError(f"HTTP {result.status} {url}")
        raise requests.RequestException(f"{result.error} {url}")

    def _cleanup_part(self, dest: str) -> None:
        try:
            if os.path.exists(dest + ".part"):
                os.remove(dest + ".part")
        except OSError:
            pass

    # ------------------------------------------------------------ 图片候选链
    def _photo_ladder(self, item: MediaItem) -> List[str]:
        candidates = [item.url, *(item.fallback_urls or [])]
        out: List[str] = []
        for url in candidates:
            if url and url not in out:
                out.append(url)
        return self.shapes.order(out)

    def _download_photo(self, item: MediaItem, dest: str) -> int:
        attempts: List[Tuple[str, Any, str]] = []
        ladder = self._photo_ladder(item)
        # 第一遍匿名；第二遍带着登录 Cookie（部分受限/敏感媒体只有登录后才给图）
        passes = [False, True] if self.cookie_header else [False]

        for with_cookie in passes:
            if with_cookie and not _needs_cookie_pass(attempts):
                # 一整轮都是 404（媒体真的没了），带 Cookie 再走一遍也没用，省下请求
                break
            for url in ladder:
                if self.stop_event.is_set():
                    raise InterruptedError("用户停止")
                result = self._fetch_once(url, dest, with_cookie=with_cookie)
                if result.ok:
                    self.shapes.note(shape_key(url), True)
                    self._note_result(True)
                    return int(result.size or 0)
                attempts.append((url, result.status or result.error, f"cookie={int(with_cookie)}"))
                self.shapes.note(shape_key(url), False)
                if result.status == 429:
                    self._note_result(False)
                    self.limiter.slow_down()
                    self.log("图片 CDN 返回 429（被限速），本轮自动降速…")
                    self._sleep(6.0)
                elif result.status is None:
                    # 网络错误：有可能只是抖动，重试同一个地址一次
                    self._note_result(False)
                    if self.stop_event.is_set():
                        raise InterruptedError("用户停止")
                    self._sleep(1.2)
                    retry = self._fetch_once(url, dest, with_cookie=with_cookie)
                    if retry.ok:
                        self.shapes.note(shape_key(url), True)
                        self._note_result(True)
                        return int(retry.size or 0)
                    attempts.append((url, retry.status or retry.error, "重试"))

        self._note_result(False)
        raise AllCandidatesFailed("所有候选地址都取不到（详见 _meta/failed.csv）", attempts)

    def _note_result(self, ok: bool) -> None:
        """连续失败太多时自动降速，避免把 CDN/账号推到风控线。"""
        with self._streak_lock:
            self._fail_streak = 0 if ok else self._fail_streak + 1
            streak = self._fail_streak
        if streak in (8, 20, 40) and self.limiter.penalty < 4.0:
            self.limiter.slow_down(1.5)
            self.log(f"连续 {streak} 个媒体失败，已自动降速（当前限速倍率 {self.limiter.penalty:.1f}×）")

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                return
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))

    # ------------------------------------------------------------ HLS 兜底
    def _download_hls(self, url: str, dest: str) -> int:
        """拉 m3u8 分片并合并成单个 .ts 文件（只有接口兜底失败时才会走到这里）。"""
        session = self._session()
        text = session.get(url, timeout=(15, 60)).text
        lines = [line.strip() for line in text.splitlines() if line.strip()]

        # 主播放列表 → 选码率最高的一路
        if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
            best_uri, best_rate = None, -1
            for idx, line in enumerate(lines):
                if line.startswith("#EXT-X-STREAM-INF"):
                    rate = 0
                    for token in line.split(","):
                        if token.startswith("BANDWIDTH="):
                            try:
                                rate = int(token.split("=", 1)[1])
                            except ValueError:
                                rate = 0
                    for nxt in lines[idx + 1:]:
                        if not nxt.startswith("#"):
                            if rate > best_rate:
                                best_uri, best_rate = nxt, rate
                            break
            if best_uri:
                url = urljoin(url, best_uri)
                text = session.get(url, timeout=(15, 60)).text
                lines = [line.strip() for line in text.splitlines() if line.strip()]

        key: Optional[bytes] = None
        iv: Optional[bytes] = None
        segments: List[Tuple[str, Optional[bytes], Optional[bytes]]] = []
        pending_extinf = False
        for line in lines:
            if line.startswith("#EXT-X-KEY"):
                method = ""
                uri = None
                iv_hex = None
                for token in line[len("#EXT-X-KEY:"):].split(","):
                    token = token.strip()
                    if token.startswith("METHOD="):
                        method = token.split("=", 1)[1]
                    elif token.startswith("URI="):
                        uri = token.split("=", 1)[1].strip('"')
                    elif token.startswith("IV="):
                        iv_hex = token.split("=", 1)[1]
                if method == "AES-128" and uri:
                    key = session.get(urljoin(url, uri), timeout=(15, 60)).content
                    iv = bytes.fromhex(iv_hex[2:]) if iv_hex and iv_hex.startswith("0x") and len(iv_hex) == 34 else None
                else:
                    key, iv = None, None
                continue
            if line.startswith("#EXTINF"):
                pending_extinf = True
                continue
            if line.startswith("#"):
                continue
            if pending_extinf or line.endswith(".ts") or ".ts?" in line or line.endswith(".m4s"):
                segments.append((urljoin(url, line), key, iv))
                pending_extinf = False

        if not segments:
            raise RuntimeError("m3u8 里没有解析出分片")

        tmp = dest + ".part"
        total = 0
        with open(tmp, "wb") as fh:
            for seq, (seg_url, seg_key, seg_iv) in enumerate(segments):
                if self.stop_event.is_set():
                    raise InterruptedError("用户停止")
                self.limiter.wait(self.stop_event)
                data = session.get(seg_url, timeout=(15, 90)).content
                if seg_key:
                    data = _aes128_cbc_decrypt(data, seg_key, seg_iv if seg_iv else seq.to_bytes(16, "big"))
                fh.write(data)
                total += len(data)
        os.replace(tmp, dest)
        return total

    # ------------------------------------------------------------ 任务封装
    def _handle(self, item: MediaItem) -> Tuple[str, int, str]:
        """返回 (状态, 字节数, 文件路径/错误信息)。"""
        _folder, dest = self._target_path(item)
        if os.path.exists(dest) and self.skip_existing:
            return "skip", os.path.getsize(dest), dest

        is_hls = ".m3u8" in item.url

        last_error: Optional[Exception] = None
        for attempt in range(1, RETRIES + 1):
            if self.stop_event.is_set():
                return "fail", 0, "用户停止"
            try:
                if is_hls:
                    size = self._download_hls(item.url, dest)
                elif item.is_video:
                    size = self._stream_to_file(item.url, dest)
                else:
                    size = self._download_photo(item, dest)
                self._append_csv(item, dest, size)
                return "ok", size, dest
            except InterruptedError as exc:
                self._cleanup_part(dest)
                return "fail", 0, str(exc)
            except AllCandidatesFailed as exc:
                # 候选链全试过了：确定性失败，重试只是白打请求
                self._cleanup_part(dest)
                self._append_fail_csv(item, str(exc), exc.attempts)
                detail = ",".join(str(a[1]) for a in exc.attempts[-6:])
                return "fail", 0, f"全部候选失败（最后状态：{detail}）"
            except requests.HTTPError as exc:
                last_error = exc
                self._cleanup_part(dest)
                status = _status_of(exc)
                if status in CDN_GATE_STATUSES:
                    return "fail", 0, f"HTTP {status} {item.url}"
                if attempt < RETRIES:
                    self._sleep(1.0 * attempt)
            except Exception as exc:  # noqa: BLE001 —— 下载必须单点失败不影响整体
                last_error = exc
                self._cleanup_part(dest)
                if attempt < RETRIES:
                    self._sleep(0.8 * attempt)
        return "fail", 0, f"{type(last_error).__name__}: {last_error}"

    # ------------------------------------------------------------ 主入口
    def run(self, items: Iterable[MediaItem]) -> Dict[str, Any]:
        tasks: List[MediaItem] = []
        seen = set()
        for item in items:
            key = (item.tweet_id, item.index, item.url)
            if key in seen:
                continue
            seen.add(key)
            tasks.append(item)

        total = len(tasks)
        ensure_dir(self.out_dir)
        self.log(
            f"待下载媒体 {total} 个，并发 {self.workers}，"
            f"限速 {self.limiter.per_sec:.1f} 请求/秒，保存到 {self.out_dir}"
        )
        if not total:
            return dict(self.stats, total=0)

        failures: List[str] = []
        done = 0
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="dl") as pool:
            futures = {pool.submit(self._handle, item): item for item in tasks}
            for future in as_completed(futures):
                item = futures[future]
                try:
                    status, size, info = future.result()
                except Exception as exc:  # noqa: BLE001
                    status, size, info = "fail", 0, f"{type(exc).__name__}: {exc}"
                done += 1
                if status == "ok":
                    self.stats["ok"] += 1
                    self.stats["bytes"] += size
                elif status == "skip":
                    self.stats["skip"] += 1
                else:
                    self.stats["fail"] += 1
                    if len(failures) < 30:
                        failures.append(f"{item.username or '?'}/{item.tweet_id or '?'} → {info}")
                self.on_progress({
                    "phase": "下载",
                    "done": done,
                    "total": total,
                    "ok": self.stats["ok"],
                    "skip": self.stats["skip"],
                    "fail": self.stats["fail"],
                    "bytes": self.stats["bytes"],
                    "current": f"@{item.username} {item.kind}",
                })
                if self.stop_event.is_set() and done < total:
                    self.log("已停止，等待在途任务收尾…")
                    pool.shutdown(wait=False, cancel_futures=True)
                    break

        result = dict(self.stats, total=total, failures=failures)
        if self.shapes.snapshot():
            self.log("地址形状统计：" + self.shapes.summary())
        self.log(
            f"下载完成：成功 {result['ok']}、跳过 {result['skip']}、失败 {result['fail']}，"
            f"共 {human_size(result['bytes'])}"
        )
        if result["fail"] and os.path.exists(self._fail_csv_path):
            self.log(f"失败明细已写入：{self._fail_csv_path}（内含每个候选地址的 HTTP 状态，排查用）")
            result["failed_csv"] = self._fail_csv_path
        return result


def _needs_cookie_pass(attempts: List[Tuple[str, Any, str]]) -> bool:
    """第一轮出现 401/403/451，或候选很少时，才值得带 Cookie 再走一遍。"""
    if len(attempts) <= 3:
        return True
    return any(attempt[1] in (401, 403, 451) for attempt in attempts)


def _status_of(exc: Exception) -> Optional[int]:
    text = str(exc)
    if not text.startswith("HTTP "):
        return None
    try:
        return int(text.split()[1])
    except (IndexError, ValueError):
        return None


def _aes128_cbc_decrypt(data: bytes, key: bytes, iv: bytes) -> bytes:
    """HLS 分片的 AES-128-CBC 解密（带 PKCS7 去填充）。"""
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        decryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
        plain = decryptor.update(data) + decryptor.finalize()
        pad = plain[-1] if plain else 0
        if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
            plain = plain[:-pad]
        return plain
    except ImportError:
        try:
            from Cryptodome.Cipher import AES as _AES

            plain = _AES.new(key, _AES.MODE_CBC, iv).decrypt(data)
            pad = plain[-1] if plain else 0
            if 1 <= pad <= 16 and plain[-pad:] == bytes([pad]) * pad:
                plain = plain[:-pad]
            return plain
        except ImportError:
            raise RuntimeError("HLS 分片是加密的，需要 cryptography 或 pycryptodome 才能解密")


def http_get_text(url: str, proxy: str = "", timeout: float = 20.0, headers: Optional[Dict[str, str]] = None) -> str:
    sess = requests.Session()
    if proxy:
        sess.proxies.update({"http": proxy, "https": proxy})
    sess.headers.update({"User-Agent": UA_CHROME})
    if headers:
        sess.headers.update(headers)
    resp = sess.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp.text

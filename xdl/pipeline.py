# -*- coding: utf-8 -*-
"""编排层：一个"任务" = 多个搜索词 × (抓取 → 过滤 → 下载)。"""
from __future__ import annotations

import datetime as dt
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from . import cache as harvest_cache
from .browser import BrowserController, cookie_header
from .download import DEFAULT_RATE_PER_SEC, Downloader
from .harvest import Harvester
from .parse import (
    MediaItem,
    Tweet,
    build_query,
    filter_by_date,
    iter_media,
)
from .util import Logger, human_size, parse_ymd


@dataclass
class JobConfig:
    queries: List[str] = field(default_factory=list)
    date_from: str = ""
    date_to: str = ""
    tab: str = "live"                 # live=最新 / top=热门
    want_photo: bool = True
    want_video: bool = True
    want_gif: bool = True
    include_retweet: bool = True
    auto_date_filter: bool = True     # 自动往搜索语句里补 since/until
    use_dom_fallback: bool = True     # 接口抓不到时用页面 DOM 兜底
    max_scrolls: int = 40
    max_tweets: int = 800
    workers: int = 8
    skip_existing: bool = True
    output_dir: str = ""
    tz_mode: str = "local"            # local=本机时区 / utc
    pause_min: float = 1.5
    pause_max: float = 2.8
    # ↓ v1.0.2 新增：防封号相关
    reuse_cache: bool = True          # 复用已抓取结果（搜索接口一次都不打）
    cache_ttl_hours: float = 72.0
    cooldown_sec: float = 20.0        # 多个搜索词之间的冷却
    dl_rate_per_sec: float = DEFAULT_RATE_PER_SEC
    cookie_to_cdn: bool = True
    safe_mode: bool = False           # 低速保守模式（界面上的"防封号"开关）


@dataclass
class JobResult:
    queries: int = 0
    tweets: int = 0
    media_found: int = 0
    media_in_range: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    bytes: int = 0
    elapsed: float = 0.0
    cancelled: bool = False
    failures: List[str] = field(default_factory=list)
    failed_csv: str = ""
    reused_queries: int = 0
    risk_hits: int = 0

    def summary(self) -> str:
        extra = ""
        if self.reused_queries:
            extra += f" · 复用缓存 {self.reused_queries} 个搜索词"
        if self.risk_hits:
            extra += f" · ⚠被限流 {self.risk_hits} 次"
        return (
            f"搜索词 {self.queries} 个 · 推文 {self.tweets} 条 · 命中媒体 {self.media_found} 个 "
            f"（区间内 {self.media_in_range}）· 下载成功 {self.downloaded} / 跳过 {self.skipped} / "
            f"失败 {self.failed} · 共 {human_size(self.bytes)} · 用时 {self.elapsed:.0f}s" + extra
        )


class Pipeline:
    def __init__(
        self,
        controller: BrowserController,
        session_id: str,
        config: JobConfig,
        log: Optional[Callable[[str], None]] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ):
        self.controller = controller
        self.session_id = session_id
        self.config = config
        self.log = log or Logger()
        self.on_progress = on_progress or (lambda info: None)
        self.stop_event = stop_event or threading.Event()

    def _sleep(self, seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, seconds)
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                return
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))

    def _cool_down(self, seconds: float, why: str) -> None:
        if seconds <= 0 or self.stop_event.is_set():
            return
        self.log(f"冷却 {seconds:.0f} 秒（{why}），避免把账号往限流线上推…")
        self._sleep(seconds)

    # ------------------------------------------------------------ 主流程
    def run(self) -> JobResult:
        cfg = self.config
        started = time.time()
        result = JobResult()
        result.queries = len(cfg.queries)

        start_date = parse_ymd(cfg.date_from)
        end_date = parse_ymd(cfg.date_to)
        if start_date and end_date and start_date > end_date:
            start_date, end_date = end_date, start_date
        self.log(f"日期区间：{start_date or '不限'} ~ {end_date or '不限'}（时区：{'UTC' if cfg.tz_mode == 'utc' else '本机'}）")
        if cfg.safe_mode:
            self.log("已启用「低速防封模式」：翻页更慢、并发更低、搜索词之间强制冷却、复用抓取缓存")

        harvester = Harvester(
            self.controller,
            self.session_id,
            log=self.log,
            stop_event=self.stop_event,
            on_progress=self.on_progress,
        )

        all_tweets: Dict[str, Tweet] = {}
        dom_items: List[MediaItem] = []

        for index, keyword in enumerate(cfg.queries, start=1):
            if self.stop_event.is_set():
                result.cancelled = True
                break
            keyword = keyword.strip()
            if not keyword:
                continue
            if index > 1:
                self._cool_down(cfg.cooldown_sec, "两个搜索词之间")
                if self.stop_event.is_set():
                    result.cancelled = True
                    break
            # X 按 UTC 计算 since/until：交给 X 的范围前后各放宽 1 天，精确筛选交给本地 filter_by_date
            wide_start = start_date - dt.timedelta(days=1) if start_date else None
            wide_end = end_date + dt.timedelta(days=1) if end_date else None
            full_query = build_query(keyword, wide_start, wide_end, cfg.auto_date_filter)
            self.log(f"—— 第 {index}/{len(cfg.queries)} 个搜索词：{full_query}")

            tweets: Optional[List[Tweet]] = None
            dom: List[MediaItem] = []
            if cfg.reuse_cache:
                cached = harvest_cache.load(cfg.output_dir, full_query, cfg.tab, cfg.cache_ttl_hours, self.log)
                if cached:
                    tweets, dom = cached
                    result.reused_queries += 1

            if tweets is None:
                tweets, dom = harvester.search(
                    full_query,
                    tab=cfg.tab,
                    max_scrolls=cfg.max_scrolls,
                    max_tweets=max(50, cfg.max_tweets // max(1, len(cfg.queries)) + 50),
                    pause=(cfg.pause_min, cfg.pause_max),
                    stop_before=start_date if cfg.tab == "live" else None,
                    tz_mode=cfg.tz_mode,
                    use_dom_fallback=cfg.use_dom_fallback,
                )
                if cfg.reuse_cache and (tweets or dom) and not self.stop_event.is_set():
                    harvest_cache.save(cfg.output_dir, full_query, cfg.tab, tweets, dom, self.log)

            for tweet in tweets:
                all_tweets.setdefault(tweet.tweet_id, tweet)
            dom_items.extend(dom)

        result.risk_hits = harvester.risk.hits
        if result.risk_hits:
            self.log(
                f"⚠ 本轮搜索共被限流 {result.risk_hits} 次。建议：勾上「低速防封模式」、"
                f"减少搜索词数量、隔几小时再跑；抓取结果已缓存，重下不会再次请求搜索接口。"
            )

        result.tweets = len(all_tweets)
        self.log(f"去重后共 {result.tweets} 条推文")

        # 抽媒体
        media: List[MediaItem] = list(
            iter_media(
                all_tweets.values(),
                want_photo=cfg.want_photo,
                want_video=cfg.want_video,
                want_gif=cfg.want_gif,
                include_retweet=cfg.include_retweet,
            )
        )
        # DOM 兜底媒体（按 URL 去重后并入）
        have_urls = {item.url.split("?")[0] for item in media}
        for item in dom_items:
            if item.url.split("?")[0] in have_urls:
                continue
            if item.kind == "photo" and not cfg.want_photo:
                continue
            if item.kind in ("video", "gif") and not (cfg.want_video or cfg.want_gif):
                continue
            media.append(item)
            have_urls.add(item.url.split("?")[0])

        result.media_found = len(media)
        media = filter_by_date(media, start_date, end_date, cfg.tz_mode)
        result.media_in_range = len(media)
        self.log(f"日期区间内共 {result.media_in_range} 个媒体待下载")

        if not media:
            result.elapsed = time.time() - started
            return result

        downloader = Downloader(
            out_dir=cfg.output_dir,
            proxy=self.controller.proxy,
            cookie_header=getattr(self.controller, "cookie_header", "") or "",
            user_agent=getattr(self.controller, "user_agent", "") or "",
            workers=cfg.workers,
            skip_existing=cfg.skip_existing,
            tz_mode=cfg.tz_mode,
            log=self.log,
            stop_event=self.stop_event,
            on_progress=self.on_progress,
            rate_per_sec=cfg.dl_rate_per_sec,
            cookie_to_cdn=cfg.cookie_to_cdn,
        )
        stats = downloader.run(media)
        result.downloaded = stats.get("ok", 0)
        result.skipped = stats.get("skip", 0)
        result.failed = stats.get("fail", 0)
        result.bytes = stats.get("bytes", 0)
        result.failures = stats.get("failures", []) or []
        result.failed_csv = stats.get("failed_csv", "") or ""
        result.cancelled = self.stop_event.is_set()
        result.elapsed = time.time() - started
        return result


def run_job(
    controller: BrowserController,
    session_id: str,
    config: JobConfig,
    log: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    stop_event: Optional[threading.Event] = None,
) -> JobResult:
    controller.cookie_header = getattr(controller, "cookie_header", "")  # noqa: B010
    return Pipeline(controller, session_id, config, log, on_progress, stop_event).run()

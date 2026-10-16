# -*- coding: utf-8 -*-
"""抓取层：驱动浏览器打开 X 搜索页，拦截 SearchTimeline 接口数据，滚动翻页收集推文。

为什么走浏览器而不是直接调接口：
  X 的 GraphQL 接口需要 `x-client-transaction-id` 等动态签名头，纯代码模拟很容易失效；
  让真实浏览器自己发请求，我们只把响应读出来，最稳且能长期可用。

v1.0.2 防封号相关改动：
  1) 翻页不再"一步冲到页面底部"（那会瞬间触发一大批时间线请求），改成按视口小步滚动 + 抖动；
  2) 监听搜索接口的 HTTP 状态：出现 429/401/403 立刻判定"触发风控"，自动冷却并降速，
     连续触发多次就停下本轮（继续硬打只会加速封号）；
  3) 每轮之间的间隔带随机抖动，并随风控次数自适应放大（最多 6 倍）；
  4) 页面报错 / 风控时不再反复刷新页面重试（刷新等于又打一批请求）。
"""
from __future__ import annotations

import base64
import json
import random
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .browser import BrowserController
from .cdp import CDPError
from .parse import MediaItem, Tweet, parse_search_response, photo_candidate_urls, search_url
from .util import Logger, parse_tweet_time, to_local_date

# 只关心这两个搜索接口
MATCH_TOKENS = ("SearchTimeline", "search_timeline", "SearchBox")

# 触发风控的状态码（X 限流主要用 429，鉴权失效/被标记时给 401/403）
RISK_STATUSES = (401, 403, 429)

# 前端风控信息的兜底上限：一轮里最多容忍几次
MAX_RISK_HITS = 3

# 页面健康检查 + DOM 兜底抓取
_PAGE_PROBE_JS = r"""
(() => {
  const body = document.body ? (document.body.innerText || '').slice(0, 6000) : '';
  return JSON.stringify({
    articles: document.querySelectorAll('article').length,
    error: /(出错了|Something went wrong|Try reloading|发生错误|重试|Rate limit|超出|尝试次数过多)/.test(body),
    loginWall: document.querySelectorAll('article').length === 0 &&
               /(登录|Log in|Sign in to X|立即注册)/.test(body),
    y: Math.round(window.scrollY),
    h: Math.round(document.documentElement.scrollHeight)
  });
})()
"""

_DOM_SWEEP_JS = r"""
(() => {
  const out = [];
  document.querySelectorAll('article').forEach(art => {
    const timeEl = art.querySelector('time[datetime]');
    const link = art.querySelector('a[href*="/status/"]');
    const media = [];
    art.querySelectorAll('img[src*="pbs.twimg.com"], img[src*="/media/"]').forEach(img => {
      let src = img.getAttribute('src') || '';
      if (!src || src.startsWith('data:')) return;
      if (/profile_images|profile_banners|emoji|hashflag|card_img|_thumb/.test(src)) return;
      try { src = new URL(src, location.href).href; } catch (e) {}
      media.push({ k: 'photo', u: src });
    });
    art.querySelectorAll('video').forEach(v => {
      const src = v.getAttribute('src') || '';
      if (src.startsWith('blob:')) {
        const poster = v.getAttribute('poster') || '';
        if (poster) media.push({ k: 'poster', u: poster });
      } else if (src) {
        media.push({ k: 'video', u: src });
      }
    });
    art.querySelectorAll('video source[src]').forEach(s => {
      const src = s.getAttribute('src') || '';
      if (src && !src.startsWith('blob:')) media.push({ k: 'video', u: src });
    });
    if (media.length || timeEl) {
      out.push({
        t: timeEl ? timeEl.getAttribute('datetime') : null,
        l: link ? link.getAttribute('href') : null,
        m: media
      });
    }
  });
  return JSON.stringify({ count: document.querySelectorAll('article').length, items: out });
})()
"""

# 小步滚动：一次滚 0.55~0.9 个视口（而不是直接冲到底），减少"瞬间触发一大批请求"
_SCROLL_JS = r"""
(() => {
  const doc = document.documentElement;
  const step = Math.round(window.innerHeight * (0.55 + Math.random() * 0.35));
  const before = window.scrollY;
  window.scrollBy(0, step);

  if (Math.abs(window.scrollY - before) < 4) {
    // 已经贴底：先后退再往下推一点，并补一次 scroll 事件
    window.scrollBy(0, -400);
    window.scrollBy(0, 400);
    try { window.dispatchEvent(new Event('scroll')); } catch (e) {}
    // 有些不吃 window 滚动的布局，需要滚动内部容器
    let best = null, bestH = 0;
    document.querySelectorAll('div, main, section').forEach(el => {
      if (el.scrollHeight > el.clientHeight + 200 && el.clientHeight > 250) {
        if (el.scrollHeight > bestH) { bestH = el.scrollHeight; best = el; }
      }
    });
    if (best) best.scrollTop = Math.min(best.scrollTop + step, best.scrollHeight);
  }
  return document.querySelectorAll('article').length;
})()
"""


class RiskGuard:
    """翻页节奏 + 风控感知：被限流就冷却降速，别把账号往封禁线上推。"""

    def __init__(self, log: Optional[Callable[[str], None]] = None,
                 stop_event: Optional[threading.Event] = None):
        self.log = log or Logger()
        self.stop_event = stop_event or threading.Event()
        self.hits = 0
        self.cooldown_until = 0.0
        self.slow = 1.0
        self.status_counts: Dict[int, int] = {}

    def note_status(self, status: int) -> None:
        if not status:
            return
        self.status_counts[status] = self.status_counts.get(status, 0) + 1
        if status in RISK_STATUSES:
            self.hits += 1
            self.slow = min(6.0, self.slow * 1.6)
            wait = min(240.0, 45.0 * self.hits)
            self.cooldown_until = max(self.cooldown_until, time.time() + wait)
            self.log(
                f"⚠ 搜索接口返回 HTTP {status}（第 {self.hits} 次被限流）：暂停 {wait:.0f} 秒并降速"
                f"到 {self.slow:.1f}×。这通常就是账号被 ban 的前兆，建议本轮跑完先歇一会儿。"
            )

    def calm_down(self) -> None:
        """一轮顺利结束：慢慢把速度收回来。"""
        self.slow = max(1.0, self.slow * 0.9)

    def wait_if_cooling(self) -> None:
        while time.time() < self.cooldown_until:
            if self.stop_event.is_set():
                return
            remain = self.cooldown_until - time.time()
            self.log(f"风控冷却中，还需 {remain:.0f} 秒…")
            time.sleep(min(10.0, max(0.5, remain)))

    def pause(self, low: float, high: float) -> float:
        """按当前降速倍率睡一会儿，返回实际睡了多久。"""
        span = max(0.0, high - low) * self.slow
        seconds = low * self.slow + random.uniform(0, span)
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stop_event.is_set():
                break
            time.sleep(min(0.25, max(0.0, deadline - time.monotonic())))
        return seconds

    def too_many_hits(self) -> bool:
        return self.hits >= MAX_RISK_HITS


class Harvester:
    """一个搜索词 = 一次 Harvester.search()。"""

    def __init__(
        self,
        controller: BrowserController,
        session_id: str,
        log: Optional[Callable[[str], None]] = None,
        stop_event: Optional[threading.Event] = None,
        on_progress: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self.controller = controller
        self.session_id = session_id
        self.log = log or Logger()
        self.stop_event = stop_event or threading.Event()
        self.on_progress = on_progress or (lambda info: None)
        self.intercepted = 0
        self._pending: Dict[str, str] = {}
        self.risk = RiskGuard(self.log, self.stop_event)

    # ------------------------------------------------------------ 事件处理
    @staticmethod
    def _is_search_api(url: str) -> bool:
        if not url:
            return False
        if "/graphql/" not in url and "/i/api/" not in url:
            return False
        return any(token in url for token in MATCH_TOKENS)

    @staticmethod
    def _is_api(url: str) -> bool:
        return bool(url) and ("/graphql/" in url or "/i/api/" in url)

    def _fetch_body(self, request_id: str, tweets: Dict[str, Tweet]) -> int:
        cdp = self.controller.cdp
        try:
            body = cdp.send(
                "Network.getResponseBody",
                {"requestId": request_id},
                session_id=self.session_id,
                timeout=40.0,
            )
        except CDPError:
            return 0
        text = body.get("body") or ""
        if body.get("base64Encoded"):
            try:
                text = base64.b64decode(text).decode("utf-8", "replace")
            except Exception:
                return 0
        if not text.strip():
            return 0
        try:
            payload = json.loads(text)
        except ValueError:
            return 0
        added = 0
        for tweet in parse_search_response(payload):
            if tweet.tweet_id not in tweets:
                tweets[tweet.tweet_id] = tweet
                added += 1
        self.intercepted += 1
        return added

    def _drain_events(self, tweets: Dict[str, Tweet]) -> Tuple[int, int]:
        """处理积压网络事件；返回 (新增推文数, 命中的接口响应数)。"""
        cdp = self.controller.cdp
        if cdp is None:
            return 0, 0
        added = 0
        hits = 0
        for event in cdp.events(timeout=0.05):
            event_session = event.get("sessionId")
            if event_session and event_session != self.session_id:
                continue
            method = event.get("method")
            params = event.get("params") or {}
            request_id = params.get("requestId")
            if not request_id:
                continue
            if method == "Network.responseReceived":
                response = params.get("response") or {}
                url = response.get("url") or ""
                if self._is_api(url):
                    # 状态码是判断"是不是被限流"的关键，逐条记下来
                    try:
                        self.risk.note_status(int(response.get("status") or 0))
                    except (TypeError, ValueError):
                        pass
                if self._is_search_api(url):
                    self._pending[request_id] = url
            elif method == "Network.loadingFinished":
                # 等响应体完整收完再读，避免读到半截数据导致整页丢失
                if self._pending.pop(request_id, None) is not None:
                    hits += 1
                    added += self._fetch_body(request_id, tweets)
            elif method == "Network.loadingFailed":
                self._pending.pop(request_id, None)
        return added, hits

    # ------------------------------------------------------------ 页面操作
    def _probe(self) -> Dict[str, Any]:
        cdp = self.controller.cdp
        if cdp is None:
            return {}
        try:
            raw = cdp.evaluate(self.session_id, _PAGE_PROBE_JS, timeout=20.0)
            return json.loads(raw) if raw else {}
        except (CDPError, ValueError):
            return {}

    def _scroll(self) -> None:
        cdp = self.controller.cdp
        if cdp is None:
            return
        try:
            cdp.evaluate(self.session_id, _SCROLL_JS, timeout=15.0)
        except CDPError:
            pass
        # 键盘翻页兜底（有些布局不吃 window.scrollTo），只有被限速时才用
        if self.risk.slow > 1.5:
            for kind in ("keyDown", "keyUp"):
                try:
                    cdp.send(
                        "Input.dispatchKeyEvent",
                        {
                            "type": kind,
                            "key": "PageDown",
                            "code": "PageDown",
                            "windowsVirtualKeyCode": 34,
                            "nativeVirtualKeyCode": 34,
                        },
                        session_id=self.session_id,
                    )
                except CDPError:
                    pass

    def _dom_sweep(self) -> List[MediaItem]:
        """DOM 兜底：直接从页面上把媒体 URL 抠出来（接口抓包失败时用）。

        页面上的图片地址是浏览器**已经成功加载过**的，所以把它放进候选链尾部当保底，
        大小档位从大往小试，最后总能落到能下的那一档。
        """
        cdp = self.controller.cdp
        if cdp is None:
            return []
        try:
            raw = cdp.evaluate(self.session_id, _DOM_SWEEP_JS, timeout=30.0)
            data = json.loads(raw) if raw else {}
        except (CDPError, ValueError):
            return []
        items: List[MediaItem] = []
        for entry in data.get("items", []):
            when = parse_tweet_time(entry.get("t"))
            link = str(entry.get("l") or "")
            tweet_id = ""
            username = ""
            if "/status/" in link:
                head, _, tail = link.partition("/status/")
                username = head.strip("/").split("/")[-1]
                tweet_id = tail.split("/")[0].split("?")[0]
            for pos, media in enumerate(entry.get("m") or [], start=1):
                src = str(media.get("u") or "")
                if not src:
                    continue
                if media.get("k") == "poster":
                    continue          # 视频封面不是媒体本体，不下载
                kind = {"photo": "photo", "video": "video"}.get(media.get("k"), "photo")
                if kind == "photo":
                    ladder = photo_candidate_urls(src)
                    url = ladder[0] if ladder else src
                    fallbacks = ladder[1:]
                else:
                    url, fallbacks = src, []
                items.append(
                    MediaItem(
                        kind=kind,
                        url=url,
                        fallback_urls=fallbacks,
                        tweet_id=tweet_id,
                        username=username,
                        created_at=when,
                        tweet_url=link if link.startswith("http") else f"https://x.com{link}",
                        index=pos,
                    )
                )
        return items

    # ------------------------------------------------------------ 主流程
    def search(
        self,
        query: str,
        tab: str = "live",
        max_scrolls: int = 40,
        max_tweets: int = 800,
        pause: Tuple[float, float] = (1.5, 2.8),
        stop_before=None,
        tz_mode: str = "local",
        use_dom_fallback: bool = True,
        page_url: Optional[str] = None,
    ) -> Tuple[List[Tweet], List[MediaItem]]:
        """跑一次搜索，返回 (推文列表, DOM 兜底媒体列表)。

        page_url 仅用于自检时指向本地假页面；正常运行时留空即可。
        """
        cdp = self.controller.ensure_ready()
        sid = self.session_id
        cdp.send("Page.enable", {}, session_id=sid)
        cdp.send("Network.enable", {}, session_id=sid)
        cdp.drain()

        url = page_url or search_url(query, tab)
        self.log(f"打开搜索页（{('最新' if tab == 'live' else '热门')}）：{url}")
        try:
            cdp.send("Page.navigate", {"url": url}, session_id=sid, timeout=40.0)
        except CDPError as exc:
            self.log(f"打开搜索页失败：{exc}")
            return [], []
        self.controller.wait_dom_ready(sid, timeout=30.0)
        self.risk.pause(1.6, 2.6)

        tweets: Dict[str, Tweet] = {}
        stagnant = 0
        last_total = 0
        reloaded = False

        for step in range(max(1, max_scrolls)):
            if self.stop_event.is_set():
                self.log("收到停止指令，结束本轮抓取")
                break
            self.risk.wait_if_cooling()
            if self.risk.too_many_hits():
                self.log("连续被限流，主动结束本轮搜索（保护账号，稍后再试更稳）")
                break

            added, _hits = self._drain_events(tweets)
            probe = self._probe()

            if probe.get("error") and not reloaded:
                self.log("页面提示出错，停 20 秒再看一次（不反复刷新，刷新等于又打一批请求）")
                reloaded = True
                self.risk.pause(18.0, 26.0)
                continue
            if probe.get("error"):
                self.log("页面持续报错（可能触发风控），停止抓取")
                break
            if probe.get("loginWall"):
                self.log("页面像是未登录状态，请先完成登录再抓取")
                break

            total = len(tweets)
            self.on_progress({
                "phase": "抓取",
                "scrolls": step + 1,
                "tweets": total,
                "dom_articles": probe.get("articles", 0),
                "intercepted": self.intercepted,
                "risk": self.risk.hits,
            })

            # 结束条件：连续多轮没有新东西
            if total + int(probe.get("articles", 0)) <= last_total and added == 0:
                stagnant += 1
            else:
                stagnant = 0
            last_total = total + int(probe.get("articles", 0))

            if total >= max_tweets:
                self.log(f"已达设定上限 {max_tweets} 条推文，停止翻页")
                break
            if stagnant >= 5:
                self.log("连续多轮没有新内容，判定已到底")
                break

            # 日期早退：最新页是按时间倒序，最早一条已经早于起始日期就可以停
            if stop_before and tab == "live" and tweets:
                oldest = min(
                    (t.created_at for t in tweets.values() if t.created_at),
                    default=None,
                )
                if oldest and to_local_date(oldest, tz_mode) and to_local_date(oldest, tz_mode) < stop_before:
                    self.log(f"已抓到早于起始日期的内容（最早 {to_local_date(oldest, tz_mode)}），提前结束翻页")
                    break

            self._scroll()
            # 最后再抽一次事件：把这一屏新发出的请求连同状态码一起收掉
            added_now, _h = self._drain_events(tweets)
            if self.risk.hits == 0:
                self.risk.calm_down()
            self.risk.pause(pause[0], pause[1])
            if added_now == 0 and added == 0 and self.risk.hits:
                # 被限流又拿不到新数据，再滚下去没意义
                break

        # 收尾：再抽一次事件，必要时做 DOM 兜底
        self._drain_events(tweets)
        dom_items: List[MediaItem] = []
        if use_dom_fallback:
            self._log_risk()
            dom_items = self._dom_sweep()

        self.log(
            f"本轮抓取结束：接口命中 {self.intercepted} 次，拿到 {len(tweets)} 条推文，"
            f"DOM 兜底 {len(dom_items)} 个媒体"
        )
        self._log_risk()
        return list(tweets.values()), dom_items

    def _log_risk(self) -> None:
        if not self.risk.status_counts:
            return
        detail = "、".join(f"{code}×{count}" for code, count in sorted(self.risk.status_counts.items()))
        self.log(f"本轮搜索接口状态码统计：{detail}（429/403 表示被限流，次数越多越接近封号）")

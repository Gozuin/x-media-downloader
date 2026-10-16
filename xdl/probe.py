# -*- coding: utf-8 -*-
"""媒体地址诊断：把"为什么这张图 404"变成一条能看懂的结论。

做法（不用猜）：
  1) 用程序自己的浏览器打开这条推文页（登录态沿用，和抓取时完全一样）；
  2) 从页面 DOM 里读出**浏览器真正加载成功**的图片/视频地址；
  3) 把该媒体的整条候选地址链逐个请求一遍，记录每个地址的 HTTP 状态；
  4) 给出结论：是"地址形态不对"还是"这个媒体本身已经取不到"，
     并顺手给出当前网络下**能用的那种写法**（写进报告，方便下次直接改）。

命令行：
    X媒体下载器.exe --probe https://x.com/user/status/1234567890
GUI：「🔍 媒体诊断」按钮，粘一条推文链接即可。
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

from .browser import BrowserController
from .download import RETRYABLE_STATUSES, ShapeStats, shape_key
from .parse import photo_candidate_urls
from .util import Logger

# 从页面里读媒体的脚本：优先取"已经在页面上渲染成功"的 img/video
_TWEET_MEDIA_JS = r"""
(() => {
  const pick = [];
  document.querySelectorAll('article img[src*="pbs.twimg.com"], img[src*="pbs.twimg.com"]').forEach(img => {
    const src = img.getAttribute('src') || '';
    if (!src || src.startsWith('data:')) return;
    if (/profile_images|profile_banners|emoji|hashflag|card_img/.test(src)) return;
    pick.push({ kind: 'photo', src: img.src || src, natural: (img.naturalWidth || 0) + 'x' + (img.naturalHeight || 0) });
  });
  document.querySelectorAll('video source[src], video[src]').forEach(v => {
    const src = v.getAttribute('src') || '';
    if (src && !src.startsWith('blob:')) pick.push({ kind: 'video', src: src, natural: '' });
  });
  const seen = {};
  const out = pick.filter(item => {
    const key = item.src.split('?')[0];
    if (seen[key]) return false;
    seen[key] = 1;
    return true;
  });
  return JSON.stringify({ url: location.href, title: document.title, items: out });
})()
"""

_MEDIA_ID_RE = re.compile(r"/(?:media|ext_tw_video_thumb|amplify_video_thumb|tweet_video)/([A-Za-z0-9_-]{8,})")


def media_base_of(url: str) -> str:
    return str(url or "").split("?")[0]


def probe_urls(url: str, proxy: str = "", user_agent: str = "", cookie_header: str = "",
               cookie_to_cdn: bool = True, timeout: float = 15.0) -> List[Tuple[str, Any, str]]:
    """逐个请求候选地址，返回 [(url, 状态码或错误, 备注)]。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": user_agent or "Mozilla/5.0",
        "Referer": "https://x.com/",
        "Accept": "*/*",
    })
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    if cookie_header and cookie_to_cdn:
        session.headers["Cookie"] = cookie_header

    rows: List[Tuple[str, Any, str]] = []
    # 图片才有一整条候选链；视频地址（video.twimg.com/...mp4、m3u8）只有它自己
    candidates = photo_candidate_urls(url) if "twimg.com/media/" in url else [str(url)]
    for candidate in candidates:
        try:
            resp = session.get(candidate, stream=True, timeout=timeout, allow_redirects=True)
            status = resp.status_code
            size = resp.headers.get("Content-Length") or ""
            ctype = resp.headers.get("Content-Type") or ""
            resp.close()
            rows.append((candidate, status, f"{ctype} {size}".strip()))
        except requests.RequestException as exc:
            rows.append((candidate, None, f"{type(exc).__name__}: {exc}"))
    return rows


def verdict(rows: List[Tuple[str, Any, str]]) -> str:
    ok = [row for row in rows if row[1] == 200]
    statuses = {row[1] for row in rows}
    if ok:
        shapes = sorted({shape_key(row[0]) for row in ok})
        return (
            f"✅ 有 {len(ok)} 个地址能取到 → 说明这个媒体存在、你的网络也通。"
            f"能用的地址形状：{'、'.join(shapes)}"
        )
    if statuses == {404}:
        return (
            "❌ 所有写法都 404 → 这个媒体在 CDN 上确实不存在（推文被删/受限/被风控居多），"
            "不是你程序写错了地址。换几条其它推文试，如果别人也这样，就别在这条上耗了。"
        )
    if statuses & {401, 403}:
        return (
            "⚠ 出现 401/403 → 地址形态可能是对的，但访问被拒。"
            "带上登录 Cookie 再试（勾选「携带 Cookie 访问图片 CDN」），或检查代理节点是否被 X 拉黑。"
        )
    if statuses & set(RETRYABLE_STATUSES):
        return "⚠ 出现 429/5xx → 是限流或网络抖动，不是地址问题。降低并发、稍后重试即可。"
    return f"⚠ 结果不明确，状态码分布：{sorted(str(s) for s in statuses)}。把这份报告发我再看。"


def probe_tweet(
    controller: Optional[BrowserController],
    tweet_url: str,
    proxy: str = "",
    cookie_header: str = "",
    log: Optional[Callable[[str], None]] = None,
    report_path: str = "",
) -> Dict[str, Any]:
    """完整诊断一条推文：开页面 → 读真实媒体地址 → 逐个试候选 → 出结论。"""
    logger = log or Logger()
    started = time.time()
    result: Dict[str, Any] = {"tweet": tweet_url, "at": dt.datetime.now().isoformat(timespec="seconds"),
                             "media": [], "verdict": "", "page": {}}

    page_items: List[Dict[str, Any]] = []
    if controller is not None:
        try:
            logger(f"用程序自带浏览器打开：{tweet_url}")
            _target, session_id = controller.open_page(tweet_url)
            controller.wait_dom_ready(session_id, timeout=30.0)
            time.sleep(3.0)
            raw = controller.cdp.evaluate(session_id, _TWEET_MEDIA_JS, timeout=25.0)
            payload = json.loads(raw) if raw else {}
            result["page"] = {"url": payload.get("url", ""), "title": payload.get("title", "")}
            page_items = [item for item in (payload.get("items") or []) if item.get("src")]
            logger(f"页面读到 {len(page_items)} 个媒体地址（这些是浏览器已经渲染成功的）")
        except Exception as exc:  # noqa: BLE001
            logger(f"打开推文页失败：{type(exc).__name__}: {exc}（改为只做离线候选测试）")

    if not page_items:
        logger("页面没读到媒体；如果这条推文本来就有图，说明页面没加载出来（登录态/代理问题）")

    user_agent = getattr(controller, "user_agent", "") if controller else ""
    for item in page_items:
        src = str(item.get("src") or "")
        base = media_base_of(src)
        rows = probe_urls(src, proxy=proxy, user_agent=user_agent,
                          cookie_header=cookie_header, cookie_to_cdn=True)
        entry = {
            "kind": item.get("kind"),
            "rendered": src,
            "rendered_size": item.get("natural") or "",
            "base": base,
            "attempts": [{"url": row[0], "status": row[1], "note": row[2]} for row in rows],
            "verdict": verdict(rows),
        }
        result["media"].append(entry)
        logger(f"· {base}")
        for row in rows:
            logger(f"    [{row[1]}] {row[0]}  {row[2]}")
        logger(f"    结论：{entry['verdict']}")

    if result["media"]:
        result["verdict"] = result["media"][0]["verdict"]
    else:
        result["verdict"] = "没有测到任何媒体（页面没加载出来，或这条推文确实没有图片/视频）"
    result["elapsed"] = round(time.time() - started, 1)

    if report_path:
        try:
            with open(report_path, "w", encoding="utf-8") as fh:
                fh.write(_render_report(result))
            logger(f"诊断报告已写入：{report_path}")
        except OSError as exc:
            logger(f"诊断报告写入失败：{exc}")
    return result


def _render_report(result: Dict[str, Any]) -> str:
    lines = [
        "X 媒体下载器 · 媒体地址诊断报告",
        f"时间：{result.get('at')}",
        f"推文：{result.get('tweet')}",
        f"页面：{result.get('page', {}).get('title', '')} {result.get('page', {}).get('url', '')}",
        "",
    ]
    for entry in result.get("media", []):
        lines.append(f"■ 媒体 {entry.get('base')}")
        lines.append(f"  浏览器渲染地址：{entry.get('rendered')}  显示尺寸 {entry.get('rendered_size')}")
        for row in entry.get("attempts", []):
            lines.append(f"    [{row.get('status')}] {row.get('url')}   {row.get('note')}")
        lines.append(f"  结论：{entry.get('verdict')}")
        lines.append("")
    lines.append(f"总体结论：{result.get('verdict')}")
    lines.append(f"用时：{result.get('elapsed')}s")
    return "\n".join(lines) + "\n"

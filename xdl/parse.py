# -*- coding: utf-8 -*-
"""解析层：把 X 搜索接口返回的 JSON 拆成「推文 → 媒体」条目。

X 的字段命名这两年在变，所以这里走"结构化路径优先 + 深度兜底"两条腿：
  1) 先按 SearchTimeline 的已知结构取 tweet 节点；
  2) 结构对不上时，深度遍历找任何"长得像推文"的节点（有 created_at + 媒体/ID）。
"""
from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import parse_qs

from .util import parse_tweet_time, time_from_snowflake, to_local_date

# 图片分辨率档位（从大到小）——orig 是最原始图，small 是页面缩略图（基本一定存在）
PHOTO_SIZE_FALLBACKS = ("orig", "4096x4096", "large", "medium", "small")
_PHOTO_EXTS = ("jpg", "jpeg", "png", "webp", "gif")


@dataclass
class MediaItem:
    kind: str               # photo | video | gif
    url: str
    tweet_id: str = ""
    username: str = ""
    created_at: Optional[dt.datetime] = None
    tweet_url: str = ""
    index: int = 1
    text: str = ""
    is_retweet: bool = False
    fallback_urls: List[str] = field(default_factory=list)

    @property
    def is_video(self) -> bool:
        return self.kind in ("video", "gif")


@dataclass
class Tweet:
    tweet_id: str
    username: str = ""
    created_at: Optional[dt.datetime] = None
    text: str = ""
    is_retweet: bool = False
    media: List[MediaItem] = field(default_factory=list)


# ------------------------------------------------------------------ 工具
def _unwrap(node: Any) -> Any:
    """剥掉 TweetWithVisibilityResults 等包装层。"""
    if isinstance(node, dict):
        if node.get("__typename") == "TweetWithVisibilityResults" and isinstance(node.get("tweet"), dict):
            return node["tweet"]
        if isinstance(node.get("tweet"), dict) and "legacy" not in node and "rest_id" not in node:
            inner = node["tweet"]
            if isinstance(inner.get("legacy"), dict) or isinstance(inner.get("core"), dict):
                return inner
    return node


def _legacy(node: Any) -> Dict[str, Any]:
    if isinstance(node, dict) and isinstance(node.get("legacy"), dict):
        return node["legacy"]
    return {}


def _looks_like_tweet(node: Any) -> bool:
    if not isinstance(node, dict):
        return False
    legacy = node.get("legacy")
    if not isinstance(legacy, dict):
        return False
    if "created_at" not in legacy and "id_str" not in legacy:
        return False
    return any(k in legacy for k in ("full_text", "extended_entities", "entities", "conversation_id_str"))


def _walk(node: Any) -> Iterable[Dict[str, Any]]:
    """深度遍历，产出所有看起来像推文的节点。"""
    if isinstance(node, dict):
        if _looks_like_tweet(node):
            yield node
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def _username_of(tweet: Dict[str, Any]) -> str:
    core = tweet.get("core")
    if isinstance(core, dict):
        user = core.get("user_results", {}).get("result") if isinstance(core.get("user_results"), dict) else None
        if isinstance(user, dict):
            legacy = _legacy(user)
            if legacy.get("screen_name"):
                return str(legacy["screen_name"])
            if isinstance(user.get("core"), dict) and user["core"].get("screen_name"):
                return str(user["core"]["screen_name"])
    legacy = _legacy(tweet)
    user = legacy.get("user")
    if isinstance(user, dict) and user.get("screen_name"):
        return str(user["screen_name"])
    return ""


def _tweet_time(tweet: Dict[str, Any]) -> Optional[dt.datetime]:
    legacy = _legacy(tweet)
    moment = parse_tweet_time(legacy.get("created_at"))
    if moment:
        return moment
    for key in ("rest_id", "id_str"):
        ident = tweet.get(key) or legacy.get(key)
        if ident:
            inferred = time_from_snowflake(ident)
            if inferred:
                return inferred
    return None


def _tweet_text(tweet: Dict[str, Any]) -> str:
    note = tweet.get("note_tweet")
    if isinstance(note, dict):
        inner = note.get("note_tweet_results", {}).get("result")
        if isinstance(inner, dict) and inner.get("text"):
            return str(inner["text"])
    return str(_legacy(tweet).get("full_text") or "")


def photo_base_of(url: str) -> str:
    """把图片地址削成 `/media/<id>` 这种不带后缀、不带参数的基址。"""
    return str(url or "").split("?")[0]


def _photo_parts(media_url: str) -> tuple:
    """拆出 (stem, 原始格式)。stem 是不带扩展名的基址，格式取自扩展名或 ?format=。"""
    raw = str(media_url or "").strip()
    base = raw.split("?")[0]
    stem, fmt = base, ""
    tail = base.rsplit(".", 1)[-1].lower() if "." in base else ""
    if tail in _PHOTO_EXTS:
        stem = base.rsplit(".", 1)[0]
        fmt = "jpg" if tail == "jpeg" else tail
    query = parse_qs(raw.split("?", 1)[1]) if "?" in raw else {}
    if not fmt:
        value = (query.get("format") or [""])[0].strip().lower()
        if value:
            fmt = "jpg" if value == "jpeg" else value
    return stem, (fmt or "jpg")


def photo_candidate_urls(media_url_https: str) -> List[str]:
    """生成图片的完整候选地址链（解析层只负责"给全"，排序和剪枝在下载层做）。

    这条链覆盖 X 图片 CDN 上实际存在的几种写法，避免"只试 5 个猜出来的形状"导致 404：
      ① `?format=<原格式>&name=<档位>`    ← 现在最标准的写法
      ② `?format=jpg&name=<档位>`         ← 原图不是 jpg 时的兜底
      ③ `<id>.jpg?name=<档位>`            ← 老写法 / 第三方工具常用
      ④ `?name=<档位>`                     ← 不带 format 的写法
      ⑤ 原样地址（含页面渲染用的参数，浏览器已经成功加载过它，一定能取到）
    档位从 orig 一路降到 small，最后还有 small 兜底，尽量不退化成零字节失败。
    """
    raw = str(media_url_https or "").strip()
    if not raw:
        return []
    stem, fmt = _photo_parts(raw)
    formats: List[str] = []
    for candidate in (fmt, "jpg"):
        if candidate and candidate not in formats:
            formats.append(candidate)

    out: List[str] = []

    def add(url: str) -> None:
        if url and url not in out:
            out.append(url)

    for name in PHOTO_SIZE_FALLBACKS:
        for form in formats:
            add(f"{stem}?format={form}&name={name}")
    for name in PHOTO_SIZE_FALLBACKS:
        add(f"{stem}.jpg?name={name}")
    for name in PHOTO_SIZE_FALLBACKS:
        add(f"{stem}?name={name}")
    add(raw)
    return out


def _photo_urls(media: Dict[str, Any]) -> List[str]:
    """从接口的 media 节点算出候选地址链（第一个是首选，其余是回退）。"""
    source = media.get("media_url_https") or media.get("media_url") or ""
    return photo_candidate_urls(source)


def _video_url(media: Dict[str, Any]) -> Optional[str]:
    info = media.get("video_info") or {}
    variants = info.get("variants") or []
    best, best_rate = None, -1
    for variant in variants:
        url = variant.get("url")
        content_type = variant.get("content_type") or ""
        if not url:
            continue
        if content_type == "video/mp4" or ".mp4" in url:
            rate = int(variant.get("bitrate") or 0)
            if rate > best_rate:
                best, best_rate = url, rate
    if best:
        return best
    # 只剩 m3u8 的情况（交给下载层的 HLS 兜底）
    for variant in variants:
        url = variant.get("url") or ""
        if ".m3u8" in url:
            return url
    return None


def tweet_from_node(node: Any) -> Optional[Tweet]:
    """把一个 tweet 节点（含转推展开）转成 Tweet。"""
    node = _unwrap(node)
    if not isinstance(node, dict):
        return None
    legacy = _legacy(node)
    tweet_id = str(node.get("rest_id") or legacy.get("id_str") or "")
    if not tweet_id:
        return None

    is_retweet = bool(legacy.get("retweeted_status_id_str"))
    target = node
    if is_retweet:
        rt = legacy.get("retweeted_status_result")
        inner = rt.get("result") if isinstance(rt, dict) else None
        if inner:
            target = _unwrap(inner)

    target_legacy = _legacy(target)
    tweet = Tweet(
        tweet_id=str(target.get("rest_id") or target_legacy.get("id_str") or tweet_id),
        username=_username_of(target) or _username_of(node),
        created_at=_tweet_time(target) or _tweet_time(node),
        text=_tweet_text(target),
        is_retweet=is_retweet,
    )
    tweet.media = media_from_node(target, tweet)
    if not tweet.media and target is not node:
        tweet.media = media_from_node(node, tweet)
    return tweet


def media_from_node(node: Any, tweet: Tweet) -> List[MediaItem]:
    """从 tweet 节点里抽出图片/视频列表。"""
    node = _unwrap(node)
    legacy = _legacy(node)
    entities = legacy.get("extended_entities") or legacy.get("entities") or {}
    raw_media = entities.get("media") if isinstance(entities, dict) else None
    if not isinstance(raw_media, list):
        return []

    items: List[MediaItem] = []
    for position, media in enumerate(raw_media, start=1):
        if not isinstance(media, dict):
            continue
        kind = media.get("type")
        if kind == "photo":
            urls = _photo_urls(media)
            if urls:
                items.append(
                    MediaItem(
                        kind="photo",
                        url=urls[0],
                        fallback_urls=urls[1:],
                        tweet_id=tweet.tweet_id,
                        username=tweet.username,
                        created_at=tweet.created_at,
                        tweet_url=tweet_url(tweet.username, tweet.tweet_id),
                        index=position,
                        text=tweet.text,
                        is_retweet=tweet.is_retweet,
                    )
                )
        elif kind in ("video", "animated_gif"):
            url = _video_url(media)
            if url:
                items.append(
                    MediaItem(
                        kind="gif" if kind == "animated_gif" else "video",
                        url=url,
                        tweet_id=tweet.tweet_id,
                        username=tweet.username,
                        created_at=tweet.created_at,
                        tweet_url=tweet_url(tweet.username, tweet.tweet_id),
                        index=position,
                        text=tweet.text,
                        is_retweet=tweet.is_retweet,
                    )
                )
    return items


def tweet_url(username: str, tweet_id: str) -> str:
    if username:
        return f"https://x.com/{username}/status/{tweet_id}"
    return f"https://x.com/i/web/status/{tweet_id}"


# ------------------------------------------------------------------ 响应解析
def parse_search_response(payload: Any) -> List[Tweet]:
    """解析一次 SearchTimeline 响应，返回推文列表。"""
    tweets: List[Tweet] = []
    seen = set()

    def add(node: Any) -> None:
        tweet = tweet_from_node(node)
        if tweet and tweet.tweet_id not in seen:
            seen.add(tweet.tweet_id)
            tweets.append(tweet)

    # 1) 结构化路径
    try:
        instructions = payload["data"]["search_by_raw_query"]["search_timeline"]["timeline"]["instructions"]
    except (KeyError, TypeError):
        instructions = None
    if isinstance(instructions, list):
        for instruction in instructions:
            if not isinstance(instruction, dict):
                continue
            entries = instruction.get("entries")
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                content = entry.get("content") or {}
                item = content.get("itemContent") or {}
                if not isinstance(item, dict):
                    continue
                result = item.get("tweet_results", {}).get("result") if isinstance(item.get("tweet_results"), dict) else None
                if result:
                    add(result)
                # 模块化条目（对话/相关推文）
                for module_item in (item.get("items") or []):
                    res = ((module_item or {}).get("item") or {}).get("itemContent", {}).get("tweet_results", {}).get("result")
                    if res:
                        add(res)

    # 2) 深度兜底
    if not tweets:
        for node in _walk(payload):
            add(node)
    else:
        # 结构化拿到了，但可能漏掉模块条目 → 补齐
        for node in _walk(payload):
            if len(tweets) > 0 and len(seen) > 4000:
                break
            add(node)

    return tweets


def iter_media(tweets: Iterable[Tweet], want_photo: bool = True, want_video: bool = True,
               want_gif: bool = True, include_retweet: bool = True) -> Iterable[MediaItem]:
    for tweet in tweets:
        if tweet.is_retweet and not include_retweet:
            continue
        for item in tweet.media:
            if item.kind == "photo" and not want_photo:
                continue
            if item.kind == "video" and not want_video:
                continue
            if item.kind == "gif" and not want_gif:
                continue
            yield item


def filter_by_date(items: Iterable[MediaItem], start: Optional[dt.date], end: Optional[dt.date],
                   tz_mode: str = "local") -> List[MediaItem]:
    result = []
    for item in items:
        day = to_local_date(item.created_at, tz_mode)
        if day is None:
            result.append(item)          # 时间未知：保留，交给用户判断
            continue
        if start and day < start:
            continue
        if end and day > end:
            continue
        result.append(item)
    return result


_FILTER_WORDS = re.compile(r"(since:|until:|filter:)", re.I)


def build_query(keyword: str, start: Optional[dt.date], end: Optional[dt.date],
                self_date_filter: bool = True) -> str:
    """把关键词和日期拼成 X 搜索语句（程序会自动补 since/until）。"""
    parts = [keyword.strip()]
    if self_date_filter:
        if start and not re.search(r"since:", keyword, re.I):
            parts.append(f"since:{start:%Y-%m-%d}")
        if end and not re.search(r"until:", keyword, re.I):
            parts.append(f"until:{(end + dt.timedelta(days=1)):%Y-%m-%d}")   # until 不含当天
    return " ".join(p for p in parts if p)


SEARCH_URL_TEMPLATE = "https://x.com/search?q={query}&src=typed_query&f={tab}"


def search_url(query: str, tab: str = "live") -> str:
    from urllib.parse import quote

    return SEARCH_URL_TEMPLATE.format(query=quote(query, safe=""), tab=tab or "live")

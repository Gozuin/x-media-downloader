# -*- coding: utf-8 -*-
"""抓取结果缓存：把「搜索结果 → 推文/媒体」落盘，同一个搜索词下次直接复用。

为什么这对"账号被封"很关键：
  · 搜索接口（SearchTimeline）要带账号 Cookie，是**唯一**会牵动账号安全的部分；
  · 图片/视频下载走的是 pbs.twimg.com / video.twimg.com，跟账号无关。
把抓取结果缓存下来，就能做到「搜一次 → 反复下载 / 换目录重下 / 只补图片」，
整个过程完全不碰账号。
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

from .parse import MediaItem, Tweet
from .util import Logger, ensure_dir

CACHE_DIR_NAME = os.path.join("_meta", "harvest_cache")
DEFAULT_TTL_HOURS = 72.0


def cache_key(query: str, tab: str) -> str:
    return hashlib.sha1(f"{tab}|{query}".encode("utf-8")).hexdigest()[:16]


def cache_path_for(out_dir: str, query: str, tab: str) -> str:
    return os.path.join(out_dir, CACHE_DIR_NAME, cache_key(query, tab) + ".json")


def _iso(value: Optional[dt.datetime]) -> str:
    return value.isoformat() if value else ""


def _parse_iso(value: Any) -> Optional[dt.datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        return dt.datetime.fromisoformat(value)
    except ValueError:
        return None


def _media_to_dict(item: MediaItem) -> Dict[str, Any]:
    return {
        "kind": item.kind,
        "url": item.url,
        "fallback_urls": list(item.fallback_urls or []),
        "tweet_id": item.tweet_id,
        "username": item.username,
        "created_at": _iso(item.created_at),
        "tweet_url": item.tweet_url,
        "index": item.index,
        "text": item.text,
        "is_retweet": item.is_retweet,
    }


def _media_from_dict(raw: Dict[str, Any]) -> MediaItem:
    return MediaItem(
        kind=str(raw.get("kind") or "photo"),
        url=str(raw.get("url") or ""),
        fallback_urls=[str(u) for u in (raw.get("fallback_urls") or [])],
        tweet_id=str(raw.get("tweet_id") or ""),
        username=str(raw.get("username") or ""),
        created_at=_parse_iso(raw.get("created_at")),
        tweet_url=str(raw.get("tweet_url") or ""),
        index=int(raw.get("index") or 1),
        text=str(raw.get("text") or ""),
        is_retweet=bool(raw.get("is_retweet")),
    )


def _tweet_to_dict(tweet: Tweet) -> Dict[str, Any]:
    return {
        "tweet_id": tweet.tweet_id,
        "username": tweet.username,
        "created_at": _iso(tweet.created_at),
        "text": tweet.text,
        "is_retweet": tweet.is_retweet,
        "media": [_media_to_dict(m) for m in tweet.media],
    }


def _tweet_from_dict(raw: Dict[str, Any]) -> Tweet:
    tweet = Tweet(
        tweet_id=str(raw.get("tweet_id") or ""),
        username=str(raw.get("username") or ""),
        created_at=_parse_iso(raw.get("created_at")),
        text=str(raw.get("text") or ""),
        is_retweet=bool(raw.get("is_retweet")),
    )
    tweet.media = [_media_from_dict(m) for m in (raw.get("media") or []) if isinstance(m, dict)]
    return tweet


def save(out_dir: str, query: str, tab: str, tweets: List[Tweet],
         dom_items: List[MediaItem], log: Optional[Callable[[str], None]] = None) -> Optional[str]:
    """把这一轮的抓取结果写进缓存（失败也不影响主流程）。"""
    logger = log or Logger()
    path = cache_path_for(out_dir, query, tab)
    payload = {
        "query": query,
        "tab": tab,
        "at": dt.datetime.now().isoformat(timespec="seconds"),
        "tweets": [_tweet_to_dict(t) for t in tweets],
        "dom": [_media_to_dict(m) for m in dom_items],
    }
    try:
        ensure_dir(os.path.dirname(path))
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)
        os.replace(tmp, path)
        return path
    except (OSError, TypeError) as exc:
        logger(f"抓取结果缓存写入失败（不影响下载）：{type(exc).__name__}: {exc}")
        return None


def load(out_dir: str, query: str, tab: str, ttl_hours: float = DEFAULT_TTL_HOURS,
         log: Optional[Callable[[str], None]] = None) -> Optional[Tuple[List[Tweet], List[MediaItem]]]:
    """读缓存；没有、过期或损坏都返回 None（调用方照常去抓）。"""
    logger = log or Logger()
    path = cache_path_for(out_dir, query, tab)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            payload = json.load(fh)
    except (OSError, ValueError):
        logger("抓取缓存损坏，已忽略并重新抓取")
        return None
    if ttl_hours <= 0:
        # TTL <= 0 表示“不要复用缓存”，直接当过期处理（否则会永远复用旧结果）
        logger("缓存复用已关闭（TTL<=0），本轮重新抓取")
        return None
    saved_at = _parse_iso(payload.get("at"))
    if saved_at is not None:
        age_hours = (dt.datetime.now() - saved_at).total_seconds() / 3600.0
        if age_hours > ttl_hours:
            logger(f"抓取缓存已过期（{age_hours:.1f} 小时前，上限 {ttl_hours:g} 小时），重新抓取")
            return None
    else:
        age_hours = 0.0
    tweets = [_tweet_from_dict(t) for t in (payload.get("tweets") or []) if isinstance(t, dict)]
    dom_items = [_media_from_dict(m) for m in (payload.get("dom") or []) if isinstance(m, dict)]
    if not tweets and not dom_items:
        return None
    logger(
        f"♻ 复用 {age_hours:.1f} 小时前的抓取结果（{len(tweets)} 条推文 / DOM {len(dom_items)} 个媒体），"
        f"本轮不请求搜索接口 —— 对账号零风险"
    )
    return tweets, dom_items

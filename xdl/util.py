# -*- coding: utf-8 -*-
"""通用工具：时间解析、时区换算、文件名清洗、日志、配置读写。"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
from typing import Any, Callable, Dict, Optional

# 推文 ID（雪花）起始毫秒，用于反推发布时间
SNOWFLAKE_EPOCH_MS = 1288834974657

# 推文 created_at 的经典格式：Wed Oct 10 20:19:24 +0000 2018
_TW_TIME_FMT = "%a %b %d %H:%M:%S %z %Y"
_ISO_FMTS = ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z")

_BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t\x00-\x1f]+')


# ---------------------------------------------------------------- 时间处理
def parse_tweet_time(value: Any) -> Optional[dt.datetime]:
    """解析推文的 created_at / DOM 的 datetime 属性，返回 UTC 时间。"""
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    for fmt in (_TW_TIME_FMT, *_ISO_FMTS):
        try:
            parsed = dt.datetime.strptime(text, fmt)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt.timezone.utc)
        return parsed.astimezone(dt.timezone.utc)
    return None


def time_from_snowflake(tweet_id: Any) -> Optional[dt.datetime]:
    """由推文 ID 反推发布时间（雪花 ID 高位含毫秒时间戳）。"""
    try:
        tid = int(str(tweet_id))
    except (TypeError, ValueError):
        return None
    if tid <= 0:
        return None
    ms = (tid >> 22) + SNOWFLAKE_EPOCH_MS
    try:
        return dt.datetime.fromtimestamp(ms / 1000.0, dt.timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def to_local_date(moment: Optional[dt.datetime], tz_mode: str = "local") -> Optional[dt.date]:
    """把 UTC 时间换算成"用于分文件夹的日期"。tz_mode: local | utc。"""
    if moment is None:
        return None
    if tz_mode == "utc":
        return moment.astimezone(dt.timezone.utc).date()
    return moment.astimezone().date()


def parse_ymd(text: str) -> Optional[dt.date]:
    """宽松解析用户输入的日期。"""
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y%m%d", "%Y-%m", "%Y%m"):
        try:
            return dt.datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def ymd(d: Optional[dt.date]) -> str:
    return d.strftime("%Y-%m-%d") if d else ""


# ---------------------------------------------------------------- 文本/文件
def sanitize(name: str, maxlen: int = 64) -> str:
    """清洗成合法文件名片段。"""
    cleaned = _BAD_CHARS.sub("_", (name or "").strip())
    cleaned = cleaned.strip(". ")
    return (cleaned or "unknown")[:maxlen]


def human_size(num: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(num) < 1024.0 or unit == "TB":
            return f"{num:.0f}{unit}" if unit == "B" else f"{num:.1f}{unit}"
        num /= 1024.0
    return f"{num:.1f}TB"


def ensure_dir(path: str) -> str:
    os.makedirs(path, exist_ok=True)
    return path


def unique_path(path: str) -> str:
    """若目标文件已存在，追加 _2、_3 ..."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    idx = 2
    while os.path.exists(f"{stem}_{idx}{ext}"):
        idx += 1
    return f"{stem}_{idx}{ext}"


def now_str() -> str:
    return dt.datetime.now().strftime("%H:%M:%S")


# ---------------------------------------------------------------- 日志
class Logger:
    """把日志送进 GUI 回调；回调签名 sink(text)。"""

    def __init__(self, sink: Optional[Callable[[str], None]] = None):
        self._sink = sink

    def set_sink(self, sink: Optional[Callable[[str], None]]) -> None:
        self._sink = sink

    def __call__(self, message: str) -> None:
        line = f"[{now_str()}] {message}"
        if self._sink:
            try:
                self._sink(line)
                return
            except Exception:  # GUI 出问题也不能让后台线程崩
                pass
        try:
            if sys.stdout:              # 打包成无控制台 exe 时 stdout 为 None
                print(line, flush=True)
        except Exception:
            pass


# ---------------------------------------------------------------- 配置读写
def load_json(path: str, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            base = dict(default or {})
            base.update(data)
            return base
    except (OSError, ValueError):
        pass
    return dict(default or {})


def save_json(path: str, data: Dict[str, Any]) -> None:
    try:
        ensure_dir(os.path.dirname(os.path.abspath(path)) or ".")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
    except OSError:
        pass

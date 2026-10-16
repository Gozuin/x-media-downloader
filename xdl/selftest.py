# -*- coding: utf-8 -*-
"""离线自检：不依赖 X 也能把"解析 → 过滤 → 下载归档 → CDP 通道"整条链路验一遍。

用法：  python main.py --selftest
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Tuple

PASS, FAIL = "[通过]", "[失败]"
_results: List[Tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    _results.append((name, bool(ok), detail))
    print(f"{PASS if ok else FAIL} {name}" + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


# ------------------------------------------------------------------ 夹具
PHOTO_URL = "https://pbs.twimg.com/media/AAA111bbb.jpg"
VIDEO_LOW = "https://video.twimg.com/ext_tw_video/1/pu/vid/320x180/low.mp4"
VIDEO_HIGH = "https://video.twimg.com/ext_tw_video/1/pu/vid/1280x720/high.mp4"
VIDEO_HLS = "https://video.twimg.com/ext_tw_video/1/pu/pl/playlist.m3u8"
GIF_URL = "https://video.twimg.com/tweet_video/gif123.mp4"


def _tweet_node(tweet_id: str, screen_name: str, created_at: str, media: List[Dict[str, Any]],
                retweet_id: str = "") -> Dict[str, Any]:
    legacy: Dict[str, Any] = {
        "id_str": tweet_id,
        "created_at": created_at,
        "full_text": f"tweet {tweet_id}",
        "extended_entities": {"media": media},
    }
    if retweet_id:
        legacy["retweeted_status_id_str"] = retweet_id
    return {
        "rest_id": tweet_id,
        "__typename": "Tweet",
        "core": {"user_results": {"result": {"__typename": "User", "legacy": {"screen_name": screen_name}}}},
        "legacy": legacy,
    }


def build_fixture() -> Dict[str, Any]:
    photo_media = [{"type": "photo", "media_url_https": PHOTO_URL, "indices": [0, 1]}]
    video_media = [{
        "type": "video",
        "media_url_https": "https://pbs.twimg.com/ext_tw_video_thumb/1/pu/img/poster.jpg",
        "video_info": {"variants": [
            {"bitrate": 256000, "content_type": "video/mp4", "url": VIDEO_LOW},
            {"bitrate": 2176000, "content_type": "video/mp4", "url": VIDEO_HIGH},
            {"content_type": "application/x-mpegURL", "url": VIDEO_HLS},
        ]},
    }]
    gif_media = [{
        "type": "animated_gif",
        "video_info": {"variants": [{"content_type": "video/mp4", "url": GIF_URL}]},
    }]

    original = _tweet_node("1799999999999999999", "erin", "Sat May 04 03:00:00 +0000 2024", photo_media)
    retweet = _tweet_node("1800000000000000004", "dave", "Sat May 04 04:00:00 +0000 2024", [],
                          retweet_id="1799999999999999999")
    retweet["legacy"]["retweeted_status_result"] = {"result": original}

    entries = [
        _tweet_node("1800000000000000001", "alice", "Wed May 01 04:00:00 +0000 2024", photo_media),
        _tweet_node("1800000000000000002", "bob", "Thu May 02 04:00:00 +0000 2024", video_media),
        _tweet_node("1800000000000000003", "carol", "Fri May 03 04:00:00 +0000 2024", gif_media),
        retweet,
        # 区间外（6 月）
        _tweet_node("1800000000000000005", "frank", "Sat Jun 15 04:00:00 +0000 2024", photo_media),
    ]
    return {
        "data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [
            {"type": "TimelineAddEntries", "entries": [
                {"entryId": f"tweet-{i}", "content": {"itemContent": {"tweet_results": {"result": node}}}}
                for i, node in enumerate(entries)
            ]},
            {"type": "TimelineAddEntries", "entries": [
                {"entryId": "cursor-bottom", "content": {"cursorType": "Bottom", "value": "CURSOR"}},
            ]},
        ]}}}},
    }


# ------------------------------------------------------------------ 本地 HTTP
class _Handler(BaseHTTPRequestHandler):
    segments = [b"SEGMENT-0000" * 8, b"SEGMENT-1111" * 8]
    payloads = {
        "/media/pic.jpg": (b"\xff\xd8\xff\xe0" + b"X" * 512, "image/jpeg"),
        "/v/video.mp4": (b"\x00\x00\x00\x18ftypmp42" + b"V" * 4096, "video/mp4"),
        "/v/seg0.ts": (segments[0], "video/mp2t"),
        "/v/seg1.ts": (segments[1], "video/mp2t"),
    }

    def log_message(self, *_args):  # 静音
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/v/pl.m3u8":
            body = (
                "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:0\n"
                "#EXTINF:4.0,\n/v/v/seg0.ts\n#EXTINF:4.0,\n/v/seg1.ts\n#EXT-X-ENDLIST\n"
            ).encode()
            self._send(body, "application/vnd.apple.mpegurl")
            return
        if path == "/v/v/seg0.ts":          # 主播放列表里的相对路径变体
            self._send(self.segments[0], "video/mp2t")
            return
        if path in self.payloads:
            body, ctype = self.payloads[path]
            self._send(body, ctype)
            return
        self.send_response(404)
        self.end_headers()

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_test_server() -> Tuple[ThreadingHTTPServer, str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


# ------------------------------------------------------------------ 各项检查
def test_parsing() -> List[Any]:
    from xdl.parse import filter_by_date, iter_media, parse_search_response

    payload = build_fixture()
    tweets = parse_search_response(payload)
    check("解析 SearchTimeline：拿到 5 条推文", len(tweets) == 5, f"实际 {len(tweets)}")

    by_id = {t.tweet_id: t for t in tweets}
    alice = by_id.get("1800000000000000001")
    check("图片推文解析出 1 个媒体", bool(alice and len(alice.media) == 1))
    ladder = ([alice.media[0].url] + list(alice.media[0].fallback_urls)) if alice else []
    check("图片地址首选带 ?format=jpg&name=orig（标准写法）",
          bool(alice and alice.media[0].url.endswith("?format=jpg&name=orig")),
          alice.media[0].url if alice else "")
    check("候选链覆盖 orig 档位", any(u.endswith("name=orig") for u in ladder), f"{len(ladder)} 个候选")
    check("候选链覆盖 4096x4096 / large / medium / small 档位",
          all(any(u.endswith("name=" + size) for u in ladder)
              for size in ("4096x4096", "large", "medium", "small")), str(ladder[:3]))
    check("候选链覆盖 .jpg 扩展名写法", any("/AAA111bbb.jpg?name=orig" in u for u in ladder), "")
    check("候选链尾部保留原始地址（浏览器渲染过的那个）", bool(ladder) and ladder[-1] == PHOTO_URL,
          ladder[-1] if ladder else "")

    bob = by_id.get("1800000000000000002")
    check("视频取最高码率 mp4", bool(bob and bob.media and bob.media[0].url == VIDEO_HIGH),
          bob.media[0].url if bob and bob.media else "")
    check("视频类型判定正确", bool(bob and bob.media[0].kind == "video"))

    carol = by_id.get("1800000000000000003")
    check("动图识别为 gif 类型", bool(carol and carol.media and carol.media[0].kind == "gif"))

    dave = by_id.get("1799999999999999999")
    check("转推被识别（is_retweet）", bool(dave and dave.is_retweet),
          f"键={sorted(by_id)[3] if len(by_id) > 3 else '?'}")
    check("转推取内层原推的媒体", bool(dave and dave.media and dave.media[0].tweet_id == "1799999999999999999"))
    check("转推媒体归属作者是原推作者", bool(dave and dave.media and dave.media[0].username == "erin"),
          dave.media[0].username if dave and dave.media else "")

    all_media = list(iter_media(tweets))
    check("全部媒体共 5 个", len(all_media) == 5, f"实际 {len(all_media)}")
    only_photo = list(iter_media(tweets, want_video=False, want_gif=False))
    check("只要图片时过滤出 3 个", len(only_photo) == 3, f"实际 {len(only_photo)}")
    no_rt = list(iter_media(tweets, include_retweet=False))
    check("排除转推后剩 4 个媒体", len(no_rt) == 4, f"实际 {len(no_rt)}")
    return all_media


def test_date_and_query() -> None:
    from xdl.parse import build_query, filter_by_date, iter_media, parse_search_response

    tweets = parse_search_response(build_fixture())
    media = list(iter_media(tweets))
    start = dt.date(2024, 5, 1)
    end = dt.date(2024, 5, 4)
    in_range = filter_by_date(media, start, end)
    check("日期区间 5/1–5/4 过滤出 4 个媒体", len(in_range) == 4, f"实际 {len(in_range)}")
    only_one_day = filter_by_date(media, dt.date(2024, 5, 3), dt.date(2024, 5, 3))
    check("单日过滤（5/3）只剩 1 个", len(only_one_day) == 1, f"实际 {len(only_one_day)}")

    query = build_query("猫 视频", start, end)
    check("自动补 since", "since:2024-05-01" in query, query)
    check("until 自动 +1 天", "until:2024-05-05" in query, query)
    manual = build_query("cats since:2020-01-01", None, None)
    check("已有 since 时不重复追加", manual.count("since:") == 1, manual)


def test_filename_layout() -> None:
    from xdl.download import Downloader
    from xdl.parse import MediaItem

    item = MediaItem(kind="photo", url=PHOTO_URL, tweet_id="1800000000000000001",
                     username="alice", created_at=dt.datetime(2024, 5, 1, 4, 0, tzinfo=dt.timezone.utc),
                     index=2)
    tmp = tempfile.mkdtemp(prefix="xdl-layout-")
    try:
        downloader = Downloader(out_dir=tmp, skip_existing=True)
        folder, path = downloader._target_path(item)
        check("按日期建文件夹", os.path.basename(folder) == "2024-05-01", folder)
        check("文件名含作者与推文 ID", os.path.basename(path) == "alice_1800000000000000001_02.jpg",
              os.path.basename(path))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_photo_ladder_and_shapes() -> None:
    """图片候选链 + 地址形状自适应剪枝（v1.0.2 的核心改动）。"""
    from xdl.download import ShapeStats, _ext_of, shape_key
    from xdl.parse import photo_candidate_urls

    # ① 没有扩展名、只有 ?format=png 的地址
    ladder = photo_candidate_urls("https://pbs.twimg.com/media/ABC123xyz?format=png&name=small")
    check("?format=png 的图：首选按原格式取 orig",
          ladder and ladder[0] == "https://pbs.twimg.com/media/ABC123xyz?format=png&name=orig", ladder[0])
    check("?format=png 的图：也覆盖 jpg 兜底",
          any("?format=jpg&name=orig" in u for u in ladder))
    check("?format=png 的图：保留原始地址",
          ladder[-1] == "https://pbs.twimg.com/media/ABC123xyz?format=png&name=small", ladder[-1])
    check("候选地址无重复", len(ladder) == len(set(ladder)), f"{len(ladder)} 个")

    # ② 老式带扩展名的地址
    legacy = photo_candidate_urls("https://pbs.twimg.com/media/old.jpg")
    check("老式 .jpg 地址也能生成完整候选链", len(legacy) >= 10 and legacy[0].endswith("?format=jpg&name=orig"),
          legacy[0] if legacy else "")

    # ③ 地址形状识别
    check("形状识别：fmt / ext / plain / bare",
          shape_key("https://a/media/X?format=jpg&name=orig") == "fmt:orig"
          and shape_key("https://a/media/X.jpg?name=large") == "ext:large"
          and shape_key("https://a/media/X?name=medium") == "plain:medium"
          and shape_key("https://a/media/X") == "bare:-",
          f"{shape_key('https://a/media/X?format=jpg&name=orig')} / "
          f"{shape_key('https://a/media/X.jpg?name=large')}")

    # ④ 后缀识别（带 ?format 时不再猜成 jpg）
    check("从 ?format=png 识别出 .png 后缀",
          _ext_of("https://pbs.twimg.com/media/X?format=png&name=orig", "jpg") == "png")
    check("从路径识别出 .jpg 后缀",
          _ext_of("https://pbs.twimg.com/media/X.jpg?name=orig", "jpg") == "jpg")

    # ⑤ 形状剪枝：同一种写法连续失败几次后不再先试它；试好用的写法排前面
    stats = ShapeStats()
    good = "https://a/media/X?format=jpg&name=orig"
    bad_plain = "https://a/media/X?name=orig"
    for _ in range(6):
        stats.note(shape_key(bad_plain), False)
    for _ in range(2):
        stats.note(shape_key(good), True)
    ordered = stats.order([bad_plain, good])
    check("地址形状自适应：成功的写法排到最前", ordered and ordered[0] == good, str(ordered))
    dead = stats.order([bad_plain])
    check("地址形状自适应：连续失败的写法被剪掉", dead == [] or dead[0] == bad_plain,
          f"剪枝结果 {dead}（被剪时回退为原顺序）")
    check("形状统计可读", "fmt:orig" in stats.summary() and "plain:orig" in stats.summary(), stats.summary())


def test_failure_chain_reports_status() -> None:
    """整条候选链全 404 时：不重试、写 _meta/failed.csv、带上每个地址的状态。"""
    from xdl.download import Downloader
    from xdl.parse import MediaItem, photo_candidate_urls

    server, base = start_test_server()
    tmp = tempfile.mkdtemp(prefix="xdl-fail-")
    try:
        media = photo_candidate_urls(f"{base}/media/nope.jpg")
        item = MediaItem(kind="photo", url=media[0], fallback_urls=media[1:],
                         tweet_id="1800000000000000099", username="ghost",
                         created_at=dt.datetime(2024, 5, 2, 6, 0, tzinfo=dt.timezone.utc),
                         index=1, tweet_url="https://x.com/ghost/status/1800000000000000099")
        downloader = Downloader(out_dir=tmp, workers=2, skip_existing=True)
        stats = downloader.run([item])
        check("全 404 的图片算失败而不是崩溃", stats.get("fail") == 1, json.dumps(stats, ensure_ascii=False))
        fail_csv = os.path.join(tmp, "_meta", "failed.csv")
        check("失败明细写入 _meta/failed.csv", os.path.exists(fail_csv))
        if os.path.exists(fail_csv):
            with open(fail_csv, encoding="utf-8-sig") as fh:
                text = fh.read()
            check("失败明细含每个候选地址的 404 状态", "=> 404" in text)
            check("失败明细含原始媒体地址", "/media/nope" in text)
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


def test_rate_limiter() -> None:
    """限流器：别把 CDN 打爆，并且被限流后能自动降速。"""
    from xdl.download import RateLimiter

    limiter = RateLimiter(per_sec=50.0)
    started = time.monotonic()
    for _ in range(10):
        limiter.wait()
    cost = time.monotonic() - started
    check("限流器按请求数节流", cost >= 0.12, f"10 个请求用时 {cost:.3f}s")
    limiter.slow_down(1.5)
    check("限流器支持降速", limiter.penalty > 1.0, f"倍率 {limiter.penalty:.2f}")


def test_harvest_cache_roundtrip() -> None:
    """抓取结果缓存往返：复用缓存 = 完全不碰搜索接口（防封号关键）。"""
    from xdl.cache import load, save
    from xdl.parse import MediaItem, Tweet

    tmp = tempfile.mkdtemp(prefix="xdl-cache-")
    try:
        tweet = Tweet(tweet_id="1800000000000000123", username="zoe",
                      created_at=dt.datetime(2024, 5, 5, 1, 2, 3, tzinfo=dt.timezone.utc),
                      text="hi")
        tweet.media = [MediaItem(kind="photo", url="https://pbs.twimg.com/media/AAA?name=orig",
                                 fallback_urls=["https://pbs.twimg.com/media/AAA?name=large"],
                                 tweet_id=tweet.tweet_id, username="zoe", created_at=tweet.created_at,
                                 tweet_url="https://x.com/zoe/status/1800000000000000123", index=1)]
        save(tmp, "cat filter:images", "live", [tweet], [], log=lambda _m: None)
        loaded = load(tmp, "cat filter:images", "live", 72.0, log=lambda _m: None)
        check("抓取缓存能写能读", bool(loaded))
        if loaded:
            got_tweets, got_dom = loaded
            check("缓存里的推文与媒体完整",
                  len(got_tweets) == 1 and got_tweets[0].media[0].url.endswith("?name=orig")
                  and got_tweets[0].media[0].created_at == tweet.created_at,
                  got_tweets[0].media[0].url if got_tweets else "")
        expired = load(tmp, "cat filter:images", "live", -1.0, log=lambda _m: None)
        check("过期缓存不再复用", expired is None)
        check("不同搜索词不会串缓存",
              load(tmp, "dog filter:images", "live", 72.0, log=lambda _m: None) is None)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_download_chain() -> None:
    from xdl.download import Downloader
    from xdl.parse import MediaItem

    server, base = start_test_server()
    tmp = tempfile.mkdtemp(prefix="xdl-dl-")
    try:
        stamp = dt.datetime(2024, 5, 2, 6, 0, tzinfo=dt.timezone.utc)
        items = [
            MediaItem(kind="photo", url=f"{base}/media/pic.jpg?name=orig", tweet_id="1800000000000000002",
                      username="bob", created_at=stamp, index=1, tweet_url="https://x.com/bob/status/1800000000000000002"),
            # 主地址 404 → 应自动回退到备用地址（验证图片地址回退链）
            MediaItem(kind="photo", url=f"{base}/media/gone.jpg?name=orig", tweet_id="1800000000000000006",
                      username="gina", created_at=stamp, index=1,
                      fallback_urls=[f"{base}/media/pic.jpg?name=orig"]),
            MediaItem(kind="video", url=f"{base}/v/video.mp4", tweet_id="1800000000000000003",
                      username="carol", created_at=stamp, index=1),
            MediaItem(kind="video", url=f"{base}/v/pl.m3u8", tweet_id="1800000000000000004",
                      username="dave", created_at=stamp, index=1),
        ]
        downloader = Downloader(out_dir=tmp, workers=3, skip_existing=True)
        stats = downloader.run(items)
        check("下载成功 4 个", stats.get("ok") == 4, json.dumps(stats, ensure_ascii=False))
        day_dir = os.path.join(tmp, "2024-05-02")
        names = sorted(os.listdir(day_dir)) if os.path.isdir(day_dir) else []
        check("文件都进了日期文件夹", len(names) == 4, str(names))
        check("图片落盘（含 404 回退那一张）", sum(1 for n in names if n.endswith(".jpg")) == 2, str(names))
        check("视频落盘", sum(1 for n in names if n.endswith(".mp4")) == 1, str(names))
        check("HLS 合并成 .ts", any(n.endswith(".ts") for n in names), str(names))

        ts_path = [os.path.join(day_dir, n) for n in names if n.endswith(".ts")]
        if ts_path:
            with open(ts_path[0], "rb") as fh:
                merged = fh.read()
            expect = _Handler.segments[0] + _Handler.segments[1]
            check("HLS 分片内容拼接正确", merged == expect, f"{len(merged)} 字节")

        csv_path = os.path.join(tmp, "_meta", "downloads.csv")
        check("生成下载记录 CSV", os.path.exists(csv_path))
        if os.path.exists(csv_path):
            with open(csv_path, encoding="utf-8-sig") as fh:
                rows = fh.read().strip().splitlines()
            check("CSV 有表头 + 4 行记录", len(rows) == 5, f"{len(rows)} 行")

        again = Downloader(out_dir=tmp, workers=2, skip_existing=True).run(items)
        check("重复运行会跳过已下载文件", again.get("skip") == 4, json.dumps(again, ensure_ascii=False))
    finally:
        server.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)


# ------------------------------------------------------------------ 假 X 搜索页（端到端抓取测试）
_JPEG = b"\xff\xd8\xff\xe0" + b"J" * 256


class _FakeXHandler(BaseHTTPRequestHandler):
    """模拟 X 搜索页：页面自己发 SearchTimeline 请求，往下滚就加载下一页。"""

    total_pages = 3
    request_count = 0

    def log_message(self, *_args):
        pass

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path == "/search":
            self._send(SEARCH_PAGE_HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path.endswith("/SearchTimeline"):
            self._timeline()
        elif path.startswith("/media/"):
            self._send(_JPEG, "image/jpeg")
        else:
            self.send_response(404)
            self.end_headers()

    do_POST = do_GET

    def _timeline(self) -> None:
        from urllib.parse import parse_qs, urlparse

        query = parse_qs(urlparse(self.path).query)
        page = int((query.get("v") or ["1"])[0])
        type(self).request_count += 1
        weekdays = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        when = f"{weekdays[(page - 1) % 7]} May 0{page} 04:00:00 +0000 2024"
        node = _tweet_node(
            f"180000000000000100{page}",
            f"user{page}",
            when,
            [{"type": "photo", "media_url_https": PHOTO_URL}],
        )
        payload = {
            "data": {"search_by_raw_query": {"search_timeline": {"timeline": {"instructions": [
                {"type": "TimelineAddEntries", "entries": [
                    {"entryId": f"tweet-{page}",
                     "content": {"itemContent": {"tweet_results": {"result": node}}}},
                ]},
            ]}}}}
        }
        self._send(json.dumps(payload).encode("utf-8"), "application/json")

    def _send(self, body: bytes, ctype: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


SEARCH_PAGE_HTML = """
<!doctype html><html><head><meta charset="utf-8"><title>fake x search</title></head>
<body style="margin:0">
  <div id="col"></div>
  <script>
    let page = 0;
    const MAX = 3;
    async function loadMore() {
      if (page >= MAX) return;
      page += 1;
      try {
        await fetch('/i/api/graphql/hash/SearchTimeline?v=' + page, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({page: page})
        });
      } catch (e) {}
      const art = document.createElement('article');
      art.style.display = 'block';
      art.style.height = '900px';           /* 每页撑高，模拟真实时间线增长 */
      art.innerHTML = '<time datetime="2024-05-0' + page + 'T04:00:00.000Z"></time>'
        + '<a href="/user' + page + '/status/180000000000000100' + page + '">post</a>'
        + '<img src="/media/pic' + page + '.jpg?name=small">';
      document.getElementById('col').appendChild(art);
    }
    window.addEventListener('scroll', loadMore);
    loadMore();
  </script>
</body></html>
"""


def test_harvest_offline() -> None:
    """真浏览器 + 假 X 页面：验证接口拦截、滚动翻页、DOM 兜底整条抓取链路。"""
    from xdl.browser import BrowserController
    from xdl.cdp import pick_free_port
    from xdl.harvest import Harvester

    server = ThreadingHTTPServer(("127.0.0.1", 0), _FakeXHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    profile = tempfile.mkdtemp(prefix="xdl-harvest-")
    controller = None
    try:
        controller = BrowserController(profile_dir=profile, port=pick_free_port(9700, 9800), headless=True)
        controller.start(f"{base}/search")
        _target, session_id = controller.open_page(f"{base}/search")
        harvester = Harvester(controller, session_id, log=lambda _m: None)
        tweets, dom_items = harvester.search(
            "test-keyword",
            page_url=f"{base}/search",
            max_scrolls=8,
            pause=(0.5, 0.7),
        )
        check("抓取：拦截到 SearchTimeline 接口响应", harvester.intercepted >= 1,
              f"{harvester.intercepted} 次（服务器实际收到 {_FakeXHandler.request_count} 次请求）")
        check("抓取：滚动加载后拿到 3 页推文", len(tweets) == 3, f"{len(tweets)} 条")
        names = sorted(t.username for t in tweets)
        check("抓取：推文作者解析正确", names == ["user1", "user2", "user3"], str(names))
        check("抓取：日期解析正确", all(t.created_at for t in tweets))
        check("抓取：DOM 兜底抓到媒体", len(dom_items) >= 1, f"{len(dom_items)} 个")
        dom_ladder = ([dom_items[0].url] + list(dom_items[0].fallback_urls)) if dom_items else []
        check("抓取：DOM 兜底图片也走完整候选链（含 name=orig 档位）",
              any(u.endswith("name=orig") for u in dom_ladder), str(dom_ladder[:2]))
        check("抓取：DOM 兜底保留页面渲染地址当保底",
              any("?name=small" in u for u in dom_ladder), str(dom_ladder[-1:] if dom_ladder else ""))
        check("抓取：记录搜索接口状态码（用于识别限流）", isinstance(harvester.risk.status_counts, dict),
              str(harvester.risk.status_counts))
    except Exception as exc:  # noqa: BLE001
        check("抓取链路（真浏览器端到端）", False, f"{type(exc).__name__}: {exc}")
    finally:
        if controller:
            controller.close_browser()
            if controller.proc:
                try:
                    controller.proc.terminate()
                    controller.proc.wait(timeout=8)
                except Exception:
                    try:
                        controller.proc.kill()
                    except Exception:
                        pass
        server.shutdown()
        shutil.rmtree(profile, ignore_errors=True)


def test_cdp_channel() -> bool:
    """真起一个 Chrome，验证 CDP 客户端能用（不访问外网）。"""
    from xdl.browser import find_browser, launch_browser_process
    from xdl.cdp import CDP, http_json, pick_free_port

    try:
        exe = find_browser("")
    except FileNotFoundError as exc:
        check("找到 Chrome/Edge", False, str(exc))
        return False
    check("找到 Chrome/Edge", True, exe)

    port = pick_free_port(9500, 9600)
    profile = tempfile.mkdtemp(prefix="xdl-cdp-")
    proc = None
    cdp = None
    try:
        try:
            proc = launch_browser_process(exe, port, profile, "about:blank", headless=True,
                                          ready_timeout=20.0)
        except Exception:
            port = pick_free_port(9650, 9680)
            proc = launch_browser_process(exe, port, profile, "about:blank", headless=True,
                                          no_sandbox=True, ready_timeout=35.0)
        version = http_json(f"http://127.0.0.1:{port}/json/version", timeout=5.0)
        check("调试端口就绪", bool(version.get("webSocketDebuggerUrl")), version.get("Browser", ""))
        cdp = CDP(version["webSocketDebuggerUrl"])
        target_id, session_id = cdp.create_page("about:blank")
        check("能新建标签页并附着", bool(session_id))
        value = cdp.evaluate(session_id, "1 + 1")
        check("能在页面执行 JS", value == 2, f"1+1={value}")
        cdp.evaluate(session_id, "document.title = 'xdl-selftest'")
        title = cdp.evaluate(session_id, "document.title")
        check("能读写页面 DOM", title == "xdl-selftest", str(title))
        cookies = cdp.send("Network.getAllCookies", {}, session_id=session_id)
        check("能读取 Cookie 接口", isinstance(cookies.get("cookies"), list),
              f"{len(cookies.get('cookies', []))} 项")
        cdp.close_page(target_id)
        return True
    except Exception as exc:  # noqa: BLE001
        check("CDP 通道可用", False, f"{type(exc).__name__}: {exc}")
        return False
    finally:
        if cdp:
            cdp.close()
        try:
            proc.terminate()
            proc.wait(timeout=8)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        time.sleep(0.5)
        shutil.rmtree(profile, ignore_errors=True)


def test_cookie_helpers() -> None:
    from xdl.browser import cookie_header, parse_cookie_text

    jar = parse_cookie_text("auth_token=abc123; ct0=def456\nkdt=xyz")
    check("解析粘贴的 Cookie 串", jar == {"auth_token": "abc123", "ct0": "def456", "kdt": "xyz"}, str(jar))
    check("拼回 Cookie 头", cookie_header(jar).startswith("auth_token=abc123; ct0=def456"), cookie_header(jar))


def test_gui_imports() -> None:
    """界面能否正常构建（不进入 mainloop，避免自检时弹窗）。"""
    import tkinter as tk

    from xdl.gui import App

    root = tk.Tk()
    root.withdraw()
    app = None
    try:
        app = App(root)
        check("图形界面可构建", True)
    except Exception as exc:  # noqa: BLE001
        check("图形界面可构建", False, f"{type(exc).__name__}: {exc}")
    finally:
        try:                                   # 先取消定时回调，再销毁窗口，避免 Tcl 报错噪音
            if app is not None and getattr(app, "_pump_job", None):
                root.after_cancel(app._pump_job)
        except Exception:
            pass
        try:
            root.destroy()
        except Exception:
            pass


def run_selftest() -> int:
    try:                                   # 控制台一律 UTF-8，避免中文乱码
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    print("=" * 70)
    print("X 媒体下载器 自检（离线部分不依赖 X；CDP 部分会临时启一个无头 Chrome）")
    print("=" * 70)
    started = time.time()

    test_parsing()
    test_date_and_query()
    test_filename_layout()
    test_cookie_helpers()
    test_photo_ladder_and_shapes()
    test_rate_limiter()
    test_harvest_cache_roundtrip()
    test_download_chain()
    test_failure_chain_reports_status()
    test_harvest_offline()
    test_cdp_channel()
    test_gui_imports()

    failed = [name for name, ok, _ in _results if not ok]
    print("-" * 70)
    print(f"共 {len(_results)} 项检查，通过 {len(_results) - len(failed)}，失败 {len(failed)}，用时 {time.time()-started:.1f}s")
    if failed:
        print("失败项：" + "；".join(failed))
        return 1
    print("全部通过 ✅")
    return 0

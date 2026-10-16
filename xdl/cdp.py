# -*- coding: utf-8 -*-
"""极简 Chrome DevTools Protocol（CDP）客户端。

只依赖 websocket-client：
  · 浏览器级 WebSocket 连接，支持 Target.attachToTarget(flatten=True) 子会话
  · 后台读线程负责分发事件到队列、唤醒等待中的命令
  · send() 同步等待命令结果；events() 非阻塞取事件
"""
from __future__ import annotations

import itertools
import json
import os
import queue
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, Iterator, Optional

try:
    import websocket  # websocket-client
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "缺少依赖 websocket-client。请执行：python -m pip install websocket-client"
    ) from exc

_LOOPBACK = ["127.0.0.1", "localhost", "::1", "0.0.0.0"]


class CDPError(RuntimeError):
    """CDP 命令执行失败或超时。"""


def http_json(url: str, timeout: float = 6.0) -> Any:
    """访问 http://127.0.0.1:<port>/json/... 这类调试端口 HTTP 接口。"""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # 本机调试端口绝不走代理
    req = urllib.request.Request(url)
    with opener.open(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def wait_devtools_ready(port: int, timeout: float = 25.0, proc=None) -> Dict[str, Any]:
    """等 Chrome 的调试端口起来，返回 /json/version 内容。

    传入 proc 时：一旦浏览器进程已经退出就立刻抛错（不再干等超时），
    这样"启动失败 → 换兼容模式重试"能在 1 秒内完成，而不是白等 20 秒。
    """
    deadline = time.time() + timeout
    last: Optional[Exception] = None
    while time.time() < deadline:
        if proc is not None:
            code = proc.poll()
            if code is not None:
                raise CDPError(f"浏览器进程已退出（exit={code}），调试端口 {port} 未起来")
        try:
            return http_json(f"http://127.0.0.1:{port}/json/version", timeout=2.5)
        except Exception as exc:  # Chrome 还在启动
            last = exc
            time.sleep(0.25)
    raise CDPError(f"调试端口 {port} 未就绪：{last}")


class CDP:
    """一条浏览器级 CDP 连接。"""

    def __init__(self, ws_url: str, timeout: float = 60.0):
        self._timeout = timeout
        self._ids = itertools.count(1)
        self._pending: Dict[int, Dict[str, Any]] = {}
        self._events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._send_lock = threading.Lock()
        self._closed = False
        self._error: Optional[BaseException] = None
        self._ws = websocket.create_connection(
            ws_url,
            timeout=timeout,
            enable_multithread=True,
            suppress_origin=True,
            http_no_proxy=_LOOPBACK,
        )
        self._reader = threading.Thread(target=self._read_loop, name="cdp-reader", daemon=True)
        self._reader.start()

    # ------------------------------------------------------------ 生命周期
    @property
    def closed(self) -> bool:
        return self._closed

    def close(self, timeout: float = 1.0) -> None:
        """关闭连接。带超时，绝不阻塞调用方（关窗口卡死的主要来源就在这）。"""
        if self._closed:
            return
        self._closed = True
        try:
            sock = getattr(self._ws, "sock", None)
            if sock is not None:
                sock.settimeout(timeout)
        except Exception:
            pass
        try:
            self._ws.close(timeout=timeout)
        except TypeError:
            try:
                self._ws.close()
            except Exception:
                pass
        except Exception:
            pass

    # ------------------------------------------------------------ 读线程
    def _read_loop(self) -> None:
        while not self._closed:
            try:
                raw = self._ws.recv()
            except Exception as exc:  # 连接断开/关闭
                self._error = exc
                self._closed = True
                break
            if not raw:
                continue
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            msg_id = message.get("id")
            if msg_id is not None:
                waiter = self._pending.pop(msg_id, None)
                if waiter is not None:
                    waiter["result"] = message
                    waiter["event"].set()
            else:
                self._events.put(message)
        # 唤醒所有等待者，避免调用方卡死
        for waiter in list(self._pending.values()):
            waiter["event"].set()
        self._pending.clear()

    # ------------------------------------------------------------ 命令
    def send(self, method: str, params: Optional[Dict[str, Any]] = None,
             session_id: Optional[str] = None, timeout: Optional[float] = None) -> Dict[str, Any]:
        """同步执行一条 CDP 命令。"""
        if self._closed:
            raise CDPError(f"CDP 连接已关闭（{method}）：{self._error}")
        msg_id = next(self._ids)
        payload: Dict[str, Any] = {"id": msg_id, "method": method}
        if params:
            payload["params"] = params
        if session_id:
            payload["sessionId"] = session_id
        waiter: Dict[str, Any] = {"event": threading.Event(), "result": None}
        self._pending[msg_id] = waiter
        with self._send_lock:
            self._ws.send(json.dumps(payload, ensure_ascii=False))
        if not waiter["event"].wait(timeout or self._timeout):
            self._pending.pop(msg_id, None)
            raise CDPError(f"CDP 命令超时：{method}")
        result = waiter["result"]
        if not result:
            raise CDPError(f"CDP 连接中断：{method}")
        if "error" in result:
            err = result["error"] or {}
            raise CDPError(f"{method} 失败：{err.get('message', err)}")
        return result.get("result", {}) or {}

    # ------------------------------------------------------------ 事件
    def events(self, timeout: float = 0.0, max_items: int = 200) -> Iterator[Dict[str, Any]]:
        """非阻塞地取出积压事件（最多 max_items 条）。"""
        for _ in range(max_items):
            try:
                yield self._events.get(timeout=timeout if _ == 0 else 0.0)
            except queue.Empty:
                return

    def drain(self) -> int:
        """丢弃积压事件，返回丢弃数量。"""
        count = 0
        while True:
            try:
                self._events.get_nowait()
                count += 1
            except queue.Empty:
                return count

    # ------------------------------------------------------------ 目标/页面
    def create_page(self, url: str = "about:blank") -> tuple:
        """新建标签页并附着，返回 (target_id, session_id)。"""
        created = self.send("Target.createTarget", {"url": url})
        target_id = created["targetId"]
        attached = self.send("Target.attachToTarget", {"targetId": target_id, "flatten": True})
        return target_id, attached["sessionId"]

    def close_page(self, target_id: str) -> None:
        try:
            self.send("Target.closeTarget", {"targetId": target_id})
        except CDPError:
            pass

    def evaluate(self, session_id: str, expression: str, timeout: float = 30.0) -> Any:
        """在页面里执行 JS 并取回值。"""
        result = self.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
                "userGesture": True,
            },
            session_id=session_id,
            timeout=timeout,
        )
        if "exceptionDetails" in result:
            detail = result["exceptionDetails"]
            raise CDPError(f"页面 JS 异常：{detail.get('text')} {detail.get('exception', {}).get('description', '')}")
        return (result.get("result") or {}).get("value")


def list_pages(port: int) -> list:
    """列出该端口下所有 page 类型标签页。"""
    try:
        targets = http_json(f"http://127.0.0.1:{port}/json/list", timeout=4.0)
    except Exception:
        return []
    return [t for t in targets if t.get("type") == "page"]


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.35)
        return sock.connect_ex((host, port)) == 0


def pick_free_port(start: int = 9222, end: int = 9333) -> int:
    for port in range(start, end + 1):
        if not port_in_use(port):
            return port
    raise CDPError(f"{start}-{end} 之间没有空闲调试端口")

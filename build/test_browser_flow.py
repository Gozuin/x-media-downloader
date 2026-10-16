# -*- coding: utf-8 -*-
"""分步验证浏览器相关链路，定位卡在哪一步。"""
import sys
import time

sys.path.insert(0, r"D:\AI\x-media-downloader")


def step(name, fn):
    t0 = time.time()
    print(f"--- 开始：{name}", flush=True)
    try:
        fn()
        print(f"--- 完成：{name}（{time.time()-t0:.1f}s）", flush=True)
    except Exception as exc:  # noqa: BLE001
        print(f"--- 异常：{name} -> {type(exc).__name__}: {exc}（{time.time()-t0:.1f}s）", flush=True)


from xdl.selftest import test_cdp_channel, test_harvest_offline  # noqa: E402

step("CDP 通道", test_cdp_channel)
step("抓取链路（真浏览器）", test_harvest_offline)
print("=== 全部步骤结束 ===", flush=True)

# -*- coding: utf-8 -*-
"""生成程序图标 xdl.ico（蓝底 + 下载箭头 + 播放三角）。"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.ico")
SIZE = 512
BG_TOP = (29, 155, 240)      # X 蓝
BG_BOTTOM = (12, 94, 168)
WHITE = (255, 255, 255)


def rounded_mask(size: int, radius: int) -> Image.Image:
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return mask


def build() -> str:
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))

    # 竖向渐变底
    grad = Image.new("RGB", (1, SIZE))
    for y in range(SIZE):
        t = y / (SIZE - 1)
        grad.putpixel((0, y), tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3)))
    grad = grad.resize((SIZE, SIZE))
    img.paste(grad, (0, 0), rounded_mask(SIZE, int(SIZE * 0.22)))

    draw = ImageDraw.Draw(img)
    cx = SIZE // 2

    # 下载箭头（竖杆 + 箭头）
    stem_w = int(SIZE * 0.115)
    draw.rounded_rectangle(
        [cx - stem_w // 2, int(SIZE * 0.20), cx + stem_w // 2, int(SIZE * 0.50)],
        radius=stem_w // 2, fill=WHITE,
    )
    arrow_top, arrow_bottom = int(SIZE * 0.42), int(SIZE * 0.66)
    arrow_half = int(SIZE * 0.20)
    draw.polygon(
        [(cx - arrow_half, arrow_top), (cx + arrow_half, arrow_top), (cx, arrow_bottom)],
        fill=WHITE,
    )

    # 底部托盘（下载完成的那条横线）
    tray_h = int(SIZE * 0.085)
    draw.rounded_rectangle(
        [int(SIZE * 0.22), int(SIZE * 0.74), int(SIZE * 0.78), int(SIZE * 0.74) + tray_h],
        radius=tray_h // 2, fill=WHITE,
    )

    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    img.save(OUT, format="ICO", sizes=sizes)
    return OUT


if __name__ == "__main__":
    path = build()
    print("icon written:", path, os.path.getsize(path), "bytes")

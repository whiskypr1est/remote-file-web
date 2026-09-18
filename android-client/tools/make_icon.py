# -*- coding: utf-8 -*-
"""
生成安卓应用的启动图标
======================

    python android-client/tools/make_icon.py

产物写进 app/src/main/res/mipmap-*/ic_launcher.png（五档密度）。

为什么要一个脚本、而不是直接塞几个 PNG
--------------------------------------
换了图标想改配色/改形状时，得能重来一遍。项目里 tools/make_lyrics_icon.py
就是同样的理由。

图标画的是什么
--------------
「一块蓝色的屏幕 + 里面一个窗口 + 底下一条任务栏」—— 也就是这个虚拟桌面
本身的样子。刻意只保留三块大色块，不做细节：启动图标最常被看到的时候
只有 48px，细线条在那个尺寸下会糊成一团。

配色用的是网页那套强调色（desktop.css 的 --accent #0a6ad6）。
"""

from __future__ import annotations

import os

from PIL import Image, ImageDraw

# 先在 1024 上画、最后缩到各档 —— 直接在小尺寸上画会有锯齿
CANVAS = 1024

# 五种密度（安卓启动图标的常规尺寸）
DENSITIES = {
    "mdpi": 48,
    "hdpi": 72,
    "xhdpi": 96,
    "xxhdpi": 144,
    "xxxhdpi": 192,
}

# 网页的强调色：浅一点的做渐变头，深一点的做底
TOP = (0x2B, 0x8C, 0xF0)
BOTTOM = (0x0A, 0x6A, 0xD6)

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "..", "app", "src", "main", "res")


def gradient_background() -> Image.Image:
    """竖向渐变：1 像素宽画好再拉宽，比逐像素填 1024x1024 快得多。"""
    strip = Image.new("RGB", (1, CANVAS))
    for y in range(CANVAS):
        t = y / (CANVAS - 1)
        strip.putpixel((0, y), (
            int(TOP[0] + (BOTTOM[0] - TOP[0]) * t),
            int(TOP[1] + (BOTTOM[1] - TOP[1]) * t),
            int(TOP[2] + (BOTTOM[2] - TOP[2]) * t),
        ))
    return strip.resize((CANVAS, CANVAS))


def build() -> Image.Image:
    image = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))

    # ---- 圆角底板 ----
    mask = Image.new("L", (CANVAS, CANVAS), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [0, 0, CANVAS - 1, CANVAS - 1], radius=int(CANVAS * 0.22), fill=255)
    image.paste(gradient_background(), (0, 0), mask)

    draw = ImageDraw.Draw(image)
    white = (255, 255, 255, 255)
    accent = (0x0A, 0x6A, 0xD6, 255)

    # ---- 中间那个「窗口」 ----
    win_left = int(CANVAS * 0.20)
    win_right = int(CANVAS * 0.80)
    win_top = int(CANVAS * 0.24)
    win_bottom = int(CANVAS * 0.66)
    draw.rounded_rectangle(
        [win_left, win_top, win_right, win_bottom],
        radius=int(CANVAS * 0.05), fill=white)

    # 窗口的标题栏：一条强调色的横条
    bar_h = int(CANVAS * 0.075)
    draw.rounded_rectangle(
        [win_left, win_top, win_right, win_top + bar_h],
        radius=int(CANVAS * 0.05), fill=accent)
    draw.rectangle(
        [win_left, win_top + bar_h // 2, win_right, win_top + bar_h], fill=accent)

    # 窗口里的两行「内容」（暗示这是个有内容的界面，而不是空框）
    line_h = int(CANVAS * 0.045)
    for index, ratio in enumerate((0.34, 0.50)):
        lw = int((win_right - win_left) * (0.62 if index == 0 else 0.42))
        draw.rounded_rectangle(
            [win_left + int(CANVAS * 0.05), int(CANVAS * (0.40 + ratio * 0.30)),
             win_left + int(CANVAS * 0.05) + lw,
             int(CANVAS * (0.40 + ratio * 0.30)) + line_h],
            radius=line_h // 2, fill=(0xC8, 0xDC, 0xF4, 255))

    # ---- 底下那条任务栏 ----
    task_left = int(CANVAS * 0.24)
    task_right = int(CANVAS * 0.76)
    task_top = int(CANVAS * 0.755)
    task_bottom = int(CANVAS * 0.845)
    draw.rounded_rectangle(
        [task_left, task_top, task_right, task_bottom],
        radius=int(CANVAS * 0.045), fill=white)

    # 任务栏上的三个「已打开的窗口」
    dot = int(CANVAS * 0.055)
    gap = int(CANVAS * 0.030)
    start_x = task_left + int(CANVAS * 0.055)
    for i in range(3):
        x = start_x + i * (dot + gap)
        draw.rounded_rectangle(
            [x, task_top + (task_bottom - task_top - dot) // 2,
             x + dot, task_top + (task_bottom - task_top - dot) // 2 + dot],
            radius=int(dot * 0.25), fill=accent)

    return image


def main() -> int:
    icon = build()
    written = []
    for density, size in DENSITIES.items():
        directory = os.path.join(RES, "mipmap-" + density)
        os.makedirs(directory, exist_ok=True)
        target = os.path.join(directory, "ic_launcher.png")
        # LANCZOS：从 1024 缩到 48 也不会糊
        icon.resize((size, size), Image.LANCZOS).save(target, "PNG", optimize=True)
        written.append((target, size))

    for target, size in written:
        print("  %-4dpx  %s" % (size, os.path.relpath(target, RES)))
    print("\n共写出 %d 个图标。" % len(written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

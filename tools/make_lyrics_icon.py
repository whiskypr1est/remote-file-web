# -*- coding: utf-8 -*-
"""
生成桌面歌词客户端要用的图标
============================

    python tools/make_lyrics_icon.py

从源图 desktop-lyrics/build/icon.png（256x256）生成：

    desktop-lyrics/build/icon.ico        安装包与 exe 的图标（内含多档尺寸）
    desktop-lyrics/src/assets/tray.png   托盘图标（16px）
    desktop-lyrics/src/assets/tray@2x.png  托盘图标高分辨率版（32px，200% 缩放屏用）

★ 生成好的图标**已经提交进仓库**，平时不需要跑这个脚本 ——
  只有在想换图标（改配色 / 改音符）时才需要重新生成。

源图是怎么来的
--------------
它是一段 HTML/CSS（渐变圆角方块 + 白色音符）用无头浏览器渲染出来的，不是
手工点出来的：这样换配色只要改 CSS 再渲染一次，不必打开任何绘图软件。
（渲染命令见 desktop-lyrics/README.md 的「换个图标」一节。）

为什么托盘要两个尺寸
--------------------
Windows 托盘的图标位在 100% 缩放下只有 16x16，在 200% 缩放下是 32x32。
只给一张 32px 的图，系统会自己缩小，边缘会糊；用 name.png + name@2x.png
这种约定，Electron 的 nativeImage.createFromPath 会自动挑合适的那张。
"""

from __future__ import annotations

import os
import sys

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SOURCE = os.path.join(BASE_DIR, "desktop-lyrics", "build", "icon.png")
ICO_TARGET = os.path.join(BASE_DIR, "desktop-lyrics", "build", "icon.ico")
TRAY_TARGET = os.path.join(BASE_DIR, "desktop-lyrics", "src", "assets", "tray.png")
TRAY_2X_TARGET = os.path.join(BASE_DIR, "desktop-lyrics", "src", "assets", "tray@2x.png")

# ICO 里要包含的尺寸。256 是必须的（electron-builder 会检查），
# 其余几档是 Explorer / 安装程序在不同视图下要用的。
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def main() -> int:
    try:
        from PIL import Image
    except ImportError:
        print("缺少 Pillow。图标已经提交在仓库里了，只有想重新生成时才需要它：")
        print("    pip install -r requirements.txt")
        return 1

    if not os.path.isfile(SOURCE):
        print("找不到源图：%s" % SOURCE)
        return 1

    image = Image.open(SOURCE).convert("RGBA")
    if image.size != (256, 256):
        # 不做静默缩放：源图尺寸不对多半是拿错了文件，说清楚比猜好
        print("源图必须是 256x256，实际是 %dx%d：%s"
              % (image.width, image.height, SOURCE))
        return 1

    # 打包用：一张 ico 带多档尺寸（Windows 会按显示场景自己挑）
    image.save(ICO_TARGET, format="ICO",
               sizes=[(size, size) for size in ICO_SIZES])

    # 托盘用：16px 与 32px 两张。缩小时用 LANCZOS —— 默认的最近邻会让
    # 音符的斜杠出现锯齿，在托盘那种小尺寸下非常明显。
    image.resize((16, 16), Image.LANCZOS).save(TRAY_TARGET, format="PNG")
    image.resize((32, 32), Image.LANCZOS).save(TRAY_2X_TARGET, format="PNG")

    for path in (ICO_TARGET, TRAY_TARGET, TRAY_2X_TARGET):
        print("已生成 %s（%d 字节）" % (os.path.relpath(path, BASE_DIR),
                                       os.path.getsize(path)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

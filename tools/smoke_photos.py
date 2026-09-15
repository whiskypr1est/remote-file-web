# -*- coding: utf-8 -*-
"""
照片应用 · 真实浏览器冒烟测试的驱动
====================================

    python tools\\smoke_photos.py

它做四件事：

1. 用**临时配置**起一个真实服务子进程（只暴露一个临时目录，不碰真实磁盘）；
2. 现场生成几张带真实 EXIF / GPS 的示例照片（外加一张没有 EXIF 的截图），
   通过 HTTP 导入（**就地索引**，不复制）；
3. 登录拿会话 Cookie，把它交给 Electron —— 等价于「用户已经在登录页登录过」；
4. 用真实的 Electron（Chromium）打开虚拟桌面，走一遍界面上的完整流程，
   并把过程截图出来供人工核对。

为什么要单独有这么一道
----------------------
前端那几道快速闸门（`node --check`、跨模块调用名、纯逻辑用例）都查不出
**「窗口打开了但里面是空的」**这类问题。这个脚本第一次跑就抓到一个真实的
bug（详见 tools/smoke_photos.js 头部）：接口测试、静态闸门、逻辑用例全绿，
而网格在浏览器里永远是空的。

★ 它**不进 unittest 套件**：需要 node + Electron，而且要有可用的桌面会话
  （要开真窗口）。部署机器上不一定有，所以与 desktop-lyrics/tools/smoke.js
  一样做成「手动跑的工具」，而不是让整套测试因为它变红。

找不到 Electron 时给一句明确的提示并退出（退出码 2），不是静默跳过 ——
静默跳过会让人以为「跑过了、没问题」。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tests._harness import ServerProcess          # noqa: E402

SMOKE_JS = os.path.join(BASE_DIR, "tools", "smoke_photos.js")

ADMIN_USER = "tester"
ADMIN_PASSWORD = "test-password-123"


# ---------------------------------------------------------------------------
# 找一个能用的 Electron
# ---------------------------------------------------------------------------

def find_electron() -> str:
    """
    依次找：项目里 desktop-lyrics 装的那份 -> PATH 上的 electron。

    本项目不把 Electron 作为根依赖（前端本身零依赖、测试只要 node），
    但 desktop-lyrics 那个桌面歌词客户端一定装了它，所以优先复用那一份。
    """
    candidates = [
        os.path.join(BASE_DIR, "desktop-lyrics", "node_modules", "electron",
                     "dist", "electron.exe"),
        os.path.join(BASE_DIR, "desktop-lyrics", "node_modules", "electron",
                     "dist", "electron"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    found = shutil.which("electron") or shutil.which("electron.exe")
    return found or ""


# ---------------------------------------------------------------------------
# 造示例照片
# ---------------------------------------------------------------------------

def make_jpeg(path: str, color, taken: str = "", gps=None, size=(900, 600)) -> None:
    """现场生成一张**带真实 EXIF** 的 JPEG（不然 EXIF 那条链路等于没测）。"""
    from PIL import Image
    from PIL.TiffImagePlugin import IFDRational

    os.makedirs(os.path.dirname(path), exist_ok=True)
    im = Image.new("RGB", size, color)
    exif = Image.Exif()
    if taken:
        exif[306] = taken            # DateTime
        exif[36867] = taken          # DateTimeOriginal
    exif[271] = "SmokeMake"
    exif[272] = "SmokeModel"
    exif[33437] = IFDRational(18, 10)
    exif[33434] = IFDRational(1, 250)
    exif[34855] = 100
    if gps:
        block = exif.get_ifd(0x8825)
        block[1], block[2] = gps[0], gps[1]
        block[3], block[4] = gps[2], gps[3]
    im.save(path, "JPEG", exif=exif)


def seed_photos(root: str) -> None:
    from PIL import Image

    # 三张带 EXIF：两个年份、跨月，便于检查时间轴分组
    make_jpeg(os.path.join(root, "2023-旅行", "海边日落.jpg"), (222, 120, 60),
              "2023:08:15 12:34:56",
              ("N", (36.0, 3.0, 40.0), "E", (120.0, 19.0, 10.0)))
    make_jpeg(os.path.join(root, "2023-旅行", "山间小路.jpg"), (60, 140, 90),
              "2023:08:16 09:15:00")
    make_jpeg(os.path.join(root, "2024-春天", "樱花.jpg"), (230, 150, 190),
              "2024:03:20 16:40:00", size=(600, 900))
    # 一张没有 EXIF 的（截图 / 微信图）：它必须落到「待整理」那一档
    Image.new("RGB", (400, 400), (40, 40, 60)).save(os.path.join(root, "截图.png"))
    with open(os.path.join(root, "说明.txt"), "w", encoding="utf-8") as fh:
        fh.write("这不是照片")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main() -> int:
    electron = find_electron()
    if not electron:
        print("★ 没有找到 Electron，无法做浏览器冒烟测试。")
        print("  装一个即可：cd desktop-lyrics && npm install（或把 electron 放进 PATH）")
        return 2

    work = tempfile.mkdtemp(prefix="fw-photo-smoke-")
    root = os.path.join(work, "照片共享")
    out_dir = os.path.join(work, "shots")

    server = None
    try:
        seed_photos(root)
        server = ServerProcess(
            [{"id": "share", "name": "照片共享", "path": root, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD).start()

        client = server.login_client()
        status, data = client.json("POST", "/api/photos/import",
                                   {"root": "share", "paths": [""], "background": False})
        if status != 200:
            print("★ 导入示例照片失败：%r %r" % (status, data))
            return 2
        print("示例照片：%s" % data.get("message"))

        os.makedirs(out_dir, exist_ok=True)
        env = dict(os.environ)
        env["PH_PORT"] = str(server.port)
        env["PH_COOKIE"] = client.cookie           # "fw_session=…"
        env["PH_OUT"] = out_dir

        print("用真实 Electron 打开虚拟桌面（Chromium）…\n")
        proc = subprocess.run(
            [electron, SMOKE_JS, "--no-sandbox", "--disable-gpu"],
            cwd=BASE_DIR, env=env, timeout=420,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

        print((proc.stdout or b"").decode("utf-8", "replace"))

        results_path = os.path.join(out_dir, "results.json")
        if os.path.isfile(results_path):
            with open(results_path, encoding="utf-8") as fh:
                payload = json.load(fh)
            failed = [r for r in payload.get("results", []) if not r.get("ok")]
            print("截图与结果已写到：%s" % out_dir)
            for item in failed:
                print("  !! %s —— %s" % (item["label"], item["extra"]))
        return proc.returncode
    finally:
        if server is not None:
            server.stop()
            server.cleanup()
        # 截图留在临时目录里供人工核对，这里不主动删（重启机器时系统会清）
        print("工作目录（含截图）：%s" % work)


if __name__ == "__main__":
    sys.exit(main())

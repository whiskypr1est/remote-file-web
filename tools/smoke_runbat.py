# -*- coding: utf-8 -*-
"""
「运行 bat / 在此处打开命令行」· 真实浏览器冒烟测试的驱动
=========================================================

    python tools\\smoke_runbat.py

它做四件事：

1. 造一个临时根目录，里面放一个子文件夹与一个 .bat —— 脚本会打印一行标记，
   并把标记同时写进一个文件（**双份证据**：终端里能看到、文件也能查）；
2. 用**临时配置**起一个真实服务子进程（只暴露那个临时目录）；
3. 登录拿会话 Cookie 交给 Electron；
4. 用真实浏览器走一遍界面：右键 .bat →「运行」→ 命令行窗口里出现标记；
   右键文件夹 →「在此处打开命令行」。

为什么要单独有这么一道
----------------------
「右键菜单里到底有没有那一项」静态闸门查不出来 —— 这个项目已经栽过一次
同类问题：desktop.js 里桌面图标与开始菜单是**两处独立渲染**，只改一处时
菜单里就没有入口，而当时所有静态检查都是绿的。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tests._harness import ServerProcess          # noqa: E402

SMOKE_JS = os.path.join(BASE_DIR, "tools", "smoke_runbat.js")

ADMIN_USER = "tester"
ADMIN_PASSWORD = "test-password-123"

SUBDIR = "mc server"
BAT_NAME = "启动服务器.bat"
MARK = "RUNBAT-OK"


def find_electron():
    import shutil
    candidates = [
        os.path.join(BASE_DIR, "desktop-lyrics", "node_modules", "electron",
                     "dist", "electron.exe"),
        os.path.join(BASE_DIR, "desktop-lyrics", "node_modules", "electron",
                     "dist", "electron"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    return shutil.which("electron") or shutil.which("electron.exe")


def seed(root: str) -> str:
    """放一个子目录 + 一个 .bat；返回脚本绝对路径。"""
    sub = os.path.join(root, SUBDIR)
    os.makedirs(sub, exist_ok=True)
    bat = os.path.join(sub, BAT_NAME)
    # 批处理用 GBK 写：中文注释/输出在 cmd 里才不会乱码
    with open(bat, "w", encoding="gbk") as fh:
        fh.write("@echo off\r\n")
        fh.write("echo %s\r\n" % MARK)
        fh.write("echo 目录=%CD%\r\n")
    return bat


def main() -> int:
    if not sys.platform.startswith("win"):
        print("★ 运行 bat 是 Windows 特有行为。")
        return 2

    electron = find_electron()
    if not electron:
        print("★ 没有找到 Electron，无法做浏览器冒烟测试。")
        print("  装一个即可：cd desktop-lyrics && npm install（或把 electron 放进 PATH）")
        return 2

    work = tempfile.mkdtemp(prefix="fw-runbat-smoke-")
    root = os.path.join(work, "root")
    out_dir = os.path.join(work, "shots")
    os.makedirs(root, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    server = None
    try:
        bat = seed(root)
        print("示例脚本：%s" % bat)

        def extra(cfg):
            cfg["terminal"]["enabled"] = True      # harness 默认关掉了终端

        server = ServerProcess(
            [{"id": "r1", "name": "root", "path": root}],
            username=ADMIN_USER, password=ADMIN_PASSWORD,
            extra_config=extra).start()

        client = server.login_client()

        # 先确认接口这一侧真的能跑起来 —— 浏览器那边要是失败，
        # 就能立刻区分「后端不行」还是「界面不行」。
        status, data = client.json("POST", "/api/terminal/session", {
            "run": {"root": "r1", "path": "%s/%s" % (SUBDIR, BAT_NAME)},
        })
        if status != 200:
            print("★ 接口侧创建「运行脚本」会话失败：%r %r" % (status, data))
            return 2
        print("接口侧会话已建立：sid=%s cwd=%s" % (data.get("id"), data.get("cwd")))

        env = dict(os.environ)
        env["RB_PORT"] = str(server.port)
        env["RB_COOKIE"] = client.cookie
        env["RB_OUT"] = out_dir
        env["RB_ROOT"] = "r1"
        env["RB_DIR"] = SUBDIR

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
        print("工作目录（含截图）：%s" % work)


if __name__ == "__main__":
    sys.exit(main())

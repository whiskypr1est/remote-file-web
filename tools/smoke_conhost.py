# -*- coding: utf-8 -*-
"""
控制台镜像 · 真实浏览器冒烟测试的驱动
=====================================

    python tools\\smoke_conhost.py

它做四件事：

1. 起一个**无窗口的控制台**作为确定性目标：`cmd.exe /k`，用 CREATE_NO_WINDOW
   启动（★ 不会在你桌面上弹窗），并让它打印一行唯一的标记文本；
2. 用**临时配置**起一个真实服务子进程（只暴露一个临时目录，不碰真实磁盘），
   并显式打开 `conhost.enabled`（默认是关的）；
3. 登录拿会话 Cookie，把它交给 Electron —— 等价于「用户已经在登录页登录过」；
4. 用真实的 Electron（Chromium）打开虚拟桌面，走一遍界面流程：
   开始菜单 → 控制台镜像 → 列表 → 选中目标 → 内容区真的画出字符栅格。

为什么选「无窗口控制台」当目标
------------------------------
  * 它是**确定性**的：pid 与标记文本都是本次现造的，不依赖用户桌面上恰好
    开着什么；
  * 它顺带覆盖了「没有窗口的后台控制台」这条路径（真实桌面上很常见）；
  * CREATE_NO_WINDOW 不会弹窗，跑测试不会打扰正在用机器的人。

★ 它**不进 unittest 套件**：需要 node + Electron，而且要有可用的桌面会话。
  与 tools/smoke_photos.py 一样做成「手动跑的工具」，而不是让整套测试因为它变红。

找不到 Electron 时给一句明确的提示并退出（退出码 2），不是静默跳过。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tests._harness import ServerProcess          # noqa: E402

SMOKE_JS = os.path.join(BASE_DIR, "tools", "smoke_conhost.js")

ADMIN_USER = "tester"
ADMIN_PASSWORD = "test-password-123"

CREATE_NO_WINDOW = 0x08000000


def find_electron():
    """依次找：项目里 desktop-lyrics 装的那份 -> PATH 上的 electron。"""
    candidates = [
        os.path.join(BASE_DIR, "desktop-lyrics", "node_modules", "electron",
                     "dist", "electron.exe"),
        os.path.join(BASE_DIR, "desktop-lyrics", "node_modules", "electron",
                     "dist", "electron"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return path
    import shutil
    return shutil.which("electron") or shutil.which("electron.exe")


def spawn_target_console():
    """
    起一个无窗口的控制台，让它打印标记并保持存活。

    返回 (进程, 标记文本)。

    ★ 三个都不能改的细节（都实测踩过）：

      1. **三个标准句柄都不能传**（不要 stdin/stdout/stderr 参数）。
         传了 stdout=DEVNULL，`cmd` 的输出就进了 NUL 而不是**它自己的控制台**，
         于是控制台缓冲区里一行都没有 —— 表现是「读取成功但 0 行」，
         而状态栏还是正常显示尺寸，非常像前端渲染坏了。
         不传句柄时 Windows 会把它们指向子进程新建的那个控制台，
         输出才会真的落进缓冲区（本功能读的就是那个缓冲区）。
         ★ 这一条同时也是功能的现实边界：真实桌面上那些
         `cmd /c "xxx.bat"` 包装进程往往把输出重定向走了，它们的控制台
         本来就是空的 —— 界面里看到空内容不一定是 bug。

      2. **stdin 绝不能接 DEVNULL。** `cmd /k` 从 stdin 读到 EOF 会立刻退出，
         于是我们拿到的是一个**已经死掉的 pid**；而 AttachConsole 对已退出的
         pid 返回的是 ERROR_ACCESS_DENIED(5)，和「权限不足」一模一样，
         能把人往错误方向带很久。

      3. 用 `CREATE_NO_WINDOW` 而不是 `CREATE_NEW_CONSOLE`：前者不弹窗，
         不会打扰正在用机器的人；而且**无窗口的控制台照样能被附加与读取**
         （实测证实），顺带覆盖了这条路径。
    """
    mark = "CONHOST-SMOKE-%d" % (os.getpid() * 1000 + int(time.time()) % 1000)
    proc = subprocess.Popen(
        ["cmd.exe", "/k", "echo %s" % mark],
        creationflags=CREATE_NO_WINDOW)
    # ↑ 刻意不传 stdin/stdout/stderr，见上面第 1、2 条
    return proc, mark


def main() -> int:
    if not sys.platform.startswith("win"):
        print("★ 控制台镜像只在 Windows 上有意义。")
        return 2

    # --input：额外打开 conhost.allow_input 并把「输入注入」那一组检查也跑掉。
    # 默认不跑，因为服务端默认就是只读的 —— 两种配置各跑一次才是完整覆盖。
    want_input = "--input" in sys.argv[1:]

    electron = find_electron()
    if not electron:
        print("★ 没有找到 Electron，无法做浏览器冒烟测试。")
        print("  装一个即可：cd desktop-lyrics && npm install（或把 electron 放进 PATH）")
        return 2

    work = tempfile.mkdtemp(prefix="fw-conhost-smoke-")
    root = os.path.join(work, "root")
    out_dir = os.path.join(work, "shots")
    os.makedirs(root, exist_ok=True)
    os.makedirs(out_dir, exist_ok=True)

    server = None
    target = None
    try:
        target, mark = spawn_target_console()
        time.sleep(1.2)
        print("目标控制台：pid=%d（无窗口），标记=%s" % (target.pid, mark))

        def extra(cfg):
            cfg["conhost"]["enabled"] = True
            # 只读那一轮把 allow_input 关掉（这是默认姿态，必须能纯只读）；
            # 带 --input 的那一轮才打开，用来验证注入链路。
            cfg["conhost"]["allow_input"] = bool(want_input)

        server = ServerProcess(
            [{"id": "r1", "name": "root", "path": root}],
            username=ADMIN_USER, password=ADMIN_PASSWORD,
            extra_config=extra).start()

        client = server.login_client()

        # 先确认接口这一侧真的能看见那个目标 —— 浏览器那边要是找不到，
        # 就能立刻区分「接口没枚举到」还是「前端没渲染」。
        status, data = client.json("GET", "/api/conhost/list")
        if status != 200:
            print("★ 列举控制台失败：%r %r" % (status, data))
            return 2
        pids = []
        for item in data.get("items") or []:
            pids.extend(m["pid"] for m in item.get("members") or [])
        print("接口枚举到 %d 个控制台；目标 pid 在里面：%s"
              % (data.get("count", 0), target.pid in pids))
        if target.pid not in pids:
            print("★ 目标控制台没有被枚举到，浏览器那边必然找不到。"
                  "先查 conhost.list_consoles 的聚合与过滤。")
            return 2

        env = dict(os.environ)
        env["CH_PORT"] = str(server.port)
        env["CH_COOKIE"] = client.cookie
        env["CH_OUT"] = out_dir
        env["CH_PID"] = str(target.pid)
        env["CH_MARK"] = mark
        env["CH_INPUT"] = "1" if want_input else "0"

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
        if target is not None and target.poll() is None:
            subprocess.run(["taskkill", "/PID", str(target.pid), "/T", "/F"],
                           capture_output=True)
        if server is not None:
            server.stop()
            server.cleanup()
        print("工作目录（含截图）：%s" % work)


if __name__ == "__main__":
    sys.exit(main())

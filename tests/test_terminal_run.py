# -*- coding: utf-8 -*-
"""
「在虚拟桌面里运行脚本 / 在此处打开命令行」的回归测试
====================================================

对应能力：
    资源管理器右键 .bat/.cmd/.ps1 →「运行」   → 新开命令行并执行它
    资源管理器右键文件夹        →「在此处打开命令行」→ 以该目录为 cwd 开命令行

钉住的点（都是这类功能容易悄悄退化的地方）：

    * **脚本真的被执行了** —— 用「脚本自己写一个文件」当证据，
      而不是只看接口返回 200（返回 200 只说明建了会话，不代表脚本跑了）；
    * **工作目录 = 脚本所在目录** —— 批处理里几乎都用相对路径引用同目录
      的文件（`java -jar server.jar`），cwd 不对就直接失败；
    * **只允许脚本扩展名** —— .exe 之类必须被拒（400）；
    * **必须过用户自己的路径解析器** —— 指向别人的根 / 不存在的文件要 403/404，
      不能因为「从终端这条路进来」就绕过闸门。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from tests._harness import ServerProcess                          # noqa: E402
from fileweb.routers.terminal import RUNNABLE_EXTS                 # noqa: E402

IS_WINDOWS = sys.platform.startswith("win")


@unittest.skipUnless(IS_WINDOWS, "运行 bat 是 Windows 特有行为")
class TerminalRunTests(unittest.TestCase):
    """起一个真实服务实例，走 HTTP。"""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-runbat-")
        cls.root = os.path.join(cls.work, "root")
        cls.bat_dir = os.path.join(cls.root, "mc server")
        os.makedirs(cls.bat_dir, exist_ok=True)

        # 脚本：把标记写进文件 + 顺手把当前目录也写进去（用来验证 cwd）
        cls.proof = os.path.join(cls.work, "proof.txt")
        cls.bat = os.path.join(cls.bat_dir, "启动 服务器.bat")
        with open(cls.bat, "w", encoding="gbk") as fh:
            fh.write("@echo off\r\n")
            fh.write("echo RAN-BY-VIRTUAL-DESKTOP> \"%s\"\r\n" % cls.proof)
            fh.write("cd >> \"%s\"\r\n" % cls.proof)
            fh.write("echo.\r\n")

        # 一个非脚本文件，用来验证扩展名拦截
        cls.exefile = os.path.join(cls.root, "木马.exe")
        with open(cls.exefile, "wb") as fh:
            fh.write(b"MZ")

        def extra(cfg):
            cfg["terminal"]["enabled"] = True       # harness 默认是关的

        cls.server = ServerProcess(
            [{"id": "r1", "name": "root", "path": cls.root}],
            extra_config=extra)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def _admin(self):
        client = self.server.client()
        status, data = client.login(self.server.username, self.server.password)
        self.assertEqual(status, 200, data)
        return client

    def _session(self, client, body):
        return client.json("POST", "/api/terminal/session", body)

    # -- 运行脚本 -----------------------------------------------------------

    def test_run_executes_the_script_in_its_own_folder(self):
        """
        ★ 核心用例：接口只负责**建会话**，真正跑脚本的是 cmd 的 /K。
        所以这里用「脚本自己写出来的文件」当证据 —— 只看 200 是自欺欺人。
        """
        admin = self._admin()
        if os.path.exists(self.proof):
            os.remove(self.proof)

        status, data = self._session(admin, {
            "run": {"root": "r1", "path": "mc server/启动 服务器.bat"},
        })
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("id"), data)
        # 会话的 cwd 必须是脚本所在目录（见下面那条用例的说明）
        self.assertEqual(os.path.normcase(os.path.normpath(data.get("cwd") or "")),
                         os.path.normcase(os.path.normpath(self.bat_dir)),
                         "cwd 应当是脚本所在目录：%s" % data.get("cwd"))

        # 等脚本跑完（启动 shell 本身要一点时间）
        deadline = time.time() + 20
        while time.time() < deadline and not os.path.exists(self.proof):
            time.sleep(0.25)

        self.assertTrue(os.path.exists(self.proof),
                        "脚本没有被执行：%s 不存在" % self.proof)
        with open(self.proof, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        self.assertIn("RAN-BY-VIRTUAL-DESKTOP", content)
        # 第二行是脚本里的 cd —— 它应当是脚本所在目录
        lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
        self.assertTrue(any(os.path.normcase(os.path.normpath(ln)) ==
                            os.path.normcase(os.path.normpath(self.bat_dir))
                            for ln in lines),
                        "脚本里的相对路径应当以脚本所在目录为基准，实际：%r" % lines)

        # 收尾：会话是要占名额的，用例之间不要互相影响
        self._close(admin, data["id"])

    def test_run_rejects_non_script_extensions(self):
        """只放脚本，不放 .exe —— 要跑别的程序请在命令行里输入。"""
        admin = self._admin()
        status, data = self._session(admin, {
            "run": {"root": "r1", "path": "木马.exe"},
        })
        self.assertEqual(status, 400, data)
        blob = str(data)
        self.assertTrue("脚本" in blob or "bat" in blob, blob)

    def test_run_missing_file_is_404(self):
        admin = self._admin()
        status, data = self._session(admin, {
            "run": {"root": "r1", "path": "不存在的.bat"},
        })
        self.assertEqual(status, 404, data)

    def test_run_outside_roots_is_forbidden(self):
        """
        ★ 路径闸门只有一道：即使从终端这条路进来，也必须过用户自己的解析器。
        （终端本身是全权限 shell，但接口层面不能出现「绕过解析器」的第二条路。）
        """
        admin = self._admin()
        # `..` 直接被解析器拒掉（403）—— 比"压平到根内再报文件不存在"更严格，
        # 也正是 security.PathResolver 的既有口径。
        status, data = self._session(admin, {
            "run": {"root": "r1", "path": "../../外面.bat"},
        })
        self.assertEqual(status, 403, data)
        self.assertIn("..", str(data))

        # 拿别人的根标识来访问也必须被拒
        status, data = self._session(admin, {
            "run": {"root": "no-such-root", "path": "x.bat"},
        })
        self.assertEqual(status, 403, data)

    # -- 在此处打开命令行 ---------------------------------------------------

    def test_start_dir_sets_the_working_directory(self):
        """★ 「在此处打开命令行」的全部意义就是 cwd 对 —— 所以直接断言 cwd。"""
        admin = self._admin()
        status, data = self._session(admin, {
            "start_dir": {"root": "r1", "path": "mc server"},
        })
        self.assertEqual(status, 200, data)
        self.assertEqual(os.path.normcase(os.path.normpath(data.get("cwd") or "")),
                         os.path.normcase(os.path.normpath(self.bat_dir)),
                         "cwd 应当是请求里那个目录：%s" % data.get("cwd"))
        self._close(admin, data["id"])

    def test_start_dir_missing_folder_is_404(self):
        admin = self._admin()
        status, data = self._session(admin, {
            "start_dir": {"root": "r1", "path": "没有这个目录"},
        })
        self.assertEqual(status, 404, data)

    def test_plain_session_still_works(self):
        """★ 反向守卫：不带任何可选字段时，行为必须与改造前一模一样。"""
        admin = self._admin()
        status, data = self._session(admin, {})
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("id"))
        self._close(admin, data["id"])

    # -- 一致性 -------------------------------------------------------------

    def test_runnable_ext_constant_is_the_documented_one(self):
        """前后端各有一份扩展名清单，这里钉住后端那份的内容。"""
        self.assertEqual(RUNNABLE_EXTS, {".bat", ".cmd", ".ps1"})

    def _close(self, client, sid):
        """结束会话，别让它占着名额影响后面的用例。"""
        try:
            client.json("POST", "/api/terminal/session/close", {"sid": sid})
        except Exception:                                       # noqa: BLE001
            pass


if __name__ == "__main__":
    unittest.main()

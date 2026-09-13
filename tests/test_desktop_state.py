# -*- coding: utf-8 -*-
"""
界面状态接口（/api/desktop/state）测试
=====================================

只用标准库 + 测试脚手架（真实 app.py 子进程）。

覆盖的关键行为：
    * GET / PUT 都能正常工作，状态原样往返
    * 未登录访问返回 401；PUT 必须带 CSRF 令牌（403），与相邻接口口径一致
    * 错误响应是项目统一的 {"ok": false, "message": ...} 信封
    * 状态**落盘**：服务重启后仍然拿得到（这才是「关掉浏览器再打开」的前提）
    * 体积超限返回 413 而不是静默截断
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from tests._harness import Client, ServerProcess

from fileweb import config as config_module


class DesktopStateApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-dstate-")
        cls.root = os.path.join(cls.work, "root")
        os.makedirs(cls.root, exist_ok=True)

        # 状态文件固定落在临时目录里，保证测试不碰项目根目录的真实状态
        cls.state_path = os.path.join(cls.work, "user_state.json")

        def _extra(cfg):
            cfg["user_state_path"] = cls.state_path

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            extra_config=_extra,
        ).start()
        cls.client = cls.server.login_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    def setUp(self):
        """每个用例从「没有状态」开始，避免用例之间互相干扰。"""
        try:
            os.unlink(self.state_path)
        except OSError:
            pass

    # -- 工具 ---------------------------------------------------------------

    def _state_file_exists(self) -> bool:
        return os.path.isfile(self.state_path)

    def _read_state_file(self):
        with open(self.state_path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)

    # -- 测试 ---------------------------------------------------------------

    def test_get_requires_login(self):
        """桌面状态属于「用户自己的桌面」，未登录绝不能读。"""
        status, data = Client(self.server.port).json("GET", "/api/desktop/state")
        self.assertEqual(status, 401, data)
        self.assertFalse(data.get("ok"), data)

    def test_get_returns_empty_state_when_never_saved(self):
        """没存过 = 空对象，而不是 404：前端据此走默认布局。"""
        status, data = self.client.json("GET", "/api/desktop/state")
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)
        self.assertEqual(data.get("state"), {}, data)

    def test_put_then_get_roundtrip(self):
        """核心往返：存进去的布局必须原样取回来。"""
        payload = {
            "version": 1,
            "windows": [
                {"id": "win_1", "title": "项目资料", "x": 30, "y": 40, "w": 900, "h": 620},
                {"id": "win_7", "cwd": {"root": "main", "path": "子目录"}},
            ],
            "active": "win_7",
            "view": "list",
        }

        status, data = self.client.json("PUT", "/api/desktop/state", {"state": payload})
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("ok"), data)

        status, data = self.client.json("GET", "/api/desktop/state")
        self.assertEqual(status, 200, data)
        self.assertEqual(data.get("state"), payload)

    def test_put_requires_csrf(self):
        """PUT 是改状态请求，缺 CSRF 令牌必须被拒（403）。"""
        status, data = self.client.json(
            "PUT", "/api/desktop/state", {"state": {"view": "icons"}}, with_csrf=False,
        )
        self.assertEqual(status, 403, data)
        self.assertFalse(data.get("ok"), data)
        self.assertFalse(self._state_file_exists(), "被拒绝的请求不该写入文件")

    def test_put_requires_login(self):
        """未登录的 PUT 直接 401，连 CSRF 那一步都到不了。"""
        status, data = Client(self.server.port).json(
            "PUT", "/api/desktop/state", {"state": {"view": "icons"}},
        )
        self.assertEqual(status, 401, data)
        self.assertFalse(data.get("ok"), data)

    def test_put_without_state_field_is_rejected(self):
        """缺少 state 字段要给出中文错误信封，而不是 500。"""
        status, data = self.client.json("PUT", "/api/desktop/state", {})
        self.assertEqual(status, 400, data)
        self.assertFalse(data.get("ok"), data)
        self.assertIn("state", str(data.get("message")), data)

    def test_put_non_object_state_is_rejected(self):
        """状态必须是对象：客户端不能拿这个接口写任意 JSON 结构。"""
        for bad in ([1, 2, 3], "just-text", 42):
            status, data = self.client.json("PUT", "/api/desktop/state", {"state": bad})
            self.assertEqual(status, 400, "state=%r 应当被拒绝" % (bad,))
            self.assertFalse(data.get("ok"), data)

    def test_put_oversized_state_returns_413(self):
        """超过体积上限必须明确报错，避免客户端把磁盘写满。"""
        huge = {"blob": "x" * (400 * 1024)}      # 400KB > 256KB 上限
        status, data = self.client.json("PUT", "/api/desktop/state", {"state": huge})
        self.assertEqual(status, 413, data)
        self.assertFalse(data.get("ok"), data)

    def test_state_is_persisted_to_disk(self):
        """状态必须真的落盘，而不是只放在内存里。"""
        payload = {"view": "icons", "active": "win_3"}
        status, _ = self.client.json("PUT", "/api/desktop/state", {"state": payload})
        self.assertEqual(status, 200)

        self.assertTrue(self._state_file_exists(), "状态接口应当把状态写到配置文件指定的路径")
        self.assertEqual(self._read_state_file(), payload)

    def test_state_survives_server_restart(self):
        """
        最关键的一条：**服务重启后状态还在**。

        这条同时证明了「状态不是内存态」以及「路径配置真的生效」——
        如果路径没被 config.prepare() 下发，文件会写到项目根目录，
        重启后新进程仍能读到（因为它读的是同一个默认路径），
        所以这里额外断言文件确实在**配置指定的临时目录**里。
        """
        payload = {"view": "list", "windows": [{"id": "win_9", "x": 11}]}
        status, _ = self.client.json("PUT", "/api/desktop/state", {"state": payload})
        self.assertEqual(status, 200)
        self.assertTrue(self._state_file_exists())

        # 换一个端口、用同一份配置路径重启服务
        def _extra(cfg):
            cfg["user_state_path"] = self.state_path

        other = ServerProcess(
            [{"id": "main", "name": "main", "path": self.root, "readonly": False}],
            extra_config=_extra,
        )
        try:
            other.start()
            fresh = other.login_client()
            status, data = fresh.json("GET", "/api/desktop/state")
            self.assertEqual(status, 200, data)
            self.assertEqual(data.get("state"), payload,
                             "重启后必须还能读到上次保存的布局")
        finally:
            other.cleanup()


class RealDeploymentStateIsolationTests(unittest.TestCase):
    """
    ★ 回归测试：**测试用的服务器绝不能碰真实部署的 user_state.json**。

    背景（本项目真实发生过）：
        状态文件的默认位置在**项目根目录**（与真实 config.json 同一个目录），
        而测试脚手架以前不覆盖 user_state_path，于是「拿临时配置跑起来的服务器」
        把真实部署的桌面布局覆盖成了测试数据 —— 项目根目录的 user_state.json
        里出现了一个指向临时测试目录（Temp\\sstest\\root）的命令行窗口，
        用户下次打开桌面就会看到这个脏窗口。

        这类事故和 FIRST_RUN_PASSWORD.txt 被覆盖是同一条教训：
        「默认路径只锚在代码目录上」的配置项，一定要在换配置文件时跟着走。

    断言刻意做得**抗并发**：用户的真实服务器（端口 8000）随时可能写入这个文件，
    所以不比较 mtime / 整体内容，而是直接断言「我们这段测试数据没有出现在
    真实文件里」—— 这才是真正要保证的性质，也不会被无关的并发写入弄成偶发失败。
    """

    REAL_PATH = os.path.join(config_module.BASE_DIR, "user_state.json")
    PROBE = "win_isolation_probe"

    def test_test_server_keeps_state_in_its_own_temp_dir(self):
        work = tempfile.mkdtemp(prefix="fw-isolation-")
        root = os.path.join(work, "root")
        os.makedirs(root, exist_ok=True)
        existed_before = os.path.isfile(self.REAL_PATH)

        # 刻意**不设置** user_state_path：脚手架必须自己把它指向临时目录，
        # 这正是本次修复要钉住的地方。
        server = ServerProcess(
            [{"id": "main", "name": "main", "path": root, "readonly": False}],
        )
        try:
            server.start()
            client = server.login_client()
            payload = {"view": "icons", "windows": [{"id": self.PROBE, "x": 7, "y": 9}]}

            status, data = client.json("PUT", "/api/desktop/state", {"state": payload})
            self.assertEqual(status, 200, data)

            # 1) 状态应当落在**脚手架自己的临时目录**里（配置文件的旁边）
            own = os.path.join(server.work, "user_state.json")
            self.assertTrue(
                os.path.isfile(own),
                "测试服务器的状态文件应当落在它自己的临时目录里：%s" % own,
            )
            with open(own, "r", encoding="utf-8-sig") as fh:
                self.assertEqual(json.load(fh), payload)

            # 2) 真实部署那份里**绝不能出现**我们的测试数据
            if os.path.isfile(self.REAL_PATH):
                with open(self.REAL_PATH, "r", encoding="utf-8-sig") as fh:
                    real = fh.read()
                self.assertNotIn(
                    self.PROBE, real,
                    "测试数据泄漏进了真实部署的 user_state.json：%s" % self.REAL_PATH,
                )
            else:
                self.assertFalse(
                    os.path.exists(self.REAL_PATH),
                    "本来不存在真实状态文件，测试却把它创建出来了：%s" % self.REAL_PATH,
                )
        finally:
            server.cleanup()
            shutil.rmtree(work, ignore_errors=True)

        # 3) 兜底：整个用例跑完后，真实部署那份依然不含我们的测试数据
        if existed_before and os.path.isfile(self.REAL_PATH):
            with open(self.REAL_PATH, "r", encoding="utf-8-sig") as fh:
                self.assertNotIn(self.PROBE, fh.read())


if __name__ == "__main__":
    unittest.main(verbosity=2)

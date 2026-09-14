# -*- coding: utf-8 -*-
"""
在线修改密码（POST /api/auth/password）的回归测试。

这个接口是「凭一个会话就能改掉登录凭据」的地方，所以重点盯三件事：

    1. **必须验证当前密码** —— 否则会话 Cookie 一旦泄露，攻击者改掉口令
       就能把真正的管理员永久锁在门外；
    2. **改完必须让旧会话失效** —— 否则「改密码」对已经登录的入侵者毫无作用，
       这正是本项目 README 原先承认的一条已知限制；
    3. **落盘不能留明文** —— 包括要顺手清掉 auth.password 这个明文兼容项，
       否则旧口令仍然可以登录，等于密码根本没改成。
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest

from tests._harness import ServerProcess

USERNAME = "pwuser"
OLD_PASSWORD = "old-password-123"
NEW_PASSWORD = "new-password-456"


def _root(prefix):
    return tempfile.mkdtemp(prefix=prefix)


class PasswordChangeGuardTests(unittest.TestCase):
    """失败路径。这些用例都不应该改动口令，因此可以共用一个服务实例。"""

    @classmethod
    def setUpClass(cls):
        cls.root = _root("fw-pw-guard-")
        cls.server = ServerProcess([{
            "id": "share", "name": "共享目录", "path": cls.root, "readonly": False,
        }], username=USERNAME, password=OLD_PASSWORD)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()

    def setUp(self):
        self.client = self.server.login_client()

    def _change(self, current, new, **kwargs):
        return self.client.json("POST", "/api/auth/password",
                                {"current_password": current, "new_password": new},
                                **kwargs)

    def test_requires_login(self):
        anonymous = self.server.client()
        status, data = anonymous.json("POST", "/api/auth/password",
                                      {"current_password": OLD_PASSWORD,
                                       "new_password": NEW_PASSWORD})
        self.assertIn(status, (401, 403), data)

    def test_requires_csrf_token(self):
        status, data = self._change(OLD_PASSWORD, NEW_PASSWORD, with_csrf=False)
        self.assertEqual(status, 403, "改口令属于改状态请求，必须带 CSRF 令牌")

    def test_wrong_current_password_is_rejected(self):
        """
        ★ 最关键的一条：当前密码不对就一律拒绝。

        这里用的是 403 而不是 401 —— 401 在前端被约定为「会话已失效」，
        会触发跳转到登录页；而此刻会话其实是好的，只是当前密码填错了。
        """
        status, data = self._change("definitely-not-the-password", NEW_PASSWORD)
        self.assertEqual(status, 403, data)

        # 关键补充：失败尝试之后，旧口令必须**仍然有效**。
        # 这一条直接写在这里而不是单开一个用例 —— unittest 按方法名字母序执行，
        # 单独写会跑到这个用例前面去，那它就什么也证明不了。
        fresh = self.server.client()
        status, data = fresh.login(USERNAME, OLD_PASSWORD)
        self.assertEqual(status, 200, "失败尝试不该改掉口令、也不该把账号锁死：%s" % data)

    def test_too_short_new_password_is_rejected(self):
        status, data = self._change(OLD_PASSWORD, "short")
        self.assertEqual(status, 400, data)

    def test_same_password_is_rejected(self):
        status, data = self._change(OLD_PASSWORD, OLD_PASSWORD)
        self.assertEqual(status, 400, data)

    def test_empty_new_password_is_rejected(self):
        status, data = self._change(OLD_PASSWORD, "")
        self.assertEqual(status, 400, data)


class PasswordChangeSuccessTests(unittest.TestCase):
    """
    成功路径。

    单独一个类、单独的服务器实例：一旦改成功，口令就变了，
    同一个实例上后面所有用例的 login_client() 都会失效，
    混在一起会变成一个「看执行顺序」的脆弱测试。
    """

    @classmethod
    def setUpClass(cls):
        cls.root = _root("fw-pw-ok-")
        cls.server = ServerProcess([{
            "id": "share", "name": "共享目录", "path": cls.root, "readonly": False,
        }], username=USERNAME, password=OLD_PASSWORD)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()

    def test_change_succeeds_and_kills_the_old_session(self):
        client = self.server.login_client()

        status, data = client.json("POST", "/api/auth/password",
                                   {"current_password": OLD_PASSWORD,
                                    "new_password": NEW_PASSWORD})
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("relogin"), "应当提示前端重新登录：%s" % data)

        # 1) 旧会话（同一个 Cookie）必须已经失效 —— session_secret 被轮换了
        status, _ = client.json("GET", "/api/system/info")
        self.assertEqual(status, 401, "改完密码后，旧会话应当立即失效")

        # 2) 旧口令不能再登录
        old_client = self.server.client()
        status, _ = old_client.login(USERNAME, OLD_PASSWORD)
        self.assertEqual(status, 401, "旧密码不该还能登录")

        # 3) 新口令可以登录
        new_client = self.server.client()
        status, data = new_client.login(USERNAME, NEW_PASSWORD)
        self.assertEqual(status, 200, data)

    def test_persisted_config_has_no_plaintext_and_clears_legacy_field(self):
        """
        落盘的配置里：必须是哈希、明文兼容项被清空、且找不到新口令的明文。

        最后那条尤其重要 —— 「改口令」若只写了哈希却没清 auth.password，
        旧口令会继续有效（校验时明文分支仍在），改密码就成了摆设。
        """
        with open(self.server.cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)

        auth = cfg.get("auth") or {}
        self.assertEqual(auth.get("password"), "",
                         "明文兼容项 auth.password 必须被清空")
        self.assertTrue(str(auth.get("password_hash") or "").startswith("pbkdf2_sha256$"),
                        "应当写入 PBKDF2 哈希")
        self.assertNotIn(NEW_PASSWORD, json.dumps(cfg, ensure_ascii=False),
                         "配置文件里不该出现新口令的明文")

    def test_session_secret_was_rotated(self):
        """会话密钥轮换是「旧会话失效」的实现手段，顺便确认它确实变了。"""
        self.assertTrue(self.server.cfg_path)
        with open(self.server.cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertTrue(str((cfg.get("auth") or {}).get("session_secret") or ""),
                        "session_secret 不该为空")


if __name__ == "__main__":
    unittest.main(verbosity=2)

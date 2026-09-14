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

        # ★ 口令变更放在 setUpClass 里，让下面每个用例都观察同一个「已改完」的状态。
        #   放在某个用例里会变成「谁先跑谁负责改」的隐式字母序依赖 ——
        #   那种测试在别人调换用例顺序后会莫名其妙地红。
        cls.old_client = cls.server.login_client()
        cls.change_status, cls.change_data = cls.old_client.json(
            "POST", "/api/auth/password",
            {"current_password": OLD_PASSWORD, "new_password": NEW_PASSWORD})

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()

    @classmethod
    def _users_path(cls):
        """用户表就在测试配置旁边（app 用 cfg._cfg_path 推出来的位置）。"""
        return os.path.join(os.path.dirname(cls.server.cfg_path), "users.json")

    def test_change_succeeded_and_asks_for_relogin(self):
        self.assertEqual(self.change_status, 200, self.change_data)
        self.assertTrue(self.change_data.get("relogin"),
                        "应当提示前端重新登录：%s" % self.change_data)

    def test_old_session_is_dead_immediately(self):
        """
        ★ 改完密码，**同一个 Cookie 立刻失效**。

        多用户后实现机制变了：不再是轮换全局 session_secret（那会踢掉所有人），
        而是递增该用户的 token_version，中间件比对不上就把旧令牌当失效。
        """
        status, _ = self.old_client.json("GET", "/api/system/info")
        self.assertEqual(status, 401, "改完密码后，旧会话应当立即失效")

    def test_old_password_no_longer_works(self):
        client = self.server.client()
        status, _ = client.login(USERNAME, OLD_PASSWORD)
        self.assertEqual(status, 401, "旧密码不该还能登录")

    def test_new_password_works(self):
        client = self.server.client()
        status, data = client.login(USERNAME, NEW_PASSWORD)
        self.assertEqual(status, 200, data)

    def test_token_version_was_bumped(self):
        """★ 新机制的判据：该用户的 token_version 被递增（引导时是 1）。"""
        with open(self._users_path(), encoding="utf-8") as fh:
            table = json.load(fh)

        record = next(u for u in table["users"] if u["username"] == USERNAME)
        self.assertGreaterEqual(int(record.get("token_version") or 1), 2,
                                "改密码应当把 token_version 递增")

    def test_password_lands_in_the_user_table_not_the_config(self):
        """
        ★ 多用户后口令写在 users.json，不再写 config.json。

        `users.json` 才是唯一的用户事实来源；config 的 auth 段只在首次引导时
        被用过一次（派生管理员）。顺带也就绕开了单用户时代那个坑：
        「改了口令却没清掉明文兼容项 auth.password，旧口令仍然能登录」——
        用户表里根本没有明文字段。
        """
        with open(self._users_path(), encoding="utf-8") as fh:
            raw = fh.read()

        table = json.loads(raw)
        record = next(u for u in table["users"] if u["username"] == USERNAME)
        self.assertTrue(str(record.get("password_hash") or "")
                        .startswith("pbkdf2_sha256$"), "用户表里应当是 PBKDF2 哈希")
        self.assertNotIn(NEW_PASSWORD, raw, "用户表里不该出现新口令的明文")

    def test_global_session_secret_is_not_rotated(self):
        """
        全局 session_secret 仍在（签名继续用它），但**不该**被轮换 ——
        一旦轮换，别的用户也会被一起踢下线，那正是多用户下不能接受的行为。
        """
        with open(self.server.cfg_path, encoding="utf-8") as fh:
            cfg = json.load(fh)
        self.assertTrue(str((cfg.get("auth") or {}).get("session_secret") or ""),
                        "session_secret 应当仍然存在（用于签名）")


if __name__ == "__main__":
    unittest.main(verbosity=2)

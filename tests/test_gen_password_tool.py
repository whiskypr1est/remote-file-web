# -*- coding: utf-8 -*-
"""
口令工具 tools/gen_password.py 的回归测试
=========================================

这个工具在多用户改造后**必须改用户表（users.json）**，不能再只改
config.json 的 auth 段 —— 那份只在「还没有用户表」时被用来派生第一个管理员，
之后不参与登录。

改错的症状很恶劣：工具打印「修改成功」，但口令根本没变。
用户会以为自己记错了密码，反复重试、最后以为服务坏了。
所以这里把「改完真的能登录」当成硬断言。

另外两条同样重要（都属于本项目反复出过的那类事故）：
    * 用 --config 指向别的实例时，**绝不能**碰真实部署的 config.json /
      users.json / FIRST_RUN_PASSWORD.txt；
    * 口令提示文件要跟着 --config 走，而不是写死到代码目录。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest

from fileweb import config as config_module
from fileweb import users as users_module

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join("tools", "gen_password.py")

REAL_CONFIG = os.path.join(BASE_DIR, "config.json")
REAL_USERS = os.path.join(BASE_DIR, "users.json")
REAL_HINT = os.path.join(BASE_DIR, "FIRST_RUN_PASSWORD.txt")


def _hash_of(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            import hashlib
            return hashlib.sha256(fh.read()).hexdigest()
    except OSError:
        return ""


def _run(*args):
    """跑一次工具，返回 (returncode, 合并后的输出)。"""
    proc = subprocess.run(
        [sys.executable, TOOL] + list(args),
        cwd=BASE_DIR, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


class GenPasswordToolTests(unittest.TestCase):

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-genpw-")
        self.cfg_path = os.path.join(self.work, "config.json")
        self.users_path = os.path.join(self.work, "users.json")

        # 真实文件的指纹：任何一条用例结束都要核对它们没被动过
        self._real = (_hash_of(REAL_CONFIG), _hash_of(REAL_USERS),
                      _hash_of(REAL_HINT),
                      os.path.exists(REAL_HINT))

        self._saved_users_path = users_module.USERS_PATH

    def tearDown(self):
        users_module.set_path(self._saved_users_path)
        shutil.rmtree(self.work, ignore_errors=True)

        self.assertEqual(
            _hash_of(REAL_CONFIG), self._real[0],
            "★ 真实 config.json 被改动了！")
        self.assertEqual(
            _hash_of(REAL_USERS), self._real[1],
            "★ 真实 users.json 被改动了！")
        self.assertEqual(
            _hash_of(REAL_HINT), self._real[2],
            "★ 真实 FIRST_RUN_PASSWORD.txt 被改动了！")
        self.assertEqual(
            os.path.exists(REAL_HINT), self._real[3],
            "★ 真实 FIRST_RUN_PASSWORD.txt 的存在状态被改动了！")

    # -- 工具 ---------------------------------------------------------------

    def _authenticate(self, username, password):
        """用工具改过的用户表试一次登录（不碰真实用户表）。"""
        users_module.set_path(self.users_path)
        try:
            record, reason = users_module.authenticate(username, password)
            return record, reason
        finally:
            users_module.set_path(self._saved_users_path)

    def _prepare_config(self, password="original-pass-123"):
        """在临时目录里放一份配置（模拟一台已部署的机器）。"""
        cfg = config_module.load(self.cfg_path)
        cfg["auth"]["username"] = "admin"
        cfg["auth"]["password_hash"] = \
            __import__("fileweb.security", fromlist=["x"]).hash_password(password)
        cfg["auth"]["password"] = ""
        config_module.save(cfg, self.cfg_path)
        return cfg

    # -- ★ 核心：改完必须真的能登录 -----------------------------------------

    def test_set_changes_the_login_password_in_the_users_table(self):
        """
        ★ 这是本文件存在的理由：改口令之后**必须真的能登录**。

        改造前这个工具只写 config.json 的 auth 段，而登录早就改成查
        users.json 了 —— 于是它打印「修改成功」却什么也没改。
        """
        self._prepare_config(password="old-password-123")

        code, out = _run("--set", "--config", self.cfg_path,
                         "--password", "new-password-456")
        self.assertEqual(code, 0, out)
        self.assertIn("修改成功", out)

        # 新口令能登录
        record, reason = self._authenticate("admin", "new-password-456")
        self.assertIsNotNone(record, "★ 工具说改成功了，新口令却登录不上：%s" % reason)

        # 旧口令不能登录
        record, _ = self._authenticate("admin", "old-password-123")
        self.assertIsNone(record, "★ 旧口令仍然能登录，等于没改")

    def test_set_works_on_a_machine_that_never_ran_the_service(self):
        """还没有用户表时应当先引导出管理员（与启动时的行为一致）。"""
        self.assertFalse(os.path.isfile(self.users_path))

        code, out = _run("--set", "--config", self.cfg_path,
                         "--password", "fresh-pass-1234")
        self.assertEqual(code, 0, out)

        self.assertTrue(os.path.isfile(self.users_path),
                        "应当自动创建用户表：%s" % out)
        record, reason = self._authenticate("admin", "fresh-pass-1234")
        self.assertIsNotNone(record, reason)
        self.assertEqual(record["role"], users_module.ROLE_ADMIN)

    def test_set_bumps_token_version_so_old_sessions_die(self):
        """改口令要顺带把该账号已登录的会话踢掉（口令泄露后重置的要点）。"""
        self._prepare_config()
        _run("--set", "--config", self.cfg_path, "--password", "first-change-1")

        users_module.set_path(self.users_path)
        before = (users_module.get("admin") or {}).get("token_version")
        users_module.set_path(self._saved_users_path)

        _run("--set", "--config", self.cfg_path, "--password", "second-change-2")

        users_module.set_path(self.users_path)
        after = (users_module.get("admin") or {}).get("token_version")
        users_module.set_path(self._saved_users_path)

        self.assertGreater(after, before,
                           "★ 改口令必须递增 token_version，否则旧会话仍然有效")

    def test_set_targets_a_named_account(self):
        self._prepare_config()
        users_module.set_path(self.users_path)
        try:
            users_module.ensure_bootstrap(config_module.load(self.cfg_path))
            users_module.create("stu01", "student-old-1",
                                role=users_module.ROLE_USER)
        finally:
            users_module.set_path(self._saved_users_path)

        code, out = _run("--set", "--config", self.cfg_path,
                         "--username", "stu01", "--password", "student-new-1")
        self.assertEqual(code, 0, out)

        record, reason = self._authenticate("stu01", "student-new-1")
        self.assertIsNotNone(record, reason)
        self.assertEqual(record["role"], users_module.ROLE_USER,
                         "给普通用户改口令不该顺手把他变成管理员")

    def test_set_with_a_new_username_creates_an_admin_and_says_so(self):
        """
        用户名是归属与审计的键，**不支持改名**；所以「换个名字」= 新建账号。
        旧账号必须仍然存在，而且工具要明确说出来（否则用户会以为已经换掉了）。
        """
        self._prepare_config()
        code, out = _run("--set", "--config", self.cfg_path,
                         "--username", "boss", "--password", "boss-pass-1234")
        self.assertEqual(code, 0, out)
        self.assertIn("新建", out)
        self.assertIn("仍然存在", out, "必须提醒旧账号还在：%s" % out)

        record, reason = self._authenticate("boss", "boss-pass-1234")
        self.assertIsNotNone(record, reason)
        self.assertEqual(record["role"], users_module.ROLE_ADMIN)

    def test_short_password_is_rejected_without_changing_anything(self):
        self._prepare_config()
        _run("--set", "--config", self.cfg_path, "--password", "good-pass-1234")
        stamp = _hash_of(self.users_path)

        code, out = _run("--set", "--config", self.cfg_path, "--password", "short")
        self.assertEqual(code, 1, out)
        self.assertEqual(_hash_of(self.users_path), stamp,
                         "口令太短时不该动用户表")

    # -- 提示信息与副作用 ---------------------------------------------------

    def test_tool_does_not_claim_a_restart_is_needed(self):
        """
        用户表是每个请求现读的，改口令**立即生效**。

        原先的提示是「需要重启后才会读取新口令」—— 在改 config.json 的年代
        是对的，但现在会让人白重启一次服务，而且会以为「没生效是还没重启」。
        """
        self._prepare_config()
        code, out = _run("--set", "--config", self.cfg_path,
                         "--password", "immediate-1234")
        self.assertEqual(code, 0, out)
        self.assertIn("不需要重启", out, out)
        self.assertNotIn("正在运行的服务需要重启", out, out)

    def test_hint_file_is_written_beside_the_given_config(self):
        """提示文件必须跟着 --config 走，不能写死到代码目录。"""
        self._prepare_config()
        code, out = _run("--set", "--config", self.cfg_path,
                         "--password", "hint-check-1234")
        self.assertEqual(code, 0, out)

        expected = os.path.join(self.work, "FIRST_RUN_PASSWORD.txt")
        self.assertTrue(os.path.isfile(expected),
                        "提示文件应当落在临时目录里：%s" % out)
        with open(expected, "r", encoding="utf-8") as fh:
            self.assertIn("hint-check-1234", fh.read())

    def test_list_shows_accounts_without_leaking_hashes(self):
        self._prepare_config()
        users_module.set_path(self.users_path)
        try:
            users_module.ensure_bootstrap(config_module.load(self.cfg_path))
            users_module.create("stu01", "student-pass-1",
                                display_name="甲同学",
                                role=users_module.ROLE_USER)
            users_module.set_enabled("stu01", False)
        finally:
            users_module.set_path(self._saved_users_path)

        code, out = _run("--list", "--config", self.cfg_path)
        self.assertEqual(code, 0, out)
        self.assertIn("admin", out)
        self.assertIn("stu01", out)
        self.assertIn("甲同学", out)
        self.assertIn("停用", out)
        self.assertNotIn("pbkdf2", out.lower(),
                         "★ 列表绝不能打印口令哈希")

    def test_generate_writes_nothing(self):
        code, out = _run()
        self.assertEqual(code, 0, out)
        self.assertIn("未修改任何文件", out)
        self.assertEqual(sorted(os.listdir(self.work)), [],
                         "纯生成不该产生任何文件")


if __name__ == "__main__":
    unittest.main(verbosity=2)

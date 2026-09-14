# -*- coding: utf-8 -*-
"""
用户存储（fileweb/users.py）的回归测试。

★ 第一件事：**每个用例都把用户表指到临时目录**。
  项目刚出过一次「测试把真实 config.json 覆盖掉」的事故（见
  test_config_persist.py），用户表同理 —— 它一旦被测试写脏，真实部署就
  没人能登录了。所以 setUp 一定改 USERS_PATH，tearDown 再改回去，
  并且专门有一条用例断言「真实那份没被动过」。

第二件事：**引导（bootstrap）必须让老部署行为不变**。
  老配置里只有 config.json 的 auth.username / password_hash，
  迁移后它要成为唯一的管理员，口令继续有效、仍然能看全机。
  这条是「267 条既有测试不用改」的前提。
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import unittest

from fileweb import users as users_module
from fileweb.security import hash_password


class UserStoreTestCase(unittest.TestCase):
    """公共脚手架：把用户表指到临时目录，并保护真实文件。"""

    def setUp(self):
        self._original_path = users_module.USERS_PATH
        self.work = tempfile.mkdtemp(prefix="fw-users-")
        self.path = os.path.join(self.work, "users.json")
        users_module.set_path(self.path)

    def tearDown(self):
        users_module.set_path(self._original_path)

    def _write_raw(self, payload) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            if isinstance(payload, str):
                fh.write(payload)
            else:
                json.dump(payload, fh, ensure_ascii=False)


class BootstrapTests(UserStoreTestCase):
    """从 config.json 的 auth 段迁移出管理员。"""

    LEGACY_CFG = {
        "auth": {
            "username": "admin",
            "password_hash": hash_password("old-password-123"),
        }
    }

    def test_creates_admin_from_legacy_config(self):
        result = users_module.ensure_bootstrap(self.LEGACY_CFG)
        self.assertTrue(result["created"])
        self.assertEqual(result["username"], "admin")
        self.assertEqual(result["warning"], "")

        admin = users_module.get("admin")
        self.assertIsNotNone(admin)
        self.assertEqual(admin["role"], "admin")
        self.assertTrue(admin["enabled"])
        # roots 为空 = 走 mount_all_drives，管理员仍然看得到全机
        self.assertEqual(admin["roots"], [])
        self.assertTrue(users_module.has_admin())

    def test_legacy_password_still_works(self):
        """★ 升级后老口令必须继续可用 —— 否则等于把管理员锁在门外。"""
        users_module.ensure_bootstrap(self.LEGACY_CFG)
        user, reason = users_module.authenticate("admin", "old-password-123")
        self.assertIsNotNone(user, reason)
        self.assertEqual(user["username"], "admin")

        bad, reason = users_module.authenticate("admin", "wrong-password")
        self.assertIsNone(bad)
        self.assertIn("用户名或密码", reason)

    def test_plaintext_legacy_password_is_hashed_on_the_way_in(self):
        """config 里只写了明文兼容项时，迁移时要顺手转成哈希。"""
        users_module.ensure_bootstrap({"auth": {"username": "boss", "password": "plain-pass-1"}})
        admin = users_module.get("boss")
        self.assertTrue(admin["password_hash"].startswith("pbkdf2_sha256$"))
        self.assertNotIn("plain-pass-1", json.dumps(admin, ensure_ascii=False))
        self.assertIsNotNone(users_module.authenticate("boss", "plain-pass-1")[0])

    def test_is_idempotent_and_never_overwrites(self):
        users_module.ensure_bootstrap(self.LEGACY_CFG)
        users_module.create("student1", "student-pass-1")

        # 再跑一次引导：不能把已有用户表冲掉
        result = users_module.ensure_bootstrap(self.LEGACY_CFG)
        self.assertFalse(result["created"])
        self.assertEqual(users_module.count(), 2)
        self.assertIsNotNone(users_module.get("student1"))

    def test_warns_loudly_when_no_enabled_admin_exists(self):
        """全员停用/全是普通用户时必须给告警，而不是静默把谁提权。"""
        users_module.create("student1", "student-pass-1")
        result = users_module.ensure_bootstrap(self.LEGACY_CFG)
        self.assertFalse(result["created"])
        self.assertIn("没有启用中的管理员", result["warning"])
        # 关键：没有偷偷造管理员、也没有偷偷提权
        self.assertEqual(users_module.count(), 1)
        self.assertEqual(users_module.get("student1")["role"], "user")

    def test_illegal_legacy_username_falls_back_instead_of_crashing(self):
        users_module.ensure_bootstrap({"auth": {"username": "导师", "password": "x-1234567"}})
        self.assertIsNotNone(users_module.get("admin"))


class UserCrudTests(UserStoreTestCase):

    def setUp(self):
        super().setUp()
        users_module.ensure_bootstrap({"auth": {"username": "admin",
                                                "password_hash": hash_password("admin-pass-1")}})

    def test_create_and_authenticate(self):
        users_module.create("student1", "student-pass-1", display_name="学生一",
                            roots=[{"id": "private", "name": "我的空间",
                                    "path": "D:/Lab/student1"}])
        user = users_module.get("student1")
        self.assertEqual(user["display_name"], "学生一")
        self.assertEqual(user["role"], "user")
        self.assertEqual(len(user["roots"]), 1)
        self.assertFalse(user["roots"][0]["readonly"])
        self.assertIsNotNone(users_module.authenticate("student1", "student-pass-1")[0])

    def test_username_is_case_insensitive_but_stored_as_given(self):
        users_module.create("StudentX", "student-pass-1")
        self.assertIsNotNone(users_module.get("studentx"))
        self.assertIsNotNone(users_module.get("STUDENTX"))
        self.assertEqual(users_module.get("studentx")["username"], "StudentX")

    def test_rejects_bad_username(self):
        for bad in ("导师", "has space", "a" * 33, "", "semi;colon", "中文名"):
            with self.assertRaises(users_module.UserError, msg=bad):
                users_module.create(bad, "student-pass-1")

    def test_rejects_short_password(self):
        with self.assertRaises(users_module.UserError):
            users_module.create("student1", "short")

    def test_rejects_duplicate_username(self):
        users_module.create("student1", "student-pass-1")
        with self.assertRaises(users_module.UserError) as ctx:
            users_module.create("STUDENT1", "another-pass-1")
        self.assertIn("已存在", str(ctx.exception))

    def test_rejects_bad_role(self):
        with self.assertRaises(users_module.UserError):
            users_module.create("student1", "student-pass-1", role="root")

    def test_authenticate_rejects_disabled_user(self):
        users_module.create("student1", "student-pass-1")
        users_module.set_enabled("student1", False)
        user, reason = users_module.authenticate("student1", "student-pass-1")
        self.assertIsNone(user)
        self.assertIn("停用", reason)

    def test_authenticate_unknown_user_says_the_same_thing(self):
        """不区分「用户不存在」和「密码错」，避免枚举用户名。"""
        _, reason = users_module.authenticate("nobody", "whatever-123")
        self.assertIn("用户名或密码错误", reason)

    def test_delete_removes_only_the_account(self):
        users_module.create("student1", "student-pass-1")
        self.assertTrue(users_module.delete("student1"))
        self.assertIsNone(users_module.get("student1"))
        self.assertFalse(users_module.delete("student1"))   # 再删返回 False

    def test_public_strips_the_password_hash(self):
        users_module.create("student1", "student-pass-1")
        exposed = users_module.public(users_module.get("student1"))
        self.assertNotIn("password_hash", exposed)
        self.assertEqual(exposed["username"], "student1")


class TokenVersionTests(UserStoreTestCase):
    """token_version 是「按用户踢下线」的机制，必须每次都被递增。"""

    def setUp(self):
        super().setUp()
        users_module.create("student1", "student-pass-1")

    def test_set_password_bumps_token_version(self):
        before = users_module.get("student1")["token_version"]
        users_module.set_password("student1", "brand-new-pass-1")
        after = users_module.get("student1")["token_version"]
        self.assertEqual(after, before + 1,
                         "改密码必须让该用户此前签发的会话失效")
        self.assertIsNotNone(users_module.authenticate("student1", "brand-new-pass-1")[0])
        self.assertIsNone(users_module.authenticate("student1", "student-pass-1")[0])

    def test_set_enabled_bumps_token_version(self):
        before = users_module.get("student1")["token_version"]
        users_module.set_enabled("student1", False)
        self.assertEqual(users_module.get("student1")["token_version"], before + 1)
        users_module.set_enabled("student1", True)
        self.assertEqual(users_module.get("student1")["token_version"], before + 2)

    def test_bump_token_version_forces_logout_without_side_effects(self):
        before = users_module.get("student1")["token_version"]
        users_module.bump_token_version("student1")
        user = users_module.get("student1")
        self.assertEqual(user["token_version"], before + 1)
        self.assertTrue(user["enabled"])
        # 强制下线不改口令，所以老密码依然有效
        self.assertIsNotNone(users_module.authenticate("student1", "student-pass-1")[0])

    def test_setting_roots_does_not_bump_token_version(self):
        """改可见目录不该把人踢下线 —— 下一次请求自然生效。"""
        before = users_module.get("student1")["token_version"]
        users_module.set_roots("student1", [{"id": "a", "path": "D:/Lab/x"}])
        self.assertEqual(users_module.get("student1")["token_version"], before)

    def test_mutating_unknown_user_raises(self):
        with self.assertRaises(users_module.UserError):
            users_module.set_password("ghost", "whatever-123")


class RootNormalizationTests(UserStoreTestCase):

    def setUp(self):
        super().setUp()
        users_module.create("student1", "student-pass-1")

    def test_drops_entries_without_a_path(self):
        users_module.set_roots("student1", [
            {"id": "ok", "path": "D:/Lab/a"},
            {"id": "no-path", "name": "缺路径"},
            {"path": ""},
            "not-a-dict",
        ])
        roots = users_module.get("student1")["roots"]
        self.assertEqual([r["id"] for r in roots], ["ok"])

    def test_fills_missing_id_and_name_from_the_path(self):
        users_module.set_roots("student1", [{"path": "D:/Lab/我的空间"}])
        root = users_module.get("student1")["roots"][0]
        self.assertEqual(root["id"], "我的空间")
        self.assertEqual(root["name"], "我的空间")

    def test_deduplicates_by_id(self):
        users_module.set_roots("student1", [
            {"id": "dup", "path": "D:/Lab/a"},
            {"id": "dup", "path": "D:/Lab/b"},
        ])
        self.assertEqual(len(users_module.get("student1")["roots"]), 1)

    def test_readonly_flag_is_preserved(self):
        users_module.set_roots("student1", [
            {"id": "private", "path": "D:/Lab/me", "readonly": False},
            {"id": "public", "path": "D:/Lab/Public", "readonly": True},
        ])
        roots = {r["id"]: r for r in users_module.get("student1")["roots"]}
        self.assertFalse(roots["private"]["readonly"])
        self.assertTrue(roots["public"]["readonly"])

    def test_paths_are_normalized(self):
        users_module.set_roots("student1", [{"id": "a", "path": "D:/Lab/a/../b"}])
        self.assertEqual(users_module.get("student1")["roots"][0]["path"],
                         os.path.normpath("D:/Lab/a/../b"))


class CorruptFileTests(UserStoreTestCase):
    """用户表损坏时必须是「退化成空表」而不是抛异常 —— 否则整个服务起不来。"""

    def test_missing_file_reads_as_empty(self):
        self.assertEqual(users_module.list_users(), [])
        self.assertEqual(users_module.count(), 0)

    def test_broken_json_reads_as_empty(self):
        self._write_raw("{ this is not json")
        self.assertEqual(users_module.count(), 0)

    def test_non_object_root_reads_as_empty(self):
        self._write_raw([1, 2, 3])
        self.assertEqual(users_module.count(), 0)

    def test_bad_records_are_dropped_but_good_ones_survive(self):
        self._write_raw({
            "version": 1,
            "users": [
                {"username": "good", "password_hash": "x", "role": "user"},
                {"username": "bad name", "password_hash": "x"},   # 非法用户名
                "not-a-dict",
                {"no_username": True},
            ],
        })
        names = [u["username"] for u in users_module.list_users()]
        self.assertEqual(names, ["good"])

    def test_unknown_role_falls_back_to_user(self):
        """★ 角色不认识时降级为普通用户 —— 绝不能"往管理员那边"退。"""
        self._write_raw({
            "version": 1,
            "users": [{"username": "weird", "password_hash": "x", "role": "superuser"}],
        })
        self.assertEqual(users_module.get("weird")["role"], "user")

    def test_broken_file_is_left_on_disk_for_manual_repair(self):
        self._write_raw("{ broken")
        users_module.count()
        with open(self.path, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "{ broken",
                             "损坏的文件不该被自动覆盖掉")


class IsolationFromRealStateTests(UserStoreTestCase):
    """
    ★ 断言真实的那份 users.json 没有被测试碰到。

    这条直接对应项目刚发生过的事故（测试把真实 config.json 覆盖成测试配置）。
    """

    def test_real_users_json_is_untouched(self):
        real = os.path.normpath(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "users.json"))
        before = None
        if os.path.isfile(real):
            with open(real, "rb") as fh:
                before = hashlib.sha256(fh.read()).hexdigest()

        # 随便做点写操作
        users_module.create("scratch", "scratch-pass-1")
        users_module.set_password("scratch", "scratch-pass-2")

        # 我们写的是临时文件
        self.assertTrue(os.path.isfile(self.path))
        self.assertNotEqual(os.path.normpath(users_module.USERS_PATH), real)

        if before is not None:
            with open(real, "rb") as fh:
                self.assertEqual(before, hashlib.sha256(fh.read()).hexdigest(),
                                 "真实的 users.json 被测试改写了！")


if __name__ == "__main__":
    unittest.main(verbosity=2)

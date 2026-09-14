# -*- coding: utf-8 -*-
"""
多用户：按用户分文件的界面状态（阶段4）
=====================================

`user_state.json`（窗口布局）与 `desktop_shortcuts.json`（桌面图标）原先各是
**一个单文件**。多人一起用就会互相覆盖，而且症状很难归因：

    * 学生 A 挪一下窗口，学生 B 刷新后桌面就变了；
    * A 建一个快捷方式，所有人的桌面上都冒出来一个；
    * A 删掉它，B 的那个也跟着没了。

本文件钉住三条：
    1. 子用户各写各的文件（`user_state.<用户名>.json` 等）；
    2. ★ **管理员沿用原文件名**（`user_state.json`）—— 现有部署升级后
       文件名、内容都不变，「关掉浏览器再打开还是原来的桌面」照旧；
    3. ★ 用户名会被拼进文件名，所以必须挡掉目录穿越（`../`）。

第 2 条不只是「好看」：测试脚手架会把 user_state_path 指到临时目录，
既有用例（test_desktop_state.py）直接断言**那个确切路径**上的文件被写出来了。
管理员若被改成带后缀的文件名，那些用例会全部失败 —— 它们一条都没改，
这本身就是「管理员那份位置不变」的回归证据。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest

from fileweb import peruser
from fileweb import shortcuts as shortcuts_module
from fileweb import users as users_module
from fileweb import userstate
from fileweb import config as config_module
from tests._harness import STATE_PATH_KEYS, ServerProcess, state_path_values

ADMIN_USER = "teacher"
ADMIN_PASSWORD = "admin-pass-123"
STUDENT_PASSWORD = "student-pass-123"

ALICE = {
    "username": "stu01", "role": "user",
    "roots": [{"id": "private", "name": "我的空间", "path": "X:\\", "readonly": False}],
}
BOB = {
    "username": "stu02", "role": "user",
    "roots": [{"id": "private", "name": "我的空间", "path": "X:\\", "readonly": False}],
}
ADMIN = {"username": "teacher", "role": "admin", "roots": []}


class PerUserPathTests(unittest.TestCase):
    """路径派生规则本身（纯函数，不碰磁盘）。"""

    BASE = os.path.join("C:", os.sep, "app", "user_state.json")

    def test_admin_keeps_the_original_file_name(self):
        self.assertEqual(peruser.state_path(self.BASE, ADMIN), self.BASE,
                         "★ 管理员必须沿用原文件，否则升级后他的桌面会「丢」")

    def test_no_user_keeps_the_original_file_name(self):
        self.assertEqual(peruser.state_path(self.BASE, None), self.BASE)

    def test_sub_user_gets_a_suffixed_file(self):
        got = peruser.state_path(self.BASE, ALICE)
        self.assertEqual(got, os.path.join("C:", os.sep, "app",
                                           "user_state.stu01.json"))

    def test_derived_file_stays_in_the_same_directory(self):
        got = peruser.state_path(self.BASE, ALICE)
        self.assertEqual(os.path.dirname(got), os.path.dirname(self.BASE),
                         "派生文件必须与原文件同目录（方便一起备份/清理）")

    def test_extension_is_preserved(self):
        got = peruser.state_path(
            os.path.join("C:", os.sep, "app", "desktop_shortcuts.json"), BOB)
        self.assertTrue(got.endswith(".json"), got)
        self.assertIn("stu02", got)

    # -- 安全：用户名会被拼进文件名 -----------------------------------------

    def test_username_cannot_escape_the_directory(self):
        """
        ★ 目录穿越：用户名一旦带着 `../` 进来，拼出来的就是**另一个目录**里的
        路径，而状态文件是会被**写**的 —— 那不是「读到别人的桌面」而已，
        而是往任意位置写文件。

        用户表已经把用户名限制成 [A-Za-z0-9_.-]，这里是第二道闸门。
        """
        for nasty in ("../../evil", "..\\..\\evil", "a/b", "a\\b", "..", "."):
            user = {"username": nasty, "role": "user"}
            got = peruser.state_path(self.BASE, user)

            # 归一化之后仍然在原目录里 —— 这是「没有穿越出去」的硬判据
            self.assertEqual(
                os.path.normpath(os.path.dirname(got)),
                os.path.normpath(os.path.dirname(self.BASE)),
                "用户名 %r 派生出的路径跑到别的目录去了：%s" % (nasty, got))
            self.assertNotIn(os.sep, os.path.basename(got),
                             "用户名 %r 不该在文件名里留下路径分隔符" % (nasty,))
            self.assertNotIn("/", os.path.basename(got))

    def test_unusable_username_falls_back_to_the_shared_file(self):
        """
        名字**完全没法用**时退回基础文件，而不是在磁盘上乱造文件。

        ★ 「没法用」限指消毒后什么都不剩（空串、纯空白、全点）。
        """
        for unusable in ("", "   ", None, "...", "...."):
            self.assertEqual(
                peruser.state_path(self.BASE,
                                   {"username": unusable, "role": "user"}),
                self.BASE, "用户名 %r 应当退回基础文件" % (unusable,))

    def test_username_with_only_symbols_gets_its_own_file(self):
        """
        消毒后还剩字符（例如 "/" → "_"）就各自一个文件。

        ★ 这是**有意**的：退回基础文件等于让这个账号去写**管理员**那份状态，
        比多出一个怪名字的文件糟得多。两个畸形名字可能消毒成同一个文件名，
        但那种名字在用户表里根本建不出来（用户名限 [A-Za-z0-9_.-]），
        这里只是「users.json 被手改坏」时的兜底。
        """
        got = peruser.state_path(self.BASE, {"username": "/", "role": "user"})
        self.assertNotEqual(got, self.BASE,
                            "★ 绝不能回退去写管理员那份")
        self.assertEqual(os.path.dirname(got), os.path.dirname(self.BASE))

    def test_role_beats_the_name(self):
        """
        ★ 判据是**角色**，不是「名字是不是 admin」。

        一个恰好叫 admin 的普通学生绝不能拿到管理员那份文件；
        管理员改了登录名也仍然用基础文件。
        """
        impostor = {"username": "admin", "role": "user"}
        self.assertNotEqual(peruser.state_path(self.BASE, impostor), self.BASE,
                            "★ 普通用户叫 admin 也不能拿到管理员的文件")

        renamed_admin = {"username": "laoshi", "role": "admin"}
        self.assertEqual(peruser.state_path(self.BASE, renamed_admin), self.BASE)


class PerUserStorageTests(unittest.TestCase):
    """shortcuts / userstate 两个模块按用户分文件的读写（真实临时目录）。"""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-pustate-")
        self._saved_shortcuts = shortcuts_module.ACTIVE_PATH
        self._saved_state = userstate.ACTIVE_PATH
        shortcuts_module.set_path(os.path.join(self.work, "desktop_shortcuts.json"))
        userstate.ACTIVE_PATH = os.path.join(self.work, "user_state.json")

    def tearDown(self):
        shortcuts_module.set_path(self._saved_shortcuts)
        userstate.ACTIVE_PATH = self._saved_state
        shutil.rmtree(self.work, ignore_errors=True)

    def _files(self):
        return sorted(os.listdir(self.work))

    # -- 快捷方式 -----------------------------------------------------------

    def test_shortcuts_are_stored_per_user(self):
        mine = shortcuts_module.add("我的资料", "private", "docs", user=ALICE)

        self.assertEqual([s["id"] for s in shortcuts_module.list_items(ALICE)],
                         [mine["id"]])
        self.assertEqual(shortcuts_module.list_items(BOB), [],
                         "★ 别人的快捷方式不该出现在我的桌面上")
        self.assertIn("desktop_shortcuts.stu01.json", self._files())

    def test_one_user_cannot_delete_another_users_shortcut(self):
        mine = shortcuts_module.add("我的资料", "private", "docs", user=ALICE)

        self.assertFalse(shortcuts_module.remove(mine["id"], user=BOB),
                         "★ 不该能删掉别人的桌面图标")
        self.assertEqual(len(shortcuts_module.list_items(ALICE)), 1,
                         "别人的删除尝试不能真的生效")

    def test_admin_uses_the_original_single_file(self):
        shortcuts_module.add("管理员的", "share", "x", user=ADMIN)
        self.assertEqual(self._files(), ["desktop_shortcuts.json"],
                         "★ 管理员的快捷方式必须还写在原来那个文件里")
        self.assertEqual(len(shortcuts_module.list_items(ADMIN)), 1)

    def test_clear_only_affects_one_user(self):
        shortcuts_module.add("甲的", "private", "a", user=ALICE)
        shortcuts_module.add("乙的", "private", "b", user=BOB)

        self.assertEqual(shortcuts_module.clear(ALICE), 1)
        self.assertEqual(shortcuts_module.list_items(ALICE), [])
        self.assertEqual(len(shortcuts_module.list_items(BOB)), 1,
                         "清空自己的桌面不该动别人的")

    # -- 界面状态 -----------------------------------------------------------

    def test_user_state_is_stored_per_user(self):
        alice_state = {"windows": [{"title": "甲"}, {"title": "乙"}]}
        bob_state = {"windows": [{"title": "丙"}]}

        userstate.save(alice_state, user=ALICE)
        userstate.save(bob_state, user=BOB)

        self.assertEqual(userstate.load(user=ALICE), alice_state)
        self.assertEqual(userstate.load(user=BOB), bob_state)
        self.assertIn("user_state.stu01.json", self._files())
        self.assertNotIn("user_state.json", self._files(),
                         "一个子用户都不该碰管理员那个基础文件")

    def test_admin_state_goes_to_the_base_file(self):
        userstate.save({"windows": [{"title": "导师"}]}, user=ADMIN)
        self.assertEqual(self._files(), ["user_state.json"])
        self.assertEqual(userstate.load(user=ADMIN)["windows"][0]["title"], "导师")

    def test_missing_per_user_state_is_empty_not_the_admins(self):
        """★ 子用户读不到自己的文件时必须是「空」，绝不能兜到管理员那份。"""
        userstate.save({"windows": [{"title": "导师的窗口"}]}, user=ADMIN)
        self.assertEqual(userstate.load(user=ALICE), {},
                         "★ 别人的桌面布局绝不能变成我的默认布局")

    def test_clear_only_affects_one_users_state(self):
        userstate.save({"a": 1}, user=ALICE)
        userstate.save({"b": 2}, user=BOB)

        self.assertTrue(userstate.clear(user=ALICE))
        self.assertEqual(userstate.load(user=ALICE), {})
        self.assertEqual(userstate.load(user=BOB), {"b": 2})


class HarnessStatePathIsolationTests(unittest.TestCase):
    """
    ★ 脚手架必须把**所有**「默认落在项目根目录」的状态文件指向临时目录。

    这条是补上三次同类事故的：
      * 真实 config.json 被测试配置覆盖（服务换端口、口令失效）；
      * 真实 user_state.json 里出现指向临时测试目录的窗口；
      * 真实 desktop_shortcuts.json / audit.log.jsonl 被测试写入。

    根因每次都一样：脚手架调 `prepare()` 时**不带 cfg_path**，于是相对路径
    被解析成代码目录下的绝对路径写进临时配置，子进程老老实实照做。
    """

    def test_harness_config_points_every_state_file_into_the_temp_dir(self):
        server = ServerProcess([{"id": "main", "name": "main",
                                 "path": tempfile.gettempdir(), "readonly": False}])
        try:
            with open(server.cfg_path, "r", encoding="utf-8") as fh:
                cfg = json.load(fh)

            for key, value in state_path_values(cfg).items():
                self.assertTrue(value, "配置里应当有 %s" % key)
                resolved = os.path.abspath(str(value))
                self.assertTrue(
                    resolved.startswith(os.path.abspath(server.work) + os.sep),
                    "★ %s 必须落在临时目录里，实际是 %s（会写脏真实部署）"
                    % (key, resolved))
        finally:
            server.cleanup()

    def test_every_state_path_in_default_config_is_covered(self):
        """
        ★★ 结构性守卫：DEFAULT_CONFIG 里凡是「默认指向某个真实路径」的配置项，
        都必须登记在 _harness.STATE_PATH_TARGETS 里。

        这条是让上面那个坑**不会再犯第五次**的关键：新增一个同类配置项时
        （会话表、下载记录、曲库、缓存……），这里会直接变红，逼作者同时把它
        加进 redirect_state_paths —— 否则「用临时配置起的服务会去写真实部署」
        这个错误在很长一段时间里都不会有任何症状，直到用户的数据没了。

        判定规则（按**键名**，不看值）：
          * `*_path` / `*_dir` / 名为 `path` 的子键算路径类配置；
          * ★ 但**默认值为空**的不算 —— 空表示「没配置 / 自动」，不会往那里写
            （例如 `archive.rar_path`、`office.soffice_path`、`terminal.start_dir`
            都是外部程序或「用第一个根」，我们从不写进去）。
        """
        path_keys = {}
        for key, value in config_module.DEFAULT_CONFIG.items():
            if isinstance(value, str) and (key.endswith("_path") or key.endswith("_dir")):
                path_keys[key] = value
            elif isinstance(value, dict):
                for sub_key, sub_value in value.items():
                    if (isinstance(sub_value, str)
                            and (sub_key in ("path", "dir")
                                 or sub_key.endswith("_path")
                                 or sub_key.endswith("_dir"))):
                        path_keys["%s.%s" % (key, sub_key)] = sub_value

        must_redirect = {key for key, value in path_keys.items() if str(value).strip()}
        covered = set(STATE_PATH_KEYS)

        uncovered = sorted(must_redirect - covered)
        self.assertEqual(
            uncovered, [],
            "★ 这些配置项的默认值指向真实路径，但没被 tests/_harness.redirect_state_paths "
            "覆盖：%s —— 请把它们加进 STATE_PATH_TARGETS，"
            "否则用临时配置起的服务会写脏真实部署" % uncovered)

        # 反向检查：登记了却已经不存在（或默认值变成空），说明守卫本身过期了
        stale = sorted(covered - must_redirect)
        self.assertEqual(
            stale, [],
            "STATE_PATH_TARGETS 里这些项在 DEFAULT_CONFIG 中已不存在、"
            "或默认值已变成空：%s" % stale)


class PerUserDesktopApiTests(unittest.TestCase):
    """
    端到端：两个学生各连一次接口，各自保存的桌面状态互不影响。

    这是用户实际会看到的症状（「我的窗口被同学挪了」），所以值得从接口层再钉一遍。
    """

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-pudesktop-")
        cls.roots = {}
        for name in ("alice", "bob"):
            root = os.path.join(cls.work, name)
            os.makedirs(root, exist_ok=True)
            with open(os.path.join(root, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write(name)
            cls.roots[name] = root

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.work, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD,
        ).start()

        cls._saved_users_path = users_module.USERS_PATH
        users_module.set_path(os.path.join(
            os.path.dirname(cls.server.cfg_path), "users.json"))
        users_module.create("stu01", STUDENT_PASSWORD, display_name="甲同学", roots=[
            {"id": "mine", "name": "我的空间",
             "path": cls.roots["alice"], "readonly": False}])
        users_module.create("stu02", STUDENT_PASSWORD, display_name="乙同学", roots=[
            {"id": "mine", "name": "我的空间",
             "path": cls.roots["bob"], "readonly": False}])

    @classmethod
    def tearDownClass(cls):
        users_module.set_path(cls._saved_users_path)
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------------

    def _login(self, username):
        client = self.server.client()
        status, data = client.login(username, STUDENT_PASSWORD)
        self.assertEqual(status, 200, "登录 %s 失败：%s" % (username, data))
        return client

    def _put_state(self, client, state):
        return client.json("PUT", "/api/desktop/state", {"state": state})

    def _get_state(self, client):
        status, data = client.json("GET", "/api/desktop/state")
        self.assertEqual(status, 200, data)
        return data["state"]

    def _shortcuts(self, client):
        status, data = client.json("GET", "/api/desktop/shortcuts")
        self.assertEqual(status, 200, data)
        return data["shortcuts"]

    # -- 测试 ---------------------------------------------------------------

    def test_window_layout_is_per_user(self):
        alice = self._login("stu01")
        bob = self._login("stu02")

        alice_state = {"windows": [{"title": "甲的窗口"}], "active": "win_1"}
        bob_state = {"windows": [{"title": "乙的窗口"}], "active": "win_9"}

        status, data = self._put_state(alice, alice_state)
        self.assertEqual(status, 200, data)
        status, data = self._put_state(bob, bob_state)
        self.assertEqual(status, 200, data)

        self.assertEqual(self._get_state(alice), alice_state,
                         "★ 甲读回来的必须是自己那份")
        self.assertEqual(self._get_state(bob), bob_state,
                         "★ 乙读回来的也必须是自己那份")

    def test_fresh_user_does_not_inherit_someone_elses_layout(self):
        alice = self._login("stu01")
        status, data = self._put_state(
            alice, {"windows": [{"title": "甲的窗口"}]})
        self.assertEqual(status, 200, data)

        # 管理员从来没存过状态，不该拿到学生的布局
        admin = self.server.login_client()
        self.assertEqual(self._get_state(admin), {},
                         "★ 别人还没存过状态时必须是空的（走默认布局）")

    def test_shortcuts_are_per_user(self):
        alice = self._login("stu01")
        bob = self._login("stu02")

        status, data = alice.json("POST", "/api/desktop/shortcuts", {
            "root": "mine", "path": "a.txt", "name": "甲的资料",
        })
        self.assertEqual(status, 200, data)

        self.assertEqual([s["name"] for s in self._shortcuts(alice)], ["甲的资料"])
        self.assertEqual(self._shortcuts(bob), [],
                         "★ 甲的桌面图标不该出现在乙的桌面上")

    def test_per_user_files_land_beside_the_test_config(self):
        """
        文件确实落在**这个实例自己的**状态目录里，而不是项目根目录。

        （项目根目录那两份是真实部署的数据。测试脚手架用临时配置起服务，
        路径相对配置文件解析，所以这里必须看到带用户名后缀的文件。）
        """
        alice = self._login("stu01")
        status, _ = self._put_state(alice, {"windows": [{"title": "甲的窗口"}]})
        self.assertEqual(status, 200)

        names = os.listdir(self.server.work)
        self.assertIn("user_state.stu01.json", names,
                      "子用户的界面状态应当落在实例自己的目录里：%s" % names)


if __name__ == "__main__":
    unittest.main(verbosity=2)

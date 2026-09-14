# -*- coding: utf-8 -*-
"""
多用户：审计日志、在线表、以及管理接口（阶段5）
=============================================

这一阶段把「管理员能看到什么、能改什么」补齐：

    * **用户管理**：建账号、改显示名/角色/额度/可见目录、重设口令、停用、踢下线；
    * **在线列表**：谁在线、从哪个 IP、什么时候登录的；
    * **审计日志**：账号层面的动作落盘留痕，事后可查。

★ 这里所有接口都走 require_admin，但要说清楚：**这是界面与接口层面的约束，
不是对抗恶意用户的边界** —— 子用户拥有全权限命令行，本来就能直接改 users.json
（MULTIUSER.md 第〇节，用户已确认接受）。它挡的是误操作和「顺手试一下」。

因此本文件里最要紧的其实不是「学生访问被拒」，而是三条**防把自己锁在门外**的
护栏：不能停用自己、不能改自己的角色、不能把最后一个管理员搞掉。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest

from fileweb import audit
from fileweb import presence
from fileweb import users as users_module
from fileweb.routers.users import _guard_last_admin
from tests._harness import ServerProcess
from tests.test_features import _ws_close_session

ADMIN_USER = "teacher"
ADMIN_PASSWORD = "admin-pass-123"
STUDENT_PASSWORD = "student-pass-123"


# ---------------------------------------------------------------------------
# 审计日志模块
# ---------------------------------------------------------------------------

class AuditModuleTests(unittest.TestCase):

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-audit-")
        self._saved = (audit.ACTIVE_PATH, audit.MAX_BYTES, audit.BACKUPS)
        audit.set_path(os.path.join(self.work, "audit.log.jsonl"))
        audit.configure(max_bytes=4 * 1024 * 1024, backups=3)

    def tearDown(self):
        audit.ACTIVE_PATH, audit.MAX_BYTES, audit.BACKUPS = self._saved
        shutil.rmtree(self.work, ignore_errors=True)

    def _path(self):
        return audit.ACTIVE_PATH

    def test_log_then_recent_roundtrip(self):
        audit.log(audit.EVENT_LOGIN_OK, username="stu01", ip="10.0.0.5",
                  detail="角色=user")
        audit.log(audit.EVENT_LOGIN_FAIL, username="stu01", ip="10.0.0.6",
                  result="fail", detail="用户名或密码错误")

        records = audit.recent(10)
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["event"], audit.EVENT_LOGIN_OK)
        self.assertEqual(records[0]["username"], "stu01")
        self.assertEqual(records[0]["ip"], "10.0.0.5")
        self.assertEqual(records[1]["result"], "fail")
        # 时间字段要能直接给人看
        self.assertRegex(records[0]["time"], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")

    def test_recent_returns_chronological_order(self):
        """顺序必须是「时间正序」——界面上最新的一条在最后，和日志文件一致。"""
        for index in range(5):
            audit.log("test", username="u%d" % index)
        names = [row["username"] for row in audit.recent(5)]
        self.assertEqual(names, ["u0", "u1", "u2", "u3", "u4"])

    def test_limit_returns_the_newest(self):
        for index in range(10):
            audit.log("test", username="u%d" % index)
        records = audit.recent(3)
        self.assertEqual([row["username"] for row in records], ["u7", "u8", "u9"])

    def test_broken_lines_are_skipped(self):
        """
        坏行不能让整个日志读不出来。

        追加写入的日志在断电/崩溃时可能留下半行；为此让管理窗口直接报错
        是不划算的 —— 跳过它、把其余记录照常展示。
        """
        audit.log("good", username="first")
        with open(self._path(), "a", encoding="utf-8") as fh:
            fh.write("{ 这不是合法 JSON\n")
            fh.write("\n")
        audit.log("good", username="second")

        names = [row["username"] for row in audit.recent(10)]
        self.assertEqual(names, ["first", "second"],
                         "坏行应当被跳过，其余记录照常返回")

    def test_rotation_creates_history_and_bounds_the_file(self):
        audit.configure(max_bytes=64 * 1024, backups=2)
        # 每条大约 200 字节，写 800 条足够触发轮转
        for index in range(800):
            audit.log("test", username="user-%04d" % index,
                      detail="x" * 100)

        self.assertTrue(os.path.isfile(self._path()))
        self.assertTrue(os.path.isfile(self._path() + ".1"),
                        "超过上限后应当轮转出 .1")
        self.assertLessEqual(os.path.getsize(self._path()), 64 * 1024 + 4096,
                             "当前文件不该无限增长")
        self.assertFalse(os.path.isfile(self._path() + ".3"),
                         "backups=2 时不该出现 .3")

    def test_backups_zero_drops_history(self):
        audit.configure(max_bytes=64 * 1024, backups=0)
        for index in range(800):
            audit.log("test", username="user-%04d" % index,
                      detail="x" * 100)
        self.assertFalse(os.path.isfile(self._path() + ".1"),
                         "backups=0 表示不保留历史")

    def test_write_failure_never_raises(self):
        """
        ★ 审计是「尽力而为」的旁路。

        磁盘满、路径被占、权限被改 —— 这些都不该让登录失败。
        这里刻意把日志路径指到一个**被普通文件占住的目录**下，
        确认 log() 只是打一行警告。
        """
        blocker = os.path.join(self.work, "blocker")
        with open(blocker, "w", encoding="utf-8") as fh:
            fh.write("我是一个文件，不是目录")
        audit.set_path(os.path.join(blocker, "audit.log.jsonl"))

        audit.log(audit.EVENT_LOGIN_OK, username="stu01")   # 不该抛异常
        self.assertEqual(audit.recent(5), [])

    def test_clear_removes_the_current_file(self):
        audit.log("test", username="u")
        self.assertTrue(audit.clear())
        self.assertFalse(audit.clear(), "文件已不存在时应当返回 False")


# ---------------------------------------------------------------------------
# 在线表
# ---------------------------------------------------------------------------

class PresenceTests(unittest.TestCase):

    def setUp(self):
        presence.reset()

    def tearDown(self):
        presence.reset()

    def test_touch_marks_user_online(self):
        presence.touch("stu01", ip="10.0.0.5", display_name="甲同学",
                       role="user", is_login=True)
        rows = presence.snapshot()
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["username"], "stu01")
        self.assertEqual(row["display_name"], "甲同学")
        self.assertEqual(row["ip"], "10.0.0.5")
        self.assertTrue(row["online"])
        self.assertTrue(row["login_at"], "登录时间必须被记下来")

    def test_activity_outside_the_window_is_offline(self):
        presence.touch("stu01", ip="10.0.0.5", is_login=True)
        seen = presence.snapshot()[0]["last_seen"]

        # 刚好超出窗口 → 离线，但记录仍在（管理员能看到「他刚才还在」）
        later = seen + presence.ONLINE_WINDOW_SECONDS + 30
        rows = presence.snapshot(now=later)
        self.assertEqual(len(rows), 1)
        self.assertFalse(rows[0]["online"])
        self.assertEqual(presence.count(now=later), 0)

    def test_very_old_records_are_pruned(self):
        presence.touch("stu01", is_login=True)
        seen = presence.snapshot()[0]["last_seen"]
        far_future = seen + presence.FORGET_AFTER_SECONDS + 3600
        self.assertEqual(presence.snapshot(now=far_future), [],
                         "太久没活动的记录应当被清掉，避免表无限增长")

    def test_repeated_touches_are_throttled(self):
        """
        每个请求都抢锁写一次没有意义 —— 5 分钟的窗口下 30 秒的误差无所谓。
        这里钉住「节流确实存在」，免得以后有人把节流去掉还以为没影响。
        """
        presence.touch("stu01", is_login=True)
        first_seen = presence.snapshot()[0]["last_seen"]

        presence.touch("stu01")
        presence.touch("stu01")
        rows = presence.snapshot()
        self.assertEqual(rows[0]["last_seen"], first_seen,
                         "窗口内的普通活动不该反复刷新 last_seen")
        self.assertEqual(rows[0]["requests"], 3, "但活动次数要照实累加")

    def test_login_touch_is_never_throttled(self):
        presence.touch("stu01")
        first_seen = presence.snapshot()[0]["last_seen"]
        time.sleep(0.01)
        presence.touch("stu01", is_login=True)
        self.assertGreater(presence.snapshot()[0]["last_seen"], first_seen,
                           "登录是事件，必须立即生效")

    def test_forget_removes_the_user(self):
        presence.touch("stu01", is_login=True)
        presence.forget("stu01")
        self.assertEqual(presence.snapshot(), [])

    def test_username_matching_is_case_insensitive(self):
        presence.touch("Stu01", is_login=True)
        presence.touch("stu01")
        self.assertEqual(len(presence.snapshot()), 1,
                         "同一个人的不同大小写写法不该算成两个人")


# ---------------------------------------------------------------------------
# 管理接口
# ---------------------------------------------------------------------------

class AdminApiTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-admin-")
        cls.root = os.path.join(cls.work, "root")
        os.makedirs(cls.root, exist_ok=True)

        def _extra(cfg):
            # 有一个用例要验证「管理员看到某人的命令行窗口数」，得把终端打开
            cfg["terminal"]["enabled"] = True
            cfg["terminal"]["max_sessions"] = 5

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD, extra_config=_extra,
        ).start()

        cls._saved_users_path = users_module.USERS_PATH
        users_module.set_path(os.path.join(
            os.path.dirname(cls.server.cfg_path), "users.json"))

        # ★ 预先放一个**第二管理员**。两个原因：
        #   1) 「不能停用自己 / 不能改自己的角色」这两条护栏与「不能把最后一个
        #      管理员搞掉」是两件事，只有存在第二个管理员时，前两条才会被真正
        #      走到 —— 否则先撞上的是「最后一个管理员」那条，测的就不是它了。
        #   2) 后一条护栏本身另有单元测试（用独立的临时用户表）。
        users_module.create("boss2", "boss2-password-1",
                            display_name="另一位管理员",
                            role=users_module.ROLE_ADMIN)

    @classmethod
    def tearDownClass(cls):
        users_module.set_path(cls._saved_users_path)
        cls.server.stop()
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------------

    def setUp(self):
        presence.reset()

    def _admin(self):
        return self.server.login_client()

    def _login(self, username, password=STUDENT_PASSWORD):
        client = self.server.client()
        return client, client.login(username, password)

    def _create_student(self, admin, username, **overrides):
        """
        建一个学生账号。

        ★ username 刻意**不给默认值**：这个类里的服务进程是共享的，
        users.json 会跨用例累积，两个用例用同一个名字就会有一个拿到
        「用户名已存在」。给默认值等于邀请这个 bug，所以强制每个用例自己起个名字。
        """
        payload = {
            "username": username,
            "password": STUDENT_PASSWORD,
            "display_name": overrides.pop("display_name", username),
            "max_terminal_sessions": overrides.pop("max_terminal_sessions", 5),
        }
        payload.update(overrides)
        status, data = admin.json("POST", "/api/users", payload)
        self.assertEqual(status, 200, data)
        return data["user"]

    def _users(self, client):
        status, data = client.json("GET", "/api/users")
        self.assertEqual(status, 200, data)
        return {row["username"]: row for row in data["users"]}

    # -- 权限：学生进不来 ---------------------------------------------------

    def test_student_cannot_use_any_admin_endpoint(self):
        admin = self._admin()
        self._create_student(admin, "perm01")
        student, (status, data) = self._login("perm01")
        self.assertEqual(status, 200, data)

        for method, url in (
            ("GET", "/api/users"),
            ("GET", "/api/users/online"),
            ("GET", "/api/users/audit"),
        ):
            got, body = student.json(method, url)
            self.assertEqual(got, 403, "%s %s 应当仅限管理员：%s" % (method, url, body))

        status, body = student.json("POST", "/api/users", {
            "username": "hacker", "password": STUDENT_PASSWORD,
        })
        self.assertEqual(status, 403, body)

        status, body = student.json("POST", "/api/users/perm01/update",
                                    {"enabled": False})
        self.assertEqual(status, 403, body)

    # -- 用户列表 -----------------------------------------------------------

    def test_admin_sees_users_with_runtime_state(self):
        admin = self._admin()
        self._create_student(admin, "list01", display_name="甲同学")

        rows = self._users(admin)
        self.assertIn(ADMIN_USER, rows)
        self.assertIn("list01", rows)

        row = rows["list01"]
        self.assertEqual(row["display_name"], "甲同学")
        for key in ("online", "terminal_sessions", "processes", "jobs",
                    "last_seen", "ip", "is_self", "roots", "role"):
            self.assertIn(key, row, "列表里应当有 %s" % key)
        self.assertNotIn("password_hash", row, "★ 口令哈希绝不能下发")
        self.assertTrue(rows[ADMIN_USER]["is_self"], "自己那一行要能认出来")
        self.assertFalse(row["is_self"])

    def test_online_list_follows_login_and_logout(self):
        admin = self._admin()
        self._create_student(admin, "onl01")

        student, (status, _) = self._login("onl01")
        self.assertEqual(status, 200)

        # 学生刚登录过，管理员应当在线表里看到他
        status, data = admin.json("GET", "/api/users/online")
        self.assertEqual(status, 200, data)
        names = [row["username"] for row in data["online"] if row["online"]]
        self.assertIn("onl01", names, "登录后应当出现在在线表里：%s" % data)

        student.json("POST", "/api/auth/logout", {})
        status, data = admin.json("GET", "/api/users/online")
        names = [row["username"] for row in data["online"] if row["online"]]
        self.assertNotIn("onl01", names, "登出后不该还算在线")

    def test_admin_sees_students_terminal_session_count(self):
        """
        ★ 用户决定：管理员能看**每个用户几个活跃命令行窗口 + 进程**，
        但看不到终端输出内容。这里钉住「计数」这一半。
        """
        admin = self._admin()
        self._create_student(admin, "term01")
        student, (status, _) = self._login("term01")
        self.assertEqual(status, 200)

        status, data = student.json("POST", "/api/terminal/session", {})
        self.assertEqual(status, 200, data)
        sid = data["id"]

        def _close():
            _ws_close_session(
                "ws://127.0.0.1:%d/api/terminal/ws?sid=%s" % (self.server.port, sid),
                {"Cookie": student.cookie,
                 "Origin": "http://127.0.0.1:%d" % self.server.port})

        self.addCleanup(_close)

        self.assertEqual(self._users(admin)["term01"]["terminal_sessions"], 1,
                         "管理员应当看到这个学生开着 1 个命令行窗口")

        # 而接口里**不含**任何会话内容
        row = self._users(admin)["term01"]
        self.assertNotIn("output", row)
        self.assertNotIn("sessions", row)

    # -- 建用户 / 改用户 ----------------------------------------------------

    def test_created_user_can_log_in(self):
        admin = self._admin()
        self._create_student(admin, "new01", display_name="乙同学")

        _client, (status, data) = self._login("new01")
        self.assertEqual(status, 200, "新建的用户应当能登录：%s" % data)
        self.assertEqual(data["username"], "new01")

    def test_duplicate_username_is_rejected(self):
        admin = self._admin()
        self._create_student(admin, "dup01")

        status, data = admin.json("POST", "/api/users", {
            "username": "dup01", "password": STUDENT_PASSWORD,
        })
        self.assertEqual(status, 400, data)
        self.assertIn("已存在", str(data.get("message") or ""))

    def test_short_password_is_rejected(self):
        admin = self._admin()
        status, data = admin.json("POST", "/api/users", {
            "username": "shortpw", "password": "123",
        })
        self.assertEqual(status, 400, data)

    def test_admin_can_change_display_name_and_quota(self):
        admin = self._admin()
        self._create_student(admin, "meta01")

        status, data = admin.json("POST", "/api/users/meta01/update", {
            "display_name": "改过的名字", "max_terminal_sessions": 2,
        })
        self.assertEqual(status, 200, data)

        row = self._users(admin)["meta01"]
        self.assertEqual(row["display_name"], "改过的名字")
        self.assertEqual(row["max_terminal_sessions"], 2)

    def test_disable_kills_the_students_session_immediately(self):
        """★ 停用必须**立刻**让他手里那个会话失效，而不是等 Cookie 过期。"""
        admin = self._admin()
        self._create_student(admin, "off01")
        student, (status, _) = self._login("off01")
        self.assertEqual(status, 200)

        status, _ = student.json("GET", "/api/fs/roots")
        self.assertEqual(status, 200, "前提：停用前他是能用的")

        status, data = admin.json("POST", "/api/users/off01/update",
                                  {"enabled": False})
        self.assertEqual(status, 200, data)

        status, data = student.json("GET", "/api/fs/roots")
        self.assertEqual(status, 401,
                         "★ 停用后他手里的会话必须立即失效：%s" % data)

        # 也不能再登录
        _client, (status, data) = self._login("off01")
        self.assertEqual(status, 401, data)

    def test_kick_invalidates_sessions_without_disabling(self):
        admin = self._admin()
        self._create_student(admin, "kick01")
        student, (status, _) = self._login("kick01")
        self.assertEqual(status, 200)

        status, data = admin.json("POST", "/api/users/kick01/kick", {})
        self.assertEqual(status, 200, data)

        status, _ = student.json("GET", "/api/fs/roots")
        self.assertEqual(status, 401, "被踢下线后旧会话应当失效")

        # 但账号本身还能重新登录（这就是「踢下线」与「停用」的区别）
        _client, (status, data) = self._login("kick01")
        self.assertEqual(status, 200, "踢下线不等于停用：%s" % data)

    def test_admin_reset_password_invalidates_old_session(self):
        admin = self._admin()
        self._create_student(admin, "pw01")
        student, (status, _) = self._login("pw01")
        self.assertEqual(status, 200)

        status, data = admin.json("POST", "/api/users/pw01/password",
                                  {"password": "brand-new-pass-1"})
        self.assertEqual(status, 200, data)

        status, _ = student.json("GET", "/api/fs/roots")
        self.assertEqual(status, 401, "★ 重设口令后他之前的会话必须失效")

        _client, (status, data) = self._login("pw01", "brand-new-pass-1")
        self.assertEqual(status, 200, "新口令应当能登录：%s" % data)

    def test_unknown_user_returns_404(self):
        admin = self._admin()
        for method, url, payload in (
            ("POST", "/api/users/nobody/update", {"display_name": "x"}),
            ("POST", "/api/users/nobody/password", {"password": "abcdefgh"}),
            ("POST", "/api/users/nobody/kick", {}),
        ):
            status, data = admin.json(method, url, payload)
            self.assertEqual(status, 404, "%s %s：%s" % (method, url, data))

    # -- ★ 护栏：别把自己锁在门外 -------------------------------------------

    def test_admin_cannot_disable_himself(self):
        admin = self._admin()
        status, data = admin.json("POST", "/api/users/%s/update" % ADMIN_USER,
                                  {"enabled": False})
        self.assertEqual(status, 400, data)
        self.assertIn("自己", str(data.get("message") or ""))
        # 而且他确实还在线
        self.assertEqual(admin.json("GET", "/api/fs/roots")[0], 200)

    def test_admin_cannot_change_his_own_role(self):
        admin = self._admin()
        status, data = admin.json("POST", "/api/users/%s/update" % ADMIN_USER,
                                  {"role": "user"})
        self.assertEqual(status, 400, data)
        self.assertEqual(admin.json("GET", "/api/users")[0], 200,
                         "被拒之后他仍然应当是管理员")

    def test_admin_cannot_kick_himself(self):
        admin = self._admin()
        status, data = admin.json("POST", "/api/users/%s/kick" % ADMIN_USER, {})
        self.assertEqual(status, 400, data)

    def test_last_admin_guard_rejects_demoting_the_only_admin(self):
        """
        直接测护栏函数：链路上「唯一的管理员」永远是自己（自己一定是启用中的
        管理员），所以走接口会先被「不能改自己」拦住。这条守的是**改自己那条
        检查将来被去掉/写错**的情况 —— 那时这条仍然能兜住。
        """
        from fastapi import HTTPException

        with tempfile.TemporaryDirectory(prefix="fw-lastadmin-") as work:
            saved = users_module.USERS_PATH
            users_module.set_path(os.path.join(work, "users.json"))
            try:
                users_module.create("onlyboss", "only-boss-pass",
                                    role=users_module.ROLE_ADMIN)
                record = users_module.get("onlyboss")
                with self.assertRaises(HTTPException) as ctx:
                    _guard_last_admin(record, removing_admin=True)
                self.assertEqual(ctx.exception.status_code, 400)

                # 有第二个管理员时就该放行
                users_module.create("backupboss", "backup-pass",
                                    role=users_module.ROLE_ADMIN)
                _guard_last_admin(record, removing_admin=True)   # 不该抛
            finally:
                users_module.set_path(saved)

    # -- 审计日志 -----------------------------------------------------------

    def test_audit_records_login_and_admin_actions(self):
        admin = self._admin()
        self._create_student(admin, "aud01", display_name="丙同学")
        self._login("aud01")

        status, data = admin.json("GET", "/api/users/audit?limit=200")
        self.assertEqual(status, 200, data)
        records = data["records"]
        self.assertTrue(records, "应当有审计记录")

        events = [(row["event"], row["username"]) for row in records]
        self.assertIn(("user_create", "aud01"), events,
                      "建账号应当留痕：%s" % events[-10:])
        self.assertIn(("login_ok", "aud01"), events,
                      "登录成功应当留痕：%s" % events[-10:])

        # 操作者要能看出来（是管理员建的，不是学生自己建的）
        created = [row for row in records
                   if row["event"] == "user_create" and row["username"] == "aud01"]
        self.assertIn(ADMIN_USER, created[-1]["detail"])

    def test_failed_login_is_audited_without_leaking_whether_the_account_exists(self):
        admin = self._admin()
        _client, (status, _) = self._login("ghost-user", "wrong-password-1")
        self.assertEqual(status, 401)

        records = admin.json("GET", "/api/users/audit?limit=50")[1]["records"]
        fails = [row for row in records
                 if row["event"] == "login_fail" and row["username"] == "ghost-user"]
        self.assertTrue(fails, "登录失败也要留痕（这是有人在猜口令的早期信号）")

    # -- ★ 可见目录：界面上那段文本框的整条链路 -----------------------------

    def test_create_user_with_roots_text_assigns_visible_folders(self):
        """
        ★ 把「管理员在文本框里写的几行路径」一路走到「这个学生实际看得到什么」。

        这是多用户里最核心的动作：管理员决定某个学生只能看到哪块文件夹。
        格式由服务端解析（fileweb/users.parse_roots_text），所以这条同时也
        在钉住「文本框格式」这个前后端之间的契约。
        """
        admin = self._admin()
        mine = os.path.join(self.root, "stu-space")
        os.makedirs(mine, exist_ok=True)

        status, data = admin.json("POST", "/api/users", {
            "username": "txt01",
            "password": STUDENT_PASSWORD,
            "display_name": "文本框建号",
            "roots_text": "%s | 我的空间\n%s | 只读区 | 只读" % (mine, self.root),
        })
        self.assertEqual(status, 200, data)

        student, (status, data) = self._login("txt01")
        self.assertEqual(status, 200, data)

        status, data = student.json("GET", "/api/fs/roots")
        self.assertEqual(status, 200, data)
        roots = data["roots"]
        self.assertEqual(len(roots), 2, "两行路径应当变成两个可见根：%s" % data)
        self.assertEqual([r["name"] for r in roots if not r["readonly"]], ["我的空间"])
        self.assertEqual(len([r for r in roots if r["readonly"]]), 1,
                         "标了只读的那个根必须是只读的：%s" % data)

        # 列表里回显的文本要包含名称与只读标记，管理员才改得动
        row = self._users(admin)["txt01"]
        self.assertIn("我的空间", row["roots_text"])
        self.assertIn("只读", row["roots_text"])

    def test_updating_roots_text_replaces_the_visible_folders(self):
        """改可见目录**不需要**把对方踢下线（下次请求即生效）。"""
        admin = self._admin()
        first = os.path.join(self.root, "space-a")
        second = os.path.join(self.root, "space-b")
        for directory in (first, second):
            os.makedirs(directory, exist_ok=True)

        self._create_student(admin, "txt02", roots_text=first)
        student, (status, _) = self._login("txt02")
        self.assertEqual(status, 200)
        self.assertEqual([r["name"] for r in student.json("GET", "/api/fs/roots")[1]["roots"]],
                         [os.path.basename(first)])

        status, data = admin.json("POST", "/api/users/txt02/update",
                                  {"roots_text": second})
        self.assertEqual(status, 200, data)

        # 同一个会话里立刻生效（不需要重新登录）
        status, data = student.json("GET", "/api/fs/roots")
        self.assertEqual(status, 200, data)
        self.assertEqual([r["name"] for r in data["roots"]],
                         [os.path.basename(second)],
                         "改了可见目录应当下一次请求就生效：%s" % data)

    def test_clearing_roots_text_means_he_sees_nothing(self):
        """
        空文本 = 「让他什么都看不到」，而不是「这次不改」。

        这两件事必须分得清：混淆的后果是管理员想收权限却收不掉，
        或者只想改个显示名却顺手把人家的目录清空了。
        """
        admin = self._admin()
        mine = os.path.join(self.root, "space-c")
        os.makedirs(mine, exist_ok=True)

        self._create_student(admin, "txt03", roots_text=mine)
        student, (status, _) = self._login("txt03")
        self.assertEqual(status, 200)
        self.assertEqual(len(student.json("GET", "/api/fs/roots")[1]["roots"]), 1)

        status, data = admin.json("POST", "/api/users/txt03/update",
                                  {"roots_text": ""})
        self.assertEqual(status, 200, data)
        self.assertEqual(student.json("GET", "/api/fs/roots")[1]["roots"], [],
                         "清空之后他应当看得到空世界")

        # 只改显示名时**不能**顺手把目录清掉
        status, data = admin.json("POST", "/api/users/txt03/update",
                                  {"display_name": "只改了名字"})
        self.assertEqual(status, 200, data)
        self.assertEqual(self._users(admin)["txt03"]["display_name"], "只改了名字")
        self.assertEqual(student.json("GET", "/api/fs/roots")[1]["roots"], [],
                         "没提可见目录就一个都不该动（这里本来就是空的）")

    # -- /api/system/info：按用户返回 ---------------------------------------

    def test_system_info_reports_the_real_logged_in_user(self):
        """
        ★ 桌面拿到的「我是谁」必须来自用户表，而不是 config 的 auth 段。

        config 里的用户名只在**首次引导**时被用过一次；之后改了用户名、
        加了新账号，config 那份就过时了。照着它显示会出现
        「每个人登录后右上角都写着管理员的名字」。
        """
        admin = self._admin()
        status, data = admin.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["user"]["username"], ADMIN_USER)
        self.assertEqual(data["user"]["role"], "admin")
        self.assertTrue(data["user"]["is_admin"])
        self.assertTrue(data["features"]["users"],
                        "管理员应当看到「用户管理」入口")

    def test_student_does_not_get_the_user_management_entry(self):
        admin = self._admin()
        self._create_student(admin, "mgr01", display_name="没有管理入口")
        student, (status, _) = self._login("mgr01")
        self.assertEqual(status, 200)

        status, data = student.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["user"]["username"], "mgr01")
        self.assertEqual(data["user"]["role"], "user")
        self.assertFalse(data["user"]["is_admin"])
        self.assertFalse(data["features"]["users"],
                         "★ 子用户不该看到「用户管理」入口（服务端也会 403）")

    def test_per_user_permission_gates_the_terminal_api(self):
        """
        ★ 把某个人的命令行关掉之后，**接口也必须真的关掉**。

        只藏起界面入口是不够的：那等于管理员以为自己收了权限，
        实际只是看不见按钮。所以 /api/system/info 的 features 与
        POST /api/terminal/session 走的是同一个判断。
        """
        admin = self._admin()
        self._create_student(admin, "noterm", permissions={"terminal": False})
        self._create_student(admin, "hasterm", permissions={"terminal": True})

        blocked, (status, _) = self._login("noterm")
        self.assertEqual(status, 200)
        status, data = blocked.json("GET", "/api/system/info")
        self.assertFalse(data["features"]["terminal"],
                         "关掉之后 features.terminal 必须是 false")
        status, data = blocked.json("POST", "/api/terminal/session", {})
        self.assertEqual(status, 403, "★ 关掉之后接口也必须拒绝：%s" % data)

        allowed, (status, _) = self._login("hasterm")
        self.assertEqual(status, 200)
        status, data = allowed.json("GET", "/api/system/info")
        self.assertTrue(data["features"]["terminal"])
        status, data = allowed.json("POST", "/api/terminal/session", {})
        self.assertEqual(status, 200, "没被关掉的人应当照常能开：%s" % data)

        sid = data["id"]
        self.addCleanup(_ws_close_session,
                        "ws://127.0.0.1:%d/api/terminal/ws?sid=%s" % (self.server.port, sid),
                        {"Cookie": allowed.cookie,
                         "Origin": "http://127.0.0.1:%d" % self.server.port})

    def test_per_user_permission_gates_the_sysmon_api(self):
        admin = self._admin()
        self._create_student(admin, "nosys", permissions={"sysmon": False})
        student, (status, _) = self._login("nosys")
        self.assertEqual(status, 200)

        status, data = student.json("GET", "/api/system/info")
        self.assertFalse(data["features"]["sysmon"])

        status, data = student.json("GET", "/api/sysmon/snapshot")
        self.assertEqual(status, 403, "★ 关掉之后接口也必须拒绝：%s" % data)

        # 管理员自己不受影响
        status, data = admin.json("GET", "/api/sysmon/snapshot")
        self.assertEqual(status, 200, data)


class RootsTextFormatTests(unittest.TestCase):
    """
    `路径 | 名称 | 只读` 这个文本格式的解析与回显。

    ★ 它是管理员界面与「这个学生能看到什么」之间唯一的契约，出错就是
    把权限分给错误的人，所以解析放在服务端并单独覆盖
    （前端不做任何解析，只负责原样收发）。
    """

    def _parse(self, text):
        return users_module.parse_roots_text(text)

    def test_bare_path_becomes_one_root(self):
        roots = self._parse(r"D:\students\zhangsan")
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["path"], os.path.normpath(r"D:\students\zhangsan"))
        self.assertFalse(roots[0]["readonly"])
        # id / name 由 _normalize_roots 兜底成目录名
        self.assertEqual(roots[0]["id"], "zhangsan")
        self.assertEqual(roots[0]["name"], "zhangsan")

    def test_name_and_readonly_segments(self):
        roots = self._parse(r"D:\public | 公共资料 | 只读")
        self.assertEqual(len(roots), 1)
        self.assertEqual(roots[0]["name"], "公共资料")
        self.assertTrue(roots[0]["readonly"])

    def test_readonly_without_a_name(self):
        """`路径 | 只读` —— 第二段写的是「只读」，不能被当成名称。"""
        roots = self._parse(r"D:\ro | 只读")
        self.assertEqual(roots[0]["name"], "ro", "名称应当退回目录名")
        self.assertTrue(roots[0]["readonly"])

    def test_readonly_accepts_ascii_aliases(self):
        for word in ("ro", "RO", "readonly", "r"):
            roots = self._parse(r"D:\x | %s" % word)
            self.assertTrue(roots[0]["readonly"], "%r 应当被认成只读" % word)

    def test_comments_and_blank_lines_are_ignored(self):
        roots = self._parse("# 这是注释\n\nD:\\a\n   \n#D:\\b\nD:\\c")
        self.assertEqual([r["id"] for r in roots], ["a", "c"])

    def test_lines_without_a_path_are_dropped(self):
        roots = self._parse("| 只读\n| \n   |   ")
        self.assertEqual(roots, [], "没有路径的行应当被丢掉，而不是变成奇怪的根")

    def test_duplicate_folder_names_are_deduplicated(self):
        """id 是去重键（同名目录只留第一个），避免出现两个 id 相同的根。"""
        roots = self._parse("D:\\a\\same\nE:\\b\\same")
        self.assertEqual(len(roots), 1, "id 撞车时应当只留一个：%s" % roots)

    def test_forward_slashes_are_normalized(self):
        roots = self._parse("D:/students/x")
        self.assertEqual(roots[0]["path"], os.path.normpath("D:/students/x"))

    # -- 回显（管理界面文本框里的内容）--------------------------------------

    def test_format_hides_the_derived_name(self):
        """名称与目录名相同时不显示 —— 那是自动派生的，写出来只是噪音。"""
        roots = self._parse(r"D:\students\zhangsan")
        self.assertEqual(users_module.format_roots_text(roots),
                         os.path.normpath(r"D:\students\zhangsan"))

    def test_format_keeps_custom_name_and_readonly(self):
        roots = self._parse(r"D:\public | 公共资料 | 只读")
        text = users_module.format_roots_text(roots)
        self.assertIn("公共资料", text)
        self.assertIn("只读", text)

    def test_empty_roots_format_to_empty_text(self):
        self.assertEqual(users_module.format_roots_text([]), "")
        self.assertEqual(users_module.format_roots_text(None), "")

    def test_round_trip_is_stable(self):
        """
        ★ 回显再解析必须得到同一份配置。

        不稳定的后果很隐蔽：管理员点开编辑、什么都没改就保存，
        权限却变了（例如只读位被吃掉）。
        """
        samples = [
            "",
            r"D:\students\zhangsan",
            r"D:\public | 公共资料 | 只读",
            r"D:\ro-only | 只读",
            "D:\\a\nE:\\b | 乙盘\nF:\\c | 丙 | 只读",
        ]
        for text in samples:
            first = self._parse(text)
            second = self._parse(users_module.format_roots_text(first))
            self.assertEqual(first, second,
                             "「%s」来回一趟后配置变了：%s -> %s"
                             % (text, first, second))


if __name__ == "__main__":
    unittest.main(verbosity=2)

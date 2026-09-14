# -*- coding: utf-8 -*-
"""
多用户：命令行会话的名额与归属（阶段4）
=====================================

改造前 `terminal.max_sessions` 是**全机**上限：谁开的窗口都算在一起。
多用户下这行不通 —— 导师留一个、两个学生各开一个，第四个人就被挡在门外，
而且被挡住的人根本看不出是谁占了名额。

现在名额按**归属（owner）**算。本文件钉住四件事：

1. 每人各有各的额度，互不影响；
2. ★★ **名额满了只在自己人的会话里挑人顶掉** —— 学生开新窗口绝不能杀掉
   同学正在跑 PyTorch 的窗口。这是本文件里最要紧的一条；
3. 全机总量仍有兜底（`max_sessions_total`），防「每人 5 个 × 很多人」；
4. 额度取自**用户表**（每人的 `max_terminal_sessions`），与配置里的全站
   政策上限取小。

不传 owner 的会话（既有单元测试、以及改造前的调用方）全部算作同一个人，
因此行为与改造前完全一致 —— 这也是既有测试不用改的原因。
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import time
import unittest

from fileweb import config as config_module
from fileweb import users as users_module
from fileweb.terminal import TerminalLimitError
from fileweb.terminal import manager as terminal_manager
from tests._harness import ServerProcess
from tests.test_features import _ws_close_session

ALICE = "alice"
BOB = "bob"
CAROL = "carol"

ADMIN_USER = "teacher"
ADMIN_PASSWORD = "admin-pass-123"
STUDENT_PASSWORD = "student-pass-123"


class SessionQuotaTests(unittest.TestCase):
    """
    直接对着 TerminalManager 验证「名额按归属算」。

    这里会真的起 cmd.exe（与 test_terminal_reattach.py 同样的做法），
    所以每个用例只开必要的几个，tearDown 里一律全部关掉。

    ★ 每个用例的全部会话都必须在**同一次 asyncio.run** 里建完：
      锁是懒创建的，第二次 asyncio.run 会拿到属于已关闭循环的那把锁。
    """

    def setUp(self):
        terminal_manager._sessions.clear()
        terminal_manager._lock = None

    def tearDown(self):
        # 兜底：不在测试进程里留下 cmd.exe
        asyncio.run(terminal_manager.close_all())
        terminal_manager._lock = None
        terminal_manager._sessions.clear()

    def _create(self, owner, limit=2, max_total=0):
        return terminal_manager.create(
            shell="cmd.exe",
            start_dir=os.getcwd(),
            idle_timeout=0,          # 与新的默认值一致：不自动回收
            max_sessions=limit,
            session_token="unit-test-token",
            cols=80,
            rows=24,
            owner=owner,
            max_total=max_total,
        )

    @staticmethod
    def _detach_and_age(session, seconds=3600):
        """
        把会话变成「分离且早已闲置」，也就是**允许被顶掉**的状态。

        顶掉有两个前提（见 _pick_evictable_locked）：没有客户端连着、
        且闲置超过了宽限期。这里把「变成无人连接」的时刻拨到过去，
        从而不必真等一个宽限期。
        """
        token = session.attach()
        session.detach(token)
        session._detached_at = time.time() - seconds

    # -- 每用户独立额度 -----------------------------------------------------

    def test_quota_is_counted_per_user(self):
        """一个人占满自己的额度，不影响另一个人开窗口。"""
        async def run():
            first = await self._create(ALICE, limit=2)
            second = await self._create(ALICE, limit=2)
            with self.assertRaises(TerminalLimitError) as ctx:
                await self._create(ALICE, limit=2)
            # bob 完全不受 alice 占满的影响
            bob_session = await self._create(BOB, limit=2)
            return first, second, bob_session, str(ctx.exception)

        first, second, bob_session, message = asyncio.run(run())

        for session in (first, second, bob_session):
            self.assertIn(session.sid, terminal_manager._sessions)
        self.assertIn("你的命令行会话数已达上限", message,
                      "报错应当说清是「你」的额度，而不是一句无主语的限制")

    def test_ownerless_sessions_share_one_quota(self):
        """
        不传 owner 时所有会话算同一个人 —— 改造前的行为必须原样保留。

        （既有单元测试就是这么构造会话的，它们一条都没改。）
        """
        async def run():
            await self._create("", limit=1)
            with self.assertRaises(TerminalLimitError):
                await self._create("", limit=1)
            return True

        self.assertTrue(asyncio.run(run()))

    # -- ★★ 淘汰绝不跨用户 -------------------------------------------------

    def test_full_quota_never_evicts_another_users_session(self):
        """
        ★★ 自己名额满了要报错，**不能**顺手把别人闲置的窗口顶掉。

        这是最容易写错、后果也最重的一处：顶掉等于直接杀掉同学正在跑的
        训练进程。alice 那个会话在这个用例里确实「可被顶掉」（分离且久未
        使用）—— 但只对 alice 自己成立。
        """
        async def run():
            alice = await self._create(ALICE, limit=1)
            self._detach_and_age(alice)         # 对 alice 自己而言已可顶掉

            bob = await self._create(BOB, limit=1)
            # bob 的额度满了，而他那个会话还连着客户端（不可顶掉）→ 必须报错
            with self.assertRaises(TerminalLimitError):
                await self._create(BOB, limit=1)
            return alice, bob

        alice, bob = asyncio.run(run())

        self.assertIn(alice.sid, terminal_manager._sessions,
                      "★ bob 名额满时绝不能顶掉 alice 的会话")
        self.assertTrue(alice.is_alive(), "★ alice 的 cmd.exe 必须还在跑")
        self.assertIn(bob.sid, terminal_manager._sessions)

    def test_eviction_picks_the_same_owners_idle_session(self):
        """
        ★ 名额满了该顶掉**自己**闲置最久的那个，并且只在自己人里挑。
        """
        async def run():
            alice_old = await self._create(ALICE, limit=2)
            alice_live = await self._create(ALICE, limit=2)
            bob_old = await self._create(BOB, limit=2)

            self._detach_and_age(alice_old, 7200)
            self._detach_and_age(bob_old, 7200)

            # alice 额度满了：她的新窗口应当顶掉 alice_old
            alice_new = await self._create(ALICE, limit=2)
            return alice_old, alice_live, bob_old, alice_new

        alice_old, alice_live, bob_old, alice_new = asyncio.run(run())

        self.assertNotIn(alice_old.sid, terminal_manager._sessions,
                         "被顶掉的应当是她自己闲置最久的那个会话")
        self.assertFalse(alice_old.is_alive(),
                         "顶掉必须真的杀掉进程，不能只摘除注册表条目")
        self.assertIn(alice_live.sid, terminal_manager._sessions,
                      "有人连着的会话（正在用）绝不能被顶掉")
        self.assertIn(alice_new.sid, terminal_manager._sessions)

        self.assertIn(bob_old.sid, terminal_manager._sessions,
                      "★ 别人闲置再久也不该被顶掉")
        self.assertTrue(bob_old.is_alive(), "★ 同学的 cmd.exe 必须还在跑")

    # -- 全机兜底与统计 -----------------------------------------------------

    def test_global_cap_bounds_the_whole_machine(self):
        """全机总量兜底：每人都在额度内，但总量到顶时也不再放行。"""
        async def run():
            await self._create(ALICE, limit=5, max_total=2)
            await self._create(BOB, limit=5, max_total=2)
            with self.assertRaises(TerminalLimitError) as ctx:
                await self._create(CAROL, limit=5, max_total=2)
            return str(ctx.exception)

        message = asyncio.run(run())
        self.assertIn("总数已达上限", message,
                      "总量兜底要给出与「个人额度」不同的原因，否则用户会以为是自己开多了")

    def test_owner_counts_groups_sessions_by_user(self):
        """管理界面要按用户显示「几个窗口」，这里钉住统计口径。"""
        async def run():
            await self._create(ALICE, limit=5)
            await self._create(ALICE, limit=5)
            await self._create(BOB, limit=5)
            return terminal_manager.owner_counts()

        counts = asyncio.run(run())
        self.assertEqual(counts.get(ALICE), 2)
        self.assertEqual(counts.get(BOB), 1)
        self.assertEqual(counts.get(CAROL, 0), 0)


class TerminalQuotaApiTests(unittest.TestCase):
    """
    端到端确认「额度取自用户表」。

    单元测试只证明了管理器按 owner 计数；只有跑通这里，才说明路由器确实
    把**用户记录里的那个额度**读了出来（而不是继续用配置里那个全站值）。
    """

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-tquota-")
        cls.root = os.path.join(cls.work, "root")
        os.makedirs(cls.root, exist_ok=True)

        def _extra(cfg):
            # 脚手架默认把命令行关掉（安全默认），这个用例必须显式打开。
            cfg["terminal"]["enabled"] = True
            # 全站政策上限给 5：如果额度取错了对象（取了配置而不是用户表），
            # 下面「额度 1」的账号就能开出 5 个窗口，测试立刻失败。
            cfg["terminal"]["max_sessions"] = 5
            cfg["terminal"]["idle_timeout_seconds"] = 0

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD, extra_config=_extra,
        ).start()

        # 用户表就在这个测试实例的配置旁边（app 已经引导出管理员）。
        cls._saved_users_path = users_module.USERS_PATH
        users_module.set_path(os.path.join(
            os.path.dirname(cls.server.cfg_path), "users.json"))
        users_module.create("one", STUDENT_PASSWORD,
                            display_name="只给一个窗口", max_terminal_sessions=1)
        users_module.create("five", STUDENT_PASSWORD,
                            display_name="给五个窗口", max_terminal_sessions=5)

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

    def _open(self, client):
        return client.json("POST", "/api/terminal/session", {})

    def _ws_url(self, sid):
        return "ws://127.0.0.1:%d/api/terminal/ws?sid=%s" % (self.server.port, sid)

    def _headers(self, client):
        return {"Cookie": client.cookie,
                "Origin": "http://127.0.0.1:%d" % self.server.port}

    def _close(self, client, sid):
        """
        显式结束会话。

        会话可分离之后，断开 WebSocket 只等于「挂起」（进程继续跑、继续占
        名额），所以清理必须发 {"type":"close"}，否则退出的 cmd.exe 会留在
        这台机器上，后面的用例也会莫名拿到 429。
        """
        _ws_close_session(self._ws_url(sid), self._headers(client))

    # -- 测试 ---------------------------------------------------------------

    def test_quota_comes_from_the_user_record(self):
        one = self._login("one")
        status, data = self._open(one)
        self.assertEqual(status, 200, data)
        self.addCleanup(self._close, one, data["id"])
        self.assertEqual(data.get("expires_in"), 0,
                         "idle_timeout_seconds=0 应当如实反映在会话信息里")

        status, data = self._open(one)
        self.assertEqual(status, 429,
                         "额度 1 的账号不该能开出第二个窗口：%s" % data)
        self.assertIn("1 个", str(data.get("message") or data),
                      "报错里应当写明是**这个人的**额度（1），而不是全站上限（5）")

        # ★ 另一个人不受影响 —— 这正是「每人 5 个」而不是「全机 5 个」
        five = self._login("five")
        status, data = self._open(five)
        self.assertEqual(status, 200,
                         "别人占满自己的额度不该影响我：%s" % data)
        self.addCleanup(self._close, five, data["id"])

    def test_admin_quota_is_independent_from_students(self):
        """管理员开窗口也不该被学生的占用挡住（名额不再共享）。"""
        student = self._login("five")
        status, data = self._open(student)
        self.assertEqual(status, 200, data)
        self.addCleanup(self._close, student, data["id"])

        admin = self.server.login_client()
        status, data = self._open(admin)
        self.assertEqual(status, 200, "管理员应当有自己的额度：%s" % data)
        self.addCleanup(self._close, admin, data["id"])

    def test_student_without_roots_can_still_open_a_shell(self):
        """
        ★ 「还没分配目录的新学生」也必须能开命令行。

        这条踩过一个真实的 500：启动目录的回退分支里残留着改造前的
        `state.base_dir`，而**没有根的用户正好会走到那条分支**
        （他的第一个根不存在，所以 `resolver.first()` 给不出目录）。

        值得留意的是它躲过了当时全套 315 条测试 —— 因为此前没有任何用例
        构造过「没有根的子用户去开终端」这个组合。子用户有全权限 cmd
        是已接受的风险，但「打不开」纯粹是 bug，不是设计。
        """
        client = self._login("five")
        status, data = self._open(client)
        self.assertEqual(status, 200, "没有根的子用户开终端不该失败：%s" % data)
        self.addCleanup(self._close, client, data["id"])
        self.assertTrue(data.get("cwd"),
                        "回退到兜底目录时也要给出一个可用的起始目录")


class TerminalDefaultsTests(unittest.TestCase):
    """
    新默认值本身也要钉住 —— 它们是用户逐条确认下来的决定，
    改错了不会让任何接口报错，只会让行为悄悄变回改造前。
    """

    def setUp(self):
        # prepare() 会顺带把路径下发给这两个模块（进程内的全局副作用），
        # 用完恢复，免得影响同进程里别的用例。
        from fileweb import shortcuts as shortcuts_module
        from fileweb import userstate as userstate_module

        self._saved = (userstate_module.ACTIVE_PATH, shortcuts_module.ACTIVE_PATH)

    def tearDown(self):
        from fileweb import shortcuts as shortcuts_module
        from fileweb import userstate as userstate_module

        userstate_module.ACTIVE_PATH, shortcuts_module.ACTIVE_PATH = self._saved

    def test_terminal_defaults_match_the_agreed_decisions(self):
        # ★ 必须指向一个**不存在**的路径 + create_if_missing=False：
        #   这样 load() 直接回默认值，既不读也不写任何真实文件。
        #   （传空串是不行的：那会退回模块默认的 CONFIG_PATH，也就是真实配置。）
        cfg = config_module.prepare(config_module.load(
            os.path.join(tempfile.gettempdir(), "fw-no-such-config.json"),
            create_if_missing=False))
        terminal = cfg["terminal"]

        self.assertEqual(terminal["idle_timeout_seconds"], 0,
                         "cmd 关掉后不应再靠「半小时自动回收」兜底")
        self.assertEqual(terminal["max_sessions"], 5,
                         "每人 5 个窗口")
        self.assertEqual(terminal["max_sessions_total"], 64,
                         "全机总量兜底要远大于单人额度，否则总量会先卡住")

    def test_a_config_without_terminal_section_gets_the_new_defaults(self):
        """老配置（没有 terminal 段，或只有旧值）升上来也要拿到新默认。"""
        cfg = config_module.prepare({"terminal": {"max_sessions": 4}})
        terminal = cfg["terminal"]

        self.assertEqual(terminal["max_sessions"], 4, "显式写的值要尊重")
        self.assertEqual(terminal["idle_timeout_seconds"], 0)
        self.assertEqual(terminal["max_sessions_total"], 64)


if __name__ == "__main__":
    unittest.main(verbosity=2)

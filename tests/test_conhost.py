# -*- coding: utf-8 -*-
"""
控制台镜像（方案 A）的回归测试
==============================

覆盖的都是「这个功能特有的、容易悄悄退化」的点：

    * 开关闸门：enabled=false 必须真的拒绝（这个功能会把别人控制台的
      屏幕内容送到浏览器，绝不能因为配置写错就默认打开）
    * 输入注入闸门：allow_input=false 必须拒绝 —— 「看一眼」和
      「替人在键盘上打字」不是一个量级的风险，默认必须是只读
    * 聚合逻辑：一个命令行窗口里会挂着好几个进程，必须按**控制台**聚合，
      否则界面会显示成「开了十几个窗口」（这正是它要回答的问题）
    * 全角尾格：中文在全角字符后带一个 TRAILING_BYTE 的尾格，不跳过的话
      每个汉字会显示两遍
    * 辅助进程的协议：未知操作/坏 pid 都要给出可读错误，而不是崩掉或挂住
    * 管理员独占：/api/conhost/* 全部走 require_admin

不需要真控制台的那部分用例在任何平台都能跑；真正要附加控制台的用例只在
Windows 上跑（其它平台 skip），因为它们必然失败。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from fileweb import conhost                                            # noqa: E402
from fileweb import conhost_helper as helper                           # noqa: E402

IS_WINDOWS = sys.platform.startswith("win")


def cfg(**over):
    base = {"enabled": True, "allow_input": False,
            "max_lines": 500, "max_input_chars": 2000}
    base.update(over)
    return {"conhost": base}


# ---------------------------------------------------------------------------
# 开关闸门
# ---------------------------------------------------------------------------

class AvailabilityTests(unittest.TestCase):

    def test_disabled_by_default(self):
        """默认必须是关的。这个功能把别人的控制台内容送到浏览器，
        宁可让人显式打开 —— 所以缺省即关闭，和其它功能的惯例相反。"""
        from fileweb import config as config_module
        default = config_module.DEFAULT_CONFIG["conhost"]
        self.assertFalse(default["enabled"])
        self.assertFalse(default["allow_input"])

    def test_available_rejects_disabled(self):
        ok, reason = conhost.available(cfg(enabled=False))
        self.assertFalse(ok)
        self.assertIn("关闭", reason)

    def test_available_rejects_non_windows(self):
        """只有 Windows 才有经典控制台窗口。"""
        if IS_WINDOWS:
            self.skipTest("Windows 上这条没有意义")
        ok, reason = conhost.available(cfg())
        self.assertFalse(ok)
        self.assertIn("Windows", reason)

    def test_list_rejects_when_disabled(self):
        with self.assertRaises(conhost.ConhostError):
            conhost.list_consoles(cfg(enabled=False))

    def test_read_rejects_when_disabled(self):
        with self.assertRaises(conhost.ConhostError):
            conhost.read_console(cfg(enabled=False), 1234)

    def test_write_rejects_when_input_disabled(self):
        """★ 关键：只看（读）与打字（写）是两个独立开关。
        这里用 allow_input=False，即便 enabled=True 也必须拒绝。"""
        with self.assertRaises(conhost.ConhostError) as ctx:
            conhost.write_console(cfg(allow_input=False), 1234, "echo hi\r")
        self.assertIn("allow_input", str(ctx.exception))

    def test_write_checks_input_toggle_before_pid(self):
        """闸门顺序：先看开关再看 pid —— 否则一个坏 pid 会让调用方
        以为是 pid 的问题，掩盖了「功能没开」这个真正的原因。"""
        with self.assertRaises(conhost.ConhostError) as ctx:
            conhost.write_console(cfg(allow_input=False), -1, "x")
        self.assertIn("allow_input", str(ctx.exception))


# ---------------------------------------------------------------------------
# 纯逻辑：属性压缩 / 全角尾格
# ---------------------------------------------------------------------------

class AttrRunsTests(unittest.TestCase):

    def test_single_run_when_uniform(self):
        runs = helper._attr_runs([7, 7, 7, 7])
        self.assertEqual(runs, [[0, 4, 7]])

    def test_runs_split_on_change(self):
        runs = helper._attr_runs([7, 7, 12, 12, 12, 7])
        self.assertEqual(runs, [[0, 2, 7], [2, 3, 12], [5, 1, 7]])

    def test_empty(self):
        self.assertEqual(helper._attr_runs([]), [])


class TrailingByteTests(unittest.TestCase):
    """全角字符占两个单元格，第二个带 TRAILING_BYTE —— 必须跳过，
    否则屏幕上每个汉字都会出现两遍（本功能第一次实测就撞上了）。"""

    def test_constant_value(self):
        self.assertEqual(helper.TRAILING_BYTE, 0x0200)

    def test_documented_in_module(self):
        doc = helper.__doc__ or ""
        self.assertIn("TRAILING_BYTE", doc)


# ---------------------------------------------------------------------------
# 聚合逻辑（按控制台，不按进程）
# ---------------------------------------------------------------------------

class GroupingTests(unittest.TestCase):
    """
    用一个假辅助进程喂数据，验证 list_consoles 的聚合。

    为什么值得测：一个命令行窗口里往往挂着 cmd.exe + python.exe + node.exe；
    按进程列出来会显示成「十几个窗口」，而这个界面存在的意义恰恰是回答
    「主机上有几个命令行窗口」。
    """

    def setUp(self):
        self._orig_helper = conhost._helper
        self._orig_rows = conhost._process_rows
        self._orig_own = conhost._own_console_pids
        self._orig_wins = conhost._window_state

    def tearDown(self):
        conhost._helper = self._orig_helper
        conhost._process_rows = self._orig_rows
        conhost._own_console_pids = self._orig_own
        conhost._window_state = self._orig_wins

    def _fake(self, items, procs, wins):
        class FakeHelper:
            def request(self, op, **kw):
                if op == "ping":
                    return {"ok": True, "helper_pid": 999}
                return {"ok": True, "items": items}
        conhost._helper = FakeHelper()
        conhost._process_rows = lambda: procs
        conhost._own_console_pids = lambda: set()
        conhost._window_state = lambda: wins

    def test_three_processes_in_one_console_become_one_entry(self):
        """★ 核心用例：cmd + python + node 共享一个控制台 → 只能算**一个**。"""
        self._fake(
            items=[
                {"pid": 100, "hwnd": 555, "title": "start.bat",
                 "console_pids": [100, 101, 999]},          # 999 是辅助进程自己
                {"pid": 101, "hwnd": 555, "title": "start.bat",
                 "console_pids": [100, 101, 999]},
                {"pid": 102, "hwnd": 555, "title": "start.bat",
                 "console_pids": [100, 101, 999]},
            ],
            procs=[{"pid": 100, "name": "cmd.exe", "user": "u", "started": 0},
                   {"pid": 101, "name": "python.exe", "user": "u", "started": 0},
                   {"pid": 102, "name": "node.exe", "user": "u", "started": 0}],
            wins={555: {"title": "start.bat", "visible": True, "minimized": False}},
        )
        data = conhost.list_consoles(cfg())
        self.assertEqual(data["count"], 1, "三个进程共享一个控制台，应聚合为 1 个")
        item = data["items"][0]
        self.assertEqual(item["member_count"], 3)
        # 辅助进程自己不该出现在成员里（否则聚合键会被它污染）
        self.assertNotIn(999, [m["pid"] for m in item["members"]])
        # 读写的 pid 应优先取命令行解释器，而不是随便一个成员
        self.assertEqual(item["pid"], 100)
        self.assertTrue(item["has_window"])
        self.assertEqual(item["hwnd"], 555)

    def test_two_consoles_stay_separate(self):
        self._fake(
            items=[
                {"pid": 100, "hwnd": 555, "title": "A", "console_pids": [100]},
                {"pid": 200, "hwnd": 666, "title": "B", "console_pids": [200]},
            ],
            procs=[{"pid": 100, "name": "cmd.exe", "user": "u", "started": 0},
                   {"pid": 200, "name": "cmd.exe", "user": "u", "started": 0}],
            wins={555: {"title": "A", "visible": True, "minimized": False},
                  666: {"title": "B", "visible": True, "minimized": True}},
        )
        data = conhost.list_consoles(cfg())
        self.assertEqual(data["count"], 2)
        self.assertEqual(data["windows"], 2)

    def test_headless_consoles_are_not_merged(self):
        """★ 没有窗口的控制台 hwnd 都是 0。若按 hwnd 聚合，它们会被错误地
        并成一个 —— 所以聚合键必须用「成员集合」。"""
        self._fake(
            items=[
                {"pid": 100, "hwnd": 0, "title": "", "console_pids": [100]},
                {"pid": 200, "hwnd": 0, "title": "", "console_pids": [200]},
            ],
            procs=[{"pid": 100, "name": "svc.exe", "user": "u", "started": 0},
                   {"pid": 200, "name": "svc.exe", "user": "u", "started": 0}],
            wins={},
        )
        data = conhost.list_consoles(cfg())
        self.assertEqual(data["count"], 2,
                         "两个无窗口控制台各自独立，不能被并成一个")
        self.assertEqual(data["windows"], 0)
        self.assertFalse(data["items"][0]["has_window"])

    def test_process_without_console_is_ignored(self):
        self._fake(
            items=[{"pid": 100, "hwnd": 0, "title": "", "console_pids": []}],
            procs=[{"pid": 100, "name": "explorer.exe", "user": "u", "started": 0}],
            wins={},
        )
        data = conhost.list_consoles(cfg())
        self.assertEqual(data["count"], 0)

    def test_allow_input_is_reported_to_frontend(self):
        """前端据此决定要不要渲染输入区 —— 没开就必须是纯只读界面。"""
        self._fake(items=[], procs=[], wins={})
        self.assertFalse(conhost.list_consoles(cfg())["allow_input"])
        self.assertTrue(conhost.list_consoles(cfg(allow_input=True))["allow_input"])


# ---------------------------------------------------------------------------
# 输入长度上限
# ---------------------------------------------------------------------------

class InputLimitTests(unittest.TestCase):

    def test_over_limit_is_rejected_before_touching_console(self):
        """超长输入要在**附加控制台之前**就被挡住：否则等于往别人的窗口里
        灌了几万个按键事件。"""
        long_text = "x" * 5000
        with self.assertRaises(conhost.ConhostError) as ctx:
            conhost.write_console(cfg(allow_input=True, max_input_chars=100),
                                  1234, long_text)
        message = str(ctx.exception)
        self.assertIn("100", message)          # 上限
        self.assertIn("5000", message)         # 实际长度


# ---------------------------------------------------------------------------
# 辅助进程协议（真起进程，需要 Windows）
# ---------------------------------------------------------------------------

@unittest.skipUnless(IS_WINDOWS, "辅助进程依赖 Windows 控制台 API")
class HelperProtocolTests(unittest.TestCase):
    """
    真起 helper 子进程对话。只测**不需要附加真实控制台**的那几条：
    ping / 未知操作 / 坏 pid —— 它们在任何机器上都应该有确定的结果。
    """

    def setUp(self):
        self.proc = subprocess.Popen(
            [sys.executable, os.path.join(BASE_DIR, "fileweb",
                                          "conhost_helper.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8",
            errors="replace", bufsize=1)
        self.seq = 0

    def tearDown(self):
        # 三个管道都要显式关掉：只关 stdin 会让 unittest 在收尾时打一堆
        # ResourceWarning（"unclosed file"），把真正有用的失败信息淹掉。
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except Exception:                               # noqa: BLE001
                pass
        try:
            self.proc.wait(timeout=5)
        except Exception:                                   # noqa: BLE001
            self.proc.kill()

    def call(self, op, **kw):
        self.seq += 1
        req = {"id": self.seq, "op": op}
        req.update(kw)
        self.proc.stdin.write(json.dumps(req) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        self.assertTrue(line, "helper 没有回应")
        return json.loads(line)

    def test_ping_reports_no_console(self):
        """★ helper 自己必须**没有控制台**，否则 AttachConsole 会
        报 ERROR_ACCESS_DENIED(5)。这是整个功能的前提。"""
        res = self.call("ping")
        self.assertTrue(res["ok"])
        self.assertEqual(res["console"], 0,
                         "helper 竟然还有控制台 —— AttachConsole 会失败")
        self.assertGreater(res["helper_pid"], 0)

    def test_unknown_op_is_a_readable_error(self):
        res = self.call("no_such_op")
        self.assertFalse(res["ok"])
        self.assertIn("未知操作", res["error"])

    def test_bad_pid_is_a_readable_error(self):
        res = self.call("read", pid=999999, mode="log", lines=5)
        self.assertFalse(res["ok"])
        self.assertIn("附加失败", res["error"])

    def test_unknown_named_key_is_rejected(self):
        res = self.call("write", pid=999999, text="", keys=["no_such_key"])
        self.assertFalse(res["ok"])
        self.assertIn("未知按键", res["error"])

    def test_struct_layout_matches_windows_abi(self):
        """INPUT_RECORD 在 Windows 上必须是 20 字节（2 字节 EventType +
        2 字节对齐填充 + 16 字节 KEY_EVENT_RECORD）。布局错了的话
        WriteConsoleInput 会静默地把按键送成垃圾。"""
        import ctypes
        self.assertEqual(ctypes.sizeof(helper.INPUT_RECORD), 20)
        self.assertEqual(ctypes.sizeof(helper.KEY_EVENT_RECORD), 16)


# ---------------------------------------------------------------------------
# 接口层：管理员独占 / 开关 / 形状（起真服务）
# ---------------------------------------------------------------------------

@unittest.skipUnless(IS_WINDOWS, "控制台镜像只在 Windows 上有意义")
class EndpointTests(unittest.TestCase):
    """
    起一个真实的服务实例，走 HTTP。

    这一层要钉住的是**只有接口层才有**的保证：
      * 子用户一律 403（这个功能不做「可单独授权」，硬性限定管理员）；
      * /api/system/info 里的 features.conhost 与接口口径一致
        （否则会出现「界面有入口、点了就 403」这种不一致）；
      * allow_input=false 时输入接口必须拒绝；
      * 读一个不存在的 pid 要给可读错误，而不是 500。
    """

    STUDENT = "chstudent01"
    STUDENT_PASSWORD = "student-pass-12345"

    @classmethod
    def setUpClass(cls):
        import tempfile
        from tests._harness import ServerProcess, redirect_state_paths  # noqa: F401

        cls.work = tempfile.mkdtemp(prefix="fw-conhost-")
        root = os.path.join(cls.work, "root")
        os.makedirs(root, exist_ok=True)

        def extra(cfg):
            cfg["conhost"]["enabled"] = True
            cfg["conhost"]["allow_input"] = False      # 本类只测只读那一侧

        cls.server = ServerProcess([{"id": "r1", "name": "root", "path": root}],
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

    def test_status_for_admin(self):
        admin = self._admin()
        status, data = admin.json("GET", "/api/conhost/status")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["available"], data)
        self.assertFalse(data["allow_input"], data)
        self.assertTrue(data["read_only"], "没开输入注入时必须是只读界面")

    def test_feature_flag_matches_endpoint(self):
        """features.conhost 必须与 status.available 一致 —— 两边跑偏就会
        出现「桌面上有入口、点开却是 403」（这个项目为这类不一致专门写过注释）。"""
        admin = self._admin()
        status, info = admin.json("GET", "/api/system/info")
        self.assertEqual(status, 200, info)
        feature = (info.get("features") or {}).get("conhost")
        _, st = admin.json("GET", "/api/conhost/status")
        self.assertEqual(bool(feature), bool(st["available"]),
                         "features.conhost 与 /api/conhost/status 口径不一致")

    def test_list_shape(self):
        admin = self._admin()
        status, data = admin.json("GET", "/api/conhost/list")
        self.assertEqual(status, 200, data)
        for key in ("count", "windows", "items", "scanned", "allow_input"):
            self.assertIn(key, data)
        # 列表项的形状（有控制台时才校验字段）
        for item in data["items"]:
            for key in ("key", "pid", "title", "has_window", "members",
                        "member_count", "hwnd"):
                self.assertIn(key, item)
            self.assertIsInstance(item["members"], list)

    def test_read_missing_pid_is_readable_error(self):
        admin = self._admin()
        status, data = admin.json("GET", "/api/conhost/read?pid=999999&lines=5")
        self.assertEqual(status, 502, data)
        self.assertIn("附加失败", json.dumps(data, ensure_ascii=False))

    def test_input_rejected_when_disabled(self):
        """★ 这是本功能最重要的闸门：默认只读。"""
        admin = self._admin()
        status, data = admin.json("POST", "/api/conhost/input",
                                  {"pid": 4, "text": "echo hi\r"})
        self.assertEqual(status, 400, data)
        blob = json.dumps(data, ensure_ascii=False)
        self.assertIn("allow_input", blob)

    def test_input_requires_text_or_keys(self):
        admin = self._admin()
        status, data = admin.json("POST", "/api/conhost/input",
                                  {"pid": 4, "text": "", "keys": []})
        self.assertEqual(status, 400, data)

    def test_sub_user_gets_403_everywhere(self):
        """★ 硬边界：子用户不能看别人的控制台。
        注意这不是「可单独授权」的功能 —— 即便管理员在用户管理里什么都没关，
        子用户也必须拿不到。"""
        admin = self._admin()
        status, data = admin.json("POST", "/api/users", {
            "username": self.STUDENT,
            "password": self.STUDENT_PASSWORD,
            "display_name": self.STUDENT,
        })
        self.assertEqual(status, 200, data)

        student = self.server.client()
        status, data = student.login(self.STUDENT, self.STUDENT_PASSWORD)
        self.assertEqual(status, 200, data)

        for method, url, body in (
            ("GET", "/api/conhost/list", None),
            ("GET", "/api/conhost/read?pid=4", None),
            # ★ 两种 body 都要 403：
            #   {} 是「能解析但不合法」，正常形状是「对方知道接口长什么样」。
            #   曾经这里对 {} 返回 422 —— 因为 FastAPI 先校验请求体、
            #   再进函数体，鉴权那句根本没执行。修法见 InputPayload 的说明。
            ("POST", "/api/conhost/input", {}),
            ("POST", "/api/conhost/input",
             {"pid": 4, "text": "echo hi\r", "keys": ["enter"]}),
        ):
            got, resp = student.json(method, url, body)
            self.assertEqual(got, 403,
                             "%s %s 应当仅限管理员：%s"
                             % (method, url, json.dumps(resp, ensure_ascii=False)))

        # status 不要求管理员（前端要据此决定显不显示入口），
        # 但对子用户必须返回 available=false
        got, body = student.json("GET", "/api/conhost/status")
        self.assertEqual(got, 200, body)
        self.assertFalse(body["available"], body)

        # 子用户的功能标记也必须是 false
        got, info = student.json("GET", "/api/system/info")
        self.assertEqual(got, 200, info)
        self.assertFalse((info.get("features") or {}).get("conhost"))

    def test_anonymous_is_rejected(self):
        anon = self.server.client()
        for method, url in (("GET", "/api/conhost/list"),
                            ("GET", "/api/conhost/status")):
            got, _ = anon.json(method, url)
            self.assertNotEqual(got, 200, "%s 不该允许未登录访问" % url)


if __name__ == "__main__":
    unittest.main()

# -*- coding: utf-8 -*-
"""
任务管理器（GET /api/sysmon/snapshot）的回归测试。

覆盖三类容易出问题的地方：

    1. **结构完整性** —— 前端 5 张卡片与进程表全靠这一份快照，少一个键就是白屏；
    2. **不泄露命令行** —— cmdline 里经常带着密码与令牌（`mysql -pXXX`），
       这个接口是输出给远端浏览器的，所以刻意只给名称/用户/资源占用；
    3. **开关一致性** —— sysmon.enabled=false 时既要 403，
       也要让 /api/system/info 的 features.sysmon 变成 false，
       否则桌面会显示一个点开就报错的入口。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from fileweb import sysmon
from tests._harness import ServerProcess


def _make_root(prefix):
    return tempfile.mkdtemp(prefix=prefix)


class SysmonSnapshotTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = _make_root("fw-sysmon-")
        cls.server = ServerProcess([{
            "id": "share", "name": "共享目录", "path": cls.root, "readonly": False,
        }])
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()

    def setUp(self):
        self.client = self.server.login_client()

    def _snapshot(self, **params):
        query = "&".join("%s=%s" % (k, v) for k, v in sorted(params.items()))
        path = "/api/sysmon/snapshot" + (("?" + query) if query else "")
        return self.client.json("GET", path)

    # -- 结构 ---------------------------------------------------------------

    def test_returns_every_section_the_ui_needs(self):
        status, data = self._snapshot()
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("available"), data)
        for key in ("cpu", "memory", "disk", "network", "gpu", "system", "processes"):
            self.assertIn(key, data, "快照缺少 %s 段" % key)

    def test_cpu_section_is_sane(self):
        _, data = self._snapshot()
        cpu = data["cpu"]
        self.assertGreater(cpu.get("count_logical") or 0, 0)
        self.assertIsInstance(cpu.get("per_cpu"), list)
        self.assertEqual(len(cpu["per_cpu"]), cpu["count_logical"],
                         "每个逻辑核都应有一个数字")
        self.assertGreaterEqual(cpu.get("percent"), 0)
        self.assertLessEqual(cpu.get("percent"), 100.5)

    def test_memory_section_adds_up(self):
        _, data = self._snapshot()
        mem = data["memory"]
        self.assertGreater(mem.get("total") or 0, 0)
        self.assertGreaterEqual(mem.get("used") or 0, 0)
        self.assertLessEqual(mem.get("used"), mem.get("total") + 1)
        self.assertGreaterEqual(mem.get("percent"), 0)
        self.assertLessEqual(mem.get("percent"), 100.5)

    def test_rate_fields_are_non_negative(self):
        """速率是差分算出来的，第一轮没有基线时必须是 0 而不是负数或 null。"""
        _, data = self._snapshot()
        self.assertGreaterEqual(data["network"].get("recv_bps"), 0)
        self.assertGreaterEqual(data["network"].get("sent_bps"), 0)
        self.assertGreaterEqual(data["disk"].get("read_bps"), 0)
        self.assertGreaterEqual(data["disk"].get("write_bps"), 0)

    # -- 进程 ---------------------------------------------------------------

    def test_process_rows_have_expected_fields(self):
        _, data = self._snapshot(top=5)
        rows = data["processes"]["list"]
        self.assertTrue(rows, "至少应当取到一些进程")
        for row in rows:
            for field in ("pid", "name", "cpu_percent", "memory",
                          "memory_percent", "username", "status"):
                self.assertIn(field, row, "进程缺少字段 %s" % field)
            self.assertIsInstance(row["pid"], int)
            self.assertGreaterEqual(row["cpu_percent"], 0)
            self.assertGreaterEqual(row["memory"], 0)

    def test_top_limits_the_returned_rows(self):
        _, data = self._snapshot(top=3)
        proc = data["processes"]
        self.assertLessEqual(len(proc["list"]), 3)
        self.assertEqual(proc.get("shown"), len(proc["list"]))
        self.assertGreaterEqual(proc.get("total") or 0, len(proc["list"]))

    def test_reports_its_own_server_process(self):
        """服务端自己的那个进程必须出现在进程列表里（它是采样者，必然在跑）。"""
        _, data = self._snapshot(top=500)
        server_pid = data["system"]["pid"]
        self.assertTrue(server_pid, "应当报告服务进程自身的 pid")
        pids = [row["pid"] for row in data["processes"]["list"]]
        self.assertIn(server_pid, pids, "服务进程自己应当出现在进程列表中")

    def test_sort_by_cpu_is_descending(self):
        _, data = self._snapshot(sort="cpu", top=10)
        rows = data["processes"]["list"]
        values = [row["cpu_percent"] for row in rows]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(data["processes"].get("sort"), "cpu")

    def test_sort_by_memory_is_descending(self):
        _, data = self._snapshot(sort="memory", top=10)
        rows = data["processes"]["list"]
        values = [row["memory"] for row in rows]
        self.assertEqual(values, sorted(values, reverse=True))
        self.assertEqual(data["processes"].get("sort"), "memory")

    def test_does_not_leak_command_line(self):
        """★ 命令行里常有口令/令牌，接口刻意不返回它 —— 这是一个安全约定。"""
        _, data = self._snapshot(top=50)
        for row in data["processes"]["list"]:
            self.assertNotIn("cmdline", row)
            self.assertNotIn("command_line", row)
            self.assertNotIn("environ", row)

    # -- GPU 的诚实性 -------------------------------------------------------

    def test_gpu_section_is_honest_when_unsupported(self):
        """
        读不到 GPU 时必须给出**原因**，而不是编一个 0%。

        这条对应一个真实取舍：把没有显卡的机器显示成「GPU 0%」会让人以为
        负载很低，比直接说「读不到」更糟。
        """
        _, data = self._snapshot()
        gpu = data["gpu"]
        self.assertIn("available", gpu)
        if not gpu["available"]:
            self.assertTrue(gpu.get("reason"), "不可用时必须说明原因")
            self.assertEqual(gpu.get("devices"), [])
        else:
            self.assertTrue(gpu.get("devices"), "可用时应当至少有一块设备")

    def test_requires_login(self):
        anonymous = self.server.client()
        status, data = anonymous.json("GET", "/api/sysmon/snapshot")
        self.assertEqual(status, 401, data)


class SysmonDisabledTests(unittest.TestCase):
    """sysmon.enabled=false 时的行为（含给前端的开关）。"""

    @classmethod
    def setUpClass(cls):
        cls.root = _make_root("fw-sysmon-off-")

        def disable(cfg):
            cfg["sysmon"]["enabled"] = False

        cls.server = ServerProcess([{
            "id": "share", "name": "共享目录", "path": cls.root, "readonly": False,
        }], extra_config=disable)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()

    def setUp(self):
        self.client = self.server.login_client()

    def test_snapshot_is_forbidden(self):
        status, data = self.client.json("GET", "/api/sysmon/snapshot")
        self.assertEqual(status, 403, data)

    def test_feature_flag_is_off_so_the_ui_hides_the_entry(self):
        status, info = self.client.json("GET", "/api/system/info")
        self.assertEqual(status, 200, info)
        self.assertFalse(info["features"]["sysmon"],
                         "关闭时 features.sysmon 必须是 false，否则桌面会显示一个"
                         "点开就 403 的入口")


class SysmonViewTests(unittest.TestCase):
    """
    view() 是纯函数（排序 + 截断），可以脱离服务直接测。

    它值得单独测，是因为缓存：snapshot() 返回的那份快照会被多个请求共用，
    如果 view() 就地排序/截断，就会污染缓存、影响下一个请求的客户端。
    """

    def _data(self):
        return {
            "available": True,
            "processes": {
                "total": 3,
                "cpu_ready": True,
                "list": [
                    {"pid": 1, "name": "a", "cpu_percent": 1.0, "memory": 300},
                    {"pid": 2, "name": "b", "cpu_percent": 9.0, "memory": 100},
                    {"pid": 3, "name": "c", "cpu_percent": 5.0, "memory": 200},
                ],
            },
        }

    def test_sorts_by_cpu_and_truncates(self):
        out = sysmon.view(self._data(), "cpu", 2)
        self.assertEqual([r["pid"] for r in out["processes"]["list"]], [2, 3])
        self.assertEqual(out["processes"]["shown"], 2)

    def test_sorts_by_memory(self):
        out = sysmon.view(self._data(), "memory", 10)
        self.assertEqual([r["pid"] for r in out["processes"]["list"]], [1, 3, 2])

    def test_does_not_mutate_the_cached_snapshot(self):
        """★ 缓存里那份必须是全量的，不能被某一次请求的排序/截断改掉。"""
        data = self._data()
        sysmon.view(data, "cpu", 1)
        self.assertEqual(len(data["processes"]["list"]), 3,
                         "view() 改动了传入的快照，缓存会被污染")

    def test_top_zero_means_use_the_default_not_zero_rows(self):
        """
        top=0 是「用默认条数」，不是「一条都不给」。

        这一点值得钉住：路由把这个 0 解释成「照配置里的 top_n 来」，
        前端也正好传 0 表示「不指定」。若哪天有人把 view() 改成
        `max(1, top)` 之类的写法，这条会立刻失败。
        """
        out = sysmon.view(self._data(), "cpu", 0)
        self.assertEqual(out["processes"]["shown"], 3)

    def test_top_is_clamped_to_at_most_500(self):
        """★ 上限必须钉死在 500：否则一次请求就能把整机进程全吐出来。"""
        rows = [{"pid": i, "name": "p%d" % i, "cpu_percent": float(i), "memory": i}
                for i in range(600)]
        data = {
            "available": True,
            "processes": {"total": 600, "cpu_ready": True, "list": rows},
        }

        out = sysmon.view(data, "cpu", 99999)
        self.assertEqual(out["processes"]["shown"], 500)
        self.assertEqual(len(data["processes"]["list"]), 600,
                         "为了截断而改动了传入的快照（缓存会被污染）")


if __name__ == "__main__":
    unittest.main(verbosity=2)

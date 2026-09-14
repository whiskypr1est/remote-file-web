# -*- coding: utf-8 -*-
"""
按文件名搜索（GET /api/fs/search）的回归测试。

搜索是**全盘递归遍历**，所以这个接口最该盯的不是「能不能搜到」，
而是「会不会失控」：一个请求不能把服务占住好几分钟，更不能跟着目录联接
转进死循环。下面有一半用例是在验证那几道刹车真的有效。

★ 写法上的坑：Client.json 的第三个位置参数是**请求体**，不是查询串，
  所以这里的 _search 自己用 urlencode 拼 URL。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from urllib.parse import urlencode

from tests._harness import ServerProcess


class SearchTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-search-")
        for rel in ("report.txt", "notes.md", "docs/report-draft.txt",
                    "docs/deep/final-report.txt", "media/Report.PNG", "media/song.mp3"):
            full = os.path.join(cls.root, *rel.split("/"))
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write("x")

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

    def _get(self, client, base, params):
        return client.json("GET", base + "?" + urlencode(params))

    def _search(self, query, **params):
        data = {"q": query}
        data.update(params)
        return self._get(self.client, "/api/fs/search", data)

    # -- 正常路径 -----------------------------------------------------------

    def test_finds_files_by_substring_ignoring_case(self):
        status, data = self._search("report")
        self.assertEqual(status, 200, data)
        names = sorted(item["name"] for item in data["results"])
        self.assertEqual(names, ["Report.PNG", "final-report.txt",
                                 "report-draft.txt", "report.txt"])

    def test_result_carries_enough_to_locate_the_file(self):
        status, data = self._search("final-report")
        self.assertEqual(status, 200, data)
        self.assertEqual(len(data["results"]), 1)

        item = data["results"][0]
        self.assertEqual(item["root"], "share")
        self.assertEqual(item["dir"], "docs/deep")
        self.assertEqual(item["rel"], "docs/deep/final-report.txt")
        self.assertFalse(item["is_dir"])
        self.assertGreater(item["size"], 0)

    def test_directories_are_searchable_too(self):
        status, data = self._search("deep")
        self.assertEqual(status, 200, data)
        dirs = [item for item in data["results"] if item["is_dir"]]
        self.assertEqual([item["name"] for item in dirs], ["deep"])

    def test_substring_within_a_name_matches(self):
        """匹配的是「包含」而不是「以…开头」—— 否则搜 report-draft 会很难用。"""
        status, data = self._search("draft")
        self.assertEqual(status, 200, data)
        self.assertEqual([item["name"] for item in data["results"]],
                         ["report-draft.txt"])

    def test_no_match_returns_empty_list_not_error(self):
        status, data = self._search("zzz-nothing-like-this-at-all")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["results"], [])
        self.assertFalse(data["truncated"])

    def test_single_root_scope(self):
        status, data = self._search("report", root="share")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["count"], 4)

    # -- 输入校验 -----------------------------------------------------------

    def test_too_short_query_is_rejected(self):
        """一个字就全盘遍历代价太大，直接拒绝（前端也是满 2 个字才发请求）。"""
        status, data = self._search("a")
        self.assertEqual(status, 400, data)

    def test_requires_login(self):
        anonymous = self.server.client()
        status, data = self._get(anonymous, "/api/fs/search", {"q": "report"})
        self.assertEqual(status, 401, data)


class SearchBrakeTests(unittest.TestCase):
    """
    刹车测试：用极小的上限来验证「到点就停」。

    这样不用真的造几万个文件 —— 上限是配置项，把它调小，
    同一套代码路径就会立刻触发，比造真实规模的数据可靠得多。
    """

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-search-brake-")
        for index in range(20):
            with open(os.path.join(cls.root, "file%02d.txt" % index),
                      "w", encoding="utf-8") as fh:
                fh.write("x")

        def tune(cfg):
            cfg["search"]["max_scanned"] = 3
            cfg["search"]["max_results"] = 2

        cls.server = ServerProcess([{
            "id": "share", "name": "共享目录", "path": cls.root, "readonly": False,
        }], extra_config=tune)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        cls.server.cleanup()

    def setUp(self):
        self.client = self.server.login_client()

    def _search(self, query, **params):
        data = {"q": query}
        data.update(params)
        return self.client.json("GET", "/api/fs/search?" + urlencode(data))

    def test_result_cap_is_enforced_and_reported(self):
        status, data = self._search("file", limit=2)
        self.assertEqual(status, 200, data)
        self.assertLessEqual(len(data["results"]), 2)
        self.assertTrue(data["truncated"], "达到上限时必须如实标记：%s" % data)
        self.assertTrue(data["reason"])

    def test_scan_cap_stops_the_walk(self):
        """扫过 max_scanned 个条目就该收工，而不是把整棵树走完。"""
        status, data = self._search("file")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["truncated"])
        # 上限是 3，允许超出一两个（是在循环里判断的），但绝不能是 20
        self.assertLess(data["scanned"], 10,
                        "扫描量没被截住，实际扫了 %s 个" % data["scanned"])

    def test_limits_are_reported_honestly_on_empty_result(self):
        """没搜到东西、但确实是因为被刹车截断时，也要说清楚原因。"""
        status, data = self._search("zzzz-none")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["results"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

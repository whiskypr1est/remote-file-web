# -*- coding: utf-8 -*-
"""
压缩包在线浏览（GET /api/fs/archive）的回归测试。

这个接口本身是只读的，但它的价值恰恰在于**替代解压**：用户想确认包里有什么时，
不该被迫先把内容解压到磁盘上、看完再删。所以测试重点在两件事：

    1. 它必须真的把条目、大小、目录/链接这些信息列对 ——
       而且条目名要与真正解压时落盘的名字一致（两边共用 list_entries）；
    2. 各种坏输入都必须给出**可读的中文原因**，不能变成 500。
       只读接口尤其不该 500：用户只是想看一眼而已。

★ 写法上的一个坑（第一版就踩了）：
    Client.json(method, path, body=None, **kwargs) 的第三个位置参数是**请求体**。
    给 GET 传 dict 会变成「带 body 的 GET」，服务端的 query 参数全是空的。
    所以下面的 _get() 一律自己用 urlencode 把参数拼进 URL。
"""

from __future__ import annotations

import io
import os
import tarfile
import tempfile
import unittest
import zipfile
from urllib.parse import urlencode

from tests._harness import ServerProcess


def _write_zip(path, entries):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)


class ArchiveBrowseTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-arcview-")
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

    @staticmethod
    def _get(client, base, params):
        return client.json("GET", base + "?" + urlencode(params))

    def _browse(self, name, **params):
        query = {"root": "share", "path": name}
        query.update(params)
        return self._get(self.client, "/api/fs/archive", query)

    def _zip(self, name, entries):
        target = os.path.join(self.root, name)
        _write_zip(target, entries)
        return target

    # -- 正常路径 -----------------------------------------------------------

    def test_lists_entries_with_metadata(self):
        self._zip("sample.zip", {
            "a.txt": "hello",          # 5 字节
            "dir/b.txt": "world!!",    # 7 字节
        })

        status, data = self._browse("sample.zip")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["format"], "zip")
        self.assertEqual(data["entry_count"], 2)
        self.assertEqual(data["file_count"], 2)
        self.assertEqual(data["total_bytes"], 12)

        by_name = {entry["name"]: entry for entry in data["entries"]}
        self.assertIn("a.txt", by_name)
        self.assertIn("dir/b.txt", by_name)
        self.assertEqual(by_name["a.txt"]["size"], 5)
        self.assertEqual(by_name["dir/b.txt"]["size"], 7)
        self.assertFalse(by_name["a.txt"]["is_dir"])

    def test_nested_directories_keep_their_path(self):
        """嵌套条目的名字里要保留目录层级 —— 前端就是靠它展示结构的。"""
        self._zip("nested.zip", {"x/y/z/deep.txt": "deep"})

        status, data = self._browse("nested.zip")
        self.assertEqual(status, 200, data)
        self.assertEqual([e["name"] for e in data["entries"]], ["x/y/z/deep.txt"])

    def test_tar_gz_is_recognised(self):
        target = os.path.join(self.root, "bundle.tar.gz")
        payload = b"tar payload"
        with tarfile.open(target, "w:gz") as tf:
            info = tarfile.TarInfo("inside.txt")
            info.size = len(payload)
            tf.addfile(info, io.BytesIO(payload))

        status, data = self._browse("bundle.tar.gz")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["format"], "tar")
        self.assertEqual([e["name"] for e in data["entries"]], ["inside.txt"])

    def test_conflicts_with_existing_files_are_reported(self):
        """
        解压遇到同名顶层项是**整体中止**的，所以浏览时就该把冲突摆出来，
        免得用户点完「解压」才发现被拒。
        """
        with open(os.path.join(self.root, "clash.txt"), "w", encoding="utf-8") as fh:
            fh.write("already here")

        self._zip("clash.zip", {"clash.txt": "from archive", "fresh.txt": "new"})

        status, data = self._browse("clash.zip")
        self.assertEqual(status, 200, data)
        self.assertEqual(data["conflict_count"], 1)
        self.assertEqual(data["conflicts"], ["clash.txt"])

    def test_limit_caps_the_returned_rows_but_reports_the_total(self):
        """一个包可能上万条目，默认只回传前若干条，但总数要如实给。"""
        self._zip("many.zip", {"f%02d.txt" % i: "x" for i in range(10)})

        status, data = self._browse("many.zip", limit=3)
        self.assertEqual(status, 200, data)
        self.assertEqual(data["entry_count"], 10)
        self.assertEqual(data["shown"], 3)
        self.assertEqual(len(data["entries"]), 3)

    # -- 坏输入：必须可读，不能 500 ----------------------------------------

    def test_corrupt_zip_gives_a_readable_error(self):
        """
        ★ 损坏的 zip 曾经会变成 500「内部服务器错误」。

        底层抛的是 zipfile.BadZipFile，它不是 ArchiveError，会一路冒到路由层
        落到通用的 500。对一个「只想看一眼」的只读操作来说，这个体验不能接受。
        """
        with open(os.path.join(self.root, "broken.zip"), "wb") as fh:
            fh.write(b"this is definitely not a zip file")

        status, data = self._browse("broken.zip")
        self.assertEqual(status, 400, data)
        # 注意错误体的键是 message（app.py 的统一错误处理），不是 FastAPI 默认的 detail
        self.assertIn("ZIP", str(data.get("message") or ""))

    def test_non_archive_file_is_rejected(self):
        with open(os.path.join(self.root, "plain.txt"), "w", encoding="utf-8") as fh:
            fh.write("just text")

        status, data = self._browse("plain.txt")
        self.assertEqual(status, 400, data)

    def test_missing_archive_is_404(self):
        status, data = self._browse("nope.zip")
        self.assertEqual(status, 404, data)

    def test_empty_path_is_400(self):
        status, data = self._get(self.client, "/api/fs/archive",
                                 {"root": "share", "path": ""})
        self.assertEqual(status, 400, data)

    def test_path_traversal_is_blocked(self):
        status, data = self._browse("../../../etc/passwd")
        self.assertIn(status, (400, 403, 404), data)

    def test_requires_login(self):
        anonymous = self.server.client()
        status, data = self._get(anonymous, "/api/fs/archive",
                                 {"root": "share", "path": "sample.zip"})
        self.assertEqual(status, 401, data)


if __name__ == "__main__":
    unittest.main(verbosity=2)

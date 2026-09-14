# -*- coding: utf-8 -*-
"""
新建空文件（POST /api/fs/newfile）的回归测试。

为什么单独一个文件：这个功能的风险点和「新建文件夹」不一样。

    * 新建文件夹最多是多出一个目录，而新建文件是**在服务器上凭空造出一个文件**，
      一旦覆盖到同名文件就是实打实的数据丢失 —— 所以这里反复确认「绝不覆盖」；
    * 扩展名由用户在界面上自选，等于开了一个可能绕开上传黑名单的口子，
      必须验证黑名单仍然生效：否则「上传 .bat 被拦、新建 .bat 却放行」
      就自相矛盾了，那个黑名单也就成了摆设。
"""

from __future__ import annotations

import os
import tempfile
import unittest

from tests._harness import ServerProcess


class NewFileTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-newfile-")
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

    def _new(self, name, path=""):
        return self.client.json("POST", "/api/fs/newfile",
                                {"root": "share", "path": path, "name": name})

    # -- 正常路径 -----------------------------------------------------------

    def test_creates_an_empty_file(self):
        status, data = self._new("hello.txt")
        self.assertEqual(status, 200, data)
        self.assertEqual(data.get("name"), "hello.txt")

        target = os.path.join(self.root, "hello.txt")
        self.assertTrue(os.path.isfile(target))
        self.assertEqual(os.path.getsize(target), 0, "新建出来的必须是空文件")

    def test_extension_is_used_verbatim(self):
        """后缀完全由用户决定：多级后缀、无后缀、非常见后缀都按原样落盘。"""
        for name in ("notes.md", "archive.tar.gz", "Makefile", "a.b.c.d", "数据.2024"):
            status, data = self._new(name)
            self.assertEqual(status, 200, data)
            self.assertEqual(data.get("name"), name)
            self.assertTrue(os.path.isfile(os.path.join(self.root, name)), name)

    def test_can_create_inside_subdirectory(self):
        os.mkdir(os.path.join(self.root, "sub"))
        status, data = self._new("inner.log", "sub")
        self.assertEqual(status, 200, data)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "sub", "inner.log")))

    # -- 最关键的一条：绝不覆盖 ---------------------------------------------

    def test_refuses_to_overwrite_existing_file(self):
        """
        ★ 同名时必须 409，且原文件内容一个字节都不能变。

        这里特意先写入内容再断言内容完好 —— 只检查「返回了错误码」是不够的：
        用 open(..., "w") 实现的话，报错之前文件就已经被清空了。
        """
        target = os.path.join(self.root, "keep.txt")
        with open(target, "w", encoding="utf-8") as fh:
            fh.write("原有内容不能被清掉")

        status, data = self._new("keep.txt")
        self.assertEqual(status, 409, data)

        with open(target, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), "原有内容不能被清掉",
                             "同名的新建请求把已有文件覆盖了！")

    def test_refuses_when_name_is_an_existing_directory(self):
        os.mkdir(os.path.join(self.root, "adir"))
        status, data = self._new("adir")
        self.assertEqual(status, 409, data)

    # -- 安全 ---------------------------------------------------------------

    def test_blocked_extension_is_rejected(self):
        """★ 上传黑名单必须同样作用于新建，否则黑名单可被绕过。"""
        for name in ("evil.bat", "evil.ps1", "evil.exe"):
            status, data = self._new(name)
            self.assertEqual(status, 403, "应当拒绝 %s：%s" % (name, data))
            self.assertFalse(os.path.exists(os.path.join(self.root, name)),
                             "%s 不该被创建出来" % name)

    def test_suffix_chain_cannot_smuggle_blocked_extension(self):
        """
        后缀链写法同样要拦住，口径与上传一致。

        这条是照 README「上传可执行文件」一节的说明写的：检查的是**完整后缀链**，
        所以 evil.bat.exe 不会因为「最后一段是 .exe」就侥幸通过（实际也是拦的）。
        """
        status, data = self._new("evil.bat.exe")
        self.assertEqual(status, 403, data)

    def test_path_traversal_in_name_stays_inside_root(self):
        """名字里夹带路径时只取最后一段，绝不能落到根目录之外。"""
        outside = os.path.join(os.path.dirname(self.root), "escaped.txt")
        if os.path.exists(outside):
            os.remove(outside)

        status, data = self._new("../escaped.txt")
        self.assertEqual(status, 200, data)
        self.assertEqual(data.get("name"), "escaped.txt", "应当只取最后一段文件名")
        self.assertFalse(os.path.exists(outside), "文件跑到根目录外面去了！")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "escaped.txt")))

    def test_missing_name_is_rejected(self):
        status, data = self.client.json("POST", "/api/fs/newfile",
                                        {"root": "share", "path": ""})
        self.assertIn(status, (400, 422), data)

    def test_requires_login(self):
        anonymous = self.server.client()
        status, data = anonymous.json("POST", "/api/fs/newfile",
                                      {"root": "share", "path": "", "name": "x.txt"})
        self.assertEqual(status, 401, data)


if __name__ == "__main__":
    unittest.main(verbosity=2)

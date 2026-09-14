# -*- coding: utf-8 -*-
"""
多用户隔离（阶段3：每用户路径解析器）的回归测试。

这是整个多用户改造里**最要紧的一组断言**：

    * 子用户只能看到分配给他的根；
    * 子用户访问别人的根必须被拒（按根标识 与 按绝对路径 两条路都要挡）；
    * ★★ **没有分配任何根的子用户是「空世界」，而不是「全机」。**

最后那条是最危险的失败模式：管理员用「roots 为空 = 走 mount_all_drives 看全机」
来表达权限（这是改造前的历史行为）。如果这条规则被顺手也套到子用户身上，
那么「新建一个还没分配目录的学生账号」就等于**把整台机器交给了他** ——
而且是静默的、看起来一切正常的。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from urllib.parse import urlencode

from fileweb import users as users_module
from tests._harness import ServerProcess

ADMIN_USER = "teacher"
ADMIN_PASSWORD = "admin-pass-123"
STUDENT_PASSWORD = "student-pass-123"


class MultiUserIsolationTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.shared = tempfile.mkdtemp(prefix="fw-iso-shared-")
        cls.private = tempfile.mkdtemp(prefix="fw-iso-private-")
        for directory, name in ((cls.shared, "shared-file.txt"),
                                (cls.private, "my-file.txt")):
            with open(os.path.join(directory, name), "w", encoding="utf-8") as fh:
                fh.write(name)

        cls.server = ServerProcess([{
            "id": "share", "name": "共享目录", "path": cls.shared, "readonly": False,
        }], username=ADMIN_USER, password=ADMIN_PASSWORD)
        cls.server.start()

        # ★ 用户表就在**这个测试实例**的配置旁边（app 已经引导出管理员 teacher）。
        #   测试进程与服务进程是两个进程，但用户表是文件，服务每次请求都会重读，
        #   所以这里改完立刻生效。
        cls._saved_path = users_module.USERS_PATH
        users_module.set_path(os.path.join(
            os.path.dirname(cls.server.cfg_path), "users.json"))

        # 学生一：分配一个私有根
        users_module.create("student1", STUDENT_PASSWORD, display_name="学生一", roots=[
            {"id": "private", "name": "我的空间",
             "path": cls.private, "readonly": False},
        ])
        # 学生二：**故意一个根都不分配**（就是上面说的最危险那条）
        users_module.create("student2", STUDENT_PASSWORD, display_name="学生二", roots=[])

    @classmethod
    def tearDownClass(cls):
        users_module.set_path(cls._saved_path)
        cls.server.stop()
        cls.server.cleanup()

    # -- 工具 ---------------------------------------------------------------

    def _login(self, username, password):
        client = self.server.client()
        status, data = client.login(username, password)
        self.assertEqual(status, 200, "登录 %s 失败：%s" % (username, data))
        return client

    def _roots(self, client):
        status, data = client.json("GET", "/api/fs/roots")
        self.assertEqual(status, 200, data)
        return [item["id"] for item in data["roots"]]

    def _list(self, client, **params):
        return client.json("GET", "/api/fs/list?" + urlencode(params))

    # -- 管理员 -------------------------------------------------------------

    def test_admin_keeps_the_historical_full_view(self):
        """管理员的 roots 为空 = 走 mount_all_drives / 配置里的根（行为不变）。"""
        client = self._login(ADMIN_USER, ADMIN_PASSWORD)
        self.assertIn("share", self._roots(client))

    def test_admin_can_read_the_configured_root(self):
        client = self._login(ADMIN_USER, ADMIN_PASSWORD)
        status, data = self._list(client, root="share", path="")
        self.assertEqual(status, 200, data)
        names = [e["name"] for e in data["entries"]]
        self.assertIn("shared-file.txt", names)

    # -- 子用户：只能看到自己的根 -------------------------------------------

    def test_student_sees_only_the_assigned_root(self):
        client = self._login("student1", STUDENT_PASSWORD)
        self.assertEqual(self._roots(client), ["private"],
                         "子用户只应看到被分配的那个根")

    def test_student_can_use_his_own_root(self):
        client = self._login("student1", STUDENT_PASSWORD)
        status, data = self._list(client, root="private", path="")
        self.assertEqual(status, 200, data)
        names = [e["name"] for e in data["entries"]]
        self.assertIn("my-file.txt", names)

    def test_student_cannot_list_someone_elses_root(self):
        """按根标识访问别人的根必须被拒。"""
        client = self._login("student1", STUDENT_PASSWORD)
        status, data = self._list(client, root="share", path="")
        self.assertNotEqual(status, 200,
                            "子用户不该能列出别人的根：%s" % data)

    def test_student_cannot_reach_another_root_by_absolute_path(self):
        """
        ★ 绝对路径也不能绕过。

        地址栏允许直接粘绝对路径，而解析器对绝对路径走的是「必须落在某个根里」
        的判断 —— 必须确认子用户走出来同样会被拒。
        """
        client = self._login("student1", STUDENT_PASSWORD)
        status, data = self._list(client, root="", path=self.shared)
        self.assertNotEqual(status, 200,
                            "子用户用绝对路径访问别人的根不该成功：%s" % data)

    def test_student_cannot_download_a_file_outside_his_roots(self):
        """换一个接口（原始文件输出）也不能绕过。"""
        client = self._login("student1", STUDENT_PASSWORD)
        status, _ = client.json("GET", "/api/fs/raw?" + urlencode({
            "root": "share", "path": "shared-file.txt",
        }))
        self.assertNotEqual(status, 200, "子用户不该能读别人根里的文件")

    # -- ★★ 没分配根的子用户：空世界，绝不是全机 ---------------------------

    def test_student_with_no_roots_sees_an_empty_world(self):
        client = self._login("student2", STUDENT_PASSWORD)
        self.assertEqual(self._roots(client), [],
                         "没分配根的子用户不该看到任何根")

    def test_student_with_no_roots_cannot_reach_anything(self):
        client = self._login("student2", STUDENT_PASSWORD)

        # 按根标识
        status, _ = self._list(client, root="share", path="")
        self.assertNotEqual(status, 200, "没分配根也不该能访问别人的根")

        # 按绝对路径
        status, _ = self._list(client, root="", path=self.shared)
        self.assertNotEqual(status, 200, "没分配根也不该能用绝对路径访问")

    def test_student_with_no_roots_does_not_inherit_mount_all_drives(self):
        """
        ★★ 这条是整个改造里最该盯住的一条。

        管理员那条路径靠「roots 为空 → mount_all_drives（全机）」实现。
        如果同一个判断被用到子用户身上，那么没分配目录的学生就会看到 C:/D:/E:……
        这里直接断言「他连一个盘符都看不到」。
        """
        client = self._login("student2", STUDENT_PASSWORD)
        ids = self._roots(client)
        self.assertEqual(ids, [], "子用户不该继承 mount_all_drives 的全机视图")

        # 再来一次，确认不是"第一次请求还没初始化"造成的假象
        self.assertEqual(self._roots(client), [])

    def test_system_info_reports_the_users_own_roots(self):
        """/api/system/info 里的 roots 也必须按用户给（前端「此电脑」用它）。"""
        admin = self._login(ADMIN_USER, ADMIN_PASSWORD)
        status, data = admin.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertIn("share", [r["id"] for r in data["roots"]])

        student = self._login("student1", STUDENT_PASSWORD)
        status, data = student.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertEqual([r["id"] for r in data["roots"]], ["private"])


if __name__ == "__main__":
    unittest.main(verbosity=2)

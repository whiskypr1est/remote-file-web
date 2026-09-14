# -*- coding: utf-8 -*-
"""
多用户：后台任务的归属（阶段4）
===============================

复制 / 移动 / 解压被改成「后台执行 + 轮询进度」之后，任务队列是**进程级单例**，
原先没有归属字段 —— 任何一个登录用户都能列出**并取消**别人的任务。

这不是「信息泄露」级别的小问题：学生按住取消键就能把同学正在跑的大复制、
大解压掐掉，而且对方只会看到进度条莫名变成「已取消」。

本文件钉住三条：
    1. 列表按归属过滤，子用户只看得见自己的；
    2. ★ 取消要校验归属 —— 别人的任务连「存在」都不该被确认（一律 404）；
    3. 管理员不限定归属，看得见全部（每条带 owner，能说清是谁提交的）。
"""

from __future__ import annotations

import os
import shutil
import tempfile
import threading
import unittest

from fileweb import users as users_module
from fileweb.jobs import STATUS_CANCELLED, JobManager
from tests._harness import ServerProcess

ADMIN_USER = "teacher"
ADMIN_PASSWORD = "admin-pass-123"
STUDENT_PASSWORD = "student-pass-123"

ALICE = "alice"
BOB = "bob"


def _quick_work(job):
    return {"message": "完成"}


def _blocking_work(release):
    """返回一个会一直挂着、直到 release.set() 的工作函数。"""
    def work(job):
        release.wait(8)
        return {"message": "已结束"}
    return work


class JobOwnerFilterTests(unittest.TestCase):
    """直接对着 JobManager 验证归属过滤（不起服务，快且确定）。"""

    def setUp(self):
        self.manager = JobManager()
        self._releases = []

    def tearDown(self):
        # 放行所有还在等的工作线程，避免它们挂到超时才退出
        for release in self._releases:
            release.set()

    def _blocking(self, owner, title="任务"):
        release = threading.Event()
        self._releases.append(release)
        return self.manager.submit("复制", title, _blocking_work(release), owner=owner)

    # -- 列表 ---------------------------------------------------------------

    def test_list_only_shows_your_own_jobs(self):
        alice_one = self.manager.submit("复制", "a1", _quick_work, owner=ALICE)
        alice_two = self.manager.submit("复制", "a2", _quick_work, owner=ALICE)
        bob_one = self.manager.submit("复制", "b1", _quick_work, owner=BOB)

        alice_ids = {j.id for j in self.manager.list(owner=ALICE)}
        bob_ids = {j.id for j in self.manager.list(owner=BOB)}
        all_ids = {j.id for j in self.manager.list(owner=None)}

        self.assertEqual(alice_ids, {alice_one.id, alice_two.id})
        self.assertEqual(bob_ids, {bob_one.id})
        self.assertEqual(all_ids, {alice_one.id, alice_two.id, bob_one.id},
                         "管理员（owner=None）应当看得见全部")

    def test_limit_is_applied_after_filtering(self):
        """
        ★ 先过滤再截断。

        反过来的话（先按最近 N 条截断、再过滤），别人的任务一多，子用户
        就会看到**空列表** —— 表现是「进度面板里自己的任务凭空消失」，
        而且任务越多越容易出现，极难排查。
        """
        mine = self.manager.submit("复制", "我的", _quick_work, owner=ALICE)
        for index in range(5):
            self.manager.submit("复制", "别人的 %d" % index, _quick_work, owner=BOB)

        got = self.manager.list(limit=1, owner=ALICE)
        self.assertEqual([j.id for j in got], [mine.id],
                         "limit 必须先过滤自己的任务再截断")

    # -- 单个任务 -----------------------------------------------------------

    def test_get_refuses_another_users_job(self):
        alice_job = self._blocking(ALICE)
        self.assertIsNone(self.manager.get(alice_job.id, owner=BOB),
                          "别人的任务应当与「不存在」同形")
        self.assertIsNotNone(self.manager.get(alice_job.id, owner=ALICE))
        self.assertIsNotNone(self.manager.get(alice_job.id, owner=None),
                             "管理员看得见")

    def test_cancel_refuses_another_users_job(self):
        """★ 这条是整组里最要紧的：取消别人正在跑的任务。"""
        alice_job = self._blocking(ALICE)

        self.assertIsNone(self.manager.cancel(alice_job.id, owner=BOB))
        self.assertFalse(alice_job.cancel_requested(),
                         "★ 别人的取消请求绝不能落到我的任务上")
        self.assertNotEqual(alice_job.status, STATUS_CANCELLED)

        # 自己取消是正常生效的
        self.assertIsNotNone(self.manager.cancel(alice_job.id, owner=ALICE))
        self.assertTrue(alice_job.cancel_requested())

    def test_stats_are_scoped_to_the_caller(self):
        self._blocking(ALICE)
        self._blocking(ALICE)
        self._blocking(BOB)

        self.assertEqual(self.manager.stats(owner=ALICE)["total"], 2)
        self.assertEqual(self.manager.stats(owner=BOB)["total"], 1)
        self.assertEqual(self.manager.stats(owner=None)["total"], 3)
        # 并发上限是全局参数，任何视角都应如实回
        self.assertEqual(self.manager.stats(owner=BOB)["max_concurrent"],
                         self.manager.stats(owner=None)["max_concurrent"])

    def test_jobs_without_owner_share_one_bucket(self):
        """不传 owner（既有调用方）全部落在同一个桶里，行为与改造前一致。"""
        first = self.manager.submit("复制", "x", _quick_work)
        second = self.manager.submit("复制", "y", _quick_work)
        self.assertEqual(first.owner, "")
        got = {j.id for j in self.manager.list(owner="")}
        self.assertEqual(got, {first.id, second.id})


class JobOwnershipApiTests(unittest.TestCase):
    """
    端到端验证接口层的归属：两个学生各有自己的根，各自提交后台复制。
    """

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-jobs-")
        cls.roots = {}
        for name in ("alice", "bob"):
            root = os.path.join(cls.work, name)
            os.makedirs(os.path.join(root, "dst"), exist_ok=True)
            with open(os.path.join(root, "a.txt"), "w", encoding="utf-8") as fh:
                fh.write("hello " + name)
            cls.roots[name] = root

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.work, "readonly": False}],
            username=ADMIN_USER, password=ADMIN_PASSWORD,
        ).start()

        cls._saved_users_path = users_module.USERS_PATH
        users_module.set_path(os.path.join(
            os.path.dirname(cls.server.cfg_path), "users.json"))
        users_module.create(ALICE, STUDENT_PASSWORD, display_name="甲同学", roots=[
            {"id": "mine", "name": "我的空间",
             "path": cls.roots["alice"], "readonly": False}])
        users_module.create(BOB, STUDENT_PASSWORD, display_name="乙同学", roots=[
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

    def _submit_copy(self, client):
        status, data = client.json("POST", "/api/fs/copy", {
            "root": "mine", "paths": ["a.txt"],
            "target_root": "mine", "target_path": "dst",
            "background": True,
        })
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("job_id"), "后台模式必须返回 job_id：%s" % data)
        return data["job_id"]

    def _list_jobs(self, client):
        status, data = client.json("GET", "/api/jobs")
        self.assertEqual(status, 200, data)
        return data["jobs"]

    # -- 测试 ---------------------------------------------------------------

    def test_jobs_are_not_listed_across_users(self):
        alice = self._login(ALICE)
        bob = self._login(BOB)

        job_id = self._submit_copy(alice)

        alice_ids = {j["id"] for j in self._list_jobs(alice)}
        bob_ids = {j["id"] for j in self._list_jobs(bob)}

        self.assertIn(job_id, alice_ids, "自己提交的任务必须看得见")
        self.assertNotIn(job_id, bob_ids, "★ 别人的任务不该出现在我的列表里")

    def test_cannot_cancel_another_users_job(self):
        alice = self._login(ALICE)
        bob = self._login(BOB)

        job_id = self._submit_copy(alice)

        status, data = bob.json("POST", "/api/jobs/%s/cancel" % job_id, {})
        self.assertEqual(status, 404,
                         "★ 别人的任务必须与「不存在」同一答复：%s" % data)

        # alice 的任务不能被 bob 的尝试影响
        status, data = alice.json("GET", "/api/jobs/%s" % job_id)
        self.assertEqual(status, 200, data)
        self.assertNotEqual(data["job"]["status"], "cancelled",
                            "★ 别人的取消请求绝不能真的取消我的任务")

    def test_cannot_read_another_users_job(self):
        """连「这个 id 是否存在」都不该被确认。"""
        alice = self._login(ALICE)
        bob = self._login(BOB)

        job_id = self._submit_copy(alice)
        status, _ = bob.json("GET", "/api/jobs/%s" % job_id)
        self.assertEqual(status, 404)

    def test_admin_sees_every_job_with_its_owner(self):
        alice = self._login(ALICE)
        job_id = self._submit_copy(alice)

        admin = self.server.login_client()
        jobs = {j["id"]: j for j in self._list_jobs(admin)}
        self.assertIn(job_id, jobs, "管理员应当看得见所有人的任务")
        self.assertEqual(jobs[job_id]["owner"], ALICE,
                         "列表里要能看出这条是谁提交的")

    def test_cancel_own_job_still_works(self):
        alice = self._login(ALICE)
        job_id = self._submit_copy(alice)

        status, data = alice.json("POST", "/api/jobs/%s/cancel" % job_id, {})
        self.assertEqual(status, 200, data)

        status, data = alice.json("GET", "/api/jobs/%s" % job_id)
        self.assertEqual(status, 200, data)
        self.assertIn(data["job"]["status"], ("cancelled", "done", "failed"),
                      "自己的任务取消后必须走到终态：%s" % data)


if __name__ == "__main__":
    unittest.main(verbosity=2)

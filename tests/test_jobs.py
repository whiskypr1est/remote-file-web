# -*- coding: utf-8 -*-
"""
后台任务队列（/api/jobs）与「后台模式」的复制 / 移动 / 解压的回归测试。

为什么要单独立一个文件：这些操作**默认仍然是同步的**（原契约一字未改），
后台模式是可选开关。所以要同时钉住两件事：

  1. **同步仍是默认**：不带 background 时行为不变（既有的 test_features /
     test_archive 覆盖了细节，这里再补一条最简的确认）；
  2. **后台模式真的能跑完、能报进度、能取消**：新能力必须自己站得住。

★ 一个容易踩的坑：后台模式返回的是 job_id 而不是结果，测试必须轮询到终态
  再断言 —— 否则会「看起来什么都没发生」。
"""

from __future__ import annotations

import os
import tempfile
import time
import unittest
import zipfile

from tests._harness import ServerProcess

TERMINAL = ("done", "failed", "cancelled")


def wait_for_job(client, job_id, timeout=30.0):
    """轮询到终态并返回 job 字典。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        status, data = client.json("GET", "/api/jobs/" + job_id)
        if status != 200:
            raise AssertionError("查询任务失败：%s %s" % (status, data))
        last = data["job"]
        if last["status"] in TERMINAL:
            return last
        time.sleep(0.05)
    raise AssertionError("任务超时未结束：%s" % last)


class JobQueueTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.root = tempfile.mkdtemp(prefix="fw-jobs-")
        with open(os.path.join(cls.root, "src.txt"), "w", encoding="utf-8") as fh:
            fh.write("hello")
        os.makedirs(os.path.join(cls.root, "dst"), exist_ok=True)

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

    # -- 同步仍是默认（原契约没被改坏）--------------------------------------

    def test_copy_without_background_stays_synchronous(self):
        status, data = self.client.json("POST", "/api/fs/copy", {
            "root": "share", "paths": ["src.txt"],
            "target_root": "share", "target_path": "dst",
        })
        self.assertEqual(status, 200, data)
        self.assertIn("copied", data)
        self.assertNotIn("job_id", data, "没开 background 就不该返回任务号")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "dst", "src.txt")))

    # -- 后台模式 -----------------------------------------------------------

    def test_background_copy_returns_a_job_and_completes(self):
        with open(os.path.join(self.root, "bg.txt"), "w", encoding="utf-8") as fh:
            fh.write("x" * 8192)

        status, data = self.client.json("POST", "/api/fs/copy", {
            "root": "share", "paths": ["bg.txt"],
            "target_root": "share", "target_path": "dst",
            "background": True,
        })
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("background"))
        self.assertTrue(data.get("job_id"))

        job = wait_for_job(self.client, data["job_id"])
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["percent"], 100.0)
        self.assertEqual(job["kind"], "复制")

        # 终态必须带上完整结果，形状与老契约一致 —— 前端就是靠这个
        # 在任务结束后继续处理「哪些成功、哪些失败」。
        self.assertIn("copied", job["result"])
        self.assertEqual(job["result"]["copied"], ["bg.txt"])
        self.assertTrue(os.path.isfile(os.path.join(self.root, "dst", "bg.txt")))

    def test_background_move_completes_and_removes_the_source(self):
        source = os.path.join(self.root, "tomove.txt")
        with open(source, "w", encoding="utf-8") as fh:
            fh.write("y" * 4096)

        status, data = self.client.json("POST", "/api/fs/move", {
            "root": "share", "paths": ["tomove.txt"],
            "target_root": "share", "target_path": "dst",
            "background": True,
        })
        self.assertEqual(status, 200, data)

        job = wait_for_job(self.client, data["job_id"])
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["moved"], ["tomove.txt"])
        self.assertFalse(os.path.exists(source), "移动完成后源文件不该还在")
        self.assertTrue(os.path.isfile(os.path.join(self.root, "dst", "tomove.txt")))

    def test_background_extract_completes_with_progress_denominator(self):
        archive_path = os.path.join(self.root, "pack.zip")
        with zipfile.ZipFile(archive_path, "w") as zf:
            for index in range(5):
                zf.writestr("entry%02d.txt" % index, "z" * 256)
        os.makedirs(os.path.join(self.root, "out"), exist_ok=True)

        status, data = self.client.json("POST", "/api/fs/extract", {
            "root": "share", "path": "pack.zip",
            "target_root": "share", "target_path": "out",
            "background": True,
        })
        self.assertEqual(status, 200, data)

        job = wait_for_job(self.client, data["job_id"])
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(job["result"]["extracted"], 5)
        # 总量是从条目表预先算出来的，所以进度条才有分母
        self.assertEqual(job["total_items"], 5)
        self.assertGreater(job["total_bytes"], 0)
        self.assertTrue(os.path.isfile(os.path.join(self.root, "out", "entry00.txt")))

    def test_completed_job_appears_in_the_list(self):
        status, data = self.client.json("POST", "/api/fs/copy", {
            "root": "share", "paths": ["src.txt"],
            "target_root": "share", "target_path": "dst",
            "background": True,
        })
        self.assertEqual(status, 200, data)

        status, listing = self.client.json("GET", "/api/jobs")
        self.assertEqual(status, 200, listing)
        ids = [item["id"] for item in listing["jobs"]]
        self.assertIn(data["job_id"], ids)
        self.assertIn("stats", listing)

    def test_cancel_ends_up_cancelled_or_already_done(self):
        """
        取消是**协作式**的：接口立刻返回，但任务要等到下一个检查点才停。
        小文件可能在收到取消前就跑完了，所以两种终态都算合理 ——
        真正要钉住的是「不会一直卡在 running」。
        """
        with open(os.path.join(self.root, "big.txt"), "w", encoding="utf-8") as fh:
            fh.write("q" * (4 * 1024 * 1024))

        status, data = self.client.json("POST", "/api/fs/copy", {
            "root": "share", "paths": ["big.txt"],
            "target_root": "share", "target_path": "dst",
            "background": True,
        })
        self.assertEqual(status, 200, data)
        job_id = data["job_id"]

        status, cancelled = self.client.json("POST", "/api/jobs/%s/cancel" % job_id)
        self.assertEqual(status, 200, cancelled)

        job = wait_for_job(self.client, job_id)
        self.assertIn(job["status"], ("cancelled", "done"), job)
        self.assertFalse(job["cancellable"])

    def test_unknown_job_is_404(self):
        status, data = self.client.json("GET", "/api/jobs/job_does_not_exist")
        self.assertEqual(status, 404, data)

    def test_requires_login(self):
        anonymous = self.server.client()
        status, data = anonymous.json("GET", "/api/jobs")
        self.assertEqual(status, 401, data)

    def test_background_validation_errors_are_still_immediate(self):
        """
        ★ 后台模式**不能**把校验错误降级成「任务失败」。

        路径穿越、只读根目录这类问题必须在提交那一刻就返回 4xx；
        否则用户拿到一个 job_id，过一会儿才发现「原来是没权限」，
        既浪费一轮往返，也让错误信息变得更难懂。
        """
        status, data = self.client.json("POST", "/api/fs/copy", {
            "root": "share", "paths": ["../../etc/passwd"],
            "target_root": "share", "target_path": "dst",
            "background": True,
        })
        self.assertIn(status, (400, 403, 404), data)
        self.assertNotIn("job_id", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
后台任务队列
============

给「复制 / 移动 / 解压」这类可能跑很久的操作提供一个可观测的执行容器：
提交后立刻拿到 job_id，前端轮询进度，随时可以取消。

为什么需要它
------------
这些操作原先都是**同步**的：请求一直挂到做完为止。选几十 GB 的时候浏览器
就那么卡着，既看不到进度、也判断不出是在干活还是已经死了，还没法取消 ——
README 的「已知限制」里一直挂着这一条。

设计取舍
--------
* **线程池而不是进程池**：这些活基本都是 IO 等待（磁盘/网络），GIL 不是瓶颈；
  而进程池要把文件描述符、日志、配置都搬过去，代价远大于收益。
* **协作式取消**：任务在自己的循环里检查 ``cancel_requested()``。
  Python 没法安全地强杀一个正在写文件的线程 —— 硬杀会留下写了一半的文件。
  所以取消的语义是「尽快停下来」，并且**如实报告**停在哪一步。
* **保留一段时间供查询**：任务结束后结果要能被前端取到（前端是轮询的，
  可能刚好错过最后一刻），所以终态任务会保留 ``keep_seconds`` 再回收。
* **不持久化**：服务重启后队列是空的。跨重启续传文件操作是另一个量级的工程
  （要记断点、要处理磁盘状态漂移），这里不做，也不假装能做。
* **并发上限**：同时只跑 N 个。几个大复制一起跑只会让每个都变慢，
  还会把磁盘的随机 IO 打爆。
* **不引入任何依赖**：只用标准库的 threading，与本项目「离线可部署」的取向一致。
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

# 状态机：pending（排队）→ running → done / failed / cancelled
STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_CANCELLED)

DEFAULT_MAX_CONCURRENT = 2
DEFAULT_KEEP_SECONDS = 600.0


class Job:
    """一个后台任务的运行状态。"""

    def __init__(self, kind: str, title: str, work: Callable[["Job"], Any],
                 owner: str = "") -> None:
        self.id = "job_" + uuid.uuid4().hex[:16]
        self.kind = kind
        self.title = title
        self.work = work
        # ★ 任务归属（多用户）：列表要按它过滤、取消要校验归属。
        # 没有 owner 的话，任何一个登录用户都能列出**并取消**别人的任务 ——
        # 学生按住「取消」就能把同学正在跑的大复制/解压掐掉。
        # 空串 = 无归属（直接构造 Job 的单元测试走这条）。
        self.owner = str(owner or "")

        self.status = STATUS_PENDING
        self.created = time.time()
        self.started: Optional[float] = None
        self.finished: Optional[float] = None

        # 进度（由工作函数通过 advance() 更新）
        self.done_items = 0
        self.total_items = 0
        self.done_bytes = 0
        self.total_bytes = 0
        self.current = ""
        self.message = ""

        self.result: Optional[Dict[str, Any]] = None
        self.error = ""

        self._cancel = threading.Event()
        # 进度字段会被工作线程写、被请求线程读，用一把小锁保证读到的是自洽的一组
        self._plock = threading.Lock()

    # -- 取消 ---------------------------------------------------------------

    def cancel_requested(self) -> bool:
        return self._cancel.is_set()

    def request_cancel(self) -> None:
        self._cancel.set()

    # -- 进度 ---------------------------------------------------------------

    def set_totals(self, items: Optional[int] = None,
                   total_bytes: Optional[int] = None) -> None:
        with self._plock:
            if items is not None:
                self.total_items = int(items)
            if total_bytes is not None:
                self.total_bytes = int(total_bytes)

    def advance(self, items: int = 0, bytes: int = 0,
                current: Optional[str] = None) -> None:
        """
        推进进度。

        :param items:   本次完成的条目数增量
        :param bytes:   本次完成的字节数增量
        :param current: 当前正在处理的对象名（用于「正在复制 xxx」）
        """
        with self._plock:
            self.done_items += int(items)
            self.done_bytes += int(bytes)
            if current is not None:
                self.current = current

    def set_message(self, text: str) -> None:
        with self._plock:
            self.message = text

    # -- 快照 ---------------------------------------------------------------

    def payload(self, with_result: bool = True) -> Dict[str, Any]:
        """给前端看的快照。"""
        with self._plock:
            total_bytes = self.total_bytes
            done_bytes = self.done_bytes
            total_items = self.total_items
            done_items = self.done_items

            if total_bytes > 0:
                percent = min(100.0, done_bytes * 100.0 / total_bytes)
            elif total_items > 0:
                percent = min(100.0, done_items * 100.0 / total_items)
            else:
                percent = 0.0

            data = {
                "id": self.id,
                "kind": self.kind,
                "title": self.title,
                # 归属：管理员看全部任务时靠它区分「这是谁提交的」
                "owner": self.owner,
                "status": self.status,
                "percent": round(percent, 1),
                "done_items": done_items,
                "total_items": total_items,
                "done_bytes": done_bytes,
                "total_bytes": total_bytes,
                "current": self.current,
                "message": self.message,
                "error": self.error,
                "created": self.created,
                "started": self.started,
                "finished": self.finished,
                "elapsed": round((self.finished or time.time())
                                 - (self.started or self.created), 1),
                "cancellable": self.status in (STATUS_PENDING, STATUS_RUNNING),
            }
            if with_result:
                data["result"] = self.result
            return data


class JobManager:
    """极简任务队列：固定并发上限，FIFO 排队，协作式取消。"""

    def __init__(self, max_concurrent: int = DEFAULT_MAX_CONCURRENT,
                 keep_seconds: float = DEFAULT_KEEP_SECONDS) -> None:
        self._lock = threading.RLock()
        self._jobs: Dict[str, Job] = {}
        self._order: List[str] = []          # 保持提交顺序，便于「最近的任务」
        self._queue: List[Job] = []
        self._running = 0
        self._max_concurrent = max(1, int(max_concurrent))
        self._keep_seconds = float(keep_seconds)

    # -- 对外接口 -----------------------------------------------------------

    def submit(self, kind: str, title: str, work: Callable[[Job], Any],
               owner: str = "") -> Job:
        """提交任务，立刻返回 Job（此时通常还在排队）。"""
        job = Job(kind, title, work, owner=owner)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            self._queue.append(job)
        self._pump()
        return job

    def get(self, job_id: str, owner: Optional[str] = None) -> Optional[Job]:
        """
        按 id 取任务。

        ``owner`` 给了就只认这个人的任务：别人的任务返回 None，
        与「不存在」**完全同形** —— 不泄露「这个 id 是存在的，只是不归你」。
        管理员传 None 表示不限定归属（看得见全部）。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if owner is not None and job.owner != owner:
                return None
            return job

    def list(self, limit: int = 50, owner: Optional[str] = None) -> List[Job]:
        """
        按提交时间倒序返回（最近的在前）。

        ``owner`` 给了就只回这个人的任务（子用户看自己的进度面板）；
        管理员传 None 看全部。★ 注意过滤要在**取 limit 之前**做，
        否则「先截断再过滤」会让子用户在别人任务多的时候看到空列表。
        """
        with self._lock:
            ids = [jid for jid in reversed(self._order)]
            selected: List[Job] = []
            for jid in ids:
                job = self._jobs.get(jid)
                if job is None:
                    continue
                if owner is not None and job.owner != owner:
                    continue
                selected.append(job)
                if len(selected) >= max(1, int(limit)):
                    break
            return selected

    def cancel(self, job_id: str, owner: Optional[str] = None) -> Optional[Job]:
        """
        请求取消。

        注意返回的是「已经打了取消标记」的任务，不代表它此刻已经停了 ——
        真正停下要等工作函数在下一次检查点退出，终态会变成 cancelled。

        归属校验与 get() 一致：给了 owner 就只允许取消自己的任务，
        别人的一律返回 None（同「不存在」）。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if owner is not None and job.owner != owner:
                return None
            if job.status in TERMINAL_STATUSES:
                return job
            job.request_cancel()
            # 还在排队的直接就地取消，不必等它被调度起来
            if job.status == STATUS_PENDING:
                job.status = STATUS_CANCELLED
                job.finished = time.time()
                if job in self._queue:
                    self._queue.remove(job)
            return job

    def stats(self, owner: Optional[str] = None) -> Dict[str, int]:
        """
        队列统计。

        ``owner`` 给了就只统计这个人的任务（子用户的进度面板显示自己的），
        但 ``max_concurrent`` 是全局参数，任何情况下都照实回。
        """
        with self._lock:
            jobs = [job for job in self._jobs.values()
                    if owner is None or job.owner == owner]
            running = sum(1 for job in jobs if job.status == STATUS_RUNNING)
            pending = sum(1 for job in jobs if job.status == STATUS_PENDING)
            return {
                "running": running,
                "pending": pending,
                "total": len(jobs),
                "max_concurrent": self._max_concurrent,
            }

    # -- 内部 ---------------------------------------------------------------

    def _pump(self) -> None:
        """把队列里排到的任务放出去跑，并顺手回收过期任务。"""
        with self._lock:
            while self._queue and self._running < self._max_concurrent:
                job = self._queue.pop(0)
                if job.cancel_requested():
                    job.status = STATUS_CANCELLED
                    job.finished = time.time()
                    continue
                self._running += 1
                job.status = STATUS_RUNNING
                job.started = time.time()
                threading.Thread(
                    target=self._run, args=(job,),
                    name="fwjob-" + job.id, daemon=True,
                ).start()
        self._reap()

    def _run(self, job: Job) -> None:
        try:
            result = job.work(job)
            with self._lock:
                # 工作函数可能在「已经写完、正要返回」的那一刻才被取消，
                # 这时文件其实已经就位了，所以以取消标记为准来定状态，
                # 但把 message 说清楚，别让用户以为白干了。
                if job.cancel_requested():
                    job.status = STATUS_CANCELLED
                    job.message = job.message or "已取消"
                    if isinstance(result, dict):
                        job.result = result
                else:
                    job.status = STATUS_DONE
                    if isinstance(result, dict):
                        job.result = result
                        job.message = str(result.get("message") or job.message or "")
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                if job.cancel_requested():
                    # 取消是「主动中止」，不是故障，不要报成失败吓人
                    job.status = STATUS_CANCELLED
                    job.message = "已取消"
                else:
                    job.status = STATUS_FAILED
                    job.error = str(exc) or exc.__class__.__name__
        finally:
            with self._lock:
                job.finished = time.time()
                self._running = max(0, self._running - 1)
            self._pump()

    def _reap(self) -> None:
        """把超过保留期的终态任务清掉，避免内存无限增长。"""
        deadline = time.time() - self._keep_seconds
        with self._lock:
            stale = [jid for jid, job in self._jobs.items()
                     if job.status in TERMINAL_STATUSES
                     and (job.finished or job.created) < deadline]
            for jid in stale:
                self._jobs.pop(jid, None)
                if jid in self._order:
                    self._order.remove(jid)

    def reset(self) -> None:
        """清空队列（测试用）。"""
        with self._lock:
            self._jobs.clear()
            self._order.clear()
            self._queue.clear()
            self._running = 0


# 进程级单例：路由层直接用这个
manager = JobManager()

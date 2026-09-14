# -*- coding: utf-8 -*-
"""
后台任务路由
============

    GET  /api/jobs                —— 任务列表（最近的在前，含正在跑的）
    GET  /api/jobs/{job_id}       —— 单个任务的进度/结果
    POST /api/jobs/{job_id}/cancel —— 请求取消

为什么要有这几个接口：复制/移动/解压这类操作被改成「后台执行 + 前端轮询」之后，
前端需要一个统一的入口看进度。放在独立路由而不是塞进 /api/fs，是因为
「任务」本身与文件系统无关 —— 将来把压缩、Office 转换也搬进来时不用改归属。
"""

from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request

from ..deps import get_state
from ..jobs import manager

router = APIRouter(prefix="/api/jobs", tags=["后台任务"])


@router.get("")
async def list_jobs(request: Request, limit: int = 20) -> Dict[str, Any]:
    """列出最近的任务（最近的在前）。前端用它一次拿到所有活跃任务的进度。"""
    get_state(request)      # 触发认证依赖（未登录会被中间件拦在前面）

    jobs = manager.list(limit=limit)
    return {
        "ok": True,
        "jobs": [job.payload(with_result=False) for job in jobs],
        "stats": manager.stats(),
    }


@router.get("/{job_id}")
async def get_job(request: Request, job_id: str) -> Dict[str, Any]:
    """
    取单个任务。

    终态任务带上 result（复制/移动/解压的完整结果，形状与原来的同步接口一致），
    前端据此在任务结束后继续用同一套逻辑处理「哪些成功、哪些失败」。
    """
    get_state(request)

    job = manager.get(job_id)
    if job is None:
        # 任务可能已经被回收，或者 id 根本不存在 —— 两种情况对前端是一回事
        raise HTTPException(status_code=404, detail="任务不存在或已被回收")

    return {"ok": True, "job": job.payload(with_result=True)}


@router.post("/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str) -> Dict[str, Any]:
    """
    请求取消任务。

    是**协作式**取消：这里只是打上标记，真正的停止发生工作函数的下一个检查点，
    所以立刻返回的成功并不代表文件操作已经停了 —— 前端应继续轮询直到
    status 变成 cancelled。
    """
    get_state(request)

    job = manager.cancel(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="任务不存在或已被回收")

    return {
        "ok": True,
        "job": job.payload(with_result=False),
        "message": "已请求取消" if job.status not in ("done", "failed", "cancelled")
                   else "任务已经结束",
    }

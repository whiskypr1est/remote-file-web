# -*- coding: utf-8 -*-
"""
系统监控路由（「任务管理器」窗口）
====================================

    GET /api/sysmon/snapshot   —— 系统负载快照（CPU / 内存 / 磁盘 / 网络 / GPU / 进程）

**刻意做成只读**，不提供「结束进程」：
    本项目已经有一个真终端窗口，真要动进程时用它就好；
    而「能按 PID 杀任意进程」的 HTTP 接口，风险与收益完全不成比例 ——
    它等于给任何能登录的人一个不用交互就能把服务器搞瘫的按钮。
"""

from __future__ import annotations

from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import sysmon
from ..deps import get_state

router = APIRouter(prefix="/api/sysmon", tags=["系统监控"])


@router.get("/snapshot")
async def sysmon_snapshot(request: Request, top: int = 0, sort: str = "cpu") -> Dict[str, Any]:
    """
    取一份系统负载快照。

    - ``top``  ：返回多少个进程（0 = 用配置里的 ``sysmon.top_n``；上限 500）
    - ``sort`` ：进程排序依据，``cpu``（默认）或 ``memory``
    """
    state = get_state(request)
    settings = state.cfg.get("sysmon") or {}

    if not settings.get("enabled", True):
        raise HTTPException(
            status_code=403,
            detail="系统监控已在服务端关闭（config.json 的 sysmon.enabled = false）",
        )

    # 采集是阻塞的（进程枚举要读一堆系统信息），丢到线程池里做
    data = await run_in_threadpool(sysmon.snapshot, settings)

    # 配置被手写成 "30" 这类字符串也要能用；坏值退默认，不要变成 500
    try:
        configured = int(settings.get("top_n") or 30)
    except (TypeError, ValueError):
        configured = 30
    limit = int(top) if top and top > 0 else configured
    return sysmon.view(data, sort_by=sort, top=limit)

# -*- coding: utf-8 -*-
"""
控制台镜像路由（方案 A）
========================

    GET  /api/conhost/list              —— 列出真实桌面上所有控制台
    GET  /api/conhost/read              —— 读某个控制台的屏幕内容
    POST /api/conhost/input             —— 往某个控制台注入按键
    GET  /api/conhost/status            —— 功能是否可用、输入注入是否打开

★ 为什么**只给管理员**
    这个功能能把真实桌面上任意控制台的屏幕内容送到浏览器 —— 那些内容可能
    包含口令、令牌、数据库导出、别人的日志；输入注入更是等于替人在键盘上
    打字（连「管理员:」开着的控制台也在范围内）。
    相比之下，sysmon（任务管理器）当初连命令行都**刻意不采集**，
    本功能的暴露面比它大得多，所以必须硬性限定管理员。

★ 为什么读不逐次写审计
    前端按秒轮询读接口。若每次都记一条，审计日志几小时就会被淹掉 ——
    这正是 audit.py 开头写明「不记录文件浏览这类高频动作」的同一个理由。
    所以：**打开某个控制台的镜像时记一次**（conhost_read），
    **输入注入每次都记**（conhost_input），后者才是真正要留痕的动作。

★ 部署约束（写在这里，免得日后踩）
    1. 辅助进程必须与目标控制台在**同一个 Windows 会话**；
    2. **不要**把本服务装成 NSSM 服务 —— 那会变成会话 0，功能整体失效；
    3. 目标若是「管理员:」控制台，本服务也必须以管理员身份运行，
       否则辅助进程附加时会拿到 ERROR_ACCESS_DENIED(5)。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import audit, conhost
from ..deps import client_ip, get_state, get_user, require_admin

router = APIRouter(prefix="/api/conhost", tags=["控制台镜像"])


def _section(request: Request) -> Dict[str, Any]:
    return (get_state(request).cfg.get("conhost") or {})


def _guard(request: Request) -> Dict[str, Any]:
    """管理员独占 + 功能开关 + 平台能力，三样一起过。"""
    user = require_admin(request)
    cfg = get_state(request).cfg
    ok, reason = conhost.available(cfg)
    if not ok:
        raise HTTPException(status_code=403, detail=reason)
    return user


@router.get("/status")
async def conhost_status(request: Request) -> Dict[str, Any]:
    """
    功能状态。**不要求管理员** —— 前端要据此决定「要不要显示这个入口」，
    子用户看到 available=false 就不会显示，也不需要为此弹一个 403。
    """
    cfg = get_state(request).cfg
    ok, reason = conhost.available(cfg)
    is_admin = str(get_user(request).get("role") or "") == "admin"
    section = _section(request)
    return {
        "available": bool(ok and is_admin),
        "reason": reason if not ok else (
            "" if is_admin else "该功能仅限管理员"),
        # 输入注入是**单独**的开关：看一眼（只读）和替人敲键盘（可写）
        # 的风险不是一个量级，默认只开前者。
        "allow_input": bool(section.get("allow_input", False)),
        "read_only": not bool(section.get("allow_input", False)),
    }


@router.get("/list")
async def conhost_list(request: Request, include_own: bool = False) -> Dict[str, Any]:
    """列出控制台。枚举要逐个 AttachConsole，是阻塞操作，丢线程池。"""
    _guard(request)
    cfg = get_state(request).cfg
    try:
        return await run_in_threadpool(conhost.list_consoles, cfg, include_own)
    except conhost.ConhostError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


@router.get("/read")
async def conhost_read(request: Request, pid: int, mode: str = "log",
                       lines: int = 200) -> Dict[str, Any]:
    """
    读一个控制台的屏幕内容。

    - ``mode`` ：``log``（默认，光标往上 N 行，适合滚动日志）
                 ``screen``（可见窗口那一块，适合全屏 TUI）
    - ``lines``：log 模式读多少行（上限见 conhost_helper.MAX_READ_LINES）
    """
    _guard(request)
    cfg = get_state(request).cfg
    section = _section(request)
    try:
        limit = int(section.get("max_lines") or 500)
    except (TypeError, ValueError):
        limit = 500
    lines = max(1, min(int(lines or 200), limit))

    try:
        data = await run_in_threadpool(conhost.read_console, cfg, pid, mode, lines)
    except conhost.ConhostError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # 只在「开始看某个控制台」时落一条审计，不逐次记（见模块注释）
    _audit_once(request, pid, data)
    return data


# 已经记过审计的 pid（避免轮询把日志淹掉）。只存进程内，重启后重记一次 ——
# 这没问题：重启本身就是要留痕的事件。
_audited_pids: set = set()


def _audit_once(request: Request, pid: int, data: Dict[str, Any]) -> None:
    key = int(pid)
    if key in _audited_pids:
        return
    _audited_pids.add(key)
    try:
        audit.log(
            audit.EVENT_CONHOST_READ,
            username=str(get_user(request).get("username") or ""),
            ip=client_ip(request),
            result="ok",
            detail="打开控制台镜像 pid=%d 标题=%s 窗口=%s"
                   % (key, (data.get("title") or "")[:80], data.get("hwnd") or 0),
        )
    except Exception:                                   # noqa: BLE001
        pass


class InputPayload(BaseModel):
    """
    往控制台注入按键。text 与 keys 至少要有一个。

    ★ 为什么每个字段都给了默认值（而不是让它们必填）：
      FastAPI 会**先校验请求体、再进函数体**。如果 pid 是必填的，那么
      「空 body 的请求」会直接以 422 结束，函数体里那句 require_admin
      **根本不会执行** —— 于是对已登录的子用户来说，这道「仅限管理员」
      的闸门在校验失败时被绕过了（拿不到数据，但口径不一致，
      而且等于先告诉了对方接口长什么样）。
      实测就是这样：POST 一个 {} 回来的是
      `422 {"message": "请求参数不合法：pid Field required"}`。
      给默认值之后，任何**能解析成 JSON** 的请求体都会走到鉴权那一步，
      语义合法性再由函数体里的检查负责（顺序明确：鉴权 -> 语义）。
    """

    pid: int = Field(0, description="目标控制台里某个进程的 pid")
    text: str = Field("", description="要键入的文本（支持中文，走 VK_PACKET）")
    keys: Optional[List[str]] = Field(
        None, description='具名按键，如 ["enter"] / ["ctrl-c"] / ["up"]')


@router.post("/input")
async def conhost_input(request: Request, payload: InputPayload) -> Dict[str, Any]:
    """
    ★ 往真实窗口里**真的打字**。

    需要 conhost.allow_input = true（默认 false）。
    没有状态反馈：我们不知道那边此刻停在什么提示符上，
    所以调用方（前端）应当让用户明确知道自己按下了什么。

    顺序是刻意的：**先鉴权，再判语义**（见 InputPayload 的说明）。
    """
    user = _guard(request)
    cfg = get_state(request).cfg

    if payload.pid <= 0:
        raise HTTPException(status_code=400, detail="pid 不合法")
    if not payload.text and not (payload.keys or []):
        raise HTTPException(status_code=400, detail="text 与 keys 至少要有一个")

    try:
        result = await run_in_threadpool(
            conhost.write_console, cfg, payload.pid, payload.text,
            list(payload.keys or []))
    except conhost.ConhostError as exc:
        audit.log(audit.EVENT_CONHOST_INPUT,
                  username=str(user.get("username") or ""),
                  ip=client_ip(request), result="fail",
                  detail="pid=%d 失败：%s" % (payload.pid, exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    # 输入是真正要留痕的动作，每次都记（含键入了什么，便于事后追溯）
    shown = payload.text if len(payload.text) <= 120 else payload.text[:120] + "…"
    audit.log(audit.EVENT_CONHOST_INPUT,
              username=str(user.get("username") or ""),
              ip=client_ip(request), result="ok",
              detail="pid=%d 键入=%r 按键=%s" % (payload.pid, shown,
                                                payload.keys or []))
    return result

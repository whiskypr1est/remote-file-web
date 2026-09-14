# -*- coding: utf-8 -*-
"""
用户管理路由（仅管理员）
========================

    GET  /api/users                    —— 用户列表 + 在线状态 + 活跃会话数 + 进程数
    GET  /api/users/online             —— 只回在线情况
    GET  /api/users/audit              —— 最近的审计日志
    POST /api/users                    —— 新建用户
    POST /api/users/{name}/update      —— 改显示名/备注/角色/额度/权限/可见目录
                                          （停用与启用也走这里：enabled=true/false）
    POST /api/users/{name}/password    —— 重置别人的口令
    POST /api/users/{name}/kick        —— 强制下线（不改口令、不停用）

★ 这些接口全部走 require_admin。但要说清楚：**这是界面与接口层面的约束，
不是对抗恶意用户的边界** —— 子用户拥有全权限命令行，本来就能直接改
users.json（详见 MULTIUSER.md 第〇节，用户已确认接受这一风险）。
它挡的是误操作和「顺手试一下」，不是有心人。

几条刻意加上的护栏（都是「防把自己锁在门外」这一类的真实事故）
------------------------------------------------------------
* **不能停用自己**：停用会立刻递增 token_version，等于当场把自己踢出去，
  而这个账号已经登不进来了 —— 只能去改文件才能恢复。
* **不能改自己的角色**（管理员 → 普通用户）：同上，只是更隐蔽 ——
  下一步所有 /api/users 都会 403，看起来像「管理界面坏了」。
* **不能把最后一个启用中的管理员降权或停用**：否则系统里再没有人能进管理界面。
  引导逻辑也是这个立场（见 users.ensure_bootstrap：没有管理员时**只告警、不自动提权**）。
* **不做删除用户**（用户决定：停用而非删除）：删账号不影响他的文件夹，
  却会让「这个人是谁」的审计线索断掉。需要彻底删除请手工编辑 users.json。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import audit, presence, sysmon
from ..deps import client_ip, get_state, owner_of, require_admin
from ..jobs import manager as jobs_manager
from ..terminal import manager as terminal_manager
from .. import users as users_module

router = APIRouter(prefix="/api/users", tags=["用户管理"])


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

class UserCreatePayload(BaseModel):
    username: str = ""
    password: str = ""
    display_name: str = ""
    role: str = users_module.ROLE_USER
    note: str = ""
    max_terminal_sessions: int = 5
    permissions: Optional[Dict[str, bool]] = None
    # 可见目录：给结构化列表，或者给管理界面那块文本框里的**文本**
    # （`路径 | 名称 | 只读`，每行一个）。两者都给时以 roots 为准。
    roots: Optional[List[Dict[str, Any]]] = None
    roots_text: Optional[str] = None


class UserUpdatePayload(BaseModel):
    """
    部分更新：只处理**显式给了**的字段。

    为什么用 Optional 而不是带默认值：这里必须能区分「没传」和「传了空」——
    `display_name=""`（清空显示名）和「不改显示名」是两件事，
    用默认值会把前者当成后者，用户改不动。
    """
    display_name: Optional[str] = None
    note: Optional[str] = None
    role: Optional[str] = None
    max_terminal_sessions: Optional[int] = None
    permissions: Optional[Dict[str, bool]] = None
    roots: Optional[List[Dict[str, Any]]] = None
    roots_text: Optional[str] = None
    enabled: Optional[bool] = None


class UserPasswordPayload(BaseModel):
    password: str = ""


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _resolve_roots(payload: Any) -> Optional[List[Dict[str, Any]]]:
    """
    把请求里的可见目录解出来（两种写法都支持）。

    返回 None 表示「这次请求没打算改可见目录」—— 与「改成空列表」是两件事，
    后者是「让他什么都看不到」这个明确意图。混在一起会让「改显示名」
    顺手把人的目录清空。
    """
    roots = getattr(payload, "roots", None)
    if roots is not None:
        return roots
    text = getattr(payload, "roots_text", None)
    if text is not None:
        return users_module.parse_roots_text(text)
    return None


def _require_user(username: str) -> Dict[str, Any]:
    record = users_module.get(username)
    if record is None:
        raise HTTPException(status_code=404, detail="用户不存在：%s" % username)
    return record


def _active_admins() -> List[str]:
    return [u["username"] for u in users_module.list_users()
            if u.get("role") == users_module.ROLE_ADMIN and u.get("enabled", True)]


def _guard_last_admin(target: Dict[str, Any], *, removing_admin: bool) -> None:
    """
    别把最后一个启用中的管理员搞掉。

    ★ 这条比「不能改自己」更彻底：改自己只挡住了常见的失误，
    而「把唯一的管理员降权」无论落在谁头上，结果都是没人能再进管理界面。
    """
    if not removing_admin:
        return
    if target.get("role") != users_module.ROLE_ADMIN:
        return
    others = [name for name in _active_admins()
              if name.lower() != target["username"].lower()]
    if not others:
        raise HTTPException(
            status_code=400,
            detail="这是唯一启用中的管理员，降权或停用后将没有人能进入管理界面。"
                   "请先创建另一个管理员。",
        )


def _process_counts(cfg: Dict[str, Any]) -> Dict[str, int]:
    """
    按进程属主统计进程数（管理界面显示「某人几个进程」）。

    只在 sysmon 开启时才采集：它在某些机器上要几百毫秒，
    而这份统计只是列表里的一个数字。sysmon.snapshot 自带很短的缓存，
    所以和任务管理器窗口同时开着也不会各自触发一次完整采集。
    """
    settings = cfg.get("sysmon") or {}
    if not settings.get("enabled", True):
        return {}
    if sysmon.psutil is None:
        return {}

    try:
        data = sysmon.snapshot(settings)
    except Exception:  # noqa: BLE001 - 统计失败不该让用户列表打不开
        return {}

    counts: Dict[str, int] = {}
    for row in (data.get("processes") or {}).get("list") or []:
        name = str(row.get("username") or "").strip()
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
    return counts


def _decorate(record: Dict[str, Any], *, online: Dict[str, Dict[str, Any]],
              terminals: Dict[str, int], processes: Dict[str, int],
              jobs: Dict[str, int], me: str) -> Dict[str, Any]:
    """把用户记录与「运行时状态」拼成一行给界面看的数据。"""
    name = record["username"]
    row = users_module.public(record)

    # 管理界面那块文本框要显示的内容：把结构化列表变回人可读、可再编辑的文本。
    # 变换放在服务端，前端不需要懂这个格式（见 users.parse_roots_text 的说明）。
    row["roots_text"] = users_module.format_roots_text(record.get("roots"))

    seen = online.get(name.lower())
    row["online"] = bool(seen and seen.get("online"))
    row["last_seen"] = (seen or {}).get("last_seen")
    row["ip"] = (seen or {}).get("ip") or ""
    row["terminal_sessions"] = int(terminals.get(name, 0))
    row["processes"] = int(processes.get(name, 0))
    row["jobs"] = int(jobs.get(name, 0))
    # 前端据此把「这一行是我自己」的按钮置灰（见文件头的护栏说明）
    row["is_self"] = name.lower() == me.lower()
    return row


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

@router.get("")
async def list_users(request: Request) -> Dict[str, Any]:
    """
    用户列表（含运行时状态）。

    口令哈希由 users.public() 剥掉；这里再补上在线情况、命令行窗口数、
    进程数与后台任务数 —— 都是**计数**，不含任何会话内容
    （用户决定：管理员看活跃会话数与进程，不看终端输出）。
    """
    require_admin(request)
    state = get_state(request)

    online = {row["username"].lower(): row for row in presence.snapshot()}
    terminals = terminal_manager.owner_counts()
    processes = _process_counts(state.cfg)

    # 后台任务的计数：按归属数一遍（任务总量很小，不需要额外索引）
    jobs: Dict[str, int] = {}
    for job in jobs_manager.list(limit=500, owner=None):
        if job.owner:
            jobs[job.owner] = jobs.get(job.owner, 0) + 1

    rows = [
        _decorate(record, online=online, terminals=terminals,
                  processes=processes, jobs=jobs, me=owner_of(request))
        for record in users_module.list_users()
    ]

    return {
        "ok": True,
        "users": rows,
        "online_count": sum(1 for row in rows if row["online"]),
        "total": len(rows),
    }


@router.get("/online")
async def list_online(request: Request) -> Dict[str, Any]:
    """只回在线情况（比用户列表轻，适合较频繁地刷新）。"""
    require_admin(request)
    rows = presence.snapshot()
    return {
        "ok": True,
        "online": rows,
        "online_count": sum(1 for row in rows if row["online"]),
        "window_seconds": presence.ONLINE_WINDOW_SECONDS,
    }


@router.get("/audit")
async def read_audit(request: Request, limit: int = 200) -> Dict[str, Any]:
    """
    最近的审计日志（时间正序，最新的在最后）。

    只回**当前文件**的最近若干条；更早的记录在轮转文件里，需要时人工查。
    """
    require_admin(request)
    records = audit.recent(limit=limit)
    return {"ok": True, "records": records, "count": len(records)}


# ---------------------------------------------------------------------------
# 变更
# ---------------------------------------------------------------------------

@router.post("")
async def create_user(request: Request, payload: UserCreatePayload) -> Dict[str, Any]:
    """新建用户（初始口令由管理员设定，用户决定）。"""
    admin = require_admin(request)
    ip = client_ip(request)

    try:
        record = users_module.create(
            payload.username,
            payload.password,
            role=payload.role,
            display_name=payload.display_name,
            roots=_resolve_roots(payload),
            note=payload.note,
            created_by=admin["username"],
            max_terminal_sessions=payload.max_terminal_sessions,
            permissions=payload.permissions,
        )
    except users_module.UserError as exc:
        audit.log(audit.EVENT_USER_CREATE, username=payload.username, ip=ip,
                  result="fail", detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))

    roots = record.get("roots") or []
    audit.log(audit.EVENT_USER_CREATE, username=record["username"], ip=ip,
              detail="角色=%s 可见目录=%d 个 操作者=%s"
                     % (record["role"], len(roots), admin["username"]))

    return {
        "ok": True,
        "user": users_module.public(record),
        "message": "已创建用户「%s」" % record["username"],
    }


@router.post("/{username}/update")
async def update_user(request: Request, username: str,
                      payload: UserUpdatePayload) -> Dict[str, Any]:
    """
    修改用户（部分更新）。

    可见目录、启用状态、以及其余字段分别走各自的处理函数：
    改可见目录不需要把人踢下线（下次请求即生效），而**停用必须踢下线**
    （靠递增 token_version），两者语义不同，不能混在一处。
    """
    admin = require_admin(request)
    ip = client_ip(request)
    target = _require_user(username)
    changed: List[str] = []

    # -- 角色 -------------------------------------------------------------
    if payload.role is not None and payload.role != target.get("role"):
        new_role = str(payload.role).strip().lower()
        if new_role != users_module.ROLE_ADMIN:
            _guard_last_admin(target, removing_admin=True)
        if target["username"].lower() == admin["username"].lower():
            raise HTTPException(
                status_code=400,
                detail="不能修改自己的角色 —— 改成普通用户后你将无法再进入管理界面。"
                       "请让另一位管理员来改。",
            )

    # -- 启用状态 ---------------------------------------------------------
    if payload.enabled is not None and bool(payload.enabled) != bool(target.get("enabled", True)):
        if not payload.enabled:
            _guard_last_admin(target, removing_admin=True)
            if target["username"].lower() == admin["username"].lower():
                raise HTTPException(
                    status_code=400,
                    detail="不能停用自己的账号 —— 停用会立刻让你的会话失效，"
                           "而这个账号已经无法登录。",
                )

    # -- 可见目录 ---------------------------------------------------------
    roots = _resolve_roots(payload)
    if roots is not None:
        try:
            users_module.set_roots(target["username"], roots)
        except users_module.UserError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        changed.append("可见目录")

    # -- 其余字段 ---------------------------------------------------------
    fields: Dict[str, Any] = {}
    for key in ("display_name", "note", "role", "max_terminal_sessions", "permissions"):
        value = getattr(payload, key)
        if value is not None:
            fields[key] = value

    if fields:
        try:
            users_module.update(target["username"], **fields)
        except users_module.UserError as exc:
            audit.log(audit.EVENT_USER_UPDATE, username=target["username"], ip=ip,
                      result="fail", detail=str(exc))
            raise HTTPException(status_code=400, detail=str(exc))
        changed.extend(sorted(fields))

    # -- 启用 / 停用（单独走 set_enabled，它负责递增 token_version）--------
    if payload.enabled is not None:
        users_module.set_enabled(target["username"], bool(payload.enabled))
        changed.append("启用" if payload.enabled else "停用")
        audit.log(audit.EVENT_USER_ENABLE if payload.enabled else audit.EVENT_USER_DISABLE,
                  username=target["username"], ip=ip,
                  detail="操作者=%s" % admin["username"])

    updated = _require_user(target["username"])
    if changed:
        audit.log(audit.EVENT_USER_UPDATE, username=updated["username"], ip=ip,
                  detail="改了 %s（操作者=%s）" % ("、".join(changed), admin["username"]))

    return {
        "ok": True,
        "user": users_module.public(updated),
        "message": "已更新「%s」" % updated["username"] if changed else "没有需要修改的内容",
    }


@router.post("/{username}/password")
async def reset_password(request: Request, username: str,
                         payload: UserPasswordPayload) -> Dict[str, Any]:
    """
    管理员重设某个用户的口令。

    会递增该用户的 token_version（users.set_password 的默认行为），
    所以他手里所有已登录的浏览器都会立刻失效 —— 这正是「口令泄露后重置」
    想要的效果。管理员自己的会话不受影响。
    """
    admin = require_admin(request)
    ip = client_ip(request)
    target = _require_user(username)

    try:
        users_module.set_password(target["username"], payload.password)
    except users_module.UserError as exc:
        audit.log(audit.EVENT_USER_PASSWORD_RESET, username=target["username"],
                  ip=ip, result="fail", detail=str(exc))
        raise HTTPException(status_code=400, detail=str(exc))

    audit.log(audit.EVENT_USER_PASSWORD_RESET, username=target["username"], ip=ip,
              detail="操作者=%s" % admin["username"])

    return {
        "ok": True,
        "message": "已重设「%s」的口令，他/她当前的登录会立即失效"
                   % target["username"],
    }


@router.post("/{username}/kick")
async def kick_user(request: Request, username: str) -> Dict[str, Any]:
    """
    把某个用户的所有会话踢下线（不改口令、不停用）。

    用途：怀疑账号被盗用但还不确定，先踢下线让他重新登录（重新登录会留审计记录）；
    或者确认了「就是他在乱来」，但还没决定要不要停用。
    """
    admin = require_admin(request)
    ip = client_ip(request)
    target = _require_user(username)

    if target["username"].lower() == admin["username"].lower():
        # 技术上可行（踢完自己得重新登录），但没有意义，而且很容易让人以为
        # 「管理界面把我踢出去了」= 出了故障。明确拒绝并说明。
        raise HTTPException(
            status_code=400,
            detail="不能强制下线自己 —— 你只需要重新登录。想退出请用「注销」。",
        )

    users_module.bump_token_version(target["username"])
    presence.forget(target["username"])
    audit.log(audit.EVENT_USER_KICK, username=target["username"], ip=ip,
              detail="操作者=%s" % admin["username"])

    return {
        "ok": True,
        "message": "已让「%s」的所有会话失效，他/她需要重新登录" % target["username"],
    }

# -*- coding: utf-8 -*-
"""
虚拟桌面路由
============

    GET  /api/desktop/shortcuts          列出桌面快捷方式
    POST /api/desktop/shortcuts          新建（"发送到桌面快捷方式"）
    POST /api/desktop/shortcuts/rename   重命名快捷方式
    POST /api/desktop/shortcuts/delete   删除快捷方式
    GET  /api/desktop/state              读取界面状态（窗口布局等）
    PUT  /api/desktop/state              保存界面状态

设计说明：
    快捷方式**只存在于 Web 虚拟桌面**，不会往 Windows 真实桌面写任何东西。
    数据由服务端持久化在 desktop_shortcuts.json，
    所以清浏览器缓存、换电脑、重启服务都不会丢。

    列表接口会顺带判断目标是否还存在，前端可以把失效的快捷方式标灰，
    避免盘符变化或文件被移走后点开一片空白。

    /api/desktop/state 则是「关掉浏览器再打开还是原来那个桌面」的支撑：
    服务端只当仓库，存一份**不透明**的 JSON（窗口位置/尺寸、当前目录、
    视图模式都由前端定义，服务端不解释），见 fileweb/userstate.py。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from .. import shortcuts, userstate
from ..deps import get_state, get_user, resolver_of
from ..security import PathSecurityError

router = APIRouter(prefix="/api/desktop", tags=["虚拟桌面"])


class ShortcutPayload(BaseModel):
    """新建快捷方式：指定「根标识 + 相对路径」以及可选的显示名。"""
    root: str = ""
    path: str = ""
    name: str = ""


class ShortcutIdPayload(BaseModel):
    id: str = ""


class ShortcutRenamePayload(BaseModel):
    id: str = ""
    name: str = ""


class UserStatePayload(BaseModel):
    """
    界面状态请求体。

    刻意声明成 `Any` 而不是 `Dict[str, Any]`：Pydantic 会先替我们挡掉一层
    明显不合法的东西（字符串、数字、数组），但**真正的形状校验归 userstate.save**，
    这样「服务端不理解状态内容」这条设计原则只有一处实现，不会两边跑偏。
    字段给默认值 {} 是让 `PUT {}`（前端没带 state）走到我们自己的中文报错，
    而不是 Pydantic 那串英文校验消息。
    """

    state: Any = None


@router.get("/shortcuts")
async def list_shortcuts(request: Request) -> Dict[str, Any]:
    """列出**当前用户**的桌面快捷方式，并标注目标是否仍然存在。"""
    state = get_state(request)
    items = shortcuts.list_items(get_user(request))

    result: List[Dict[str, Any]] = []
    for item in items:
        entry = dict(item)
        try:
            _root, abs_path = resolver_of(request).resolve(item["root"], item["path"])
            entry["exists"] = os.path.exists(abs_path)
            entry["abs"] = abs_path
        except PathSecurityError:
            # 盘符被移除或根目录配置变了，保留记录但标记失效
            entry["exists"] = False
            entry["abs"] = ""
        result.append(entry)

    return {"ok": True, "shortcuts": result}


@router.post("/shortcuts")
async def create_shortcut(request: Request, payload: ShortcutPayload) -> Dict[str, Any]:
    """
    把某个文件/文件夹发送到虚拟桌面。

    传 root + path 而不是绝对路径，是为了让快捷方式跟随根目录配置走；
    显示名默认取最后一段目录名，也允许前端自定义。
    """
    state = get_state(request)

    try:
        root_cfg, abs_path = resolver_of(request).resolve(payload.root, payload.path)
    except PathSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="目标不存在，无法创建快捷方式")

    default_name = os.path.basename(abs_path.rstrip("\\/")) or abs_path
    name = (payload.name or "").strip() or default_name

    try:
        item = shortcuts.add(
            name=name,
            root=root_cfg["id"],
            rel=resolver_of(request).to_rel(root_cfg, abs_path),
            is_dir=os.path.isdir(abs_path),
            user=get_user(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {
        "ok": True,
        "shortcut": item,
        "message": "已发送到桌面：「%s」" % item["name"],
    }


@router.post("/shortcuts/rename")
async def rename_shortcut(request: Request, payload: ShortcutRenamePayload) -> Dict[str, Any]:
    """重命名快捷方式（只改显示名，不影响指向的真实路径）。"""
    try:
        item = shortcuts.rename(payload.id, payload.name, get_user(request))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if item is None:
        raise HTTPException(status_code=404, detail="快捷方式不存在")

    return {"ok": True, "shortcut": item, "message": "已重命名为「%s」" % item["name"]}


@router.post("/shortcuts/delete")
async def delete_shortcut(request: Request, payload: ShortcutIdPayload) -> Dict[str, Any]:
    """删除快捷方式（只删桌面图标，不动真实文件）。"""
    if not shortcuts.remove(payload.id, get_user(request)):
        raise HTTPException(status_code=404, detail="快捷方式不存在或已被删除")

    return {"ok": True, "message": "快捷方式已从桌面移除（真实文件未受影响）"}


# ---------------------------------------------------------------------------
# 界面状态（窗口布局 / 当前目录 / 视图模式）
# ---------------------------------------------------------------------------

@router.get("/state")
async def get_desktop_state(request: Request) -> Dict[str, Any]:
    """
    读取上次保存的界面状态。

    认证由 app.py 的 SecurityMiddleware 统一负责（/api 下的接口一律要求登录），
    这里再显式依赖一次 get_user 是防御性写法：将来若有人给某个路由加白名单，
    也不会连带把这份「用户自己的桌面」泄出去。

    文件不存在/损坏时 userstate.load() 返回 {}，前端据此走默认布局，
    所以这里不会出现 404 —— 「没有状态」本身就是一个正常状态。
    """
    get_state(request)
    user = get_user(request)

    return {"ok": True, "state": userstate.load(user=user)}


@router.put("/state")
async def put_desktop_state(
    request: Request, payload: Optional[UserStatePayload] = None
) -> Dict[str, Any]:
    """
    保存界面状态（整体覆盖）。

    整体覆盖而不是增量合并：前端每次保存时手上有完整的布局，
    合并语义要定义「删掉的窗口怎么表达」，反而更容易出错。

    注意 PUT 属于改状态请求，中间件已经强制校验同源 + X-CSRF-Token，
    与同目录的快捷方式接口完全一致。
    """
    get_state(request)
    user = get_user(request)

    data = payload.state if payload is not None else None
    if data is None:
        raise HTTPException(status_code=400, detail="缺少 state 字段")

    try:
        userstate.save(data, user=user)
    except userstate.UserStateTooLargeError as exc:
        # 超过体积上限：明确报错而不是静默截断，否则存进去的 JSON 已经不可解析，
        # 前端下次取回来只会更困惑
        raise HTTPException(status_code=413, detail=str(exc))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return {"ok": True}

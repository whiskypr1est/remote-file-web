# -*- coding: utf-8 -*-
"""
桌面歌词转发路由
================

    POST /now-playing        —— 网页播放器上报「现在在放什么」
    GET  /now-playing        —— 查询当前状态（悬浮窗重连后补画面 / 排障）
    WS   /ws/lyrics          —— 悬浮窗订阅（也接受 report 消息，两条路等价）

★ 这三个路径**故意不放在 /api 下**，这是本功能唯一的架构性决定，值得说清：

app.py 的中间件只对 `/api` 开头的路径做登录校验与 CSRF 校验。悬浮窗是独立进程
（desktop-lyrics/ 里的 Electron），它**没有会话 Cookie**，而且必须在没有人
打开浏览器的深夜里也能连上来 —— 如果放在 /api 下，它会一直吃 403。
按用户的要求（局域网内互相信任的部署，不做鉴权），这里就落在 /api 之外。

由此产生的两个后果都写在这里，避免以后有人以为是漏配：
  1. 局域网内任何人都能 POST 上报，往所有人的悬浮窗上推任意文字；
     也能连 WS 订阅，看到别人在放什么歌。这是用户明确接受的取舍。
  2. 因此服务端**不信任**上报内容：长度、类型、数值全部在 lyrics.py 里截断，
     并且提供 `lyrics.token` 作为预留的鉴权扩展点（留空 = 不校验）。
     将来要收紧时，只要给 token 配一个值，然后在客户端与上报里带上它即可，
     不需要改动任何调用方代码。
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict

from fastapi import APIRouter, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from ..lyrics import MAX_SOURCE_CHARS, REPORT_TYPES, clamp_text, get_hub

router = APIRouter(tags=["桌面歌词"])

# 上报请求体上限。真实歌词受 music.MAX_LYRICS_BYTES（256KB）限制，
# 加上 JSON 转义也不会超过 1MB，所以 2MB 足够宽松，同时挡住「一个超大 body
# 把内存吃光」这种最省事的攻击。
MAX_BODY_BYTES = 2 * 1024 * 1024


# ---------------------------------------------------------------------------
# 配置与校验
# ---------------------------------------------------------------------------

def _settings(request_or_ws) -> Dict[str, Any]:
    app = request_or_ws.app
    cfg = app.state.app_state.cfg
    settings = cfg.get("lyrics")
    return settings if isinstance(settings, dict) else {}


def _to_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("0", "false", "no", "off", "")
    return bool(value)


def _enabled(settings: Dict[str, Any]) -> bool:
    return _to_bool(settings.get("enabled"), True)


def _token_ok(settings: Dict[str, Any], provided: str) -> bool:
    """
    预留的鉴权扩展点。

    服务端没配 token（默认）时不校验任何东西 —— 这是局域网部署的正常状态。
    配了 token 就要求调用方原样带上（?token=xxx 或 POST 体里的 token）。
    这里用普通字符串比较而不是 compare_digest：它不是密码，只是一个
    「别让隔壁实验室顺手连上来」的门槛，而且长度也不固定。
    """
    expected = str(settings.get("token") or "").strip()
    if not expected:
        return True
    return str(provided or "").strip() == expected


def _forbidden(message: str, code: str) -> JSONResponse:
    return JSONResponse({"ok": False, "code": code, "message": message},
                        status_code=403)


def _disabled_response() -> JSONResponse:
    return _forbidden(
        "桌面歌词已在服务端关闭（config.json 的 lyrics.enabled = false）",
        "lyrics_disabled")


def _bad_token_response() -> JSONResponse:
    return _forbidden("桌面歌词需要 token（config.json 的 lyrics.token 已配置）",
                      "bad_token")


async def _reject_ws(websocket: WebSocket, code: int, reason: str) -> None:
    """
    在握手阶段拒绝 WebSocket 连接。

    必须在 accept() 之前 close，uvicorn 才会把它翻译成 HTTP 403 握手失败。
    ★ 代价是**客户端看不到 reason**（握手失败没有响应体），所以悬浮窗在
    握手失败时会另外用 GET /now-playing 问一次原因，好把「服务端关了功能」
    和「服务端连不上」区分开显示。见 desktop-lyrics/src/main.js。
    """
    try:
        await websocket.close(code=code, reason=reason)
    except Exception:  # noqa: BLE001
        pass


def _query(websocket: WebSocket, name: str) -> str:
    try:
        return str(websocket.query_params.get(name) or "")
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# 上报（POST）
# ---------------------------------------------------------------------------

class _BodyTooLarge(Exception):
    pass


async def _read_json(request: Request) -> Any:
    """
    读取并解析 JSON 请求体，带体积上限。

    不用 pydantic 模型：上报有三种形状（song / progress / stop），而且它是一条
    **尽力而为**的通道 —— 播放器绝不会因为服务端拒了一条上报就停止播放。
    所以宁可宽进（缺字段走默认值），由 lyrics.report() 统一做清洗与判定。
    """
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > MAX_BODY_BYTES:
                raise _BodyTooLarge()
        except ValueError:      # 非数字的 Content-Length：忽略，靠下面的累计判断
            pass

    collected = bytearray()
    async for chunk in request.stream():
        collected.extend(chunk)
        if len(collected) > MAX_BODY_BYTES:
            raise _BodyTooLarge()

    if not collected:
        return {}
    try:
        return json.loads(collected.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None


@router.post("/now-playing")
async def report_now_playing(request: Request) -> Any:
    """网页播放器上报。幂等、无副作用，失败不影响播放。"""
    settings = _settings(request)
    if not _enabled(settings):
        return _disabled_response()

    hub = get_hub()
    hub.configure(settings.get("stale_seconds"))

    try:
        payload = await _read_json(request)
    except _BodyTooLarge:
        # ★ 这里不回吐请求体（与音乐上传同一套取舍）：为了省流量，
        #   直接拒绝而不是把 2MB 读完再拒。上报是尽力而为的，
        #   客户端拿到的可能是一次连接重置，但它下一拍还会再报。
        return JSONResponse(
            {"ok": False, "code": "too_large",
             "message": "上报内容过大（上限 %d 字节）" % MAX_BODY_BYTES},
            status_code=413)

    if payload is None:
        return JSONResponse(
            {"ok": False, "code": "bad_json", "message": "请求体不是合法 JSON"},
            status_code=400)

    provided = payload.get("token") if isinstance(payload, dict) else ""
    if not _token_ok(settings, provided):
        return _bad_token_response()

    result, error = hub.report(payload)
    if error:
        # 400 而不是 500：这是上报方的问题，而且它不会重试（也不需要重试）。
        return JSONResponse(result, status_code=400)
    return result


# ---------------------------------------------------------------------------
# 查询（GET）
# ---------------------------------------------------------------------------

@router.get("/now-playing")
async def get_now_playing(request: Request, user: str = "", token: str = "",
                          sources: int = 0) -> Any:
    """
    当前状态。

    用途有三个，都在设计里是有位置的：
      1. 悬浮窗每次重连成功后先取一次，避免「刚开窗时一片空白」；
      2. 悬浮窗第一次握手失败时用它区分「功能被关了」与「连不上」；
      3. 排障：`?sources=1` 会把所有源列出来（谁在放、放了多久、有没有歌词）。
    """
    settings = _settings(request)
    if not _enabled(settings):
        return _disabled_response()
    if not _token_ok(settings, token):
        return _bad_token_response()

    hub = get_hub()
    hub.configure(settings.get("stale_seconds"))

    wanted = clamp_text(user, MAX_SOURCE_CHARS)
    payload: Dict[str, Any] = {"ok": True, "enabled": True,
                               "serverTime": time.time(),
                               "staleSeconds": hub.stale_seconds,
                               "display": hub.snapshot(wanted)}
    if sources:
        payload["sources"] = hub.sources()
        payload["subscribers"] = hub.subscriber_count
    return payload


# ---------------------------------------------------------------------------
# 订阅（WebSocket）
# ---------------------------------------------------------------------------

@router.websocket("/ws/lyrics")
async def lyrics_ws(websocket: WebSocket) -> None:
    """
    悬浮窗订阅通道。

    ?user=wangqi 只订阅这个用户；不带则订阅全部（谁在放就显示谁）。
    ?token=xxx   仅当服务端配置了 lyrics.token 时才要求。

    服务端 -> 客户端：
        {"type":"state", "idle":bool, "revision":N, "serverTime":...,
         "source":"...", "title":..., "artist":..., "album":...,
         "duration":..., "lyrics":[{"time":..,"text":".."}],
         "currentTime":..., "playing":bool, "staleSeconds":N}
        {"type":"pong","t":<客户端回显>}        对客户端 ping 的回应
        {"type":"report-result", ...}           对客户端 report 的回应

    客户端 -> 服务端（**可选**，浏览器走的是 POST /now-playing）：
        {"type":"song"|"progress"|"stop", ...}  与 POST 的请求体完全一致
        {"type":"ping","t":<任意>}              保活 / 测延迟

    ★ 连上后**立刻**会收到一条 state（空闲时 idle=true）：客户端只需要处理
    一种消息形状，不必区分「初始快照」和「后续变化」。
    """
    settings = _settings(websocket)
    if not _enabled(settings):
        await _reject_ws(websocket, 1008, "桌面歌词已在服务端关闭")
        return
    if not _token_ok(settings, _query(websocket, "token")):
        await _reject_ws(websocket, 1008, "缺少或错误的 token")
        return

    hub = get_hub()
    hub.configure(settings.get("stale_seconds"))
    wanted = clamp_text(_query(websocket, "user"), MAX_SOURCE_CHARS)

    await websocket.accept()
    subscriber = hub.subscribe(wanted)
    try:
        await websocket.send_json(hub.welcome(subscriber))

        reader = asyncio.create_task(_ws_read_loop(websocket, hub))
        writer = asyncio.create_task(_ws_write_loop(websocket, subscriber))
        try:
            # 任一方向结束（客户端断开 / 发送失败）就整体收摊：
            # 单独留下另一半任务只会让它对着一个死连接空转。
            done, pending = await asyncio.wait(
                {reader, writer}, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
            for task in done | pending:
                try:
                    await task
                except (asyncio.CancelledError, WebSocketDisconnect):
                    pass
                except Exception:  # noqa: BLE001 - 连接层的异常无需上抛
                    pass
        finally:
            for task in (reader, writer):
                if not task.done():
                    task.cancel()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001 - 断线路径上什么异常都不该让日志变脏
        pass
    finally:
        hub.unsubscribe(subscriber)


async def _ws_write_loop(websocket: WebSocket, subscriber) -> None:
    """把该订阅者该看到的状态推出去。"""
    while True:
        payload = await subscriber.queue.get()
        await websocket.send_json(payload)


async def _ws_read_loop(websocket: WebSocket, hub) -> None:
    """
    接收客户端的消息。

    浏览器不走这条路（它用 POST，见模块头部说明），这里是给「愿意多用一条
    WS 的客户端」和排障工具准备的：发什么都不会影响播放。
    """
    while True:
        raw = await websocket.receive_text()
        try:
            message = json.loads(raw)
        except (TypeError, ValueError):
            await websocket.send_json({"type": "error", "code": "bad_json",
                                       "message": "消息不是合法 JSON"})
            continue

        if not isinstance(message, dict):
            await websocket.send_json({"type": "error", "code": "bad_json",
                                       "message": "消息必须是 JSON 对象"})
            continue

        kind = clamp_text(message.get("type"), 32)
        if kind == "ping":
            # 回显 t，客户端可以据此算往返延迟；没有 t 也照样回。
            await websocket.send_json({"type": "pong", "t": message.get("t"),
                                       "serverTime": time.time()})
            continue

        if kind in REPORT_TYPES:
            result, _error = hub.report(message)
            result = dict(result)
            result["type"] = "report-result"
            result["kind"] = kind
            await websocket.send_json(result)
            continue

        await websocket.send_json({
            "type": "error", "code": "unknown_type",
            "message": "只支持 %s 或 ping" % "、".join(REPORT_TYPES),
        })

# -*- coding: utf-8 -*-
"""
认证路由
========

    POST /api/auth/login    —— 登录，成功后下发 HttpOnly 会话 Cookie
    POST /api/auth/logout   —— 注销，清除 Cookie
    GET  /api/auth/status   —— 查询登录状态（登录页与桌面页都会调用）

安全要点：
    * 口令只与配置中的 PBKDF2 哈希（或兼容的明文配置项）比对；
    * 用户名与口令比较都用恒定时间函数，避免时序侧信道；
    * 同一 IP 连续失败达到阈值后临时锁定，防止暴力破解；
    * Cookie 为 HttpOnly + SameSite=Lax，脚本读不到、跨站带不上。
"""

from __future__ import annotations

import hmac
import time
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from .. import users
from ..deps import (
    SESSION_COOKIE,
    client_ip,
    get_state,
    get_user,
    read_session,
    resolve_session_user,
    session_max_age,
)
from ..security import csrf_token_for, sign_token

router = APIRouter(prefix="/api/auth", tags=["认证"])


class LoginPayload(BaseModel):
    """登录请求体。"""
    username: str = ""
    password: str = ""


def _safe_equals(left: str, right: str) -> bool:
    """
    恒定时间字符串比较。

    hmac.compare_digest 对 str 参数要求只能包含 ASCII，
    用户名/口令可能含中文，所以统一先编码成 UTF-8 字节再比。
    """
    try:
        return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
    except Exception:  # noqa: BLE001
        return False


@router.post("/login")
async def login(request: Request, payload: LoginPayload) -> JSONResponse:
    """账号口令登录（多用户：查用户表）。"""
    state = get_state(request)
    cfg = state.cfg
    auth = cfg.get("auth") or {}

    ip = client_ip(request)

    # 1) 先看是否处于锁定期
    locked_seconds = state.login_locked_seconds(ip)
    if locked_seconds > 0:
        raise HTTPException(
            status_code=429,
            detail="登录失败次数过多，请在 %d 秒后重试" % locked_seconds,
        )

    # 2) 校验账号口令。
    #    ★ 用户表是唯一事实来源：config.json 的 auth 段只在「首次引导」时
    #      用来派生管理员（见 users.ensure_bootstrap），之后不再参与登录。
    account, reason = users.authenticate(payload.username, payload.password)
    if account is None:
        remaining = state.register_login_failure(
            ip,
            int(auth.get("max_login_fails") or 5),
            int(auth.get("lockout_seconds") or 300),
        )
        if remaining <= 0:
            raise HTTPException(
                status_code=429,
                detail="登录失败次数过多，账号已临时锁定，请稍后再试",
            )
        # reason 由 users.authenticate 给出：口令错 / 账号停用 / 没设密码
        # 各有各的说法，比笼统一句「用户名或密码错误」更好排查。
        raise HTTPException(
            status_code=401,
            detail="%s（还可尝试 %d 次）" % (reason, remaining),
        )

    # 3) 登录成功
    state.clear_login_failures(ip)
    users.record_login(account["username"])

    secret = str(auth.get("session_secret") or "")
    max_age = session_max_age(cfg)
    now = int(time.time())
    token = sign_token(
        {
            "u": account["username"],
            # ★ 令牌版本。改密码 / 停用 / 强制下线都会把它 +1，
            #   于是旧令牌在中间件里立刻被判为失效 —— 这就是「按用户踢下线」。
            #   （全局轮换 session_secret 也能踢人，但会把所有人一起踢掉，
            #     多用户下不可用。）
            "v": int(account.get("token_version") or 1),
            "iat": now,
            "exp": now + max_age,
        },
        secret,
    )

    response = JSONResponse({
        "ok": True,
        "username": account["username"],
        "display_name": account.get("display_name") or account["username"],
        "role": account.get("role") or "user",
        # 前端后续所有改状态请求都要带上这个头
        "csrf_token": csrf_token_for(token, secret),
        "expires_in": max_age,
    })
    response.set_cookie(
        key=SESSION_COOKIE,
        value=token,
        max_age=max_age,
        httponly=True,       # 脚本读不到，防 XSS 窃取
        samesite="lax",      # 跨站 POST 不会带上，防 CSRF
        path="/",
        # 因为部署在局域网 HTTP 环境，这里不能设 secure=True，否则 Cookie 根本不会被发送
    )
    return response


# 新口令的最小长度。取 8 是常见下限：对单口令系统来说再长收益有限，
# 而太短会让局域网内的暴力破解真的可行。
MIN_PASSWORD_LENGTH = 8


class PasswordPayload(BaseModel):
    """修改口令的请求体。"""
    current_password: str = ""
    new_password: str = ""


@router.post("/password")
async def change_password(request: Request, payload: PasswordPayload) -> Dict[str, Any]:
    """
    修改登录口令。

    四个要点，每条都对应一个真实的攻击面：

    1. **必须校验当前口令。** 只认会话是不够的：会话 Cookie 一旦被窃取，
       攻击者就能直接改口令、把真正的管理员永远锁在外面。要求重输当前口令，
       等于把「改口令」这一步重新拉回到「知道口令」的证明上。

    2. **复用登录那套失败锁定。** 当前口令同样可以被暴力猜；如果这里不限次数，
       登录页的锁定就形同虚设 —— 绕过它只需要先有一个会话。

    3. **改完只让「这一个用户」的旧会话失效。**
       实现上是递增他的 token_version（中间件随即判旧令牌失效），
       而**不再**像单用户时代那样轮换全局 session_secret ——
       那样会把所有用户一起踢下线，多用户下不可用。
       响应带 relogin=true，前端据此引导他重新登录。

    4. **写的是用户表，不是 config.json。** 多用户下 `users.json` 才是唯一的
       用户事实来源；config 的 auth 段只在首次引导时被用过一次。
       （顺带也就不会再碰到「明文兼容项 auth.password 留着、旧口令仍可登录」
       那个坑：用户表里根本不存在明文字段。）
    """
    state = get_state(request)
    cfg = state.cfg
    auth = cfg.get("auth") or {}
    ip = client_ip(request)
    account = get_user(request)          # 当前登录用户

    # 1) 是否处于锁定期（与登录共用同一份失败计数）
    locked_seconds = state.login_locked_seconds(ip)
    if locked_seconds > 0:
        raise HTTPException(
            status_code=429,
            detail="尝试次数过多，请在 %d 秒后重试" % locked_seconds,
        )

    # 2) 校验当前口令 —— 对的是**当前用户**的哈希，不再是 config 里那一个
    checked, reason = users.authenticate(
        account["username"], payload.current_password or "")
    if checked is None:
        remaining = state.register_login_failure(
            ip,
            int(auth.get("max_login_fails") or 5),
            int(auth.get("lockout_seconds") or 300),
        )
        if remaining <= 0:
            raise HTTPException(
                status_code=429,
                detail="当前密码错误次数过多，已临时锁定，请稍后再试",
            )
        raise HTTPException(
            status_code=403,
            detail="%s（还可尝试 %d 次）" % (reason, remaining),
        )

    # 3) 校验新口令
    new_password = payload.new_password or ""
    if len(new_password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(
            status_code=400,
            detail="新密码至少需要 %d 位" % MIN_PASSWORD_LENGTH,
        )
    if _safe_equals(new_password, payload.current_password or ""):
        raise HTTPException(status_code=400, detail="新密码不能与当前密码相同")

    # 4) 写回用户表：set_password 会递增 token_version，
    #    于是该用户此前签发的所有会话（含他自己的其它浏览器）立即失效。
    state.clear_login_failures(ip)
    users.set_password(account["username"], new_password)

    return {
        "ok": True,
        "message": "密码已修改，请用新密码重新登录",
        # 该用户自己的其它设备也一并下线
        "relogin": True,
    }


@router.post("/logout")
async def logout(request: Request) -> JSONResponse:
    """注销当前会话。"""
    response = JSONResponse({"ok": True, "message": "已注销"})
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/status")
async def status(request: Request) -> Dict[str, Any]:
    """
    查询登录状态。

    这个接口是公开的（登录页需要它来判断该显示登录框还是直接进桌面），
    但只有持有**有效**会话的请求才会拿到 csrf_token。

    也要走 resolve_session_user：否则被停用、或被改过密码的用户，
    浏览器仍会显示"已登录"，要一直点到某个接口才被 401 打回登录页 ——
    体验上像是"莫名其妙掉线"，而且登录页的自动跳转逻辑也会判断错。
    """
    state = get_state(request)
    cfg = state.cfg
    auth = cfg.get("auth") or {}
    secret = str(auth.get("session_secret") or "")

    payload = read_session(request, secret, session_max_age(cfg))
    account = resolve_session_user(payload) if payload else None
    if not account:
        return {"authenticated": False, "username": ""}

    token = request.cookies.get(SESSION_COOKIE) or ""
    return {
        "authenticated": True,
        "username": account["username"],
        "display_name": account.get("display_name") or account["username"],
        "role": account.get("role") or "user",
        "csrf_token": csrf_token_for(token, secret),
    }

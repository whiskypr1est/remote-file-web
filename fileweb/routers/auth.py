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

from ..deps import (
    SESSION_COOKIE,
    client_ip,
    get_state,
    read_session,
    session_max_age,
)
from ..security import csrf_token_for, sign_token, verify_password

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


def _verify_credentials(cfg: Dict[str, Any], username: str, password: str) -> bool:
    """
    校验账号口令。

    支持两种配置：
      1. auth.password_hash —— PBKDF2 哈希（推荐，配置文件里看不到明文）
      2. auth.password      —— 明文（兼容用，启动时会高亮警告）
    """
    auth = cfg.get("auth") or {}

    expected_user = str(auth.get("username") or "admin")
    user_ok = _safe_equals(username or "", expected_user)

    password_hash = str(auth.get("password_hash") or "")
    if password_hash:
        # 注意：即使用户名不对也要走一遍口令校验，让耗时保持稳定
        password_ok = verify_password(password or "", password_hash)
        return user_ok and password_ok

    plain = str(auth.get("password") or "")
    if plain:
        password_ok = _safe_equals(password or "", plain)
        return user_ok and password_ok

    # 两种都没配置：拒绝一切登录，避免出现「无密码可进」的危险状态
    return False


@router.post("/login")
async def login(request: Request, payload: LoginPayload) -> JSONResponse:
    """账号口令登录。"""
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

    # 2) 校验账号口令
    if not _verify_credentials(cfg, payload.username, payload.password):
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
        raise HTTPException(
            status_code=401,
            detail="用户名或密码错误（还可尝试 %d 次）" % remaining,
        )

    # 3) 登录成功
    state.clear_login_failures(ip)

    secret = str(auth.get("session_secret") or "")
    max_age = session_max_age(cfg)
    now = int(time.time())
    token = sign_token(
        {"u": str(auth.get("username") or "admin"), "iat": now, "exp": now + max_age},
        secret,
    )

    response = JSONResponse({
        "ok": True,
        "username": str(auth.get("username") or "admin"),
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
    但只有持有有效会话的请求才会拿到 csrf_token。
    """
    state = get_state(request)
    cfg = state.cfg
    auth = cfg.get("auth") or {}
    secret = str(auth.get("session_secret") or "")

    user = read_session(request, secret, session_max_age(cfg))
    if not user:
        return {"authenticated": False, "username": ""}

    token = request.cookies.get(SESSION_COOKIE) or ""
    return {
        "authenticated": True,
        "username": user.get("u") or "",
        "csrf_token": csrf_token_for(token, secret),
    }

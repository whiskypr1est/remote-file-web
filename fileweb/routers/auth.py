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
from ..security import (
    csrf_token_for,
    hash_password,
    random_secret,
    sign_token,
    verify_password,
)

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

    3. **成功后轮换 session_secret，让所有已签发的会话立即失效。**
       这正好补上 README 里「修改密码不会让已登录的浏览器立即掉线」那条已知限制。
       代价是当前这个会话也会失效，所以响应带 relogin=true，前端据此引导重新登录。

    4. **顺手清掉明文兼容项 auth.password。** 它的优先级低于哈希，但只要留着，
       旧口令就仍然能登录 —— 「改了密码却改不掉旧密码」是最容易被忽略的漏洞。
    """
    state = get_state(request)
    cfg = state.cfg
    auth = cfg.get("auth") or {}
    ip = client_ip(request)

    # 1) 是否处于锁定期（与登录共用同一份失败计数）
    locked_seconds = state.login_locked_seconds(ip)
    if locked_seconds > 0:
        raise HTTPException(
            status_code=429,
            detail="尝试次数过多，请在 %d 秒后重试" % locked_seconds,
        )

    # 2) 校验当前口令
    username = str(auth.get("username") or "admin")
    if not _verify_credentials(cfg, username, payload.current_password or ""):
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
            detail="当前密码不正确（还可尝试 %d 次）" % remaining,
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

    # 4) 落盘：新哈希 + 清掉明文项 + 轮换会话密钥
    state.clear_login_failures(ip)

    auth["password_hash"] = hash_password(new_password)
    auth["password"] = ""
    auth["session_secret"] = random_secret(32)
    cfg["auth"] = auth
    state.persist()

    return {
        "ok": True,
        "message": "密码已修改，请用新密码重新登录",
        # 会话密钥已轮换 → 当前这个 Cookie 同时也失效了
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

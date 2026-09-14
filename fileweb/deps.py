# -*- coding: utf-8 -*-
"""
共享依赖与运行期状态
====================

这里集中放三类东西：

1. AppState —— 进程级共享状态（配置、路径解析器、登录失败计数、打包令牌）
2. FastAPI 依赖 —— 取配置、取路径解析器、要求已登录
3. 安全校验小工具 —— CSRF 令牌校验、Origin 校验、客户端 IP 获取

认证中间件本身在 app.py 中注册，它保证「未登录访问任何 /api 接口都返回 401」。
"""

from __future__ import annotations

import hmac
import ipaddress
import os
import socket
import threading
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

from fastapi import HTTPException, Request

from . import users
from .config import save as save_config
from .security import PathResolver, csrf_token_for, verify_token

# 会话 Cookie 名称
SESSION_COOKIE = "rfs_session"
# 前端在改状态请求上必须带的 CSRF 头
CSRF_HEADER = "x-csrf-token"
# 打包下载的一次性令牌参数名
ZIP_TOKEN_PARAM = "token"

# 不需要登录就能访问的 API（登录页依赖这几个接口）
PUBLIC_API_PATHS = {
    "/api/auth/login",
    "/api/auth/status",
}

# 改状态的方法需要过 CSRF 校验
STATE_CHANGING_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# 打包临时目录名（位于项目根目录下，便于集中清理）
ZIP_TEMP_DIRNAME = "temp_zip"
# 打包文件的存活时间（秒），超时自动清理
ZIP_TOKEN_TTL = 3600


# ---------------------------------------------------------------------------
# 运行期状态
# ---------------------------------------------------------------------------

class AppState:
    """
    进程级共享状态。

    之所以不把所有东西都塞进 FastAPI 的依赖注入，是因为其中一部分
    （登录失败计数、打包令牌）需要跨请求共享且要加锁。
    """

    def __init__(self, cfg: Dict[str, Any], base_dir: str):
        self.base_dir = base_dir
        self.cfg = cfg
        self.resolver = self._build_resolver(cfg)
        self.started_at = time.time()

        self._lock = threading.RLock()
        # 登录失败计数：{ip: (失败次数, 锁定到期时间戳)}
        self._login_fails: Dict[str, Tuple[int, float]] = {}
        # 打包令牌：{token: {"path":..., "filename":..., "expires":..., "size":...}}
        self._zip_tokens: Dict[str, Dict[str, Any]] = {}

        self.zip_temp_dir = os.path.join(base_dir, ZIP_TEMP_DIRNAME)

    # -- 路径解析 -----------------------------------------------------------

    @staticmethod
    def _build_resolver(cfg: Dict[str, Any]) -> PathResolver:
        """
        依据配置构建路径解析器。

        开启 mount_all_drives 后，本机所有磁盘会被自动挂成可访问根目录，
        「此电脑」里就能直接看到 C:/D:/E:…，不必逐个写进配置。
        """
        return PathResolver(
            cfg.get("roots") or [],
            auto_drives=bool(cfg.get("mount_all_drives", False)),
            include_network_drives=bool(cfg.get("mount_network_drives", False)),
            include_removable_drives=bool(cfg.get("mount_removable_drives", True)),
        )

    # -- 配置 ---------------------------------------------------------------

    def reload(self) -> None:
        """配置变更后重建解析器（例如换了壁纸，或插入了新磁盘）。"""
        with self._lock:
            self.resolver = self._build_resolver(self.cfg)

    def persist(self) -> None:
        """把当前配置写回 config.json。"""
        save_config(self.cfg)

    # -- 登录失败锁定 -------------------------------------------------------

    def login_locked_seconds(self, ip: str) -> int:
        """返回该 IP 还需锁定多少秒；0 表示未被锁定。"""
        with self._lock:
            record = self._login_fails.get(ip)
            if not record:
                return 0
            _count, locked_until = record
            remaining = int(locked_until - time.time())
            if remaining <= 0:
                # 锁定期已过，顺手清掉记录
                if locked_until > 0:
                    self._login_fails.pop(ip, None)
                return 0
            return remaining

    def register_login_failure(self, ip: str, max_fails: int, lockout_seconds: int) -> int:
        """记录一次登录失败，返回剩余可尝试次数（0 表示已锁定）。"""
        with self._lock:
            count, _locked_until = self._login_fails.get(ip, (0, 0.0))
            count += 1
            if max_fails > 0 and count >= max_fails:
                self._login_fails[ip] = (count, time.time() + max(0, lockout_seconds))
                return 0
            self._login_fails[ip] = (count, 0.0)
            return max(0, max_fails - count)

    def clear_login_failures(self, ip: str) -> None:
        with self._lock:
            self._login_fails.pop(ip, None)

    # -- 打包令牌 -----------------------------------------------------------

    def add_zip_token(self, token: str, path: str, filename: str, size: int) -> None:
        with self._lock:
            self._zip_tokens[token] = {
                "path": path,
                "filename": filename,
                "size": size,
                "expires": time.time() + ZIP_TOKEN_TTL,
            }
            self._cleanup_zip_locked()

    def pop_zip_token(self, token: str) -> Optional[Dict[str, Any]]:
        """取出令牌对应信息（不删除，允许断点续传重复请求）。"""
        with self._lock:
            info = self._zip_tokens.get(token)
            if not info:
                return None
            if info["expires"] < time.time():
                self._zip_tokens.pop(token, None)
                return None
            return info

    def _cleanup_zip_locked(self) -> None:
        """清理过期令牌及其临时文件（调用方需持有锁）。"""
        now = time.time()
        expired = [t for t, info in self._zip_tokens.items() if info["expires"] < now]
        for token in expired:
            info = self._zip_tokens.pop(token, None)
            if not info:
                continue
            try:
                if os.path.isfile(info["path"]):
                    os.unlink(info["path"])
            except OSError:
                pass

    def cleanup_zip_temp(self, max_age_seconds: int = ZIP_TOKEN_TTL) -> int:
        """
        清理打包临时目录：删除过期令牌对应文件，以及目录内所有超龄文件
        （防止进程异常退出后留下孤儿文件）。
        """
        removed = 0
        with self._lock:
            self._cleanup_zip_locked()

        if not os.path.isdir(self.zip_temp_dir):
            return 0

        now = time.time()
        for name in os.listdir(self.zip_temp_dir):
            full = os.path.join(self.zip_temp_dir, name)
            try:
                if os.path.isfile(full) and now - os.path.getmtime(full) > max_age_seconds:
                    os.unlink(full)
                    removed += 1
            except OSError:
                continue
        return removed


# ---------------------------------------------------------------------------
# FastAPI 依赖
# ---------------------------------------------------------------------------

def get_state(request: Request) -> AppState:
    """取进程级共享状态。"""
    state = getattr(request.app.state, "app_state", None)
    if state is None:
        raise HTTPException(status_code=500, detail="服务内部状态未初始化")
    return state


def get_cfg(request: Request) -> Dict[str, Any]:
    """取当前配置。"""
    return get_state(request).cfg


def get_resolver(request: Request) -> PathResolver:
    """取路径解析器。"""
    return get_state(request).resolver


def get_user(request: Request) -> Dict[str, Any]:
    """
    取当前登录用户（由认证中间件写入 request.state.user）。

    拿到的是**完整的用户记录**（username / role / roots / enabled …），
    不是令牌 payload —— 中间件已经用 resolve_session_user 换成了用户表里
    的当前记录。这里再做一次兜底判断，防止某个路由被绕过中间件时出现越权。
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="未登录或会话已过期")
    return user


def resolve_session_user(payload: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """
    把「已验签的令牌 payload」换成**用户表里的当前记录**。

    令牌里只放 `{u, v, iat, exp}`：身份 + 一个版本号。**角色与可见目录一律
    实时从用户表读** —— 好处是降权、改可见目录、改权限下一次请求就生效，
    既不要求人重新登录，也不必去遍历或作废已签发的令牌。

    返回 None 表示这个会话应当视为失效，三种情况：
      * 用户已被删除；
      * 用户被停用（enabled=false）；
      * `token_version` 对不上 —— 改过密码、或被管理员强制下线。

    ⚠️ 兼容性：多用户改造**之前**签发的令牌里没有 `v`。这里把缺失当作 1，
    而引导出来的管理员 token_version 也是 1，所以**升级不会把现有登录踢下线**
    （这是有意的：不要为了上线多用户而打断正在用的人）。
    """
    username = str((payload or {}).get("u") or "").strip()
    if not username:
        return None

    record = users.get(username)
    if record is None:
        return None
    if not record.get("enabled", True):
        return None

    try:
        token_version = int((payload or {}).get("v") or 1)
    except (TypeError, ValueError):
        return None

    if token_version != int(record.get("token_version") or 1):
        return None

    return record


def require_admin(request: Request) -> Dict[str, Any]:
    """
    要求当前用户是管理员。

    用于用户管理、在线会话列表、审计日志这类**只有管理员能用**的接口。

    注意这是界面/接口层面的约束：子用户拥有全权限 cmd，所以它不是对抗
    恶意用户的边界（详见 MULTIUSER.md 第〇节）。
    """
    user = get_user(request)
    if str(user.get("role") or "") != "admin":
        raise HTTPException(status_code=403, detail="该操作仅限管理员")
    return user


# ---------------------------------------------------------------------------
# 安全小工具
# ---------------------------------------------------------------------------

def _ip_in_networks(ip: str, patterns: list) -> bool:
    """
    判断 ip 是否命中 patterns 中的任意一项。

    支持单个 IP（10.0.0.5）与 CIDR（10.0.0.0/8）；无法解析的项直接跳过。
    """
    if not ip:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False

    for pattern in patterns or ():
        text = str(pattern or "").strip()
        if not text:
            continue
        try:
            if "/" in text:
                if addr in ipaddress.ip_network(text, strict=False):
                    return True
            elif addr == ipaddress.ip_address(text):
                return True
        except ValueError:
            continue
    return False


def client_ip(request: Request) -> str:
    """
    取客户端 IP，用于登录失败计数与审计日志。

    安全要点：X-Forwarded-For 是客户端可以随意伪造的请求头。早先的实现无条件
    采信它，导致攻击者每次换一个伪造值就能让该 IP 的失败计数永远归零，
    从而完全绕过 max_login_fails 锁定（实测 5 次失败锁定后，换一个
    X-Forwarded-For 立刻又能拿到有效会话）。

    因此这里默认**不信任**该头，只使用 TCP 连接的真实对端 IP；
    只有当对端本身就落在 auth.trusted_proxies 里（即我们自己的反向代理）时，
    才采信 X-Forwarded-For 的第一段。
    """
    peer = ""
    if request.client and request.client.host:
        peer = request.client.host

    state = getattr(request.app.state, "app_state", None)
    cfg = (state.cfg if state is not None else None) or {}
    trusted = (cfg.get("auth") or {}).get("trusted_proxies") or []

    if trusted:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded and _ip_in_networks(peer, trusted):
            first = forwarded.split(",")[0].strip()
            if first:
                return first

    return peer or "unknown"


def check_csrf(request: Request, session_token: str, secret: str) -> bool:
    """
    校验 CSRF 令牌。

    令牌由会话令牌派生（见 security.csrf_token_for），前端通过
    X-CSRF-Token 头带上来。因为跨站请求无法读取该值，
    也无法自定义该请求头，所以能有效阻断 CSRF。
    """
    provided = request.headers.get(CSRF_HEADER) or ""
    if not provided:
        return False
    expected = csrf_token_for(session_token, secret)
    return hmac.compare_digest(provided, expected)


def check_origin(request: Request) -> bool:
    """
    校验请求来源与 Host 是否一致。

    浏览器发起的跨站表单/请求会带上 Origin 头，这里做一次同源判断。
    非浏览器客户端（脚本、curl）通常不带 Origin，此时放行，
    因为这类客户端本来也不受 CSRF 影响。
    """
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True
    try:
        parsed = urlparse(origin)
    except Exception:  # noqa: BLE001
        return False
    host = request.headers.get("host") or ""
    return parsed.netloc.lower() == host.lower()


def read_session(request: Request, secret: str, max_age_seconds: int) -> Optional[Dict[str, Any]]:
    """从 Cookie 里读会话令牌并校验。"""
    token = request.cookies.get(SESSION_COOKIE)
    if not token:
        return None
    return verify_token(token, secret, max_age_seconds=max_age_seconds)


def session_max_age(cfg: Dict[str, Any]) -> int:
    """会话有效期（秒）。"""
    hours = int((cfg.get("auth") or {}).get("session_hours") or 12)
    return max(1, hours) * 3600


# ---------------------------------------------------------------------------
# 网络信息（系统托盘显示服务器 IP 用）
# ---------------------------------------------------------------------------

def local_ip_addresses() -> list:
    """
    获取本机所有 IPv4 地址。

    用 UDP 连接技巧拿「默认出口 IP」不依赖外网是否可达
    （UDP connect 不会真的发包），拿不到时退回 hostname 解析。
    """
    addresses = []

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.connect(("8.8.8.8", 80))
            addresses.append(sock.getsockname()[0])
        finally:
            sock.close()
    except Exception:  # noqa: BLE001
        pass

    try:
        _hostname, _aliases, ips = socket.gethostbyname_ex(socket.gethostname())
        for ip in ips:
            if ip not in addresses and not ip.startswith("127."):
                addresses.append(ip)
    except Exception:  # noqa: BLE001
        pass

    if not addresses:
        addresses.append("127.0.0.1")

    return addresses

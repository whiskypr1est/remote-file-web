# -*- coding: utf-8 -*-
"""
远程文件管理服务 —— 程序入口
============================

启动方式：
    python app.py                     # 读取 config.json
    python app.py --port 9000         # 临时换端口
    python app.py --config other.json # 指定配置文件
    uvicorn app:app --host 0.0.0.0 --port 8000   # 用 uvicorn 直接跑

本文件负责：
    1. 加载配置、创建根目录与缓存目录
    2. 组装 FastAPI 应用（认证中间件、统一错误处理、静态资源、路由）
    3. 打印启动横幅（本机/局域网访问地址）

安全相关的三件事都在这里收口：
    * 除白名单外的所有 /api 接口，未登录一律 401
    * 改状态请求（POST/PUT/PATCH/DELETE）必须带正确的 CSRF 令牌且同源
    * 所有响应都加 nosniff，HTML 响应额外加 CSP，防止上传内容被当页面执行
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from contextlib import asynccontextmanager
from urllib.parse import urlparse

# 让中文日志在 GBK 控制台里也能正常输出（Windows 上很常见）
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException

from fileweb import APP_NAME, __version__, config as config_module
from fileweb import presence
from fileweb import users as users_module
from fileweb.deps import (
    PUBLIC_API_PATHS,
    SESSION_COOKIE,
    STATE_CHANGING_METHODS,
    AppState,
    check_csrf,
    check_origin,
    resolve_client_ip,
    resolve_session_user,
    session_max_age,
)
from fileweb.http_utils import json_error
from fileweb.office import find_soffice
from fileweb.routers import auth as auth_router
from fileweb.routers import content as content_router
from fileweb.routers import desktop as desktop_router
from fileweb.routers import fs as fs_router
from fileweb.routers import jobs as jobs_router
from fileweb.routers import sysmon as sysmon_router
from fileweb.routers import system as system_router
from fileweb.routers import terminal as terminal_router
from fileweb.routers import users as users_router
from fileweb.security import verify_token

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

# 内容安全策略：只允许加载本服务自己的脚本，禁止把上传的文件当页面执行
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob:; "
    "media-src 'self' blob: data:; "
    "connect-src 'self'; "
    "worker-src 'self' blob:; "
    "font-src 'self' data:; "
    "object-src 'none'; "
    "frame-src 'self' blob:; "
    "base-uri 'self'; "
    "form-action 'self'"
)


# ===========================================================================
# 纯 ASGI 安全中间件
# ===========================================================================

class SecurityMiddleware:
    """
    认证 + CSRF + 安全响应头中间件。

    这里刻意使用「纯 ASGI 中间件」而不是 Starlette 的 BaseHTTPMiddleware：
    后者会把响应整体包一层，对视频/大文件的流式输出有额外开销，
    也更容易在客户端中断时出错。纯 ASGI 版本只是旁路检查，不碰响应体。

    只读取请求头与 Cookie，不读取请求体，
    因此 2GB 的上传流不会被中间件缓冲。
    """

    def __init__(self, asgi_app, base_dir: str):
        self.app = asgi_app
        self.base_dir = base_dir

    async def __call__(self, scope, receive, send):
        scope_type = scope.get("type")

        if scope_type == "websocket":
            # ★ 安全修复：WebSocket 绝不能绕过认证。
            #
            # 原先这里是把所有非 http 的 scope 直接放行的，对 lifespan 没问题，
            # 但对 WebSocket 是致命的：浏览器发起 WS 握手时会**自动带上 Cookie**，
            # 于是局域网内任何一台机器只要打开一个页面就能连上来。
            # 命令行功能正是通过 WS 给出一个真实 shell，
            # 一旦放行就等于把服务器的命令行交出去（以服务身份运行时还是 SYSTEM）。
            #
            # 因此 WS 必须和 http 一样过认证，并且额外做 Origin 同源校验
            # （见 _websocket_allowed），失败时不调用下游应用。
            if not await self._websocket_allowed(scope, send):
                return
            await self.app(scope, receive, send)
            return

        if scope_type != "http":
            # lifespan 等其它类型直接放行
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "") or ""

        # 只有 /api 下的接口需要登录，静态资源与页面本身必须放行，
        # 否则登录页自己都加载不出来。
        if path.startswith("/api") and path not in PUBLIC_API_PATHS:
            state: AppState = scope["app"].state.app_state
            secret = str((state.cfg.get("auth") or {}).get("session_secret") or "")

            headers = Headers(scope=scope)
            cookie_header = headers.get("cookie") or ""
            token = self._read_cookie(cookie_header, SESSION_COOKIE)

            payload = verify_token(token, secret, max_age_seconds=session_max_age(state.cfg)) if token else None

            if not payload:
                response = json_error("未登录或会话已过期，请重新登录", status_code=401, code="unauthorized")
                await response(scope, receive, send)
                return

            # ★ 令牌里只有 {u, v, iat, exp}，这里换成**用户表里的当前记录**
            #   （多用户改造）。角色与可见目录实时从用户表读，所以降权、改可见
            #   目录下一次请求就生效；用户被删、被停用、或 token_version 对不上
            #   （改过密码 / 被强制下线）都算会话失效。
            account = resolve_session_user(payload)
            if account is None:
                response = json_error("会话已失效，请重新登录", status_code=401, code="unauthorized")
                await response(scope, receive, send)
                return

            # 把用户记录挂到 scope["state"]，下游 request.state.user 就能取到
            scope.setdefault("state", {})["user"] = account

            # ★ 在线表：每个已认证请求都把「最近见过他」刷新一下。
            # 放在这里而不是各路由里，是为了「谁在线」不取决于某个路由有没有
            # 记得上报 —— 漏一个路由就会出现「人在用但显示离线」。
            # 更新是节流的（见 presence.TOUCH_INTERVAL_SECONDS），
            # 而且失败只打警告，绝不影响请求本身。
            peer = ""
            client = scope.get("client")
            if client:
                peer = client[0] or ""
            presence.touch(
                account.get("username") or "",
                ip=resolve_client_ip(
                    peer, headers,
                    (state.cfg.get("auth") or {}).get("trusted_proxies") or []),
                display_name=account.get("display_name") or "",
                role=account.get("role") or "",
            )

            # 改状态请求：校验同源 + CSRF 令牌
            method = (scope.get("method") or "GET").upper()
            if method in STATE_CHANGING_METHODS:
                request = Request(scope, receive=receive, send=send)

                if not check_origin(request):
                    response = json_error("请求来源校验失败（跨站请求已被拒绝）", status_code=403, code="bad_origin")
                    await response(scope, receive, send)
                    return

                if not check_csrf(request, token or "", secret):
                    response = json_error(
                        "CSRF 校验失败，请刷新页面后重试",
                        status_code=403,
                        code="bad_csrf",
                    )
                    await response(scope, receive, send)
                    return

        # 统一追加安全响应头
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                response_headers = MutableHeaders(scope=message)
                response_headers.setdefault("X-Content-Type-Options", "nosniff")
                response_headers.setdefault("Referrer-Policy", "same-origin")
                response_headers.setdefault("X-Frame-Options", "SAMEORIGIN")
                content_type = response_headers.get("content-type", "") or ""
                if "text/html" in content_type:
                    response_headers.setdefault("Content-Security-Policy", CONTENT_SECURITY_POLICY)

                # 自有前端代码（js/css）必须每次都回源校验。
                # 如果完全不设 Cache-Control，浏览器会按「启发式缓存」规则
                # 依据 Last-Modified 推算新鲜期，在此期间根本不会来问服务器，
                # 结果就是改了前端代码、用户刷新页面却仍在使用旧版本。
                # no-cache 表示「可以缓存，但每次都要带 ETag 回源校验」，
                # 未变更时服务端返回 304，几乎没有额外开销。
                if path.startswith("/static/js/") or path.startswith("/static/css/"):
                    response_headers.setdefault("Cache-Control", "no-cache")
            await send(message)

        await self.app(scope, receive, send_with_headers)

    @staticmethod
    def _read_cookie(cookie_header: str, name: str) -> str:
        """从 Cookie 头里取出指定名称的值。"""
        if not cookie_header:
            return ""
        for chunk in cookie_header.split(";"):
            chunk = chunk.strip()
            if not chunk:
                continue
            key, _, value = chunk.partition("=")
            if key.strip() == name:
                return value.strip()
        return ""

    # -- WebSocket 认证（终端等实时通道的安全入口） -------------------------

    async def _websocket_allowed(self, scope, send) -> bool:
        """
        校验一个 WebSocket 连接是否允许建立。

        返回 False 表示已经发送了拒绝帧，调用方必须直接 return，
        **不要**再调用下游应用。

        校验顺序（先校验来源再做会话校验，便宜的先做）：
          1. 只有 /api 下的路径需要认证；PUBLIC_API_PATHS 里的公开接口放行
             （目前都是 HTTP 接口，这里保留判断是为了口径一致）。
          2. Origin 必须存在，且其 host 必须与 Host 头一致。
             —— 浏览器发起 WS 握手一定会带 Origin，因此「缺失」本身就是异常，
                一律拒绝（`Origin: null` 这种也一并拒绝）。
                这一条防的是其它站点用用户浏览器当跳板连上本服务的 WS。
          3. Cookie 里的 rfs_session 必须能通过 verify_token 验签且未过期。

        通过后把用户信息与会话令牌写进 scope["state"]：
        路由层要用 session_token 去校验「这个终端会话确实属于当前登录者」，
        避免 sid 泄露后被他人接管。
        """
        path = scope.get("path", "") or ""

        # 非 /api 下的 WS 不涉及认证（本项目当前没有这类通道）
        if not path.startswith("/api") or path in PUBLIC_API_PATHS:
            return True

        state: AppState = scope["app"].state.app_state
        secret = str((state.cfg.get("auth") or {}).get("session_secret") or "")
        headers = Headers(scope=scope)

        # ---- 1) 同源校验 ----
        if not self._origin_matches_host(headers):
            await self._reject_websocket(send)
            return False

        # ---- 2) 会话校验 ----
        cookie_header = headers.get("cookie") or ""
        token = self._read_cookie(cookie_header, SESSION_COOKIE)
        payload = verify_token(token, secret, max_age_seconds=session_max_age(state.cfg)) if token else None

        if not payload:
            await self._reject_websocket(send)
            return False

        # 与 HTTP 走同一套：令牌 → 用户表里的当前记录。
        # 这一条对已经建立的 WS 同样有效：被停用或改过密码的人，
        # 下次重连会被拒（否则"停用"对他手里的终端毫无作用）。
        account = resolve_session_user(payload)
        if account is None:
            await self._reject_websocket(send)
            return False

        scope_state = scope.setdefault("state", {})
        scope_state["user"] = account
        # 供路由做「会话归属」二次校验（见 routers/terminal.py）
        scope_state["session_token"] = token
        return True

    @staticmethod
    def _origin_matches_host(headers) -> bool:
        """
        Origin 头的 host 是否与 Host 头一致。

        与 deps.check_origin 的宽松版不同，这里要求 Origin **必须存在**：
        WebSocket 握手一定由脚本发起，浏览器必定附带 Origin，
        所以缺失 Origin 只可能是非浏览器客户端，对「给 shell」这种高危功能
        宁可拒绝（需要在脚本里连 WS 时必须自己带上正确的 Origin 头）。
        """
        origin = headers.get("origin") or ""
        host = headers.get("host") or ""
        if not origin or not host:
            return False

        try:
            parsed = urlparse(origin)
        except Exception:  # noqa: BLE001
            return False

        # 只接受 http/https；"null"、file: 等一律拒绝
        if parsed.scheme not in ("http", "https"):
            return False

        return parsed.netloc.lower() == host.lower()

    @staticmethod
    async def _reject_websocket(send) -> None:
        """
        在握手完成前拒绝 WebSocket。

        1008 = Policy Violation。因为是在 accept 之前发送 close，
        ASGI 服务器会把它翻译成 HTTP 403 握手失败，
        客户端连 "connected" 状态都不会进入。
        """
        try:
            await send({"type": "websocket.close", "code": 1008})
        except Exception:  # noqa: BLE001
            pass


# ===========================================================================
# 应用组装
# ===========================================================================

@asynccontextmanager
async def app_lifespan(fastapi_app: FastAPI):
    """
    应用生命周期钩子。

    启动时清理上次异常退出可能遗留的打包临时文件；
    关闭时（含 Ctrl+C 正常退出）再清一次，避免临时 zip 长期占用磁盘。
    """
    state: AppState = fastapi_app.state.app_state

    removed = state.cleanup_zip_temp()
    if removed:
        print("[启动] 清理了 %d 个过期的打包临时文件" % removed)

    try:
        yield
    finally:
        state.cleanup_zip_temp(max_age_seconds=0)

        # 关掉所有命令行会话：否则退出后会留下常驻的 cmd.exe 孤儿进程，
        # 而它们可能正以服务身份（SYSTEM）在跑。
        try:
            from fileweb.terminal import manager as terminal_manager

            await terminal_manager.close_all()
        except Exception:  # noqa: BLE001
            pass


def create_app(cfg=None) -> FastAPI:
    """创建并配置 FastAPI 应用。"""
    if cfg is None:
        cfg = config_module.load()

    # ---- 运行期目录与根目录准备 ----
    config_module.ensure_runtime_dirs(cfg)

    app = FastAPI(
        title=cfg.get("server", {}).get("title") or APP_NAME,
        version=__version__,
        docs_url=None,       # 关掉自动文档，避免对外暴露接口结构
        redoc_url=None,
        openapi_url=None,
        lifespan=app_lifespan,
    )

    # ---- 多用户：用户表位置 + 首次引导 ----
    #
    # 用户表放在**配置文件旁边**：用 --config 起多实例时各存各的，
    # 这也是测试能安全隔离的前提（不会碰到真实部署的那一份）。
    # cfg 里的 _cfg_path 是 config.load() 记下来的来源路径 —— 当初正是为了
    # 「必须写回同一个文件」这类场景才加的（见 config.save 的注释与那起事故）。
    cfg_path = str(cfg.get("_cfg_path") or "")
    if cfg_path:
        users_module.set_path(
            os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "users.json"))

    # 还没有用户表时，用 config.json 的 auth 段派生管理员：老部署升级上来
    # 口令继续有效、管理员 roots 为空走 mount_all_drives 仍看全机。
    _bootstrap = users_module.ensure_bootstrap(cfg)
    if _bootstrap.get("warning"):
        print("\n" + "!" * 68)
        print("[!] " + _bootstrap["warning"])
        print("!" * 68 + "\n")

    # 进程级共享状态
    app.state.app_state = AppState(cfg, BASE_DIR)

    # ---- 安全中间件 ----
    app.add_middleware(SecurityMiddleware, base_dir=BASE_DIR)

    # ---- 统一错误响应（前端只需要读 message 字段） ----
    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        message = exc.detail if isinstance(exc.detail, str) else "请求处理失败"
        if request.url.path.startswith("/api"):
            return json_error(message, status_code=exc.status_code)
        return Response(message, status_code=exc.status_code, media_type="text/plain; charset=utf-8")

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        # 参数校验失败：给出人能看懂的提示，而不是 Pydantic 的原始结构
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", []) if part != "body")
        detail = first.get("msg", "参数不合法")
        return json_error(
            "请求参数不合法：%s%s" % (location and (location + " ") or "", detail),
            status_code=422,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        # 未预期的异常：服务端打完整堆栈，客户端只给简短提示
        print("\n[错误] 处理 %s %s 时发生未捕获异常：" % (request.method, request.url.path))
        traceback.print_exc()
        if request.url.path.startswith("/api"):
            return json_error("服务器内部错误：%s" % exc, status_code=500)
        return Response("服务器内部错误", status_code=500, media_type="text/plain; charset=utf-8")

    # ---- 业务路由 ----
    app.include_router(auth_router.router)
    app.include_router(system_router.router)
    app.include_router(fs_router.router)
    app.include_router(content_router.router)
    app.include_router(desktop_router.router)
    app.include_router(terminal_router.router)
    app.include_router(sysmon_router.router)
    app.include_router(jobs_router.router)
    app.include_router(users_router.router)

    # ---- 静态资源 ----
    if os.path.isdir(STATIC_DIR):
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    # ---- 页面 ----
    @app.get("/", include_in_schema=False)
    async def index_page():
        """桌面主页面。未登录时由前端脚本跳转到登录页。"""
        path = os.path.join(STATIC_DIR, "index.html")
        if not os.path.isfile(path):
            return Response("前端资源缺失：static/index.html", status_code=500)
        return FileResponse(path, media_type="text/html; charset=utf-8", headers={
            "Cache-Control": "no-cache",
        })

    @app.get("/login.html", include_in_schema=False)
    async def login_page():
        """Windows 风格登录页。"""
        path = os.path.join(STATIC_DIR, "login.html")
        if not os.path.isfile(path):
            return Response("前端资源缺失：static/login.html", status_code=500)
        return FileResponse(path, media_type="text/html; charset=utf-8", headers={
            "Cache-Control": "no-cache",
        })

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon():
        """图标：优先返回 svg，没有就 204，避免控制台一直报 404。"""
        svg = os.path.join(STATIC_DIR, "favicon.svg")
        if os.path.isfile(svg):
            return FileResponse(svg, media_type="image/svg+xml")
        return Response(status_code=204)

    return app


# 供 `uvicorn app:app` 使用
app = create_app()


# ===========================================================================
# 启动横幅与命令行入口
# ===========================================================================

def print_banner(cfg) -> None:
    """打印启动信息：访问地址、根目录、依赖检测结果。"""
    state = app.state.app_state
    server_cfg = cfg.get("server") or {}
    auth_cfg = cfg.get("auth") or {}
    office_cfg = cfg.get("office") or {}

    host = server_cfg.get("host") or "0.0.0.0"
    port = int(server_cfg.get("port") or 8000)

    from fileweb.deps import local_ip_addresses

    line = "=" * 68
    print("\n" + line)
    print("  %s  v%s" % (APP_NAME, __version__))
    print(line)

    print("  本机访问 : http://127.0.0.1:%d" % port)
    for ip in local_ip_addresses():
        print("  局域网   : http://%s:%d" % (ip, port))
    print("  监听地址 : %s:%d" % (host, port))

    print("\n  登录账号 : %s" % (auth_cfg.get("username") or "admin"))
    print("  会话时长 : %s 小时" % (auth_cfg.get("session_hours") or 12))

    print("\n  允许访问的根目录：")
    for root in state.resolver.roots:
        flags = []
        if root.get("readonly"):
            flags.append("只读")
        if not os.path.isdir(root["path"]):
            flags.append("目录不存在")
        suffix = ("  [%s]" % "、".join(flags)) if flags else ""
        print("    - %s  ->  %s%s" % (root["name"], root["display_path"], suffix))
    if not state.resolver.roots:
        print("    (未配置任何根目录！请检查 config.json 的 roots 字段)")

    # 依赖检测
    print("\n  依赖检测：")
    if office_cfg.get("enabled", True):
        soffice = find_soffice(office_cfg.get("soffice_path") or "")
        if soffice:
            print("    LibreOffice : 已找到 -> %s" % soffice)
        else:
            print("    LibreOffice : 未安装")
            print("                  doc/xls/ppt 老格式将无法预览；")
            print("                  docx/xlsx/pptx 会用内置解析器降级显示内容。")
            print("                  安装地址：https://www.libreoffice.org/download/")
    else:
        print("    LibreOffice : 已在配置中关闭 Office 预览")

    thumbs_cfg = cfg.get("thumbs") or {}
    print("    缩略图缓存  : %s" % thumbs_cfg.get("cache_dir"))
    office_cache = (cfg.get("office") or {}).get("cache_dir")
    print("    转换缓存    : %s" % office_cache)

    delete_cfg = cfg.get("delete") or {}
    use_recycle = delete_cfg.get("use_recycle_bin", True)
    print("    删除方式    : %s" % ("移至回收站" if use_recycle else "永久删除"))
    if use_recycle:
        try:
            import send2trash  # noqa: F401
        except ImportError:
            print("                  [!] 未安装 send2trash，删除会失败；")
            print("                      请执行 pip install send2trash，")
            print("                      或把 config.json 的 delete.use_recycle_bin 改为 false")

    # 明文口令警告
    if auth_cfg.get("password") and not auth_cfg.get("password_hash"):
        print("\n  [安全警告] config.json 中使用了明文密码（auth.password）。")
        print("             建议执行 python tools/gen_password.py --set 改为哈希存储。")

    print("\n  提示：如果局域网其他电脑打不开，请放行防火墙端口：")
    print('    netsh advfirewall firewall add rule name="FileWeb %d" dir=in action=allow protocol=TCP localport=%d' % (port, port))
    print(line + "\n")


def main() -> int:
    """命令行入口。"""
    parser = argparse.ArgumentParser(
        description="%s —— 局域网 Windows 风格文件管理服务" % APP_NAME,
    )
    parser.add_argument("--host", default=None, help="监听地址，默认取 config.json 的 server.host")
    parser.add_argument("--port", type=int, default=None, help="监听端口，默认取 config.json 的 server.port")
    parser.add_argument("--config", default=None, help="指定配置文件路径，默认 config.json")
    parser.add_argument("--log-level", default=None, help="日志级别：debug/info/warning/error")
    parser.add_argument("--reload", action="store_true", help="开发用：代码变更自动重载")
    args = parser.parse_args()

    # ---- 加载配置 ----
    try:
        cfg = config_module.load(args.config)
    except SystemExit as exc:
        print(exc)
        return 1

    global app
    app = create_app(cfg)

    # ---- 创建/检查根目录 ----
    results = config_module.ensure_roots_exist(cfg)
    for path, ok, note in results:
        if not ok:
            print("[!] 根目录不可用：%s（%s）" % (path, note))

    # 重新构建解析器，让新建好的目录立即生效
    app.state.app_state.reload()

    server_cfg = cfg.get("server") or {}
    host = args.host or server_cfg.get("host") or "0.0.0.0"
    port = args.port or int(server_cfg.get("port") or 8000)
    log_level = args.log_level or (cfg.get("log") or {}).get("level") or "info"

    print_banner(cfg)

    try:
        import uvicorn
    except ImportError:
        print("[错误] 未安装 uvicorn，请先执行：pip install -r requirements.txt")
        return 1

    # 与 auth.trusted_proxies 保持一致。
    # uvicorn 默认开启 proxy_headers，且 forwarded_allow_ips 默认是 "127.0.0.1" ——
    # 它会在我们的 SecurityMiddleware **之前**就把 scope["client"] 改写成
    # X-Forwarded-For 里的值。那个头客户端可以随意伪造，于是登录失败计数会按
    # 伪造的 IP 记录，攻击者换一个伪造值就能绕过 max_login_fails 锁定
    # （实测：锁定之后换一个伪造 IP 依然能拿到有效会话）。
    # 所以没有配置可信代理时干脆关掉；配置了才把该列表作为允许来源交给 uvicorn。
    trusted_proxies = [
        str(item).strip()
        for item in ((cfg.get("auth") or {}).get("trusted_proxies") or [])
        if str(item).strip()
    ]

    try:
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=str(log_level).lower(),
            # 大文件上传/下载可能很慢，关掉 keep-alive 超时的干扰，交给浏览器
            timeout_keep_alive=30,
            access_log=str(log_level).lower() == "debug",
            reload=bool(args.reload),
            # 只信任显式配置的反向代理，避免 uvicorn 自己采信可伪造的 X-Forwarded-For
            proxy_headers=bool(trusted_proxies),
            forwarded_allow_ips=",".join(trusted_proxies) if trusted_proxies else "127.0.0.1",
        )
    except OSError as exc:
        print("\n[错误] 启动失败：%s" % exc)
        print("可能原因：端口 %d 已被占用，或没有权限绑定该端口。" % port)
        print("可以换个端口：python app.py --port 9000")
        return 1
    except KeyboardInterrupt:
        print("\n已停止。")

    return 0


if __name__ == "__main__":
    sys.exit(main())

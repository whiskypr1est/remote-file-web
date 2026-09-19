# -*- coding: utf-8 -*-
"""
命令行路由
==========

    POST /api/terminal/session   —— 创建一个命令行会话，返回 sid
    WS   /api/terminal/ws?sid=x  —— 双向转发终端输入输出

★ 会话可分离（detach）
=====================
    这是本次的核心行为：**WebSocket 断开不再结束会话**。

    用户关掉浏览器标签页、刷新页面、网络抖动导致 WS 断掉时，
    cmd.exe 子进程继续跑，输出继续进入会话自己的滚动缓冲（上限
    terminal.max_output_kb）。之后用**同一个 sid** 重新连上来，
    会先收到这段时间积压的输出，然后无缝继续交互。

    只有下面三种情况才真正结束会话（杀掉进程）：
      1. 空闲超时（terminal.idle_timeout_seconds，且没有客户端连着）
      2. 客户端显式发 {"type":"close"}
      3. 服务退出（app.py 的 lifespan 调用 manager.close_all()）
    这是刻意的取舍：「浏览器崩了」远比「用户不要这个会话了」常见，
    所以把「断开」解释成「离开」而不是「结束」。

★ 重连协议（前端需要照此实现）
=============================
    连接方式（二选一，两种都支持）：
      A) 建连时就带会话 id：  ws://<host>/api/terminal/ws?sid=<sid>
         这是原有方式，不需要改任何东西就能重连。
         查询参数名就是 **sid**（同时兼容 session / s 两个别名）。
      B) 建连后第一条消息指定：
         {"type":"attach","sid":"<sid>"}
         适合「一个 WS 连接之后才决定要看哪个会话」的写法。
         已经连上之后再发 attach 只允许指向当前会话，指向别的会话
         会回一条 error（不支持中途切换会话）。

    服务端 -> 客户端 的新增消息：
      {"type":"attached","id":"<sid>","backend":"conpty|pipe",
       "cols":N,"rows":M,"replay":<bool>,"buffered_kb":<float>,
       "expires_in":<int>}
          连接成功可用时**立即**下发，前端据此确认「连上的是哪个会话」。
          replay=true 表示这是一次重连（会话之前已经存在），
          前端可以据此提示「已恢复到之前的会话」。
      {"type":"closed","reason":"nomatch|forbidden|ended",
       "message":"会话已结束（空闲超时被回收）"}
          会话已经不存在了（被回收 / 进程结束 / 被显式关闭）。
          收到它就说明**没法继续**了，前端应显示「会话已结束」并禁用输入。
          随后服务端会以关闭码 4404 关闭连接。
          reason 就是上面这三种取值（与代码实际发送的一致）：
            nomatch / forbidden 只出现在「首条 attach 消息」那条建连路径上
            （那条路必须先 accept 才能读到消息，所以没法再用握手 403 拒绝）；
            ended 表示会话已结束（进程退出 / 被回收 / 被显式关闭）。
          ★ 前端**不要**去判 "reaped"、"closed" 这类值：空闲回收只发生在
          「没有客户端连着」的时候，所以「会话被回收」的用户实际是在下一次
          连接时于**握手阶段**被拒（HTTP 403），根本走不到这条消息；
          已经连着的会话被结束时统一报 ended。
      {"type":"replaced","message":"该终端会话已被新的连接接管"}
          同一个会话被另一个连接（通常是同一个浏览器新开的标签页/刷新）
          接管了。旧连接会被以关闭码 4001 关闭（见 _WS_CLOSE_REPLACED）。
          注意不是 4401 —— 这里以前写错过，前端据此实现会认错码；
          前端目前两个码都接受，但文档必须与常量一致。
      {"type":"truncated","dropped_bytes":N}
          重连时发现离开期间输出超过了 terminal.max_output_kb，
          最旧的部分已被丢弃（N 是估算的丢弃字节数），前端可据此提示用户。
      {"type":"ping","t":<unix 秒>}
          长时间没有输出时发的保活帧（约 60 秒一次）。
          ★ 它只是为了让人能察觉「连接其实已经僵死了」，不要求前端回应任何
          东西：收到后**忽略即可**，既不要当错误，也不要用它去重置终端内容。

    客户端 -> 服务端：原有消息（input / resize / interrupt / close）不变，
    另新增 attach（见上）。

安全说明（这是本模块存在的意义所在，改动前请先读完整段）：
    * HTTP 侧：本路由挂在 /api 下，认证、同源、CSRF 由 app.py 的
      SecurityMiddleware 统一把关，所以创建会话必须带 X-CSRF-Token。
    * WS 侧：**中间件对 WebSocket 也要做登录校验**（见 app.py 中
      SecurityMiddleware 对 scope["type"] == "websocket" 的分支），
      并且校验 Origin 与 Host 同源。否则任何能访问到本服务的设备
      都可以直接连上来拿到一个 shell——这是本功能最大的风险点。
    * sid 使用 secrets.token_urlsafe(24) 生成，无法枚举；同时把
      「创建会话时的登录令牌摘要」绑在会话上，连 WS 时必须提供同一个
      令牌才能通过，即使 sid 因为某种原因泄露也无法被他人接管。
      ★ 会话可分离之后这一条更重要：会话寿命变长了，
        如果 sid 能跨登录身份复用，就等于把别人的 shell 变成了公共资源。

审计：
    「打开一个 shell」是高危动作，因此每次创建会话都会往服务端控制台
    打一行审计日志（含客户端 IP 与实际使用的 shell），
    连接、断开（分离）也都会留痕，便于事后追溯。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Request, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from ..deps import (
    SESSION_COOKIE,
    client_ip,
    feature_allowed_for,
    get_state,
    get_user,
    owner_of,
    require_feature,
    resolver_of,
)
from ..terminal import (
    SessionGone,
    SessionReplaced,
    TerminalDisabledError,
    TerminalError,
    TerminalLimitError,
    TerminalSpawnError,
    manager,
)

router = APIRouter(prefix="/api/terminal", tags=["命令行"])

# 单条消息的最大长度（防止恶意客户端用超大 JSON 撑爆内存）
_MAX_MESSAGE_CHARS = 1_000_000

# WebSocket 关闭码（自定义段 4000-4999，避免与协议保留码冲突）。
# 前端可以用它区分「被接管」和「会话没了」，比只看文案可靠。
_WS_CLOSE_REPLACED = 4001
_WS_CLOSE_GONE = 4404

# 没有输出时也定期给客户端发一次 ping。
# 作用不是保活服务端（服务端在 WS 连着时本来就不会回收会话），
# 而是让前端能察觉「连接其实早就断了」——否则一个僵死的连接会一直以
# connected 状态挂在界面上，用户敲什么都没反应。
_WS_KEEPALIVE_SECONDS = 60.0

# sid 查询参数接受的别名（sid 是正式名，另外两个是兼容写法）
_SID_QUERY_KEYS = ("sid", "session", "s")



class StartDirPayload(BaseModel):
    """一个「根标识 + 相对路径」引用（沿用文件管理器那套寻址）。"""

    root: str = ""
    path: str = ""


# 允许「双击运行」的扩展名。
# ★ 只放脚本，不放 .exe：这个功能的定位是「把已经写好的启动脚本跑起来」。
#   .exe 直接双击在真实桌面上是另一个风险量级的东西（而且控制台程序跑起来
#   也看不到窗口），要跑 exe 请在命令行里敲 —— 终端本来就是全权限的。
RUNNABLE_EXTS = {".bat", ".cmd", ".ps1"}


class SessionPayload(BaseModel):
    """
    创建会话的请求体。

    cols/rows 是客户端 xterm.js 量出来的**真实**列宽与行高。
    必须在创建时一并传过来：ConPTY 是在 spawn 那一刻把初始尺寸交给内核的，
    虽然之后可以用 resize 消息纠正，但那样第一屏的换行位置会先错一次。

    ★ 下面两个可选字段实现「在虚拟桌面里直接运行脚本」：

      start_dir  以某个**目录**为工作目录开命令行（资源管理器右键
                 「在此处打开命令行」）。批处理里几乎都用相对路径引用同目录的
                 文件（`java -jar server.jar`），cwd 不对就会直接失败。

      run        启动时立刻执行某个**文件**（双击 .bat → 「运行」）。
                 它会被串进同一个命令行（cmd 的 /K 参数里），所以输出留在
                 本会话里；cwd 自动取该文件所在目录。

    两者都用「根标识 + 相对路径」表达，并且**必须过用户自己的解析器** ——
    路径闸门只有一道，不能因为从终端进来就放行（见 security.PathResolver）。
    """

    cols: int = 120
    rows: int = 30
    start_dir: Optional[StartDirPayload] = None
    run: Optional[StartDirPayload] = None


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _terminal_cfg(state) -> Dict[str, Any]:
    """取 terminal 配置块（缺失时给一份安全默认值）。"""
    return state.cfg.get("terminal") or {}


def _is_enabled(tcfg: Dict[str, Any]) -> bool:
    """
    功能是否启用。

    默认 True：配置里明确写了 false 才关闭。与 config.py 的
    DEFAULT_CONFIG 保持一致（项目负责人明确选择「默认开启」）。
    """
    return bool(tcfg.get("enabled", True))


def _resolve_start_dir(resolver, tcfg: Dict[str, Any], base_dir: str = "") -> str:
    """
    决定 shell 的启动目录。

    优先用配置里的 terminal.start_dir；它为空或不存在时，退到**该用户**第一个
    可访问的根目录（也就是他「此电脑」里的第一个位置），这样新开的命令行就落在
    他熟悉的地方，而不是服务的安装目录。

    这里接收 resolver 而不是 state：多用户下解析器是**按用户**的，
    而本函数没有 request 可用来取当前用户（调用方负责传进来）。
    base_dir 同理由调用方传入，只作最后的兜底 —— 写死 state 会直接 NameError，
    而这条分支恰恰会被「还没有分配任何目录的新学生」走到。
    """
    configured = str(tcfg.get("start_dir") or "").strip()
    if configured:
        try:
            expanded = os.path.normpath(os.path.expandvars(os.path.expanduser(configured)))
            if os.path.isdir(expanded):
                return expanded
        except Exception:  # noqa: BLE001
            pass

    try:
        first = resolver.first()
        if first and os.path.isdir(first["path"]):
            return first["path"]
    except Exception:  # noqa: BLE001
        pass

    return base_dir or os.getcwd()


def _audit(message: str) -> None:
    """
    往服务端控制台打一行带时间戳的审计日志。

    flush=True 是必须的：以 Windows 服务（NSSM）方式运行时 stdout 被重定向，
    此时 Python 用的是块缓冲，不主动 flush 的话审计日志会一直卡在缓冲区里，
    服务异常退出时直接丢失——那样审计就等于没做。
    """
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    print("[审计][命令行] %s %s" % (stamp, message), flush=True)


# ---------------------------------------------------------------------------
# 创建会话
# ---------------------------------------------------------------------------

@router.post("/session")
async def create_session(request: Request, payload: Optional[SessionPayload] = None) -> Dict[str, Any]:
    """
    创建一个命令行会话。

    返回 sid，前端拿它去连 WebSocket。sid 本身**不是**认证凭据，
    WS 握手时仍然要带上登录 Cookie（并由中间件校验）。
    """
    state = get_state(request)
    tcfg = _terminal_cfg(state)

    if not _is_enabled(tcfg):
        # 明确告诉调用方「是配置关掉了」，而不是含糊的 404
        raise HTTPException(
            status_code=403,
            detail="命令行功能已在服务端关闭（config.json 的 terminal.enabled = false）",
        )

    # ★ 与 /api/system/info 下发的 features.terminal 用同一个判断：
    #   管理员在用户管理里对某个人关掉命令行之后，接口也必须真的关掉，
    #   否则「关掉」只是让他看不见入口（见 deps.feature_allowed 的说明）。
    require_feature(request, "terminal", "命令提示符")

    shell = str(tcfg.get("shell") or "cmd.exe")
    resolver = resolver_of(request)
    start_dir = _resolve_start_dir(resolver, tcfg, state.base_dir)
    idle_timeout = int(tcfg.get("idle_timeout_seconds") or 0)

    # ---- 「在此处打开命令行」/「运行脚本」----------------------------------
    # ★ 两者都必须过**当前用户的**解析器：路径闸门只有一道。从终端这条路
    #   进来的 cwd / 可执行文件同样能读全机文件（终端本来就是全权限 shell），
    #   但接口层面仍要按同一个口径收敛，免得出现「绕过解析器」的第二条路。
    run_file = ""
    if payload is not None and getattr(payload, "run", None) is not None:
        try:
            _root, abs_run = resolver.resolve(payload.run.root, payload.run.path)
        except Exception as exc:                                # noqa: BLE001
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not os.path.isfile(abs_run):
            raise HTTPException(status_code=404,
                                detail="要运行的文件不存在：%s" % payload.run.path)
        ext = os.path.splitext(abs_run)[1].lower()
        if ext not in RUNNABLE_EXTS:
            raise HTTPException(
                status_code=400,
                detail="只能直接运行脚本（%s），当前是 %s。"
                       "要运行别的程序请在命令行里输入。"
                       % ("/".join(sorted(RUNNABLE_EXTS)), ext or "无扩展名"))
        run_file = abs_run
        # ★ cwd 取脚本所在目录：批处理里几乎都用相对路径引用同目录的文件
        #   （`java -jar server.jar`），cwd 不对就直接失败。
        start_dir = os.path.dirname(abs_run) or start_dir
    elif payload is not None and getattr(payload, "start_dir", None) is not None:
        try:
            _root, abs_dir = resolver.resolve(payload.start_dir.root,
                                              payload.start_dir.path)
        except Exception as exc:                                # noqa: BLE001
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        if not os.path.isdir(abs_dir):
            raise HTTPException(status_code=404,
                                detail="目录不存在：%s" % payload.start_dir.path)
        start_dir = abs_dir

    # ★ 命令行名额现在是**每用户**的（用户决定：每人 5 个窗口）。
    # 涉及两个数，取小的那个：
    #   cfg_policy —— 全站政策上限（terminal.max_sessions），对所有人生效；
    #   user_quota —— 这个人的额度（用户表的 max_terminal_sessions，默认 5）。
    # 取小是为了两头都不失控：配置收紧能立刻约束所有人（改造前 max_sessions=1
    # 就是靠这条生效的），管理员也能单独给某人调低。想给某人开得更多，
    # 把配置一起调大即可 —— 两个数字里任何一个都能单独卡住人，
    # 比「静默忽略其中一个」好排查得多。
    user = get_user(request)
    cfg_policy = int(tcfg.get("max_sessions") or 5)
    user_quota = int(user.get("max_terminal_sessions") or cfg_policy)
    max_sessions = max(1, min(cfg_policy, user_quota))
    # 全机总量兜底（0 = 不设限）：防「每人 5 个 × 很多人」把机器拖垮。
    # 它只在真的失控时才触发，淘汰范围不限定归属（否则谁都腾不出名额）。
    max_total = int(tcfg.get("max_sessions_total") or 0)
    # 会话归属：管理界面按它统计「某人几个窗口」，名额与淘汰也都按它分。
    owner = owner_of(request)

    # 会话自己保留多少输出。★ 会话可分离之后这个值的作用从「浏览器侧裁剪」
    # 变成了「服务端保留多少积压输出」：它决定了用户关掉标签页一段时间后
    # 再回来能看到多久以前的输出。
    max_output_kb = int(tcfg.get("max_output_kb") or 512)
    # 分离会话可以被新会话挤掉前需要闲置多久（秒）
    evict_grace = float(tcfg.get("evict_grace_seconds") or 20)

    # 终端尺寸（前端量好带过来；没带就给一个常见的默认值）
    cols = int(getattr(payload, "cols", 0) or 120) if payload is not None else 120
    rows = int(getattr(payload, "rows", 0) or 30) if payload is not None else 30

    # 会话与登录令牌绑定：这里取出当前请求的会话令牌（中间件已校验过）
    token = request.cookies.get(SESSION_COOKIE) or ""

    ip = client_ip(request)

    try:
        session = await manager.create(
            shell=shell,
            start_dir=start_dir,
            idle_timeout=idle_timeout,
            max_sessions=max_sessions,
            session_token=token,
            cols=cols,
            rows=rows,
            max_output_kb=max_output_kb,
            evict_grace=evict_grace,
            owner=owner,
            run=run_file,
            max_total=max_total,
        )
    except TerminalLimitError as exc:
        raise HTTPException(status_code=429, detail=str(exc))
    except TerminalSpawnError as exc:
        # 启动失败也记审计：说明有人尝试开 shell 但没开起来
        _audit("创建失败 ip=%s shell=%s 原因=%s" % (ip, shell, exc))
        raise HTTPException(status_code=500, detail=str(exc))
    except TerminalDisabledError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except TerminalError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        _audit("创建异常 ip=%s shell=%s 原因=%s" % (ip, shell, exc))
        raise HTTPException(status_code=500, detail="创建命令行会话失败：%s" % exc)

    # ★ 审计：每一次成功创建命令行会话都留痕（谁、从哪里、什么 shell、起始目录）
    # 多用户之后「谁」是这里最要紧的一列：出问题时得能说清是哪个账号开的 shell。
    info = session.describe()
    _audit(
        "创建会话 用户=%s ip=%s sid=%s shell=%s 后端=%s cwd=%s 尺寸=%sx%s 代码页=%s 编码=%s 超时=%s秒 名额=%s"
        % (
            owner or "(未知)",
            ip,
            info["sid"],
            info["shell"],
            info["backend"],
            info["cwd"],
            info["cols"],
            info["rows"],
            info["code_page"] if info["code_page"] is not None else "未探测到",
            info["codec"],
            info["idle_timeout"],
            max_sessions,
        )
    )

    return {
        "ok": True,
        "id": session.sid,
        "shell": session.shell,
        "cwd": session.cwd,
        # "conpty" = 真 TTY（裸 python 等交互式程序可用）；"pipe" = 回退模式。
        # 前端据此提示「管道模式下交互式程序不可用」。
        "backend": session.backend,
        "cols": session.cols,
        "rows": session.rows,
        # 空闲多久自动断开（秒），0 表示不限制；前端可据此提示用户
        "expires_in": idle_timeout,
        "code_page": session.code_page,
    }


# ---------------------------------------------------------------------------
# WebSocket：终端输入输出
# ---------------------------------------------------------------------------

async def _reject(websocket: WebSocket, reason: str) -> None:
    """
    在握手阶段拒绝连接。

    注意：这里必须在 accept() **之前**发送 close，uvicorn 才会把它
    翻译成 HTTP 403（握手失败），而不是先建好连接再关掉。
    1008 = Policy Violation。
    """
    try:
        await websocket.close(code=1008, reason=reason)
    except Exception:  # noqa: BLE001
        pass


def _query_sid(websocket: WebSocket) -> str:
    """
    从查询参数里取会话 id。

    主名字是 sid；session / s 是兼容别名（前端换写法时不至于连不上）。
    没有就用空串，走后面的「等 attach 消息」流程。
    """
    for key in _SID_QUERY_KEYS:
        value = websocket.query_params.get(key)
        if value:
            return value
    return ""


async def _send_close_notice(
    websocket: WebSocket, message: Dict[str, Any], code: int
) -> None:
    """
    先发一条说明消息、再关连接。

    顺序很重要：如果直接 websocket.close()，前端只能看到「连接断了」，
    分不清是「会话被回收了」还是「网络抖了一下」，会误导用户以为要重连。
    先把原因发过去，前端就能显示「会话已结束」这类准确提示。
    """
    try:
        await websocket.send_json(message)
    except Exception:  # noqa: BLE001 - 客户端可能已经彻底走了
        pass
    try:
        await websocket.close(code=code)
    except Exception:  # noqa: BLE001
        pass


@router.websocket("/ws")
async def terminal_ws(websocket: WebSocket) -> None:
    """
    终端数据通道。

    ★ 断开 = 分离，不是结束：见模块头部「会话可分离」一节。

    客户端 -> 服务端：
        {"type":"input","data":"dir\\r\\n"}   用户敲下的一整行
        {"type":"resize","cols":N,"rows":M}   窗口尺寸变化（ConPTY 下真实生效）
        {"type":"interrupt","force":bool}     中断（true = 强杀卡住的子进程）
        {"type":"attach","sid":"..."}         指定要接管的会话（也可用 ?sid= 传）
        {"type":"close"}                      主动结束会话（会真的杀掉进程）
    服务端 -> 客户端：
        {"type":"attached",...}               已接上会话（含 replay / 尺寸信息）
        {"type":"output","data":"..."}        终端输出（含重连时补发的积压内容）
        {"type":"exit","code":0}              进程结束
        {"type":"error","message":"..."}      错误提示
        {"type":"interrupted",...}            中断结果
        {"type":"closed","reason":...,"message":...}   会话已不存在
        {"type":"replaced","message":...}     本连接被新连接接管
        {"type":"truncated","dropped_bytes":N} 积压输出超出上限被丢弃
        {"type":"ping","t":<秒>}              无输出时的保活帧
    """
    state = websocket.app.state.app_state
    tcfg = _terminal_cfg(state)

    if not _is_enabled(tcfg):
        await _reject(websocket, "命令行功能已在服务端关闭")
        return

    ip = websocket.client.host if websocket.client else "unknown"
    sid_from_query = _query_sid(websocket)

    # ★ 权限被管理员单独收掉时，连**重连已有的会话**也要拒。
    #   只在「创建会话」那一步挡是不够的：收权限之前开着的窗口，
    #   刷新页面就能靠 sid 重新接上（会话本身还活着），
    #   于是「关掉权限」对已经在跑的 shell 完全无效。
    if not feature_allowed_for(getattr(websocket.state, "user", None), "terminal"):
        _audit("拒绝连接 sid=%s 原因=管理员已关闭该账号的命令行权限" % sid_from_query)
        await _reject(websocket, "管理员已对你的账号关闭「命令提示符」功能")
        return


    session = manager.get(sid_from_query) if sid_from_query else None
    first_message: Optional[Dict[str, Any]] = None

    # ---- 建连时没带 sid：允许客户端用第一条 attach 消息补上 ----
    #
    # ★ 这里必须**先 accept() 再读消息**。早先的写法是在 accept() 之前
    # receive_text()，结果是死锁：客户端在握手完成前根本发不出任何帧，
    # 服务端却一直在等它说话，最后超时被拒绝（表现为 HTTP 403）。
    # 代价是一旦走到这条路径就无法再「在握手阶段拒绝」，
    # 所以拒绝时改为 accept() 之后先发一条 closed 说明原因、再关连接。
    if session is None and not sid_from_query:
        await websocket.accept()

        try:
            raw_first = await asyncio.wait_for(websocket.receive_text(), timeout=10)
            parsed = json.loads(raw_first)
        except Exception:  # noqa: BLE001 - 超时/非 JSON 都按「没说话」处理
            parsed = None

        if isinstance(parsed, dict) and str(parsed.get("type") or "") == "attach":
            first_message = parsed
            sid_from_query = str(parsed.get("sid") or parsed.get("session") or "")
            session = manager.get(sid_from_query) if sid_from_query else None

        if session is None:
            # 既没带 sid、第一条又不是有效的 attach：没法知道要接哪个会话
            await _send_close_notice(
                websocket,
                {"type": "closed", "reason": "nomatch",
                 "message": "缺少会话标识（请用 ?sid= 或在首条消息里发 attach）"},
                _WS_CLOSE_GONE,
            )
            return
    else:
        # ---- 1) 会话必须存在 ----
        if session is None:
            # 故意不区分「不存在」和「已过期」，避免被用来探测有效 sid。
            # 但要把原因说清楚：这是「重新打开时发现会话已经没了」的唯一入口，
            # 用户需要知道该重新开一个命令行窗口。
            await _reject(websocket, "会话不存在或已结束（可能已空闲超时被回收），请重新打开命令提示符")
            return

        # ---- 1b) 会话必须还没结束 ----
        # ★ 已结束的会话（空闲超时被回收 / 用户敲了 exit）要在**握手阶段**就拒掉。
        # 不这样做的话握手会成功、随后才收到结束消息，前端会经历一次
        # 「连上了又断开」的困惑状态，而用户看到的提示也含糊不清。
        # 这里直接给出「会话已结束」，前端即可显示明确文案。
        if session.is_dead():
            _audit("拒绝连接 sid=%s 原因=会话已结束 client=%s" % (sid_from_query, ip))
            await _reject(
                websocket,
                "会话已结束（可能已空闲超时被回收），请重新打开命令提示符",
            )
            return

        # ---- 2) 会话必须属于当前登录身份 ----
        # 中间件已经把当前 Cookie 里的会话令牌放进了 scope["state"]；
        # 这里要求它与创建会话时记录的令牌摘要一致，
        # 因此 sid 泄露也无法被别的登录者（或未登录者）接管。
        token = getattr(websocket.state, "session_token", "") or ""
        if not session.matches_token(token):
            _audit(
                "拒绝连接 sid=%s 原因=会话与当前登录身份不匹配 client=%s"
                % (sid_from_query, ip)
            )
            await _reject(websocket, "无权访问该终端会话")
            return

        await websocket.accept()

    # ---- 归属校验（attach 路径在 accept 之后补做） ----
    if first_message is not None:
        token = getattr(websocket.state, "session_token", "") or ""
        if not session.matches_token(token):
            _audit(
                "拒绝连接 sid=%s 原因=会话与当前登录身份不匹配 client=%s"
                % (sid_from_query, ip)
            )
            await _send_close_notice(
                websocket,
                {"type": "closed", "reason": "forbidden", "message": "无权访问该终端会话"},
                _WS_CLOSE_GONE,
            )
            return

        # 这条路径已经在 accept 之后，没法再用握手 403 拒绝，
        # 于是发一条 closed 把「会话已结束」讲清楚（对齐查询参数那条路径的行为）。
        if session.is_dead():
            await _send_close_notice(
                websocket,
                {"type": "closed", "reason": "ended",
                 "message": "会话已结束（可能已空闲超时被回收），请重新打开命令提示符"},
                _WS_CLOSE_GONE,
            )
            return

    # ★ 这次连接之前会话是否已经有客户端连着（用于审计日志）
    was_attached = session.has_clients()
    # ★ 这次连接是不是「用户回到刚才那个会话」
    #
    # 判断依据是「会话之前有没有被连过」而不是「此刻有没有人连着」：
    # 客户端先断开、稍后再重连时，重连那一刻没有客户端连着，
    # 但这对用户就是一次回归，必须让前端知道（好提示「已恢复到之前的会话」）。
    is_replay = session.has_ever_been_attached()
    dropped_before = session.buffered_bytes()

    # ★ 接管：登记本连接并把（可能有的话）上一个连接顶掉
    cursor_token = session.attach()

    sid = session.sid
    _audit(
        "连接 ip=%s sid=%s 重连=%s 积压=%dKB 后端=%s"
        % (ip, sid, "是" if was_attached else "否",
           dropped_before // 1024, session.backend)
    )

    # 服务端已经知道会话被回收 / 输出已结束 / 本连接被顶掉时，
    # 靠这个标志避免在收尾阶段又把会话关一次或再发一遍结束消息。
    state_flags = {"terminated": False}

    async def _notify_gone(reason: str, message: str, code: int) -> None:
        """
        给客户端一条明确的结束通知（幂等，重复调用只有第一次生效）。

        会额外补一条旧的 {"type":"exit"} 消息：前端现有代码以及既有测试都是
        按它来判断「进程结束了」的，保留它才不会把已经能用的东西改坏；
        新代码用 closed 里的 reason 能拿到更准确的原因。
        """
        if state_flags["terminated"]:
            return
        state_flags["terminated"] = True

        if reason == "ended":
            try:
                await websocket.send_json({
                    "type": "exit",
                    "code": int(session.exit_code or 0),
                })
            except Exception:  # noqa: BLE001 - 客户端可能已经彻底走了
                pass

        await _send_close_notice(
            websocket, {"type": "closed", "reason": reason, "message": message}, code
        )

    # ---- 输出方向：会话缓冲 -> 浏览器 ----
    async def pump_output() -> None:
        """
        把会话输出推给浏览器，直到会话结束 / 本连接被接管。

        ★ 第一件事是把积压内容冲出去：session.attach() 返回的游标指向
        「缓冲现存的最旧一块」，所以这里天然会先补发客户端离开期间产生的输出，
        然后才继续收实时输出——这正是「重连后能看到刚才发生了什么」的实现。
        """
        # 积压可能超过上限而被裁掉过：先如实告诉客户端，别让它以为输出是连续的
        if session.take_truncated():
            await websocket.send_json({
                "type": "truncated",
                "dropped_bytes": max(0, dropped_before),
            })

        while True:
            try:
                chunk = await session.next_chunk(cursor_token, wait=_WS_KEEPALIVE_SECONDS)
            except SessionReplaced:
                # 同一个会话被新的连接（刷新/新标签页）接管了。不是错误，
                # 说明清楚即可：旧页面不该再抢着显示输出。
                await _send_close_notice(
                    websocket,
                    {"type": "replaced", "message": "该终端会话已被新的连接接管"},
                    _WS_CLOSE_REPLACED,
                )
                return
            except SessionGone as exc:
                await _notify_gone("ended", str(exc) or "会话已结束", _WS_CLOSE_GONE)
                return
            except Exception as exc:  # noqa: BLE001 - 连接已断等
                _audit("输出转发异常 sid=%s 原因=%s" % (sid, exc))
                return

            if chunk:
                await websocket.send_json({"type": "output", "data": chunk})
                # 服务端保留的输出越过上限时明确告诉用户，
                # 避免「怎么少了半屏」的困惑
                if session.take_truncated():
                    await websocket.send_json({
                        "type": "truncated",
                        "dropped_bytes": session.buffered_bytes(),
                    })
                continue

            # 空串 = 这一轮没等到新输出。发一个 ping 让前端能察觉连接僵死。
            await websocket.send_json({"type": "ping", "t": int(time.time())})

    # ---- 输入方向：浏览器 -> 会话 ----
    async def pump_input() -> None:
        """把浏览器发来的按键/整行文本写进会话；收到 close 就请求结束会话。"""
        while True:
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return

            if len(raw) > _MAX_MESSAGE_CHARS:
                await websocket.send_json({"type": "error", "message": "单条输入过长，已忽略"})
                continue

            try:
                message = json.loads(raw)
            except Exception:  # noqa: BLE001
                await websocket.send_json({"type": "error", "message": "消息格式错误（需要 JSON）"})
                continue

            if not isinstance(message, dict):
                await websocket.send_json({"type": "error", "message": "消息格式错误（需要 JSON 对象）"})
                continue

            kind = str(message.get("type") or "")

            if kind == "input":
                data = message.get("data")
                if not isinstance(data, str):
                    continue
                if not await session.write(data):
                    # 进程已经结束，通知客户端后结束输入循环
                    await _notify_gone("ended", "命令行进程已结束", _WS_CLOSE_GONE)
                    return
            elif kind == "resize":
                # ConPTY 下这是**真实生效**的：改动内核的终端尺寸，
                # 让远端换行位置与前端 xterm.js 的列宽保持一致。
                session.resize(message.get("cols") or 0, message.get("rows") or 0)
            elif kind == "interrupt":
                # 中断。force=False：真 TTY 走原生 Ctrl+C；
                # force=True 或管道模式：杀掉卡住的子进程树但保留 shell。
                result = await session.interrupt(force=bool(message.get("force")))
                await websocket.send_json({
                    "type": "interrupted",
                    "mode": result.get("mode"),
                    "killed": result.get("killed", []),
                })
            elif kind == "attach":
                # 连接建立之后又发 attach：只接受指向当前会话（重复一次没有副作用），
                # 指向别的会话要明确拒绝 —— 不支持在一个连接里中途换会话，
                # 那样两个会话的输出会混在同一条流里。
                wanted = str(message.get("sid") or message.get("session") or "")
                if wanted and wanted != sid:
                    await websocket.send_json({
                        "type": "error",
                        "message": "当前连接已绑定会话 %s，不能中途切换到别的会话" % sid,
                    })
            elif kind == "close":
                # ★ 与「断开连接」不同：这是客户端**明确要求结束**会话，
                # 所以这里要真的把进程杀掉（用户点了关闭窗口）。
                state_flags["terminated"] = True
                await manager.close_session(sid)
                _audit("客户端请求关闭 sid=%s ip=%s" % (sid, ip))
                return
            else:
                await websocket.send_json({
                    "type": "error",
                    "message": "未知的消息类型：%s" % kind,
                })

    # ★★ 从这里开始必须整体包在 try/finally 里。
    #
    # session.attach() 已经在上面执行过，而**注销它（detach）的唯一地方是下面
    # 的 finally**。所以 attach 之后、try 之前的任何一步只要抛异常，都会让
    # detach 永远不执行，后果不是「这次连接没清理干净」，而是：
    #
    #   客户端条目永久留在会话里 → has_clients() 恒为真 →
    #   idle_seconds() 恒为 0 → 看门狗与巡检都不会回收它 →
    #   那个 cmd.exe 和它的 max_sessions 名额永久泄漏。
    #   用户最终只会看到「命令行会话数已达上限」，且找不到原因。
    #
    # 而「握手刚完成连接就断」恰恰会在这里抛：下面第一条 send 就是往一个
    # 已经消失的客户端写数据（刷新页面 / 关标签页 / 连接被重置时都会发生）。
    # 这不是理论风险 —— 修复前用假 WebSocket 把第一次 send 打回异常，
    # 可以稳定复现 has_clients() 仍为真的泄漏（见
    # tests/test_terminal_reattach.py 的 AttachIsAlwaysPairedWithDetachTests）。
    output_task: Optional[asyncio.Task] = None
    input_task: Optional[asyncio.Task] = None

    try:
        # 先告诉客户端「连上的是哪个会话、这次是不是重连」，前端据此决定提示文案
        await websocket.send_json({
            "type": "attached",
            "id": sid,
            "shell": session.shell,
            "backend": session.backend,
            "cols": session.cols,
            "rows": session.rows,
            "replay": bool(is_replay),
            "buffered_kb": round(dropped_before / 1024.0, 1),
            "expires_in": int(tcfg.get("idle_timeout_seconds") or 0),
        })

        # 第一条消息如果是 attach（建连时没带 sid 那条），已经消费掉了，
        # 不能再丢进输入循环里等 —— 那样会白等一轮。
        if first_message is not None:
            _audit("通过首条 attach 消息接入 sid=%s ip=%s" % (sid, ip))

        output_task = asyncio.create_task(pump_output())
        input_task = asyncio.create_task(pump_input())

        # 任一方向结束（会话结束 / 客户端断开 / 被接管 / 客户端要求关闭）就整体收尾
        await asyncio.wait({output_task, input_task}, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        # 连接在握手之后立刻断掉：这里必须**吞掉**异常继续走 finally。
        # 抛出去的话 finally 就不会执行，客户端条目会永久留在会话里
        # （见上面那段说明）。这是一种正常的客户端行为，不是服务端故障。
        _audit(
            "连接建立后立即断开 sid=%s ip=%s 原因=%s: %s"
            % (sid, ip, type(exc).__name__, exc)
        )
    finally:
        # ★ 先注销客户端，再取消任务。
        # 顺序反过来的话，输出任务可能在「已经 detach 之后」才发现自己该退出，
        # 而 detach 之后的会话已经被排除在「有客户端」之外，
        # 空闲超时的时钟会立刻开始走 —— 本来还连着的会话会被提前计时。
        try:
            session.detach(cursor_token)
        except Exception:  # noqa: BLE001
            pass

        for task in (output_task, input_task):
            if task is None:
                # 还没建起来就失败了（上面的 send 抛异常）：没有任务要收
                continue
            if task.done():
                # 取回异常，否则「客户端突然断开导致 send 失败」会在日志里
                # 留下 "Task exception was never retrieved" 的噪音
                try:
                    task.exception()
                except BaseException:  # noqa: BLE001 - 已取消的任务会抛 CancelledError
                    pass
            else:
                task.cancel()

        # ★ 这里**不再** close_session()。
        #
        # 原先断开连接就杀进程树，结果浏览器一刷新窗口里的东西全没了。
        # 现在改成「只分离」：进程继续跑，输出继续进会话缓冲，
        # 用户重连时先收到积压内容。真正的回收交给空闲超时
        # （会话看门狗 + manager.reap_idle 双保险），
        # 服务退出时则由 app.py 的 lifespan 调用 close_all() 兜底，
        # 因此不会留下孤儿 cmd.exe。
        #
        # 唯一的例外是「会话已经跑完了」：进程都退出了，留着它只会白占
        # max_sessions 名额，所以这时主动把它从注册表里摘掉。
        if not state_flags["terminated"] and not session.is_alive():
            try:
                await manager.close_session(sid)
            except Exception:  # noqa: BLE001
                pass

        # 会话已结束时上面发过 closed 并关过连接，这里再关一次是幂等的
        try:
            await websocket.close()
        except Exception:  # noqa: BLE001
            pass

        _audit(
            "断开（分离）ip=%s sid=%s 存活=%s 缓冲=%dKB"
            % (ip, sid, "是" if session.is_alive() else "否",
               session.buffered_bytes() // 1024)
        )

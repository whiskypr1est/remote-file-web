# -*- coding: utf-8 -*-
"""
命令行会话管理
==============

为虚拟桌面里的「命令提示符」窗口提供后端支撑：启动一个**常驻**的 shell
子进程，把它的标准输出通过内存队列交给 WebSocket，把 WebSocket 收到的按键
写回它的标准输入。

为什么是「常驻进程 + 管道」而不是「每条命令起一个进程」：
    * 每条命令起一个进程的话，`cd` 这类状态无法保留，
      用户 `cd /d C:\\Windows` 后下一条 `dir` 又会回到原目录；
    * 常驻进程只需要付一次启动开销，交互手感接近真实 CMD。

两种后端：ConPTY（默认）与管道（回退）
=====================================
    本模块有两条通路，启动会话时自动选择：

    1) **ConPTY（真 TTY，首选）** —— 用 pywinpty 给 shell 分配一个伪控制台。
       这是「裸 `python` 卡死」的正解：管道模式下 python 检测到 stdin 不是
       终端，会改成「把 stdin 当脚本读」，于是后续每一行都被它吞掉、
       提示符再也不回来（实测确认）。有了真 TTY 之后：
         * 裸 `python` 正常进入交互式 REPL（出现 `>>>`）；
         * Tab 补全、方向键历史、Ctrl+C 真正中断程序都可用；
         * 窗口尺寸可变（setwinsize），全屏程序也能跑；
         * 输出是**带 ANSI 转义序列**的流，前端必须用终端模拟器渲染
           （本项目已内置 xterm.js），不能再当纯文本塞进 div。

    2) **管道（回退）** —— pywinpty 缺失或启动失败时使用。没有 TTY，
       上述交互能力都不具备，前端会显示「管道模式」提示。
       保留它只是为了「少装一个依赖也能跑」，不是推荐用法。

编码处理（关键，中文路径能用的前提）：
    Windows 控制台默认不是 UTF-8。若按 UTF-8 解码 GBK 输出，
    中文会变成乱码。因此在会话启动时先跑一次 `chcp` 探测当前控制台代码页，
    再据此选择解码器，输入也用同一个编解码器编码。
    探测结果只取一次；探测失败时按 gbk -> utf-8 的顺序退回。

    输出还要用**增量解码器**（codecs.getincrementaldecoder）逐块解码：
    一次 read() 拿到的字节可能正好把一个汉字切成两半，直接按块 decode
    会在分片处产生替换字符（乱码）。增量解码器会把不完整的尾巴留到下一次，
    拼起来才是正确的文本。

进程树清理：
    cmd.exe 可能派生子进程（例如 `start notepad`）。只 kill cmd.exe 会留下孤儿
    进程，本项目在别处已经踩过这个坑，所以这里统一用
    `taskkill /F /T /PID` 杀整棵进程树，并额外用 proc.kill() 兜底。

线程/事件循环安全：
    本模块的所有异步对象（子进程、队列、锁）都必须在**运行中的事件循环**里
    创建与使用，不能跨线程或跨循环共享。进程级共享的会话注册表放在模块级，
    并配一把 asyncio.Lock 串行化「创建/查找/清理」，避免并发创建时
    出现超出 max_sessions 的竞态。
"""

from __future__ import annotations

import asyncio
import codecs
import hmac
import os
import re
import secrets
import subprocess
import threading
import time
from collections import deque
from hashlib import sha256
from typing import Any, Awaitable, Callable, Deque, Dict, List, Optional

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 创建子进程时不弹出黑色控制台窗口（Windows 专用；非 Windows 上是 0）
_CREATE_NO_WINDOW: int = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))

# 内部输出队列最多缓存多少块（块大小见 _READ_CHUNK）。
# 队列有界是为了防止「命令疯狂刷屏 + 浏览器来不及消费」导致内存无上限增长；
# 满了以后丢弃**最旧**的块并置截断标志，让用户仍能看到最新的输出。
# 注意：现在这块逻辑由 TerminalSession._remember() 实现，
# 按**字节数**（terminal.max_output_kb）封顶而不是按块数——
# 按块数封顶的话，256 块可能是 256 字节也可能是 16MB，上限根本不可控。
_DEFAULT_MAX_OUTPUT_KB = 512

# 单次从子进程读取的字节数
_READ_CHUNK = 65536

# 探测代码页时等待子进程输出的上限（秒），超时就退回默认编码
_PROBE_TIMEOUT = 2.0

# 控制台代码页 -> Python 编解码器
_CODEPAGE_CODECS: Dict[int, str] = {
    437: "cp437",
    850: "cp850",
    932: "shift_jis",
    936: "gbk",
    949: "cp949",
    950: "big5",
    1252: "cp1252",
    54936: "gb18030",
    65001: "utf-8",
}

# 探测失败时的退回顺序：先 gbk（简体中文 Windows 的常见默认值），再 utf-8
_FALLBACK_CODECS = ("gbk", "utf-8")

# 从 chcp 输出里抓代码页数字。用字节正则而不是先解码，
# 因为此时还不知道该用哪个编码，但数字是 ASCII，任何编码下字节都一样。
_CODE_PAGE_RE = re.compile(rb"(\d{3,5})")

# session id 长度（token_urlsafe(24) 约 32 个字符，足够抗枚举）
_SID_BYTES = 24

# 分离会话最少闲置多久才允许被「顶掉」以腾出 max_sessions 名额（秒）。
#
# 这个宽限期只影响「名额已满时要不要顶掉一个旧会话」，**不影响重连**：
# 会话能否重连只取决于它有没有被空闲超时回收（terminal.idle_timeout_seconds），
# 所以即使超过这个宽限期，用户正常重连也照样能连回来。
#
# 它要解决的问题是：分离会话会一直占着 max_sessions 名额，
# 用户关掉几个标签页之后如果不加处理就得等满 idle_timeout 才能再开命令行。
#   * 太短（比如 0）：刚分离的会话立刻可被顶掉，用户刷新页面那一瞬间
#     若有别的窗口在开新会话，旧会话就可能被误杀；
#   * 太长：被彻底遗弃的窗口长期占用名额。
# 20 秒足以覆盖「刷新页面 / 误关标签页马上重开」这类真正的重连场景
# （那种操作通常几秒内就完成），又不会让遗弃的窗口长期堵住名额。
_EVICT_GRACE_SECONDS = 20.0

# ConPTY 单次读取的单位是「字符」而不是字节
_PTY_READ_CHUNK = 65536

# 客户端游标的两个特殊取值。
#
# 用哨兵而不是 None 是因为「缺席」和「失效」是两种含义：
#   * _MISSING  —— 这个 token 根本不在表里（会话已被回收，或客户端已注销）
#   * _REPLACED —— token 还在表里，但已被新的连接顶掉
# 路由层要据此给用户不同的提示（「会话已结束」vs「已被新的连接接管」），
# 混成一个 None 就分不出来了。
_REPLACED: int = -1
_MISSING: Any = object()

# ConPTY 是否可用。装了 pywinpty 就用真 TTY；没装只是能力降级，
# 不是错误，所以这里吞掉 ImportError 并回退到管道模式。
try:
    import winpty as _winpty
    _PTY_AVAILABLE = True
except Exception:  # noqa: BLE001
    _winpty = None
    _PTY_AVAILABLE = False


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------

class TerminalError(Exception):
    """终端相关错误的基类。路由层应转换为对应的 HTTP 状态码。"""


class TerminalDisabledError(TerminalError):
    """配置里关闭了命令提示符功能。"""


class TerminalLimitError(TerminalError):
    """并发会话数已达上限。"""


class TerminalSpawnError(TerminalError):
    """shell 进程启动失败（通常是路径不对或没有权限）。"""


class SessionGone(Exception):
    """
    会话已经被回收（空闲超时 / 服务关闭）或命令行进程已经结束。

    不是 TerminalError 的子类：这个异常只在「WS 已经连上之后」才有意义，
    路由层用它给客户端补一句明确的中文提示（「会话已结束」），
    让前端能显示提示而不是干等着什么都不发生。
    """


class SessionReplaced(Exception):
    """同一个终端会话被新的 WebSocket 连接接管，旧连接应当退出。"""


# ---------------------------------------------------------------------------
# 编解码器选择
# ---------------------------------------------------------------------------

def _codec_for_code_page(code_page: Optional[int]) -> str:
    """
    代码页 -> Python 编解码器名。

    没探测到、或探测到一个我们没登记过的代码页时，按
    gbk -> utf-8 的顺序退回（两者在 CPython 里必定存在，这里仍做一次
    lookup 校验，避免将来有人改坏常量表导致整个会话起不来）。
    """
    if code_page is not None:
        codec = _CODEPAGE_CODECS.get(code_page)
        if codec:
            return codec

    for codec in _FALLBACK_CODECS:
        try:
            codecs.lookup(codec)
            return codec
        except LookupError:
            continue
    return "utf-8"


def _parse_code_page(raw: bytes) -> Optional[int]:
    """
    从 `chcp` 的输出里解析代码页。

    输出形如：
        Active code page: 936
        活动代码页: 936
    取「最后一个」能对上我们登记表的数字：chcp 的响应通常以代码页数字结尾，
    后面还可能跟着下一行的提示符（提示符里若含数字会造成干扰），
    所以优先返回登记表里出现过的数字，避免误判。
    """
    if not raw:
        return None

    candidates: List[int] = []
    for match in _CODE_PAGE_RE.findall(raw):
        try:
            value = int(match)
        except ValueError:
            continue
        if 100 <= value <= 65535:
            candidates.append(value)

    if not candidates:
        return None

    # 优先选我们登记过的代码页（避免把提示符里的数字当成代码页）
    for value in candidates:
        if value in _CODEPAGE_CODECS:
            return value

    # 都没登记过时，取最后一个（chcp 的答案一般就在行尾）
    return candidates[-1]


# ---------------------------------------------------------------------------
# 命令行参数构造
# ---------------------------------------------------------------------------

def _build_argv(shell: str, run: str = "") -> List[str]:
    """
    根据配置里的 shell 生成启动参数。

    cmd.exe 用 `/Q /K <init>`：
        /Q     关闭命令回显（否则每条命令会被重复打印一遍）
        /K     执行后面的命令但**不退出**，这正是「常驻」的关键
        prompt 把我们自己设置成 `C:\\path>`，与真实 CMD 一致

    PowerShell 系（powershell.exe / pwsh.exe）不认识 /Q /K，
    走它自己的参数：-NoLogo 去掉版权横幅，-NoExit 保持常驻。
    注意：PowerShell 分支只是「合理尝试」，本次没有做完整验证
    （验证只覆盖了 cmd.exe），配置成 PowerShell 时请自行确认。

    其它 shell 一律走「裸启动」，不做任何假设。

    ``run``（可选）是要**在启动时立刻执行**的那个文件（绝对路径），
    用来实现「在虚拟桌面里双击 .bat 直接运行」。

    ★★ 路径的引号问题是个大坑，这里是实测出来的结论，别改回去 ★★

      正确做法：**把 `call` 与路径作为两个独立的 argv 元素**，我们自己
      **绝不给路径加引号**：

          [shell, "/Q", "/K", "call", <路径>]

      为什么不能自己加引号：调用方（pywinpty / subprocess）在拼命令行时，
      会给含空格的参数加引号，并把参数**内部**的引号转义成 `\\"`。
      于是我们写的 `"C:\\a b\\x.bat"` 到了 cmd 那里变成 `\\"C:\\a b\\x.bat\\"`，
      cmd 直接报「不是内部或外部命令」。实测：自己加引号的六种写法**全军覆没**，
      而把路径当独立参数交出去的写法**全部成功**（含空格、含中文都行）。

      为什么用 `call`：它是从命令行调用批处理的正规方式，脚本结束后控制权
      正常回到本 shell（对应 /K 保持常驻）。

      为什么这里**不带** `prompt $P$G`：实测 `cmd /K "prompt $P$G & call" <路径>`
      这种串接在路径含空格时会失败（参数边界与引号打架）。而 cmd 的默认提示符
      本来就是 `$P$G`（`C:\\path>`），所以脚本窗口不设它没有实际损失；
      普通命令行窗口那条路**不受影响**，仍然照旧设 prompt。
    """
    name = os.path.basename(shell or "").strip().lower()

    if name in ("cmd", "cmd.exe"):
        if run:
            return [shell, "/Q", "/K", "call", run]
        return [shell, "/Q", "/K", "prompt $P$G"]

    if name in ("powershell", "powershell.exe", "pwsh", "pwsh.exe"):
        # -NoLogo 去横幅；-NoProfile 避免加载用户配置拖慢启动；
        # -NoExit 保证进程不退出（对应 cmd 的 /K）
        argv = [shell, "-NoLogo", "-NoProfile", "-NoExit"]
        if run:
            # -File 接路径，同样**不自己加引号**（道理与 cmd 那条相同）；
            # -NoExit 对 -File 也生效，脚本跑完窗口仍留着。
            argv += ["-File", run]
        return argv

    return [shell, run] if run else [shell]


# ---------------------------------------------------------------------------
# 进程树操作
# ---------------------------------------------------------------------------

def _win_child_pids(parent_pid: int) -> List[int]:
    """
    列出某个进程的**直接**子进程（Windows）。

    用 CreateToolhelp32Snapshot 而不是 wmic / Get-CimInstance：
    前者是纯 API 调用，毫秒级、不依赖任何外部命令；
    wmic 在新版 Windows 上已被弃用，而每次起一个 PowerShell 要几百毫秒 ——
    对「点了中断按钮要立刻有反应」来说太慢。

    任何异常都返回空列表：拿不到子进程列表不该让中断操作整个失败。
    """
    if os.name != "nt" or not parent_pid:
        return []

    try:
        import ctypes
        from ctypes import wintypes

        TH32CS_SNAPPROCESS = 0x00000002
        INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if snapshot == INVALID_HANDLE_VALUE:
            return []

        children: List[int] = []
        try:
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while more:
                if entry.th32ParentProcessID == parent_pid:
                    children.append(int(entry.th32ProcessID))
                more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)
        return children
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# 单个会话
# ---------------------------------------------------------------------------

class TerminalSession:
    """
    一个常驻 shell 会话。

    生命周期：start() -> (write()/read() 反复) -> close()
    """

    def __init__(
        self,
        sid: str,
        shell: str,
        start_dir: str,
        idle_timeout: int,
        token_hash: str,
        max_output_kb: int = _DEFAULT_MAX_OUTPUT_KB,
        evict_grace: float = _EVICT_GRACE_SECONDS,
        owner: str = "",
        run: str = "",
    ):
        self.sid = sid
        self.shell = shell
        self.cwd = start_dir or ""
        # ★ 启动时要立刻执行的文件（绝对路径，通常是个 .bat）。
        #   空串 = 普通交互式命令行。实现「双击 .bat 直接运行」用的，
        #   见 _build_argv 的说明（必须串在同一个命令行里，不能另起进程）。
        self.run = str(run or "")
        # ★ 会话归属（多用户）：每用户的名额、淘汰范围、以及管理员看到的
        # 「某人几个会话」都靠它。空串 = 无归属（直接构造会话的单元测试走这条，
        # 于是它们全都算作同一个人，行为与改造前一致）。
        #
        # 只存**用户名**，不存角色/可见目录等会变的东西：那些要实时读用户表，
        # 存一份快照早晚会和真相对不上。
        self.owner = str(owner or "")
        self.idle_timeout = max(0, int(idle_timeout or 0))
        # 把「创建该会话的登录令牌哈希」绑在会话上：
        # 即使 sid 泄露，攻击者拿不到同一个 Cookie 也无法连上这个会话。
        self.token_hash = token_hash

        self.created_at = time.time()
        self._last_active = time.time()
        self._closed = False
        self._truncated = False
        self._idle_expired = False
        self.exit_code: Optional[int] = None
        self.code_page: Optional[int] = None

        self._codec = _FALLBACK_CODECS[0]
        # 增量解码器：在确定代码页后创建（见 _probe_code_page）。
        # 用它而不是每次 decode()，是为了正确处理「一个汉字被 read 边界切开」
        # 的情况——否则每块边界都会多出一个乱码字符。
        self._decoder: Optional[codecs.IncrementalDecoder] = None

        # 后端类型："conpty"（真 TTY）或 "pipe"（回退）
        self.backend = "pipe"
        # 终端尺寸（列 x 行）。ConPTY 下会同步给系统，管道模式下仅作记录。
        self.cols = 120
        self.rows = 30

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pty: Optional[Any] = None
        self._pty_thread: Optional[threading.Thread] = None

        # ★ 输出缓冲（替代原先的 asyncio.Queue）。
        #
        # 原先队列只在「有 WS 连着」时被消费，所以浏览器一断开就没人在读，
        # 缓冲区迅速写满、旧输出被丢掉——这对「重新连上要看到离开期间发生了什么」
        # 是致命的。现在改成一块**按字节数封顶**的滚动缓冲：
        # 会话自己持续解码并追加，谁连上来都从缓冲里读，
        # 因此没人连着的时候输出依然被保留（上限=max_output_kb）。
        self._chunks: "deque[str]" = deque()
        self._buffered_bytes = 0
        self._max_output_bytes = max(1, int(max_output_kb or _DEFAULT_MAX_OUTPUT_KB)) * 1024
        # 被新会话「顶掉」所需的最少闲置时间（见 _EVICT_GRACE_SECONDS 与
        # config 的 terminal.evict_grace_seconds）。挂在会话上而不是用全局常量，
        # 是为了让测试能把它调小、从而在秒级验证淘汰逻辑。
        self.evict_grace = max(0.0, float(evict_grace or 0.0))
        # 缓冲里最旧一块文本在「全量输出」中的偏移量，供计算「已丢弃多少」用
        self._buffer_base = 0
        # 每块文本的起始字节偏移（与 _chunks 一一对应），重连时据此告诉前端
        # 「你离开期间有 N 字节被挤掉了」，前端可以据此显示一条截断提示。
        self._chunk_offsets: "deque[int]" = deque()
        self._produced_bytes = 0

        # 唤醒信号：缓冲区变化或会话状态变化时 set()，
        # 正在等待新输出的客户端被唤醒后重新取数据（避免忙等轮询）。
        #
        # 配一个单调递增的 _emitted_seq：只有 Event 会有「丢唤醒」问题
        # （两个客户端在等时，一个清标志会把另一个刚被 set 的信号一起吞掉，
        # 结果另一个要白等到超时）。用序号就能在等待前先判断
        # 「我要等的那个数据是不是已经来了」，从而做到不丢唤醒也不空转。
        self._wakeup = asyncio.Event()
        self._emitted_seq = 0

        # 已连接的客户端：token -> 读取游标（字节偏移），或被顶掉时置 _REPLACED
        self._clients: Dict[int, Any] = {}
        self._next_client_id = 1
        # 每个客户端的「补发边界」：attach 那一刻已有的输出字节总量。
        # 起始偏移**严格小于**它的块属于补发（积压），之后的块是实时输出。
        # 只有补发的那部分需要剔除终端查询序列，见 _TERMINAL_QUERY_RE。
        self._replay_end: Dict[int, int] = {}
        # 这个会话**曾经**被客户端连过没有。
        #
        # 为什么不能用 has_clients() 代替：客户端先断开、再重连时，
        # 重连那一刻没有任何客户端连着，has_clients() 必然是 False，
        # 但这对用户来说明明是「回到了刚才那个会话」。
        # 前端要靠 replay 这个标志决定是否提示「已恢复到之前的会话」，
        # 所以这里必须记录历史而不是当前状态。
        self._ever_attached = False
        # ★ 会话「变成没人连」的时刻；None 表示当前有客户端连着。
        #
        # 空闲判定必须基于它，而不能基于 _last_active：
        # _last_active 同时被「读了输出」「写了输入」「连接/断开」刷新，
        # 于是刚断开的那一刻 _last_active 就是「现在」，看起来一点都不空闲。
        # 用 _last_active 判超时会变成「刚断开 → 判定为不空闲 → 再等一轮」，
        # 每轮都把时钟推后，分离会话就永远回收不掉。
        # 记录「从什么时候开始没人连」才是空闲的真正含义。
        self._detached_at: Optional[float] = time.time()

        # 空闲回收时调用的回调，由管理器注入（见 set_reap_callback）。
        self._reap_callback: Optional[Callable[[str], Awaitable[None]]] = None

        self._reader_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None

    def set_reap_callback(self, callback: Callable[[str], Awaitable[None]]) -> None:
        """
        由 TerminalManager 注入「回收本会话」的回调。

        回调和 self.close() 的区别：管理器会**先把自己从注册表摘掉**再关闭，
        这样超时回收之后注册表里不会留下「已死但仍占名额」的条目，
        而且下一次重连会被明确拒绝（而不是握手成功后再告诉用户会话结束了）。
        """
        self._reap_callback = callback

    # -- 启动 ---------------------------------------------------------------

    async def start(self) -> None:
        """
        启动 shell：优先 ConPTY（真 TTY），失败则回退管道模式。

        两个后端最终都把输出推进同一个队列，因此 read()/write() 以及路由层
        完全不需要关心背后是哪一种。
        """
        argv = _build_argv(self.shell, self.run)

        # cwd 不存在时不要直接失败，退回项目进程的当前目录更可用
        cwd = self.cwd if (self.cwd and os.path.isdir(self.cwd)) else None

        if _PTY_AVAILABLE and await self._start_conpty(argv, cwd):
            return

        await self._start_pipe(argv, cwd)

    async def _start_conpty(self, argv: List[str], cwd: Optional[str]) -> bool:
        """
        用 pywinpty 启动一个真 TTY。成功返回 True。

        这里刻意**不抛异常**：ConPTY 起不来只是能力降级，
        不该让「打开命令提示符」整个失败 —— 回退到管道模式仍然能用。
        """
        try:
            self._pty = _winpty.PtyProcess.spawn(
                argv,
                cwd=cwd,
                dimensions=(self.rows, self.cols),
            )
        except Exception:  # noqa: BLE001
            self._pty = None
            return False

        self.backend = "conpty"
        # ConPTY 的输出本身就是 UTF-8，不需要探测控制台代码页，
        # 中文可直接正确解码（管道模式才需要那套 chcp 探测）。
        self.code_page = 65001
        self._codec = "utf-8"

        self._start_pty_reader()
        if self.idle_timeout > 0:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        return True

    def _start_pty_reader(self) -> None:
        """
        用后台线程把 ConPTY 的输出搬进 asyncio 队列。

        pywinpty 的 read() 是**阻塞调用**，直接放在事件循环里会把整个服务卡住，
        所以必须放到线程里；线程拿到数据后用 call_soon_threadsafe 投递回循环，
        保证输出缓冲只在事件循环线程里被改动（它不是线程安全的）。
        """
        pty = self._pty
        if pty is None:
            return
        loop = asyncio.get_running_loop()

        def pump() -> None:
            while True:
                try:
                    data = pty.read(_PTY_READ_CHUNK)
                except EOFError:
                    break
                except Exception:  # noqa: BLE001 - 读失败按进程结束处理
                    break
                if not data:
                    break
                try:
                    loop.call_soon_threadsafe(self._emit, data)
                except RuntimeError:
                    # 事件循环已关闭（服务正在退出），线程直接收工
                    return
            try:
                loop.call_soon_threadsafe(self._on_pty_eof)
            except RuntimeError:
                pass

        self._pty_thread = threading.Thread(
            target=pump, name="pty-reader-%s" % self.sid, daemon=True
        )
        self._pty_thread.start()

    def _on_pty_eof(self) -> None:
        """（仅事件循环线程调用）ConPTY 进程结束：记录退出码并放 EOF 哨兵。"""
        status = None
        try:
            if self._pty is not None:
                status = self._pty.exitstatus
        except Exception:  # noqa: BLE001
            status = None
        # 记 0 而不是 None：管理器靠 exit_code is not None 判断「确实结束了」
        # 来回收会话，否则死会话会一直占着 max_sessions 的名额。
        self.exit_code = int(status) if isinstance(status, int) else 0
        self._push_eof()

    async def _start_pipe(self, argv: List[str], cwd: Optional[str]) -> None:
        """管道模式（回退）：没有 TTY，但只依赖标准库。"""
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *argv,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,   # 错误与输出合并，用户能看到报错
                cwd=cwd,
                creationflags=_CREATE_NO_WINDOW,
            )
        except FileNotFoundError:
            raise TerminalSpawnError("找不到可执行文件：%s" % self.shell)
        except PermissionError:
            raise TerminalSpawnError("没有权限启动：%s" % self.shell)
        except Exception as exc:  # noqa: BLE001
            raise TerminalSpawnError("启动命令行失败：%s" % exc)

        if self._proc.stdout is None or self._proc.stdin is None:
            await self.close()
            raise TerminalSpawnError("无法建立与命令行的管道")

        self.backend = "pipe"

        # 探测代码页。这一步必须在后台读取任务启动**之前**做，
        # 否则 chcp 的输出会和后续输出混在一起，没法可靠解析。
        await self._probe_code_page()

        self._reader_task = asyncio.create_task(self._reader_loop())
        if self.idle_timeout > 0:
            self._watchdog_task = asyncio.create_task(self._watchdog_loop())

    async def _probe_code_page(self) -> None:
        """
        跑一次 chcp 得到当前控制台代码页。

        顺带把启动横幅（"Microsoft Windows [版本 ...]"）收进队列，
        这样前端能像真实 CMD 一样先看到版本信息，而不是一片空白。
        """
        proc = self._proc
        assert proc is not None and proc.stdout is not None

        banner = await self._probe_read()
        # chcp 用默认退回编码发送即可：这几个字符都是纯 ASCII，编码无关
        await self._raw_write("chcp\r\n")
        response = await self._probe_read()

        code_page = _parse_code_page(response)
        self.code_page = code_page
        self._codec = _codec_for_code_page(code_page)
        # 必须先建好解码器再回填横幅字节，保证解码顺序与字节顺序一致
        self._decoder = codecs.getincrementaldecoder(self._codec)(errors="replace")

        # 横幅按新确定的编码交给前端；chcp 的应答属于探测噪声，不展示
        if banner:
            self._push(banner)

    async def _probe_read(self) -> bytes:
        """在探测阶段直接读一次 stdout（此时还没有后台读取任务竞争）。"""
        proc = self._proc
        if proc is None or proc.stdout is None:
            return b""
        try:
            return await asyncio.wait_for(proc.stdout.read(_READ_CHUNK), timeout=_PROBE_TIMEOUT)
        except asyncio.TimeoutError:
            return b""
        except Exception:  # noqa: BLE001
            return b""

    # -- 后台读取 -----------------------------------------------------------

    async def _reader_loop(self) -> None:
        """
        把子进程 stdout 的原始字节搬运到输出缓冲。

        这里刻意**不解码**，只搬原始字节：解码统一交给 _emit() 里的
        增量解码器处理，这样即使块边界正好切断一个多字节汉字也不会乱码。
        """
        proc = self._proc
        assert proc is not None and proc.stdout is not None

        try:
            while True:
                chunk = await proc.stdout.read(_READ_CHUNK)
                if not chunk:
                    break
                self._emit(chunk)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            # 管道被关闭等异常：当作进程结束处理
            pass
        finally:
            if self.exit_code is None:
                try:
                    self.exit_code = await proc.wait()
                except Exception:  # noqa: BLE001
                    self.exit_code = proc.returncode
            # EOF 哨兵：让正在等待输出的客户端立刻知道进程已经结束
            self._push_eof()

    def _emit(self, raw: Any) -> None:
        """
        解码一块输出并追加进缓冲（仅事件循环线程调用）。

        ConPTY 推进来的已经是 str（ConPTY 输出本身就是 UTF-8），
        管道推进来的是原始字节，需要用会话的增量解码器解码。

        注意这里在**没有客户端连接时也照常解码并保留**——
        这正是「关掉浏览器再打开还能看到刚才的输出」的实现基础。
        """
        if raw is None:
            return

        if isinstance(raw, str):
            text = raw
        elif self._decoder is not None:
            # errors="replace" 保证任何脏字节都不会把整个会话搞崩；
            # 增量解码器负责处理被块边界切开的半个汉字。
            text = self._decoder.decode(raw, final=False)
        else:
            text = raw.decode(self._codec, errors="replace")

        if text:
            self._remember(text)

    def _remember(self, text: str) -> None:
        """
        把一段文本追加进滚动缓冲，超出上限时从**最旧**的一端裁掉。

        按字节数封顶（terminal.max_output_kb）而不是按块数：块大小并不固定，
        按块数封顶的话上限会随输出速度在 256 字节到 16MB 之间飘。

        裁掉的量记进 _truncated，客户端重连时会被明确告知「中间有内容被丢弃」，
        否则用户只会疑惑「怎么少了半屏」。
        """
        size = len(text.encode("utf-8", errors="replace"))

        self._chunks.append(text)
        self._chunk_offsets.append(self._produced_bytes)
        self._produced_bytes += size
        self._buffered_bytes += size
        self._emitted_seq += 1

        # 裁到上限以内。用 while 而不是 if：单块就可能远大于上限
        # （例如一条 cat 大文件命令），必须一直裁到放得下。
        dropped = False
        while self._chunks and self._buffered_bytes > self._max_output_bytes:
            old = self._chunks.popleft()
            self._chunk_offsets.popleft()
            self._buffered_bytes -= len(old.encode("utf-8", errors="replace"))
            dropped = True

        if dropped:
            self._truncated = True
            # 缓冲起点前移了：所有客户端的游标不能小于新的起点，
            # 否则它们会永远等一个已经不存在的偏移量
            base = self._buffer_base
            if self._chunk_offsets:
                base = self._chunk_offsets[0]
            self._buffer_base = base
            for token, cursor in list(self._clients.items()):
                if cursor < base:
                    self._clients[token] = base

        self._wakeup.set()

    def _push_eof(self) -> None:
        """
        标记输出结束并唤醒所有等待者。

        原先是在队列里放一个 None 哨兵；现在缓冲用的是「字节偏移 + 结束标志」，
        所以只置 exit_code（_reader_loop / _on_pty_eof 里已经置过）并叫醒客户端，
        它们在缓冲读到末尾后会自行看到「已结束」。
        """
        self._wakeup.set()

    # -- 客户端连接管理 -----------------------------------------------------

    def attach(self) -> int:
        """
        登记一个客户端，返回它的读取游标。

        ★ 游标从「缓冲现存的最旧一块」开始，而不是从 0：
        缓冲区里保留的正是客户端离开期间产生的输出，所以新连上来的客户端
        会**先收到这段积压内容**，然后才继续收实时输出 —— 这就是
        「关掉标签页再打开，能看到离开期间发生了什么」的实现点。

        重复连接（同一浏览器开了两个窗口 / 页面刷新时旧连接还没彻底断开）
        采取「新连接顶掉旧连接」策略：把旧客户端游标设为 _REPLACED（= 已失效），
        路由层看到它就知道该给旧连接发一句「已被新的连接接管」并关掉它。
        这里之所以选择顶掉而不是拒绝新连接，是因为「旧连接其实已经死了但服务端
        还没察觉」是最常见的真实情况（拔网线、休眠、直接关标签页），
        那种情况下拒绝新连接会让用户永远连不回来，只能等服务端超时回收。
        """
        token = self._next_client_id
        self._next_client_id += 1

        for old_token in list(self._clients.keys()):
            # _REPLACED 表示「这个客户端已被顶掉，下次取数据时自行退出」
            self._clients[old_token] = _REPLACED
            # 被顶掉的连接不会再取数据了，它的补发边界一并清掉
            self._replay_end.pop(old_token, None)

        self._clients[token] = self._buffer_base
        # ★ 划出「补发」与「实时」的分界线：此刻已经缓冲下来的输出全部是积压
        #   （客户端离开期间产生的），严格小于这个偏移量的块在送出前要剔除
        #   终端查询序列 —— 否则重连时那段启动序列会重新触发终端回话，
        #   而那句回复会被当成用户输入（见 _TERMINAL_QUERY_RE 的详细说明）。
        self._replay_end[token] = self._produced_bytes
        self._ever_attached = True
        self._last_active = time.time()
        # 有人连上了：不再处于「无人连接」状态，空闲计时暂停
        self._detached_at = None
        # 唤醒可能正在等待的旧客户端，让它立刻发现自己被顶掉了
        self._wakeup.set()
        return token

    def has_ever_been_attached(self) -> bool:
        """
        这个会话之前是否已经有过客户端连过。

        路由层用它判断「本次连接是不是一次回归」：先断开再重连时，
        重连那一刻 has_clients() 是 False（没人连着），只有这个标志能说明
        「用户是回到刚才那个会话」，从而给前端一个准确的 replay 提示。
        """
        return self._ever_attached

    def client_state(self, token: int) -> str:
        """
        该客户端当前的连接状态：attached / replaced / gone。

        路由层用它区分「被新连接接管」与「会话已被回收」，
        好给用户一句准确的提示，而不是笼统地说「会话已结束」。
        """
        cursor = self._clients.get(token, _MISSING)
        if cursor is _MISSING:
            return "gone"
        if cursor is _REPLACED:
            return "replaced"
        return "attached"

    def detach(self, token: int) -> None:
        """
        注销一个客户端，但**不结束会话**——这正是「关掉浏览器进程还活着」的关键。

        会话会继续运行、输出继续进缓冲，直到空闲超时（或服务关闭）才回收。
        """
        self._clients.pop(token, None)
        self._replay_end.pop(token, None)
        self._last_active = time.time()
        if not self.has_clients():
            # 最后一个客户端走了：空闲计时从此刻开始
            self._detached_at = time.time()
        self._wakeup.set()

    def has_clients(self) -> bool:
        """当前是否有客户端连着（已被顶掉的失效客户端不算）。"""
        return any(cursor is not _REPLACED and cursor is not _MISSING
                   for cursor in self._clients.values())

    def idle_seconds(self) -> float:
        """
        已经「没人连着」多少秒；有人连着时返回 0。

        这是空闲回收唯一应当依据的量（见 _detached_at 的说明）。
        """
        if self.has_clients() or self._detached_at is None:
            return 0.0
        return max(0.0, time.time() - self._detached_at)

    def buffered_bytes(self) -> int:
        """当前缓冲里有多少字节输出（测试与诊断用）。"""
        return self._buffered_bytes

    async def next_chunk(self, token: int, wait: float = 1.0) -> str:
        """
        取该客户端尚未读过的下一块输出。

        返回一段文本；暂时没有新输出时最多等 wait 秒，超时返回空字符串
        （调用方应继续循环，不要把空串当成结束）。

        异常：
          * SessionReplaced：这条连接被新的连接顶掉了
          * SessionGone    ：会话已被回收，或命令行进程已结束且输出读空

        用「字节偏移 + 滚动缓冲」而不是队列：队列是消费即消失的，
        而重连的客户端需要能**从头读到积压内容**，
        因此缓冲必须允许多个客户端各自维护独立的读取位置。
        """
        while True:
            state = self.client_state(token)

            if state == "replaced":
                raise SessionReplaced("该终端会话已被新的连接接管")
            if state == "gone":
                raise SessionGone("会话已结束")

            cursor = self._clients[token]

            # 有未读内容就立刻返回（重连后的第一件事就是把它冲出去）
            index = self._cursor_index(cursor)
            if index < len(self._chunks):
                start = self._chunk_offsets[index]
                text = self._chunks[index]
                self._clients[token] = start + len(text.encode("utf-8", errors="replace"))
                self._last_active = time.time()

                # ★ 补发的积压里必须剔除「终端查询序列」。
                #   理由见 _TERMINAL_QUERY_RE：那段启动序列会再问终端一次，
                #   终端按规范自动回一句，而重连时 shell 已经停在提示符上，
                #   那句回复就变成用户输入，和用户下一条命令拼在一起 ——
                #   实测症状是重连后第一条命令必失败。
                #   实时输出（起始偏移 >= 补发边界）**原样送出，绝不改动**。
                if start < self._replay_end.get(token, 0):
                    text = _strip_terminal_queries(text)
                    if not text:
                        # 整块都是查询序列：没有任何可见内容，直接取下一块。
                        # 不要返回空串 —— 空串在协议里表示「暂时没有输出」，
                        # 会让调用方多发一个 ping。
                        continue

                return text

            # 读完了：命令行进程已经结束就该收尾，否则继续等新输出
            if self.exit_code is not None:
                raise SessionGone("命令行进程已结束")
            if self._closed:
                raise SessionGone("会话已结束")

            # 等新数据。见 _emitted_seq 的说明：先记下当前序号，
            # 醒来后如果序号变了说明确实有新数据；如果没变而超时返回空串，
            # 调用方会继续循环（并发客户端较多时，别的客户端可能清掉了
            # 本该给我的那次 set，靠这里重新判断即可，不会丢内容）。
            seen_seq = self._emitted_seq
            try:
                await asyncio.wait_for(self._wakeup.wait(), timeout=wait)
            except asyncio.TimeoutError:
                return ""
            finally:
                if self._emitted_seq == seen_seq:
                    # 没有新数据，说明这次唤醒是状态变化（比如被接管）引起的：
                    # 清掉标志，避免下一轮空转
                    self._wakeup.clear()

    def _cursor_index(self, cursor: int) -> int:
        """把字节游标换算成缓冲里的下标（第一个尚未读完的块）。"""
        index = 0
        # 缓冲通常只有几十块，线性查找足够；不必为了 O(log n) 引入二分
        for offset in self._chunk_offsets:
            if offset >= cursor:
                break
            index += 1
        return min(index, len(self._chunks))

    # -- 空闲看门狗 ---------------------------------------------------------

    async def _watchdog_loop(self) -> None:
        """
        空闲超时自动关闭会话。

        检查间隔取「超时时间的 1/4」并夹在 5~60 秒之间：既不会因为
        检查太频繁而空转，也不会在超时后拖太久才回收。

        ★ 与「会话可分离」配合的两条规则（这是本次改动的核心之一）：

        1) **没有客户端连着时，时钟继续走。**
           断开连接不刷新 _last_active，所以一个 detached 会话到达
           terminal.idle_timeout_seconds 后照常被回收，不会永久泄漏。

        2) **有客户端连着时不回收。**
           _last_active 只记录「最后一条数据」。用户打开终端后盯着屏幕不动手
           （在读长输出、或在等一个慢命令）时时间照样会走到超时，
           那时把会话杀掉是错的 —— 所以只要还有有效客户端连着就给时钟续期。
           注意续期发生在**看门狗自己的循环里**，不依赖客户端发消息，
           因此前端不需要为「保活」专门发心跳。
        """
        interval = max(5.0, min(60.0, self.idle_timeout / 4.0))
        while True:
            try:
                await asyncio.sleep(interval)
                if self._closed:
                    return

                if self.has_clients():
                    # 有人在用：续期，绝不在这里回收
                    self._last_active = time.time()
                    continue

                if self.idle_seconds() > self.idle_timeout:
                    self._idle_expired = True
                    callback = self._reap_callback
                    if callback is not None:
                        # ★ 交给管理器回收：它负责**先从注册表摘除再关闭**。
                        # 直接 self.close() 会让这个已经死掉的会话继续留在
                        # 注册表里占着 max_sessions 名额。
                        await callback(self.sid)
                    else:
                        await self.close()
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 看门狗绝不能因单次异常而死掉
                _warn_watchdog(self.sid, exc)

    # -- 读写 ---------------------------------------------------------------

    async def write(self, data: str) -> bool:
        """
        把文本写进 shell。

        返回 False 表示已经不可写（进程结束或管道断开），
        路由层据此推送 error / exit 消息。

        ConPTY 后端收到的是**原始按键流**（含 ESC 序列、\r、\x03 等），
        直接按 UTF-8 写进 pty —— ConPTY 会把它当作键盘输入交给控制台，
        因此 Tab 补全、方向键、Ctrl+C 全部由 shell 自己处理，
        前端不再需要模拟行编辑。
        """
        if not self.is_alive():
            return False

        try:
            if self.backend == "conpty":
                pty = self._pty
                if pty is None:
                    return False
                # pywinpty 的 write 接受 str，内部按 UTF-8 编码
                pty.write(data)
            else:
                proc = self._proc
                if proc is None or proc.stdin is None:
                    return False
                # 用与会话相同的编码写回：这是中文路径能正确传入的前提，
                # 编码与子进程控制台代码页一致时 cmd.exe 才能理解。
                proc.stdin.write(data.encode(self._codec, errors="replace"))
                await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError):
            return False
        except Exception:  # noqa: BLE001
            return False

        self._last_active = time.time()
        return True

    async def _raw_write(self, data: str) -> None:
        """探测阶段使用的写入口（此时还没确定编码，用 ASCII 安全内容）。"""
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        try:
            proc.stdin.write(data.encode("ascii", errors="replace"))
            await proc.stdin.drain()
        except Exception:  # noqa: BLE001
            pass

    def take_truncated(self) -> bool:
        """取出并清除「输出被截断」标志，用于向客户端提示一次。"""
        if not self._truncated:
            return False
        self._truncated = False
        return True

    # -- 状态 ---------------------------------------------------------------

    def is_alive(self) -> bool:
        """shell 是否仍在运行。"""
        if self.backend == "conpty":
            pty = self._pty
            if pty is None:
                return False
            try:
                return bool(pty.isalive())
            except Exception:  # noqa: BLE001
                return False

        proc = self._proc
        if proc is None:
            return False
        return proc.returncode is None

    def _shell_pid(self) -> Optional[int]:
        """取 shell 自身的 pid（两种后端都支持）。"""
        try:
            if self.backend == "conpty":
                return int(self._pty.pid) if self._pty is not None else None
            return int(self._proc.pid) if self._proc is not None else None
        except Exception:  # noqa: BLE001
            return None

    def resize(self, cols: int, rows: int) -> None:
        """
        调整终端尺寸。

        只有 ConPTY 下才有真实意义：真 TTY 的内核按这个尺寸做换行与光标定位，
        而客户端 xterm.js 也按同样的列宽渲染，两边必须一致才不会错行。
        管道模式没有 TTY，仅记录数值。
        """
        try:
            self.cols = max(20, min(500, int(cols)))
            self.rows = max(5, min(300, int(rows)))
        except (TypeError, ValueError):
            return

        if self.backend != "conpty" or self._pty is None:
            return
        try:
            # 注意：pywinpty 的参数顺序是 (rows, cols)，与直觉相反
            self._pty.setwinsize(self.rows, self.cols)
        except Exception:  # noqa: BLE001 - 改尺寸失败不该影响会话
            pass

    async def interrupt(self, force: bool = False) -> Dict[str, Any]:
        """
        中断当前正在运行的东西。

        两种策略：
          * 非强制 + ConPTY：往 pty 写一个 Ctrl+C（0x03）。这是**原生中断**，
            shell 会像用户真的按下 Ctrl+C 一样处理，最干净。
          * 强制（或管道模式）：杀掉 shell 的**子进程树**但**保留 shell 本身**。
            这是逃生口：卡住的 python 被杀掉、提示符回来，
            而当前目录与 shell 状态都还在（管道模式下这是唯一可行的中断方式，
            因为没有 TTY 可以发控制台中断事件）。

        返回描述，供路由层回给前端做提示。
        """
        if not force and self.backend == "conpty" and self._pty is not None:
            try:
                # sendintr() 就是 pywinpty 封装的 Ctrl+C
                self._pty.sendintr()
                return {"mode": "ctrl-c", "killed": []}
            except Exception:  # noqa: BLE001
                pass
            try:
                # 兜底：直接写控制字符
                self._pty.write("\x03")
                return {"mode": "ctrl-c", "killed": []}
            except Exception:  # noqa: BLE001
                pass

        killed = await self._kill_children()
        return {"mode": "kill-children", "killed": killed}

    async def _kill_children(self) -> List[str]:
        """
        杀掉 shell 的所有直接子进程（每个都带 /T，子树一起走），**保留 shell**。

        做法是先枚举再逐个 taskkill，而不是对 shell 本身执行 taskkill /T ——
        后者会把用户正在用的 shell 一并干掉，等于废掉整个会话。
        """
        if os.name != "nt":
            return []

        pid = self._shell_pid()
        if not pid:
            return []

        killed: List[str] = []
        for child in _win_child_pids(pid):
            try:
                killer = await asyncio.create_subprocess_exec(
                    "taskkill", "/F", "/T", "/PID", str(child),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                    creationflags=_CREATE_NO_WINDOW,
                )
                await asyncio.wait_for(killer.wait(), timeout=10)
                killed.append(str(child))
            except Exception:  # noqa: BLE001 - 子进程可能已经自己退出了
                continue
        if killed:
            self._last_active = time.time()
        return killed

    @property
    def idle_expired(self) -> bool:
        """是否因为空闲超时被自动关闭。"""
        return self._idle_expired

    def is_idle_expired(self) -> bool:
        """
        现在是否已经到了该被回收的时候（没有客户端连着 + 超过空闲时限）。

        ★ 两个必要条件缺一不可：
          * 没有客户端连着 —— 有人在用就绝不回收；
          * idle_timeout > 0   —— 0 表示「不限制空闲」，永不回收。

        判定依据是 idle_seconds()（从「最后一个客户端离开」起算），
        不是 _last_active —— 否则刚断开那一刻会被算成「刚刚活动过」，
        分离会话就永远回收不掉。

        管理器定期用这个判断兜底回收，防止「浏览器关掉后会话一直挂着」。
        """
        if self._closed or self.exit_code is not None:
            return False
        if self.idle_timeout <= 0:
            return False
        if self.has_clients():
            return False
        return self.idle_seconds() > self.idle_timeout

    def matches_token(self, session_token: str) -> bool:
        """
        校验「连接 WS 用的登录令牌」是否就是创建该会话的那个令牌。

        用 sha256 摘要比较，避免在内存里留着可直接使用的会话令牌；
        比较使用恒定时间函数防止时序侧信道。
        """
        if not session_token:
            return False
        candidate = _token_hash(session_token)
        return hmac.compare_digest(candidate, self.token_hash)

    def expire_seconds(self) -> int:
        """距离空闲超时还剩多少秒；0 表示不限制。"""
        if self.idle_timeout <= 0:
            return 0
        remaining = int(self.idle_timeout - (time.time() - self._last_active))
        return max(0, remaining)

    # -- 关闭 ---------------------------------------------------------------

    async def close(self) -> None:
        """
        关闭会话：杀整棵进程树 + 取消后台任务。

        幂等，可以重复调用（空闲超时、服务关闭、客户端要求关闭都会走到这里）。

        ★ 注意与 detach() 的区别：detach 只是「客户端走开」，会话继续跑；
        close 才是真正结束会话、杀掉进程。本次改动把「断开连接」从 close 改成了
        detach，所以这里必须自己唤醒所有在等输出的客户端，让它们立刻收到
        「会话已结束」，而不是一直等到超时才反应过来。
        """
        if self._closed:
            return
        self._closed = True

        # 让所有正在等输出的客户端立刻醒过来（它们会发现会话已经不在了）
        self._clients.clear()
        self._replay_end.clear()
        self._wakeup.set()

        current = asyncio.current_task()
        for task in (self._reader_task, self._watchdog_task):
            # 不要 cancel 自己：看门狗会调用本方法，取消自身会让
            # 后面的清理代码直接被 CancelledError 打断。
            if task is not None and task is not current and not task.done():
                task.cancel()

        if self.backend == "conpty":
            await self._close_conpty()
        else:
            await self._close_pipe()

    async def _close_conpty(self) -> None:
        """关闭 ConPTY 会话：先清子进程，再结束 shell 本体。"""
        pty = self._pty
        if pty is None:
            return

        # 子进程（比如正在跑的 python）并不是 pty 的父子关系，
        # 只 terminate shell 会把它们留成孤儿进程，所以先显式清掉。
        try:
            await self._kill_children()
        except Exception:  # noqa: BLE001
            pass

        try:
            pty.terminate(force=True)
        except Exception:  # noqa: BLE001
            pass
        try:
            pty.close()
        except Exception:  # noqa: BLE001
            pass

    async def _close_pipe(self) -> None:
        """关闭管道会话：杀进程树 + 兜底 kill + 释放管道。"""
        proc = self._proc
        if proc is not None and proc.returncode is None:
            # 先杀进程树，再兜底 kill 自己
            await self._kill_tree(proc.pid)
            try:
                proc.kill()
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except Exception:  # noqa: BLE001
                pass

        # 关闭管道，释放文件描述符
        if proc is not None:
            for stream in (proc.stdin, proc.stdout):
                try:
                    if stream is not None and not stream.is_closing():
                        stream.close()
                except Exception:  # noqa: BLE001
                    pass

    async def _kill_tree(self, pid: int) -> None:
        """
        用 taskkill /F /T 杀掉整棵进程树。

        /T 是「连同子进程」：cmd.exe 自己退出不会带走它启动的程序
        （例如 `start notepad` 或跑起来的 python 子进程），
        只 kill cmd.exe 会留下孤儿进程，所以这里必须带 /T。

        非 Windows 平台没有 taskkill，直接跳过（交给 proc.kill()）。
        """
        if os.name != "nt":
            return
        try:
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(pid),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                creationflags=_CREATE_NO_WINDOW,
            )
            await asyncio.wait_for(killer.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            # 进程可能已经自己退出了，taskkill 报错属正常情况
            pass

    # -- 展示 ---------------------------------------------------------------

    def is_dead(self) -> bool:
        """
        会话是否**确定**已经结束，可以立刻从注册表剔除、腾出 max_sessions 名额。

        两种情况算「确定结束」：
          1. `_closed` 为真 —— close() 已经跑过，进程树已杀。这包括空闲超时
             回收掉的会话。
          2. 进程退出码已记录且进程确实不在了 —— shell 自己退出（用户敲了
             exit）的情况。

        ★ 必须包含第 1 条：看门狗回收会话时只调用 close()，会话对象仍留在
        注册表里。若这里不认 `_closed`，那个已经死掉的会话就会一直占着
        max_sessions 名额，并把「重连」变成一次看似成功的连接
        （握手通过、随后才收到结束消息），用户与测试都会困惑。

        与 `not is_alive()` 的区别：刚创建还没 start() 完的会话 is_alive()
        也是 False，但它并没有死，不能剔。
        """
        if self._closed:
            return True
        return self.exit_code is not None and not self.is_alive()

    def describe(self) -> Dict[str, Any]:
        """给审计日志用的描述（含后端类型与终端尺寸）。"""
        return {
            "sid": self.sid,
            "shell": self.shell,
            "cwd": self.cwd,
            "backend": self.backend,
            "code_page": self.code_page,
            "codec": self._codec,
            "cols": self.cols,
            "rows": self.rows,
            "idle_timeout": self.idle_timeout,
            "alive": self.is_alive(),
            # 会话能否分离之后，这两个字段对排查「为什么会话没被回收」很关键
            "clients": 1 if self.has_clients() else 0,
            "buffered_kb": round(self._buffered_bytes / 1024.0, 1),
        }


def _token_hash(session_token: str) -> str:
    """会话令牌的 sha256 十六进制摘要（不保存令牌本身）。"""
    return sha256((session_token or "").encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# 补发积压时必须剔除的序列：会让终端「自己开口说话」的那些
# ---------------------------------------------------------------------------
# 这里说的不是普通输出，而是两类会**诱发终端往回发数据**的序列：
#
#   1) 查询类 —— 自己没有任何可见效果，唯一作用是要求终端回话
#      （DA1/DA2 报告设备属性、DSR 报告光标位置、DECRQM 问模式状态……）。
#   2) 「焦点上报」的开关（?1004h）—— 打开之后，终端一获得焦点就自动回
#      一句 ESC[I（失焦回 ESC[O]）。它本身是开关，不是查询，但效果一样：
#      让终端往 shell 里注入用户没敲过的字符。
#
# 为什么补发积压时必须去掉它们（这是真实浏览器验证抓到的缺陷）：
#   会话启动时 ConPTY 会发一段固定的启动序列，实测抓到的原文是
#       \x1b[1t \x1b[c \x1b[?1004h \x1b[?9001h \x1b]0;cmd\x07 \x1b[2J \x1b[H ...
#   其中 \x1b[c 是设备属性查询，\x1b[?1004h 打开了焦点上报。
#   **新建会话**时 shell 正在启动，终端那两句自动回复被启动流程吃掉/丢掉，
#   界面上看不出问题；但**重连**时把同一段启动序列当积压补发出去，shell
#   已经停在提示符上等着敲命令了，于是这两句回复变成「用户打的字符」，
#   和用户下一条命令拼在一起。实测症状就是**重连后第一条命令必失败**：
#       ^[[?1;2cecho BBB86K3
#       '是内部或外部命令，也不是可运行的程序
#   浏览器端同时抓到补发后自动发出的两句：["\x1b[?1;2c", "\x1b[I"] ——
#   正好对应上面那两个来源，所以两个都必须剔。
#
# 只剔这两类，其它转义序列（颜色、光标定位、清屏、标题、?9001h 之类）必须
# 原样补发，否则恢复出来的画面是错的。**实时输出一律不动** —— 会话中间真正
# 需要问终端要光标位置的全屏程序（vim 之类）必须照常拿到回复，剔除只发生在
# 补发的那一段（见 TerminalSession.attach 划出的补发边界）。
#
# 已知局限：做不到跨块拼接。如果某个待剔序列正好被缓冲的分块边界切成两半，
# 这里认不出来。实际影响很小（启动那一小段通常落在同一块里），但不假装它不存在。
_TERMINAL_QUERY_RE = re.compile(
    r"\x1b\["
    r"(?:"
    r">0?c"            # DA2：ESC[>c / ESC[>0c
    r"|0?c"            # DA1：ESC[c  / ESC[0c（ConPTY 启动时发的就是这个）
    r"|\?1004h"        # 打开焦点上报：开了之后焦点一变就自动回 ESC[I / ESC[O
    r"|\?[0-9;]*\$p"   # DECRQM：询问某个模式当前处于什么状态
    r"|\?[0-9;]*n"     # DECDSR：ESC[?6n 等
    r"|[0-9;]*[56]n"   # DSR：ESC[5n / ESC[6n（要光标位置）
    r"|>[0-9;]*q"      # XTVERSION
    r")"
)


def _strip_terminal_queries(text: str) -> str:
    """
    去掉会让终端自己往回发数据的序列（**只用于补发积压**）。

    详见 _TERMINAL_QUERY_RE：包含「查询」和「焦点上报开关」两类。
    绝大多数输出块里根本没有 CSI 序列，所以先用一个廉价的子串判断短路掉，
    避免对每块输出都跑一遍正则。
    """
    if "\x1b[" not in text:
        return text
    return _TERMINAL_QUERY_RE.sub("", text)


def _warn_watchdog(sid: str, exc: BaseException) -> None:
    """
    看门狗单次检查失败时的告警（**不终止循环**）。

    ★ 为什么必须捕获而不是放它冒泡：看门狗是会话回收的唯一保障。
    任务里未处理的异常只会被 asyncio 静静地存进 Task 对象，
    直到对象被 GC 才可能打印——排查时完全看不到线索，
    而后果是**这个会话从此再也不会被空闲回收**（实测踩到过：
    分离的会话一直占着 max_sessions 名额，用户以为是自己没关窗口）。

    所以单次异常只记一条日志，循环继续跑，下一次检查再试。
    """
    print("[警告][命令行] 空闲检查异常 sid=%s: %s: %s"
          % (sid, type(exc).__name__, exc), flush=True)


def _warn_sweeper(exc: BaseException) -> None:
    """
    巡检协程没能启动时的告警。

    以前这里是把 _sweep_task 置空就悄悄过去了。那等于「分离会话不泄漏」的
    兜底防线根本没有开，而日志里什么都看不到 —— 出问题时无从排查，
    所以必须留下一条明确的痕迹。
    """
    print("[警告][命令行] 未能启动空闲巡检协程（分离会话将只依赖会话自身的看门狗）："
          "%s: %s" % (type(exc).__name__, exc), flush=True)


# ---------------------------------------------------------------------------
# 会话管理器（进程级单例）
# ---------------------------------------------------------------------------

class TerminalManager:
    """
    命令行会话注册表。

    只允许在运行中的事件循环里使用：内部持有一把 asyncio.Lock，
    并且保存的是 asyncio 子进程与队列这些绑定事件循环的对象。
    """

    def __init__(self) -> None:
        self._sessions: Dict[str, TerminalSession] = {}
        self._lock: Optional[asyncio.Lock] = None
        self._sweep_task: Optional[asyncio.Task] = None
        # 用来「叫醒」正在睡的巡检协程（懒创建，见 _get_sweep_wake）
        self._sweep_wake: Optional[asyncio.Event] = None

    def _get_lock(self) -> asyncio.Lock:
        """
        懒创建锁。

        不在 __init__ 里创建是为了不依赖「导入本模块时事件循环已存在」，
        也避免把锁绑定到错误的循环上。
        """
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    # -- 查询 ---------------------------------------------------------------

    def get(self, sid: str) -> Optional[TerminalSession]:
        """
        按 sid 取会话；只返回**还能用于新连接**的会话，否则返回 None。

        「不能用」的两种情况都在这里判掉，原因是：连接路径必须**确定性地**
        在 accept 之前拒绝一个已经不在的会话，而不是「先连上、再补一条 closed」。
        否则同一件事会有两种用户可见行为，取决于后台回收/巡检上一次跑在什么时候：
          * 巡检已经扫过 → 握手阶段被拒（明确的「会话已结束」）；
          * 还没扫过 → 握手成功、紧接着收到结束通知。
        这是不该有的不确定性，所以判定必须落在查询这一步。

        注意**不要**只依赖路由里那道兜底检查：判定散在两处，将来任何一处被改动
        都会悄悄把不确定性带回来。

        ★ 两种情况的处理刻意不同（重要，别顺手统一）：

        1) 已经彻底结束（被回收 / 进程自己退出）→ **顺手从注册表摘除**再返回 None。
           摘除是安全的：进程已经不在了（或 close() 已经跑过），不存在
           「还有活着的进程需要有人去关」的问题；而且立刻腾出 max_sessions 名额。

        2) 已经过了空闲时限、但进程可能还活着 → **只拒绝，绝不摘除**。
           看门狗和巡检都是「按注册表遍历」来找会话的，一旦在这里把它摘掉，
           就再没有人负责关掉那个 cmd.exe —— 省下一次拒绝的功夫，
           却留下一个孤儿进程。关进程交给它们，这里只负责「不让新连接连上」。

        另外：is_idle_expired() 在会话「已经 close」之后会返回 False（它的首行
        守卫），所以这里还要看 idle_expired 这个标志位（看门狗回收时置的那个）。
        """
        if not sid:
            return None
        session = self._sessions.get(sid)
        if session is None:
            return None
        # 1) 已彻底结束：摘掉并视为不存在
        if session.is_dead():
            self._sessions.pop(sid, None)
            return None
        # 2) 已空闲超时但进程可能还在：只拒绝，不摘除（见上面说明）
        #    注意 idle_expired 是 @property（不是方法），这里不能加括号；
        #    is_idle_expired() 才是方法，要加括号。
        if session.idle_expired or session.is_idle_expired():
            return None
        return session

    def count(self) -> int:
        return len(self._sessions)

    def owner_counts(self) -> Dict[str, int]:
        """
        按归属统计**活跃**会话数，供管理界面显示「某用户几个命令行窗口」。

        ★ 只回数量，绝不回任何会话内容（用户决定：管理员看活跃会话数 + 进程，
        不看终端输出）。所以这里刻意返回 dict[str,int] 而不是会话列表 ——
        接口形状本身就杜绝了「顺手把输出也带上」。

        已经死掉且没客户端连着的会话不算：它们下一次 create/prune 就会被清掉，
        统计里带上会让管理员看到永远不降的数字。
        """
        counts: Dict[str, int] = {}
        for session in list(self._sessions.values()):
            if session.is_dead() and not session.has_clients():
                continue
            key = session.owner or ""
            counts[key] = counts.get(key, 0) + 1
        return counts

    def _prune_locked(self) -> None:
        """
        清理**已经死掉且没有客户端连着**的会话（调用方需持有锁）。

        为什么必须限定「没有客户端连着」：会话现在可以分离，客户端断开后
        cmd.exe 仍然活着，此时 is_alive() 仍是 True，不会被这里清掉——
        这正是我们要的效果。只有进程确实退出（exit_code 已记录）才算死。

        不在这里 await close()：死进程的清理是同步可靠的，
        而 close() 内部还会 await，持锁 await 容易造成死锁。
        """
        dead = [
            sid for sid, s in self._sessions.items()
            if s.is_dead() and not s.has_clients()
        ]
        for sid in dead:
            self._sessions.pop(sid, None)

    # -- 创建 ---------------------------------------------------------------

    async def create(
        self,
        *,
        shell: str,
        start_dir: str,
        idle_timeout: int,
        max_sessions: int,
        session_token: str,
        cols: int = 120,
        rows: int = 30,
        max_output_kb: int = _DEFAULT_MAX_OUTPUT_KB,
        evict_grace: float = _EVICT_GRACE_SECONDS,
        owner: str = "",
        max_total: int = 0,
        run: str = "",
    ) -> TerminalSession:
        """
        创建一个新会话。

        ★ max_sessions 只约束「新建」，不约束「重连」：
        分离（detached）的会话仍然占着一个真实的 cmd.exe，所以它继续计入
        名额是对的 —— 否则反复关掉/打开浏览器就能不断攒出新的 shell，
        直到空闲超时才被回收，等于绕过了这个限制。
        而**重连一个已存在的会话根本不走这里**（见 routers/terminal.py 的
        WebSocket 路由，它只用 manager.get 取会话），因此重连永远不会因为
        名额已满而被拒绝，这正是我们要的：限制只管新开，不管回来。

        名额已满时，若存在「没有客户端连着且已闲置够久」的分离会话，就顶掉
        其中闲置最久的那个（见 _pick_evictable_locked），否则抛
        TerminalLimitError（中文提示）。启动失败抛 TerminalSpawnError。

        cols/rows 是客户端 xterm.js 的真实列宽/行高：**必须在 start() 之前**
        设好，因为 ConPTY 是在 spawn 的那一刻把初始尺寸一次性交给内核的。

        max_output_kb 决定会话自己保留多少输出（terminal.max_output_kb）。
        它与客户端无关：即使一个客户端都没连，会话也会保留最近这么多输出，
        供之后重连的客户端读取。

        ★ 多用户下 max_sessions 的含义（重要）：
        它约束的是**该会话归属者（owner）自己的**会话数，不是全机总数。
        配套两条：
          * 名额满了只在该 owner 的会话里挑人顶掉 —— 学生 A 开新窗口
            绝不能杀掉学生 B 正在跑训练的 cmd，那是灾难性的。
          * owner 为空串时（直接构造会话的单元测试）所有会话都算同一个人，
            于是行为与改造前完全一致。
        全机总量另有 max_total 兜底（<=0 表示不设限）：它只防「总量失控」，
        淘汰范围不限定 owner（否则谁都腾不出名额），也是最后手段。
        """
        lock = self._get_lock()
        async with lock:
            # 先把「确定已经结束」的会话剔掉，再判名额：
            # 用户敲 exit 结束的 shell、以及已被空闲超时回收的会话，
            # 都不该继续占着名额。
            self._prune_locked()

            limit = max(1, int(max_sessions or 1))
            victim: Optional[TerminalSession] = None

            # 全机总量兜底（最后手段，所以放在前面判）：到这里还没满就说明
            # 问题只可能在「这个人的名额」，交给下面那段。
            if max_total > 0 and len(self._sessions) >= int(max_total):
                victim = self._pick_evictable_locked()
                if victim is None:
                    raise TerminalLimitError(
                        "服务器上命令行会话总数已达上限（%d 个），请稍后再试，"
                        "或先关闭不再使用的命令提示符窗口。" % int(max_total)
                    )
                self._sessions.pop(victim.sid, None)

            # 本用户的名额（常规限制）
            mine = sum(1 for s in self._sessions.values() if s.owner == owner)
            if mine >= limit:
                victim = self._pick_evictable_locked(owner)
                if victim is None:
                    raise TerminalLimitError(
                        "你的命令行会话数已达上限（%d 个），请先关闭其它命令提示符窗口后重试。"
                        "（浏览器断开只是让窗口挂起，请在该窗口里点关闭，"
                        "或等空闲超时自动回收）" % limit
                    )
                # 先把名额腾出来，再在锁外真正杀掉它（close 里有 await）
                self._sessions.pop(victim.sid, None)

            sid = secrets.token_urlsafe(_SID_BYTES)
            session = TerminalSession(
                sid=sid,
                shell=shell,
                start_dir=start_dir,
                idle_timeout=idle_timeout,
                token_hash=_token_hash(session_token),
                max_output_kb=max_output_kb,
                evict_grace=evict_grace,
                owner=owner,
                run=run,
            )
            # 让空闲回收走管理器：它负责先从注册表摘除再关闭（见 set_reap_callback）
            session.set_reap_callback(self.close_session)
            session.resize(cols, rows)

        # 顶掉旧会话与启动新会话都在锁外做：两者都要 spawn / kill 进程，
        # 持锁 await 会把其它请求（包括 WS 的连接校验）全部堵住。
        if victim is not None:
            try:
                await victim.close()
            except Exception:  # noqa: BLE001
                pass

        await session.start()

        lock = self._get_lock()
        async with lock:
            self._sessions[sid] = session
            self._ensure_sweeper()
            return session

    def _pick_evictable_locked(self, owner: Optional[str] = None) -> Optional[TerminalSession]:
        """
        名额已满时挑一个可以牺牲的会话（调用方需持有锁）。

        ★ 为什么需要这个：会话现在能跨浏览器断开存活。如果断开就一直占着名额，
        用户关掉几个标签页之后就必须等满 idle_timeout（默认半小时）才能再开
        命令行，体验不可接受。空闲超时是「兜底回收」，
        不该变成「用户必须先等半小时」的硬门槛。

        ★ 但为什么必须加一个宽限期，而不是「只要是分离的就顶掉」：
        分离会话**存在的意义**就是等人重连（刷新页面、关掉标签页再打开）。
        如果一超限就把它顶掉，那用户刷新页面回来只会看到「会话已结束」，
        整个持久化特性等于白做。所以只有「分离且已经闲置够久」（明显是被
        彻底遗弃的窗口）才允许被顶掉；刚断开几秒的会话一律不动 ——
        这正是 terminal.max_sessions 依然是有意义的硬限制的原因。

        有客户端连着的会话一律不动（正在用，绝不能抢）；
        没有任何可淘汰的才真的报「会话数已达上限」。
        优先顶掉闲置最久的那个（LRU）。

        ★ owner 参数是多用户加的一道**硬边界**：给了 owner 就只在这个人的
        会话里挑。绝不能因为「张同学开新窗口」把「李同学正在跑 PyTorch 的
        窗口」顶掉 —— 那是直接毁掉别人几小时的训练。传 None 表示不限定归属，
        只给全机总量兜底那条路用（那种情况必须能腾出名额，否则整机卡死）。
        """
        # 统一用 idle_seconds（= 从最后一个客户端离开起算了多久）作为
        # 「闲置程度」的唯一口径，与空闲回收用的是同一个定义，避免两处跑偏。
        candidates = [
            s for s in self._sessions.values()
            if (owner is None or s.owner == owner)
            and not s.has_clients() and s.idle_seconds() >= s.evict_grace
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda s: s.idle_seconds())

    # -- 空闲回收（兜底巡检） -----------------------------------------------

    def _get_sweep_wake(self) -> asyncio.Event:
        """
        巡检用的唤醒信号（懒创建）。

        和 _get_lock() 一样不在 __init__ 里建：模块级的 manager 是在导入时
        创建的，那时还没有事件循环；懒创建可以保证它绑定到真正在跑的那个循环。
        """
        if self._sweep_wake is None:
            self._sweep_wake = asyncio.Event()
        return self._sweep_wake

    def _ensure_sweeper(self) -> None:
        """
        确保后台巡检协程在跑（幂等），并把**正在睡的那个叫醒**。

        巡检**不**依赖客户端连接：它专门负责收拾「客户端已经走了、会话还在跑」
        的残留，所以必须在创建会话时就启动，而不是等 WS 连上来才启动。

        ★ 为什么活着也要叫醒它，而不是直接 return：
        它可能正睡在一个「按当时情况算出来」的长周期里 —— 例如之前一个会话都
        没有（周期 = 60 秒）或所有会话都不限空闲。新会话的 idle_timeout 可能
        远小于那个周期，那样回收最坏要被拖到 60 秒后才发生。
        叫醒它，让它立刻用最新会话集合重算周期。
        """
        task = self._sweep_task
        if task is not None and not task.done():
            self._get_sweep_wake().set()
            return
        try:
            self._sweep_task = asyncio.create_task(self._sweep_loop())
        except RuntimeError as exc:
            # 没有运行中的事件循环。理论上走不到（create() 本身就是协程），
            # 但真发生了就意味着「兜底回收」**整个没有启动**：分离会话只剩
            # 会话自身的看门狗在管。这种事绝不能静默 —— 以前这里是把
            # _sweep_task 置空就完了，出问题时排查不到任何线索。
            self._sweep_task = None
            _warn_sweeper(exc)
        else:
            # 刚起步时也叫醒一次：让第一轮用最新的会话集合算周期，
            # 不必先白等一个「创建前算出来的」周期。
            self._get_sweep_wake().set()

    async def _sweep_loop(self) -> None:
        """
        定期回收空闲会话（分离会话不泄漏的兜底防线）。

        间隔按「所有会话中最小的那个正式空闲时限的 1/4」来定，并夹在 2~60 秒
        之间：idle_timeout_seconds 被调小时回收不会滞后，调大时也不会空转。
        没有任何会话时按 60 秒睡，但**一有新会话就会被 _ensure_sweeper 叫醒**，
        所以不存在「睡太久没人管」的问题（这一点以前只是文档里的说法，
        实现上其实不会叫醒，现已补齐）。
        """
        wake = self._get_sweep_wake()
        try:
            while True:
                # 等到「周期到点」或「有人叫醒我」——两种情况都重新算周期
                try:
                    await asyncio.wait_for(wake.wait(), timeout=self._sweep_interval())
                except asyncio.TimeoutError:
                    pass
                wake.clear()
                if not self._sessions:
                    continue
                try:
                    await self.reap_idle()
                except Exception:  # noqa: BLE001 - 巡检失败不该拖垮服务
                    pass
        except asyncio.CancelledError:
            raise

    def _sweep_interval(self) -> float:
        """算出下一次巡检该等多久（秒）。"""
        timeouts = [
            s.idle_timeout for s in self._sessions.values()
            if s.idle_timeout > 0
        ]
        if not timeouts:
            # 所有会话都不限制空闲（或还没有会话）：久一点巡检没坏处
            return 60.0
        return max(2.0, min(60.0, min(timeouts) / 4.0))

    # -- 关闭 ---------------------------------------------------------------

    async def close_session(self, sid: str) -> None:
        """关闭并注销一个会话（幂等）。"""
        lock = self._get_lock()
        async with lock:
            session = self._sessions.pop(sid, None)

        if session is None:
            return
        try:
            await session.close()
        except Exception:  # noqa: BLE001
            pass

    async def close_all(self) -> None:
        """
        关闭所有会话（服务退出时调用，避免留下孤儿进程）。

        ★ 这里必须连**已分离**的会话一起关掉：客户端断开不再结束会话之后，
        用户关掉浏览器时 cmd.exe 仍在跑；服务退出若不清理，就会留下常驻的
        cmd.exe 孤儿进程（以服务身份运行时还是 SYSTEM）。
        """
        lock = self._get_lock()
        async with lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()

        # 先停巡检，避免它在关闭过程中又去碰已经清空的注册表
        self.stop_sweeper()

        for session in sessions:
            try:
                await session.close()
            except Exception:  # noqa: BLE001
                pass

    def stop_sweeper(self) -> None:
        """停掉后台巡检（服务退出时调用，幂等）。"""
        task = self._sweep_task
        self._sweep_task = None
        # 唤醒信号也一起丢掉：它可能绑定在即将关闭的事件循环上，
        # 下次启动时应该新建一个（见 _get_sweep_wake 的懒创建说明）。
        self._sweep_wake = None
        if task is not None and not task.done():
            task.cancel()

    async def prune(self) -> int:
        """主动清理已结束的会话，返回清理数量（供巡检调用）。"""
        lock = self._get_lock()
        async with lock:
            before = len(self._sessions)
            self._prune_locked()
            return before - len(self._sessions)

    async def reap_idle(self) -> List[str]:
        """
        回收所有「没有客户端连着且已超过空闲时限」的会话，返回被回收的 sid 列表。

        ★ 这是 detached 会话不泄漏的**兜底防线**。会话自身的看门狗已经能做到
        超时回收，但看门狗只在 idle_timeout > 0 时才会被创建，而且它只盯着自己
        那一个会话；从管理器的角度定期扫一遍更稳妥（将来若有人把
        terminal.idle_timeout_seconds 设得很小，也能保证回收是准时的）。

        先持锁把会话摘出来、再在锁外 close()：close() 内部要 await
        杀进程和 taskkill，持锁 await 会把并发创建/查询全部堵住。
        """
        lock = self._get_lock()
        async with lock:
            expired = [
                sid for sid, s in self._sessions.items()
                # 三类都要摘：空闲到期的、**已经被看门狗关掉的**、进程已经结束的。
                #
                # 为什么不能只判 is_idle_expired()：那个方法的语义是
                # 「现在是否到了该被回收的时候」，它在会话已经 close 之后
                # 会返回 **False**（见它的第一行守卫）。于是「看门狗先动手回收」
                # 的会话，巡检永远扫不到、也就永远留在 _sessions 里 ——
                # 白占一个 max_sessions 名额，而且 get() 仍然能取到它。
                # idle_expired 是 @property，读的是「看门狗已经回收过」那个
                # 标志位，注意不要写成 idle_expired()；
                # is_dead() 兜住进程自己退出的情况，三者合起来才不漏。
                if s.is_idle_expired() or s.idle_expired or s.is_dead()
            ]
            sessions = [self._sessions.pop(sid, None) for sid in expired]

        reaped: List[str] = []
        for session in sessions:
            if session is None:
                continue
            try:
                await session.close()
                reaped.append(session.sid)
            except Exception:  # noqa: BLE001
                pass
        return reaped


# 进程级单例：路由层直接 import 使用
manager = TerminalManager()

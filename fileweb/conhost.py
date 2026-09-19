# -*- coding: utf-8 -*-
"""
控制台镜像与输入（方案 A）
==========================

让虚拟桌面能**看见并操作真实桌面上已经开着的命令行窗口**。

和终端（fileweb/terminal.py）的区别，先说清楚
---------------------------------------------
    terminal.py  ：本服务**自己启动**一个 ConPTY 会话（伪控制台）。
                   它天生**没有窗口**，所以真实桌面看不到它。
    本模块       ：去**附着**别人**已经存在**的经典控制台，
                   读它的屏幕、也可以往它的输入缓冲区打字。
                   那扇窗依然属于真实桌面 —— 我们只是看和敲，
                   **不接管、不迁移**（Windows 没有这种能力）。

为什么需要一个独立的辅助进程
----------------------------
要 AttachConsole 到别人的控制台，**本进程自己必须没有控制台**。而服务
进程是有的（用 start.bat 启动时就 attach 在那个控制台上，实测
GetConsoleProcessList 里能看到服务自己的 pid）。服务不能 FreeConsole，
那会丢掉自己的控制台与审计日志输出。

所以这里拉起 fileweb/conhost_helper.py 作为常驻子进程，
通过**行分隔 JSON**（stdin/stdout 管道）与它对话。
详见那个文件开头的协议说明与「踩过的坑」。

它是无状态的
------------
每次请求都是「附加 -> 读/写 -> 分离」，不在辅助进程里保存任何附着状态。
好处：目标进程重启/退出后不需要清理，下一次请求自然报错；
也不需要为每个控制台维护一个长期进程。

★ 安全边界（改动前请先读这段）
------------------------------
1. **这是管理员独占功能。** 它能把任意控制台的屏幕内容（可能含口令、
   令牌、日志）送到浏览器，也能往任意控制台打字。路由层用
   require_admin 强制；**不要**把它开放给子用户。
2. **输入注入默认关闭**（conhost.allow_input）。它没有任何状态反馈：
   我们不知道对方此刻停在什么提示符上，也不知道是不是有人正在用。
3. 每次读/写都写审计日志（谁、从哪个 IP、对哪个 pid 做了什么）。
4. 部署约束：辅助进程必须与目标控制台**在同一个 Windows 会话**，
   且**不能**把本服务装成 NSSM 服务（会变成会话 0，功能整体失效）。
   目标若是「管理员:」控制台，本服务也必须以管理员身份运行。

★ 读到的内容可能**本来就是空的** —— 这不是 bug
------------------------------------------------
读的是控制台的**屏幕缓冲区**。如果那个进程把自己的标准输出重定向走了
（`cmd /c "xxx.bat" > log.txt`、`start /b` 之类），它的输出压根没进控制台
缓冲区，缓冲区里就只有一行提示符甚至全空 —— 实测确认过：明明是同一个
`cmd.exe`，把 stdout 接成 NUL 之后控制台里一行都没有。
真实桌面上那些 `cmd /c "…bat"` 的包装进程大多是这种情况。
所以界面里看到空内容时，先确认对方有没有重定向输出，别急着当成渲染坏了。

另外 `AttachConsole` 对**已经退出的 pid** 返回的是 ERROR_ACCESS_DENIED(5)，
和「权限不足」是同一个码 —— helper 会先查一次进程存活，再把两种原因
分开讲清楚（这里踩过，被"权限"带偏过）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from typing import Any, Dict, List, Optional

IS_WINDOWS = sys.platform.startswith("win")

HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "conhost_helper.py")

# 单次请求的超时（秒）。超时就杀掉辅助进程并重启 —— 否则一个卡死的
# 请求会把后续所有请求一起堵死（锁一直握着），整个功能就永久废了。
REQUEST_TIMEOUT = 8.0

# 枚举控制台时的扫描上限：每个候选 pid 都要 AttachConsole 一次，
# 进程特别多的机器上要有上限，避免一次请求拖太久。
SCAN_LIMIT = 400


class ConhostError(Exception):
    """本模块的统一异常（路由层把它转成 4xx/5xx）。"""


# ---------------------------------------------------------------------------
# 辅助进程
# ---------------------------------------------------------------------------

class _Helper:
    """常驻辅助进程的管理器：拉起、请求、超时重启、收尾。

    整个服务只用一个实例（模块级 _helper）：协议是「一问一答」的，
    用锁串行化即可，不需要为每个控制台各起一个进程。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._proc: Optional[subprocess.Popen] = None
        self._seq = 0

    # -- 生命周期 --
    def _ensure(self) -> subprocess.Popen:
        p = self._proc
        if p is not None and p.poll() is None:
            return p
        if p is not None:
            self._reap(p)
        # ★ 管道是必需的：MSDN 说 AttachConsole 会更新「未被重定向的」
        #   标准句柄指向新控制台 —— 那样 JSON 就会写进目标那个窗口里。
        self._proc = subprocess.Popen(
            [sys.executable, HELPER_PATH],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
            close_fds=True,
        )
        return self._proc

    @staticmethod
    def _reap(p: subprocess.Popen) -> None:
        try:
            if p.stdin:
                p.stdin.close()
        except Exception:                               # noqa: BLE001
            pass
        try:
            p.wait(timeout=3)
        except Exception:                               # noqa: BLE001
            try:
                p.kill()
            except Exception:                           # noqa: BLE001
                pass

    def _kill(self) -> None:
        p, self._proc = self._proc, None
        if p is not None:
            try:
                p.kill()
            except Exception:                           # noqa: BLE001
                pass
            self._reap(p)

    def stop(self) -> None:
        """服务退出时收尾（app.py 的 lifespan 调用）。"""
        with self._lock:
            self._kill()

    # -- 请求 --
    def request(self, op: str, timeout: float = REQUEST_TIMEOUT,
                **kw: Any) -> Dict[str, Any]:
        with self._lock:
            proc = self._ensure()
            self._seq += 1
            rid = self._seq
            req = {"id": rid, "op": op}
            req.update(kw)
            try:
                proc.stdin.write(json.dumps(req) + "\n")
                proc.stdin.flush()
            except Exception as exc:                    # noqa: BLE001
                self._kill()
                raise ConhostError("辅助进程写入失败：%s" % exc) from exc

            # 读一行；用线程 + join 实现超时（Windows 上没法对管道做 select）
            box: Dict[str, Any] = {}

            def _read() -> None:
                try:
                    box["line"] = proc.stdout.readline()
                except Exception as exc:                # noqa: BLE001
                    box["err"] = exc

            t = threading.Thread(target=_read, daemon=True,
                                 name="conhost-helper-read")
            t.start()
            t.join(timeout)

            if t.is_alive():
                # 杀掉进程会让 readline 立刻返回，那个线程自己会结束
                self._kill()
                raise ConhostError("辅助进程超时（%.0f 秒），已重启"
                                   % timeout)
            if "err" in box:
                self._kill()
                raise ConhostError("辅助进程读取失败：%s" % box["err"])
            line = box.get("line") or ""
            if not line.strip():
                self._kill()
                raise ConhostError("辅助进程意外退出")

            try:
                resp = json.loads(line)
            except Exception as exc:                    # noqa: BLE001
                raise ConhostError("辅助进程返回了非 JSON 内容：%s"
                                   % line[:200]) from exc
            if not resp.get("ok"):
                raise ConhostError(str(resp.get("error") or "未知错误"))
            return resp


_helper = _Helper()


def shutdown() -> None:
    """服务退出：停掉辅助进程（避免留下孤儿）。"""
    _helper.stop()


# ---------------------------------------------------------------------------
# 能力探测
# ---------------------------------------------------------------------------

def available(cfg: Dict[str, Any]) -> tuple:
    """返回 (是否可用, 原因)。给路由层与「关于」对话框用。"""
    section = (cfg or {}).get("conhost") or {}
    if not section.get("enabled", False):
        return False, "控制台镜像已在服务端关闭（config.json 的 conhost.enabled = false）"
    if not IS_WINDOWS:
        return False, "只有 Windows 才有经典控制台窗口，当前平台不支持"
    if not os.path.isfile(HELPER_PATH):
        return False, "缺少辅助进程脚本：%s" % HELPER_PATH
    return True, ""


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------

def _process_rows() -> List[Dict[str, Any]]:
    """列出候选进程（用 psutil；它已经是本项目的依赖）。"""
    try:
        import psutil
    except Exception:                                   # noqa: BLE001
        return []
    me = os.getpid()
    rows = []
    for p in psutil.process_iter(["pid", "name", "username", "create_time"]):
        info = p.info
        pid = int(info.get("pid") or 0)
        if pid <= 4 or pid == me:
            continue                                    # System / Idle / 自己
        rows.append({
            "pid": pid,
            "name": info.get("name") or "",
            "user": info.get("username") or "",
            "started": info.get("create_time") or 0,
        })
    rows.sort(key=lambda r: r["pid"])
    return rows[:SCAN_LIMIT]


def _own_console_pids() -> set:
    """本服务的终端会话（ConPTY）所对应的 cmd.exe —— 它们没有窗口，
    列出来只会让人困惑，所以标记出来由前端隐藏。"""
    try:
        import psutil
        me = psutil.Process(os.getpid())
        return {c.pid for c in me.children(recursive=True)}
    except Exception:                                   # noqa: BLE001
        return set()


def _window_state() -> Dict[int, Dict[str, Any]]:
    """枚举经典控制台窗口：hwnd -> {title, visible, minimized}。

    这一步**不需要**附加任何控制台，纯 user32 调用。
    """
    if not IS_WINDOWS:
        return {}
    import ctypes
    import ctypes.wintypes as wt

    u32 = ctypes.WinDLL("user32", use_last_error=True)
    u32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
    u32.IsWindowVisible.argtypes = [wt.HWND]
    u32.IsIconic.argtypes = [wt.HWND]

    out: Dict[int, Dict[str, Any]] = {}
    CB = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    def _cb(hwnd, _lp):
        cls = ctypes.create_unicode_buffer(256)
        u32.GetClassNameW(hwnd, cls, 256)
        if cls.value != "ConsoleWindowClass":
            return True
        title = ctypes.create_unicode_buffer(512)
        u32.GetWindowTextW(hwnd, title, 512)
        out[int(hwnd)] = {
            "title": title.value,
            "visible": bool(u32.IsWindowVisible(hwnd)),
            "minimized": bool(u32.IsIconic(hwnd)),
        }
        return True

    u32.EnumWindows(CB(_cb), 0)
    return out


def _helper_pid() -> int:
    """辅助进程自己的 pid。

    它在扫描期间会依次附加到**每个**控制台上，所以会出现在每个
    GetConsoleProcessList 的结果里。聚合控制台时必须把它滤掉，
    否则「成员集合」这个聚合键会被一个无关进程污染。
    """
    try:
        return int(_helper.request("ping").get("helper_pid") or 0)
    except Exception:                                   # noqa: BLE001
        return 0


def list_consoles(cfg: Dict[str, Any], include_own: bool = False) -> Dict[str, Any]:
    """
    列出真实桌面上的控制台（按**控制台**聚合，不是按进程）。

    ★ 为什么要聚合：
      一个命令行窗口里往往挂着好几个进程（cmd.exe 启动了 python.exe /
      node.exe …）。实测一台机器上 197 个进程里有 16 个进程带着控制台，
      但真正的**窗口只有几个** —— 逐个进程列出来会让人以为开了十几个窗口，
      而「主机上有几个命令行窗口」恰恰是这个界面要回答的问题。

      聚合键用 GetConsoleProcessList 的**成员集合**，而不是窗口句柄：
      后者对「没有窗口的后台控制台」一律是 0，会把它们错误地并成一个。

    窗口 ↔ 进程的映射为什么不能用「conhost 的父进程」推断：实测那返回的是
    **启动者**（explorer.exe / svchost.exe），不是拥有该控制台的那个程序。
    所以权威做法是逐个 AttachConsole 再取 GetConsoleWindow()（辅助进程里做）。
    """
    ok, reason = available(cfg)
    if not ok:
        raise ConhostError(reason)

    procs = _process_rows()
    own = _own_console_pids()
    scanned = _helper.request("scan", pids=[r["pid"] for r in procs])
    by_pid = {int(r["pid"]): r for r in procs}
    wins = _window_state()
    helper = _helper_pid()

    groups: Dict[str, Dict[str, Any]] = {}
    for row in scanned.get("items") or []:
        members_set = {int(x) for x in (row.get("console_pids") or [])}
        members_set.discard(helper)                     # 见 _helper_pid 的说明
        if not members_set:
            continue                                    # 这个进程没有控制台
        key = ",".join(str(x) for x in sorted(members_set))
        pid = int(row.get("pid") or 0)
        info = by_pid.get(pid, {})

        g = groups.get(key)
        if g is None:
            hwnd = int(row.get("hwnd") or 0)
            win = wins.get(hwnd) or {}
            g = {
                "key": key,
                "hwnd": hwnd,
                "has_window": bool(hwnd),
                "title": row.get("title") or win.get("title") or "",
                "visible": bool(win.get("visible")),
                "minimized": bool(win.get("minimized")),
                "members": [],
                "pid": pid,
            }
            groups[key] = g
        g["members"].append({
            "pid": pid,
            "name": info.get("name") or "",
            "user": info.get("user") or "",
            "ours": pid in own,
        })
        # 读/写用哪个 pid：优先命令行解释器（cmd / powershell / pwsh）。
        # 其实同一控制台里任何一个成员都能读，选解释器只是让界面更直观。
        if (info.get("name") or "").lower() in (
                "cmd.exe", "powershell.exe", "pwsh.exe"):
            g["pid"] = pid

    items = list(groups.values())
    for g in items:
        g["ours"] = bool(g["members"]) and all(m["ours"] for m in g["members"])
        g["member_count"] = len(g["members"])
        g["members"].sort(key=lambda m: (0 if m["pid"] == g["pid"] else 1, m["pid"]))

    if not include_own:
        items = [g for g in items if not g["ours"]]

    # 有窗口的排前面（那才是用户眼里的「一个命令行窗口」），再按 pid
    items.sort(key=lambda g: (0 if g["has_window"] else 1, g["pid"]))
    return {
        "ok": True,
        "count": len(items),
        "windows": sum(1 for g in items if g["has_window"]),
        "items": items,
        "scanned": len(procs),
        "allow_input": bool((cfg.get("conhost") or {}).get("allow_input", False)),
        "ts": time.time(),
    }


# ---------------------------------------------------------------------------
# 读 / 写
# ---------------------------------------------------------------------------

def read_console(cfg: Dict[str, Any], pid: int, mode: str = "log",
                 lines: int = 200) -> Dict[str, Any]:
    ok, reason = available(cfg)
    if not ok:
        raise ConhostError(reason)
    if int(pid) <= 0:
        raise ConhostError("pid 不合法")
    return _helper.request("read", pid=int(pid), mode=mode, lines=int(lines))


def write_console(cfg: Dict[str, Any], pid: int, text: str = "",
                  keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    ★ 往目标控制台打字。**默认关闭**，要在 config.json 里显式打开
    conhost.allow_input 才会生效。
    """
    section = (cfg or {}).get("conhost") or {}
    ok, reason = available(cfg)
    if not ok:
        raise ConhostError(reason)
    if not section.get("allow_input", False):
        raise ConhostError(
            "输入注入已关闭（config.json 的 conhost.allow_input = false）。"
            "这个功能会往真实窗口里真的打字，默认不开。")
    if int(pid) <= 0:
        raise ConhostError("pid 不合法")

    # 文本长度上限：一次敲几万个字符没有意义，而且可能把目标程序灌死
    limit = int(section.get("max_input_chars") or 2000)
    if text and len(text) > limit:
        raise ConhostError("一次最多输入 %d 个字符（当前 %d）" % (limit, len(text)))
    return _helper.request("write", pid=int(pid), text=text or "",
                           keys=list(keys or []))

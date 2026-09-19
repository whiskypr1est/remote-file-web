# -*- coding: utf-8 -*-
"""
控制台读写的**辅助进程**（方案 A 的引擎）
==========================================

这个文件被 fileweb/conhost.py 以子进程方式拉起，通过 stdin/stdout
（**行分隔的 JSON**）对话。它单独存在的唯一原因：

    ★ 要 AttachConsole 到别人的控制台，本进程自己**必须没有控制台**。
      而服务进程是有的（用 start.bat 启动时它就 attach 在那个控制台上 ——
      实测 GetConsoleProcessList 里能看到服务自己的 pid）。
      所以服务不能自己干这件事：FreeConsole() 会让服务丢掉自己的控制台与
      日志输出。于是必须另起一个进程，由它 FreeConsole() 之后再附加。

协议（每行一个 JSON 对象）
--------------------------
请求：
    {"id":1,"op":"ping"}
    {"id":2,"op":"scan","pids":[123,456]}
    {"id":3,"op":"read","pid":123,"mode":"log","lines":200}
    {"id":4,"op":"write","pid":123,"text":"stop\\r"}
    {"id":5,"op":"write","pid":123,"keys":["ctrl-c"]}
    {"id":6,"op":"info","pid":123}
应答：
    {"id":1,"ok":true,...}  或  {"id":1,"ok":false,"error":"..."}

踩过的坑（每一条都真的吃过一次，别改回去）
------------------------------------------
1. **必须先 FreeConsole()。**
   实测：给子进程加 DETACHED_PROCESS **不足以**让它「没有控制台」——
   控制台子系统的程序即使 DETACHED 也会被分配一个新控制台，
   于是 AttachConsole 报 ERROR_ACCESS_DENIED(5)。
   这个错误码非常容易误判成「权限不足」，其实是「我已经有控制台了」。
   （另一种可行做法是用 GUI 子系统的 pythonw.exe，但 FreeConsole 更通用。）

2. **所有 Win32 调用都要声明 argtypes/restype。**
   不声明时 ctypes 把返回值当 32 位 int，而 HANDLE 是 64 位。
   实测同一段读取代码：没声明签名时报了一个毫不相干的 err=534 而失败，
   补齐签名后**四种区间全部成功**。这是典型的「有时能用有时不能用」。

3. **AttachConsole 成功后 GetLastError 仍是过期值（6）。**
   必须看 BOOL 返回值，不能看 last error。

4. **标准句柄必须重定向。**
   MSDN：AttachConsole 会更新「未被重定向的」标准句柄指向新控制台。
   如果 helper 的 stdout 没被重定向，附加之后 JSON 就会写进目标控制台里
   （用户会在那个窗口里看到一串乱码）。调用方用 PIPE 拉起它即满足此条。

5. **中文全角字符占两个单元格。**
   全角字的第二个单元格带 COMMON_LVB_TRAILING_BYTE(0x0200) 标记。
   不按属性跳过尾格，屏幕上每个汉字都会**出现两遍**（本本机机访访问问）。

6. **ctypes 下 NULL 句柄是 None 而不是 0。**
   直接拿去 %d 格式化会抛 TypeError。

7. **MapVirtualKeyW 是 user32 导出的，不在 kernel32。**
   绑成 kernel32 会在导入模块时就 AttributeError（不是运行到才报）。

安全边界
--------
* 本文件**不自己做任何鉴权**：它只监听父进程的管道，是父进程的私有工具。
  鉴权、审计、开关全在 fileweb/routers/conhost.py 与 conhost.py 那一侧。
* op=write 会**真的往别人正在用的窗口里打字**。是否允许由服务端配置决定
  （conhost.allow_input，默认关闭）；helper 只负责照做。
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes as wt
import json
import os
import sys
import time

# ---------------------------------------------------------------------------
# Win32 绑定
# ---------------------------------------------------------------------------

k32 = ctypes.WinDLL("kernel32", use_last_error=True)
u32 = ctypes.WinDLL("user32", use_last_error=True)

TRAILING_BYTE = 0x0200          # COMMON_LVB_TRAILING_BYTE：全角字符的尾格


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", ctypes.c_ushort), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


class CHAR_INFO(ctypes.Structure):
    _fields_ = [("Char", ctypes.c_wchar), ("Attributes", ctypes.c_ushort)]


class _CharUnion(ctypes.Union):
    _fields_ = [("UnicodeChar", ctypes.c_wchar), ("AsciiChar", ctypes.c_char)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wt.BOOL), ("wRepeatCount", wt.WORD),
                ("wVirtualKeyCode", wt.WORD), ("wVirtualScanCode", wt.WORD),
                ("uChar", _CharUnion), ("dwControlKeyState", wt.DWORD)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wt.WORD), ("Event", KEY_EVENT_RECORD)]


# --- 函数签名（见模块注释第 2 条：不声明就会出玄学问题） ---
k32.GetConsoleWindow.argtypes = []
k32.GetConsoleWindow.restype = wt.HWND
k32.FreeConsole.argtypes = []
k32.FreeConsole.restype = wt.BOOL
k32.AttachConsole.argtypes = [wt.DWORD]
k32.AttachConsole.restype = wt.BOOL
k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
k32.OpenProcess.restype = wt.HANDLE
k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
k32.WaitForSingleObject.restype = wt.DWORD
k32.GetConsoleProcessList.argtypes = [ctypes.POINTER(wt.DWORD), wt.DWORD]
k32.GetConsoleProcessList.restype = wt.DWORD
k32.CreateFileW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p,
                            wt.DWORD, wt.DWORD, wt.HANDLE]
k32.CreateFileW.restype = wt.HANDLE
k32.CloseHandle.argtypes = [wt.HANDLE]
k32.CloseHandle.restype = wt.BOOL
k32.GetConsoleScreenBufferInfo.argtypes = [
    wt.HANDLE, ctypes.POINTER(CONSOLE_SCREEN_BUFFER_INFO)]
k32.GetConsoleScreenBufferInfo.restype = wt.BOOL
k32.ReadConsoleOutputW.argtypes = [wt.HANDLE, ctypes.POINTER(CHAR_INFO),
                                   COORD, COORD, ctypes.POINTER(SMALL_RECT)]
k32.ReadConsoleOutputW.restype = wt.BOOL
k32.WriteConsoleInputW.argtypes = [wt.HANDLE, ctypes.POINTER(INPUT_RECORD),
                                   wt.DWORD, ctypes.POINTER(wt.DWORD)]
k32.WriteConsoleInputW.restype = wt.BOOL

u32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]
u32.GetWindowTextW.restype = ctypes.c_int
u32.GetWindowTextLengthW.argtypes = [wt.HWND]
u32.GetWindowTextLengthW.restype = ctypes.c_int
u32.VkKeyScanW.argtypes = [ctypes.c_wchar]
u32.VkKeyScanW.restype = ctypes.c_short
# ★ 见模块注释第 7 条：这个函数在 user32，不在 kernel32
u32.MapVirtualKeyW.argtypes = [wt.UINT, wt.UINT]
u32.MapVirtualKeyW.restype = wt.UINT

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
OPEN_EXISTING = 3
SHARE_ALL = 3

KEY_EVENT = 0x0001

VK_BACK, VK_TAB, VK_RETURN, VK_ESCAPE = 0x08, 0x09, 0x0D, 0x1B
VK_LEFT, VK_UP, VK_RIGHT, VK_DOWN = 0x25, 0x26, 0x27, 0x28
VK_HOME, VK_END, VK_DELETE, VK_INSERT = 0x24, 0x23, 0x2E, 0x2D
VK_PRIOR, VK_NEXT, VK_SPACE = 0x21, 0x22, 0x20
VK_PACKET = 0xE7               # 传任意 Unicode 字符的标准做法

LEFT_CTRL_PRESSED, RIGHT_CTRL_PRESSED = 0x0008, 0x0004
LEFT_ALT_PRESSED, RIGHT_ALT_PRESSED = 0x0002, 0x0001
SHIFT_PRESSED = 0x0010

# 具名按键：前端点「功能键」按钮时用
NAMED_KEYS = {
    "enter": (VK_RETURN, "\r", 0),
    "tab": (VK_TAB, "\t", 0),
    "esc": (VK_ESCAPE, "\x1b", 0),
    "backspace": (VK_BACK, "\b", 0),
    "delete": (VK_DELETE, "\x00", 0),
    "insert": (VK_INSERT, "\x00", 0),
    "home": (VK_HOME, "\x00", 0),
    "end": (VK_END, "\x00", 0),
    "up": (VK_UP, "\x00", 0),
    "down": (VK_DOWN, "\x00", 0),
    "left": (VK_LEFT, "\x00", 0),
    "right": (VK_RIGHT, "\x00", 0),
    "pageup": (VK_PRIOR, "\x00", 0),
    "pagedown": (VK_NEXT, "\x00", 0),
    "space": (VK_SPACE, " ", 0),
    "ctrl-c": (0x43, "\x03", LEFT_CTRL_PRESSED),
    "ctrl-break": (0x03, "\x03", LEFT_CTRL_PRESSED),
    "ctrl-d": (0x44, "\x04", LEFT_CTRL_PRESSED),
    "ctrl-z": (0x5A, "\x1A", LEFT_CTRL_PRESSED),
}

MAX_READ_LINES = 4000           # 单次最多读多少行，防前端要一个天量区间


def _h(v):
    """ctypes 下 NULL 句柄是 None，统一成 int 方便比较/返回（见注释第 6 条）。"""
    return 0 if v is None else int(v)


SYNCHRONIZE = 0x00100000
WAIT_TIMEOUT = 0x00000102


def pid_alive(pid):
    """目标进程还在不在。

    ★ 为什么值得单独做一次查询：AttachConsole 对**已经退出的 pid** 返回的是
      ERROR_ACCESS_DENIED(5)，和「对方是管理员权限的控制台」**一模一样**。
      不区分这两种情况，排查时会被"权限"带偏很久 —— 而实际情况往往是
      目标只是已经退出了（实测就踩到：把子进程的 stdin 接成 DEVNULL 会让
      cmd /k 立刻读到 EOF 然后退出）。
    """
    h = k32.OpenProcess(SYNCHRONIZE, False, int(pid))
    if not h:
        return False
    try:
        return k32.WaitForSingleObject(h, 0) == WAIT_TIMEOUT
    finally:
        k32.CloseHandle(h)


class Attached:
    """上下文管理器：进入时附加到目标控制台，退出时一定分离。

    每次请求都重新附加（无状态）是有意为之：
      * 目标进程重启/退出后不需要任何清理，下一次请求自然报错；
      * 不必为每个控制台维护一个长期进程。
    """

    def __init__(self, pid):
        self.pid = int(pid)
        self.hwnd = 0

    def __enter__(self):
        k32.FreeConsole()                       # 见注释第 1、3 条
        if not k32.AttachConsole(wt.DWORD(self.pid)):
            err = ctypes.get_last_error()
            raise OSError("附加失败 pid=%d err=%d%s"
                          % (self.pid, err, self._hint(err)))
        self.hwnd = _h(k32.GetConsoleWindow())
        return self

    def _hint(self, err):
        """
        把错误码翻译成能直接照着排查的话。

        ★ 重点是 err=5：它对「目标已经退出」和「目标是管理员控制台」
          返回的是**同一个码**，所以必须先查存活再下结论 ——
          否则会把人往"权限"上带偏（实测就踩过：目标其实只是退出了）。
        """
        if err == 5:
            if not pid_alive(self.pid):
                return ("（目标进程**已经不在了**。AttachConsole 对已退出的 pid "
                        "也返回 ACCESS_DENIED，所以别往权限上查）")
            return ("（目标存在但被拒：多半是它以管理员身份运行，"
                    "而本服务不是 —— 需要两边权限级别一致）")
        if err == 6:
            return "（目标进程没有控制台）"
        if err == 87:
            return "（pid 不存在）"
        return ""

    def __exit__(self, *exc):
        k32.FreeConsole()
        return False

    def out(self):
        h = k32.CreateFileW("CONOUT$", GENERIC_READ | GENERIC_WRITE, SHARE_ALL,
                            None, OPEN_EXISTING, 0, None)
        if _h(h) in (0, 0xFFFFFFFFFFFFFFFF):
            raise OSError("打开 CONOUT$ 失败 err=%d" % ctypes.get_last_error())
        return h

    def inp(self):
        h = k32.CreateFileW("CONIN$", GENERIC_READ | GENERIC_WRITE, SHARE_ALL,
                            None, OPEN_EXISTING, 0, None)
        if _h(h) in (0, 0xFFFFFFFFFFFFFFFF):
            raise OSError("打开 CONIN$ 失败 err=%d" % ctypes.get_last_error())
        return h

    def title(self):
        if not self.hwnd:
            return ""
        n = u32.GetWindowTextLengthW(wt.HWND(self.hwnd))
        buf = ctypes.create_unicode_buffer(n + 2)
        u32.GetWindowTextW(wt.HWND(self.hwnd), buf, n + 2)
        return buf.value

    def processes(self):
        arr = (wt.DWORD * 256)()
        n = k32.GetConsoleProcessList(arr, 256)
        n = max(0, min(int(n), 256))
        return [int(arr[i]) for i in range(n)]

    def buffer_info(self, h):
        bi = CONSOLE_SCREEN_BUFFER_INFO()
        if not k32.GetConsoleScreenBufferInfo(h, ctypes.byref(bi)):
            raise OSError("GetConsoleScreenBufferInfo 失败 err=%d"
                          % ctypes.get_last_error())
        return bi


def _attr_runs(attrs):
    """把一行的每格属性压成「变化段」列表，减少传输量。

    返回 [[起始序号, 长度, 颜色属性], ...]；全行一致时只返回一段。
    前端据此上色（MC 服务器的日志就是带色的）。
    """
    runs = []
    for idx, a in enumerate(attrs):
        if runs and runs[-1][2] == a:
            runs[-1][1] += 1
        else:
            runs.append([idx, 1, a])
    return runs


def _read_region(h, cols, top, bottom):
    """读 [top,bottom] 行，返回 (文本行, 属性段)；正确处理全角尾格。"""
    rows = bottom - top + 1
    if rows <= 0:
        return [], []
    buf = (CHAR_INFO * (cols * rows))()
    region = SMALL_RECT(0, top, cols - 1, bottom)
    if not k32.ReadConsoleOutputW(h, buf, COORD(cols, rows), COORD(0, 0),
                                  ctypes.byref(region)):
        raise OSError("ReadConsoleOutputW 失败 err=%d" % ctypes.get_last_error())

    lines, runs = [], []
    for r in range(rows):
        # 见注释第 5 条：跳过全角尾格，否则中文每个字出现两遍
        keep = [(c, buf[r * cols + c]) for c in range(cols)
                if not (buf[r * cols + c].Attributes & TRAILING_BYTE)]
        text = "".join((ch.Char if ch.Char != "\x00" else " ") for _c, ch in keep)
        lines.append(text.rstrip())
        # 跳过尾格之后列号不再连续，所以按「保留顺序」重新编号：
        # 前端只关心着色区间，不关心物理列。
        runs.append(_attr_runs([ch.Attributes & 0x00FF for _c, ch in keep]))
    return lines, runs


def op_ping(_req):
    return {"ok": True, "helper_pid": os.getpid(),
            "console": _h(k32.GetConsoleWindow()),
            "python": sys.version.split()[0]}


def op_scan(req):
    """权威的「窗口 <-> 进程」映射。

    做法：对每个候选 pid 依次 AttachConsole 并取 GetConsoleWindow()。
    ★ 不要用「conhost 的父进程」来推断客户端 —— 实测那是**启动者**
      （explorer.exe / svchost.exe），不是真正拥有该控制台的那个程序。
    每个 pid 都会 FreeConsole，彼此独立。
    """
    out = []
    for pid in (req.get("pids") or [])[:400]:
        entry = {"pid": int(pid), "hwnd": 0, "title": "", "console_pids": []}
        try:
            with Attached(pid) as at:
                entry["hwnd"] = at.hwnd
                entry["title"] = at.title()
                entry["console_pids"] = at.processes()
        except Exception as exc:                        # noqa: BLE001
            entry["error"] = str(exc)
        out.append(entry)
    return {"ok": True, "items": out}


def op_info(req):
    with Attached(req["pid"]) as at:
        h = at.out()
        try:
            bi = at.buffer_info(h)
        finally:
            k32.CloseHandle(h)
        return {
            "ok": True,
            "hwnd": at.hwnd,
            "title": at.title(),
            "console_pids": at.processes(),
            "cols": bi.dwSize.X, "buffer_rows": bi.dwSize.Y,
            "cursor": {"x": bi.dwCursorPosition.X, "y": bi.dwCursorPosition.Y},
            "window": {"top": bi.srWindow.Top, "bottom": bi.srWindow.Bottom},
        }


def op_read(req):
    """
    读内容。两种口径：

      mode="log"    光标往上 N 行（滚动日志的正经视图，MC 这类程序用这个）
      mode="screen" 可见窗口那一块（所见即所得；跑全屏 TUI 时用这个）
    """
    mode = str(req.get("mode") or "log")
    want = max(1, min(int(req.get("lines") or 200), MAX_READ_LINES))

    with Attached(req["pid"]) as at:
        h = at.out()
        try:
            bi = at.buffer_info(h)
            cols = max(1, int(bi.dwSize.X))
            buffer_rows = max(1, int(bi.dwSize.Y))
            cur = max(0, min(int(bi.dwCursorPosition.Y), buffer_rows - 1))
            if mode == "screen":
                top = max(0, int(bi.srWindow.Top))
                bottom = min(int(bi.srWindow.Bottom), buffer_rows - 1)
            else:
                top = max(0, cur - want + 1)
                bottom = cur
            lines, runs = _read_region(h, cols, top, bottom)
            info = {
                "cols": cols, "buffer_rows": buffer_rows,
                "cursor": {"x": int(bi.dwCursorPosition.X), "y": cur},
                "window": {"top": int(bi.srWindow.Top),
                           "bottom": int(bi.srWindow.Bottom)},
            }
        finally:
            k32.CloseHandle(h)

        title, hwnd = at.title(), at.hwnd

    # 尾部空行对日志视图没意义，去掉（中间的空行保留）
    while lines and not lines[-1].strip():
        lines.pop()
        runs.pop()
    trimmed = 0
    while trimmed < len(lines) - 1 and not lines[trimmed].strip():
        trimmed += 1
    if trimmed:
        lines = lines[trimmed:]
        runs = runs[trimmed:]

    info.update({
        "ok": True, "mode": mode, "rows": len(lines),
        "first_row": top + trimmed,
        "hwnd": hwnd, "title": title,
        "lines": lines, "runs": runs,
        "ts": time.time(),
    })
    return info


def _records_for_char(ch):
    """把一个字符变成 keyDown + keyUp 两条记录。"""
    vk = u32.VkKeyScanW(ch)
    shift = 0
    if vk == -1:
        # 非 ASCII（中文等）：VkKeyScan 认不出来，用 VK_PACKET 传 Unicode
        vk, scan = VK_PACKET, 0
    else:
        shift = (vk >> 8) & 0xFF
        vk = vk & 0xFF
        scan = u32.MapVirtualKeyW(wt.UINT(vk), 0)
    ctrl = SHIFT_PRESSED if (shift & 1) else 0

    recs = []
    for down in (1, 0):
        r = INPUT_RECORD()
        r.EventType = KEY_EVENT
        r.Event.bKeyDown = down
        r.Event.wRepeatCount = 1
        r.Event.wVirtualKeyCode = vk
        r.Event.wVirtualScanCode = scan
        r.Event.uChar.UnicodeChar = ch
        r.Event.dwControlKeyState = ctrl
        recs.append(r)
    return recs


def _records_for_named(name):
    key = str(name or "").lower()
    if key not in NAMED_KEYS:
        raise ValueError("未知按键: %s" % name)
    vk, ch, ctrl = NAMED_KEYS[key]
    scan = u32.MapVirtualKeyW(wt.UINT(vk), 0)
    recs = []
    for down in (1, 0):
        r = INPUT_RECORD()
        r.EventType = KEY_EVENT
        r.Event.bKeyDown = down
        r.Event.wRepeatCount = 1
        r.Event.wVirtualKeyCode = vk
        r.Event.wVirtualScanCode = scan
        r.Event.uChar.UnicodeChar = ch
        r.Event.dwControlKeyState = ctrl
        recs.append(r)
    return recs


def op_write(req):
    """
    ★ 往目标控制台**真的敲字**。

    写的是控制台的**输入缓冲区**，前景进程（cmd.exe 等）会像用户真的敲了
    一样读到它。风险在于：没有任何状态反馈 —— 我们不知道那边此刻停在
    什么提示符上，也不知道是不是有人正在用同一台机器。
    """
    recs = []
    if req.get("text"):
        for ch in str(req["text"]):
            recs.extend(_records_for_char(ch))
    for name in (req.get("keys") or []):
        recs.extend(_records_for_named(name))
    if not recs:
        return {"ok": True, "written": 0, "records": 0}

    with Attached(req["pid"]) as at:
        h = at.inp()
        try:
            arr = (INPUT_RECORD * len(recs))(*recs)
            written = wt.DWORD(0)
            if not k32.WriteConsoleInputW(h, arr, len(recs),
                                          ctypes.byref(written)):
                raise OSError("WriteConsoleInputW 失败 err=%d"
                              % ctypes.get_last_error())
            n = int(written.value)
        finally:
            k32.CloseHandle(h)
    return {"ok": True, "written": n, "records": len(recs)}


OPS = {"ping": op_ping, "scan": op_scan, "info": op_info,
       "read": op_read, "write": op_write}


def main():
    """主循环：一行一个请求，一行一个应答。"""
    k32.FreeConsole()               # 见注释第 1 条

    out = sys.stdout
    for raw in sys.stdin:
        raw = raw.strip()
        if not raw:
            continue
        req_id = None
        try:
            req = json.loads(raw)
            req_id = req.get("id")
            op = str(req.get("op") or "")
            fn = OPS.get(op)
            if fn is None:
                raise ValueError("未知操作: %s" % op)
            resp = fn(req)
        except Exception as exc:                        # noqa: BLE001
            resp = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        resp["id"] = req_id
        out.write(json.dumps(resp, ensure_ascii=False) + "\n")
        out.flush()


if __name__ == "__main__":
    main()

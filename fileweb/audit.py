# -*- coding: utf-8 -*-
"""
审计日志
========

多用户之后，「谁在什么时候做了什么」必须能事后查证 —— 一份**落盘的追加日志**
比任何界面都可靠，因为它在服务重启、甚至用户被删掉之后依然存在。

记录什么
--------
登录成功 / 登录失败 / 登出 / 改密码 / 管理员改别人的账号 / 停用 / 启用 /
强制下线。**不记录**文件浏览、上传下载这些高频动作：那会把日志淹掉，
而且真实的取证需求集中在「账号层面的动作」上。

为什么是 JSONL
--------------
一行一个 JSON 对象。追加写、不需要读全文、单行损坏不影响其余记录、
用任何工具（记事本 / jq / Python）都能读。与之相对，一个整体的 JSON 数组
每次写入都要重写全文，日志一多就不可行。

文件与轮转
----------
按大小轮转成 `audit.log.jsonl.1` / `.2` / …，最多保留 `backups` 份。
读取接口只回**当前文件**的最近 N 条 —— 出问题时要看的是最近发生了什么，
不是三个月前的陈年旧账（那些仍然留在轮转文件里，需要时人工查）。

★ 写失败绝不能影响业务
--------------------
磁盘满、权限被改、路径被占……这些都不该让「登录」失败，也不该让「改密码」
失败。所以 log() 把所有异常吞掉并打一行警告到控制台：审计是**尽力而为**的
旁路，不是关键路径。反过来（为了写日志而拒绝服务）会造成更糟的可用性问题。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional

# 基础路径（相对路径由 config.prepare 解析成绝对路径后下发，与 userstate 同套写法）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
AUDIT_PATH = os.path.join(BASE_DIR, "audit.log.jsonl")

ACTIVE_PATH = AUDIT_PATH
MAX_BYTES = 4 * 1024 * 1024        # 单个文件上限，超出就轮转
BACKUPS = 3                        # 保留几个历史文件

_LOCK = threading.RLock()

# 事件名（字符串常量，避免各处手敲拼错）。前端按这些值做中文映射。
EVENT_LOGIN_OK = "login_ok"
EVENT_LOGIN_FAIL = "login_fail"
EVENT_LOGOUT = "logout"
EVENT_PASSWORD_CHANGE = "password_change"
EVENT_USER_CREATE = "user_create"
EVENT_USER_UPDATE = "user_update"
EVENT_USER_DISABLE = "user_disable"
EVENT_USER_ENABLE = "user_enable"
EVENT_USER_KICK = "user_kick"
EVENT_USER_PASSWORD_RESET = "user_password_reset"
# ★ 控制台镜像（方案 A）。这两条是**高危**动作：
#   conhost_read  —— 读了真实桌面上某个控制台的屏幕内容；
#   conhost_input —— 往那个控制台里注入了按键（等于替人敲键盘）。
#   读会被前端按秒轮询，所以**不逐次记录**（那会把日志淹掉），
#   只在「打开某个控制台的镜像」时记一次；输入则每次都记。
EVENT_CONHOST_READ = "conhost_read"
EVENT_CONHOST_INPUT = "conhost_input"


def set_path(path: str) -> None:
    """由 config.prepare() 调用：指定日志文件位置。"""
    global ACTIVE_PATH
    ACTIVE_PATH = str(path or "") or AUDIT_PATH


def configure(max_bytes: Optional[int] = None,
              backups: Optional[int] = None) -> None:
    """由 config.prepare() 调用：轮转参数（不配就用默认值）。"""
    global MAX_BYTES, BACKUPS
    try:
        if max_bytes is not None:
            MAX_BYTES = max(64 * 1024, int(max_bytes))
    except (TypeError, ValueError):
        pass
    try:
        if backups is not None:
            BACKUPS = max(0, min(20, int(backups)))
    except (TypeError, ValueError):
        pass


def _path() -> str:
    return ACTIVE_PATH or AUDIT_PATH


def _rotate_locked(path: str) -> None:
    """
    把当前文件轮转成 .1（调用方需持锁）。

    从最旧的一份开始改名，这样不会覆盖还没让位的文件。
    BACKUPS = 0 表示不保留历史，直接删掉当前文件重新开始。
    """
    if BACKUPS <= 0:
        try:
            os.unlink(path)
        except OSError:
            pass
        return

    oldest = "%s.%d" % (path, BACKUPS)
    try:
        os.unlink(oldest)
    except OSError:
        pass

    for index in range(BACKUPS - 1, 0, -1):
        src = "%s.%d" % (path, index)
        if os.path.isfile(src):
            try:
                os.replace(src, "%s.%d" % (path, index + 1))
            except OSError:
                pass

    try:
        os.replace(path, "%s.1" % path)
    except OSError:
        pass


def log(event: str, username: str = "", ip: str = "",
        result: str = "ok", detail: str = "") -> None:
    """
    追加一条审计记录。

    :param event:   事件名，用本模块的 EVENT_* 常量。
    :param username: 与这条记录相关的人（登录失败时就是**尝试登录的那个名字**）。
    :param result:  "ok" / "fail"。
    :param detail:  可读的补充说明（失败原因、改了哪些字段……）。

    ★ 本函数**不抛异常**：审计写不进去也不能连累登录/改密码这些真正的业务。
    """
    now = time.time()
    record = {
        "ts": now,
        "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now)),
        "event": str(event or ""),
        "username": str(username or ""),
        "ip": str(ip or ""),
        "result": str(result or "ok"),
        "detail": str(detail or ""),
    }

    path = _path()
    line = json.dumps(record, ensure_ascii=False) + "\n"
    encoded = line.encode("utf-8")

    try:
        with _LOCK:
            directory = os.path.dirname(os.path.abspath(path))
            if directory:
                os.makedirs(directory, exist_ok=True)
            # 轮转判断放在写入前：先让旧文件让位，再追加这一条
            try:
                if (MAX_BYTES > 0 and os.path.isfile(path)
                        and os.path.getsize(path) + len(encoded) > MAX_BYTES):
                    _rotate_locked(path)
            except OSError:
                pass

            # 单次 write 写入整行：并发下不会把两条记录插在一起。
            # 用二进制追加而不是文本模式，是为了自己控制编码与换行 ——
            # 文本模式在 Windows 上会把 "\n" 翻成 "\r\n"，读回来多一个 \r，
            # 虽然 json.loads 能容忍，但用别的工具看就多一个看不见的字符。
            with open(path, "ab") as fh:
                fh.write(encoded)
    except Exception as exc:  # noqa: BLE001
        _warn("审计日志写入失败（%s）：%s" % (path, exc))


def _read_lines(path: str, max_bytes: int = 512 * 1024) -> List[str]:
    """
    读文件**末尾**至多 max_bytes 的内容，返回各行。

    只读尾巴：日志文件上限 4MB，而界面通常只要最近一两百条；
    读全文在机械盘上要几十毫秒，而这是每次打开管理窗口都会走的路径。
    """
    try:
        size = os.path.getsize(path)
    except OSError:
        return []

    try:
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()          # 丢掉被截断的半行
            raw = fh.read()
    except OSError:
        return []

    text = raw.decode("utf-8", "replace")
    return text.splitlines()


def recent(limit: int = 200) -> List[Dict[str, Any]]:
    """
    最近 limit 条记录，**按时间正序**返回（和文件里的顺序一致）。

    坏行直接跳过：日志是被追加写入的，断电/崩溃可能留下半行。
    为此让整个管理窗口打不开是不划算的。
    """
    try:
        wanted = max(1, int(limit))
    except (TypeError, ValueError):
        wanted = 200
    wanted = min(wanted, 2000)

    path = _path()
    with _LOCK:
        lines = _read_lines(path)

    records: List[Dict[str, Any]] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            records.append(item)
        if len(records) >= wanted:
            break

    records.reverse()
    return records


def clear() -> bool:
    """
    清空当前日志文件（保留轮转出来的历史）。

    管理界面用不上这个 —— 日志能被操作者随手抹掉就不叫审计了。
    留着是给测试与「重置本机演示环境」用的。
    """
    path = _path()
    with _LOCK:
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            _warn("清空审计日志失败（%s）：%s" % (path, exc))
            return False


def _warn(message: str) -> None:
    """flush=True 是必须的：以 Windows 服务方式运行时 stdout 是块缓冲。"""
    print("[警告][审计] %s" % message, flush=True)

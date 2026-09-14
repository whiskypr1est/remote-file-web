# -*- coding: utf-8 -*-
"""
在线用户（内存表）
==================

管理员需要看到「现在谁在线、从哪个 IP、什么时候登录的」。

为什么是内存
------------
会话是无状态签名 Cookie，服务端**本来就无法枚举有效会话** ——
它只能在请求进来时验签，没有任何「当前有哪些会话」的清单。
所以这里记的不是「有效会话」，而是「我最近见过谁」：
每个已认证请求把 last_seen 刷新一下，**最近 5 分钟有活动就算在线**。

重启后这张表会清空 —— 这是可接受的：持久记录由 audit.py 的审计日志承担，
而且「服务刚起来时谁都还没活动过」本身也是事实。

为什么不做成「精确的会话列表」
------------------------------
要精确，就得在服务端存一份会话清单并让它和 Cookie 的过期时间保持一致 ——
那等于把无状态会话改成有状态会话，还要处理清理、并发、重启恢复。
为了一个「谁在线」的展示，代价完全不成比例。

★ 与「活跃会话数」的区别
------------------------
管理界面里的「活跃会话数」指的是**命令行窗口数**（见 terminal.owner_counts），
不是这里的 HTTP 在线状态。两者的口径不同，界面上也分开显示，
免得管理员把「开着 3 个 cmd」误读成「登录了 3 次」。
"""

from __future__ import annotations

import threading
import time
from typing import Any, Dict, List, Optional

# ★ 「在线」的判定窗口：最近这么久之内有过请求就算在线。
#
# 取 5 分钟是因为它同时满足两件事：比「刷新一下页面」的间隔长得多
# （否则刚打开就显示离线，用户会以为坏了），又不至于把「半小时前关掉页面」
# 的人一直显示成在线。
ONLINE_WINDOW_SECONDS = 300.0

# last_seen 的刷新节流：每个请求都抢锁写一次没有意义，5 分钟窗口下
# 30 秒的误差可以忽略。登录/登出这类**事件**不受节流影响，永远立即生效。
TOUCH_INTERVAL_SECONDS = 30.0

# 超过这么久没有任何活动的记录会被清掉（防止长期运行后表无限增长）。
FORGET_AFTER_SECONDS = 24 * 3600.0

_LOCK = threading.Lock()
_ENTRIES: Dict[str, Dict[str, Any]] = {}


def _key(username: str) -> str:
    return str(username or "").strip().lower()


def touch(username: str, ip: str = "", display_name: str = "",
          role: str = "", is_login: bool = False) -> None:
    """
    记一次活动。

    :param is_login: True 表示这是一次**登录**（或显式刷新），
                     此时不受节流限制，并更新 login_at。

    ★ 本函数不抛异常：在线表只是展示用的旁路，不该影响任何业务请求。
    """
    key = _key(username)
    if not key:
        return

    now = time.time()
    try:
        with _LOCK:
            entry = _ENTRIES.get(key)
            if entry is None:
                entry = {
                    "username": str(username or "").strip(),
                    "display_name": str(display_name or ""),
                    "role": str(role or ""),
                    "ip": str(ip or ""),
                    # 第一次见到他时把 login_at 也记上：管理员看到的
                    # 「登录时间」至少不会因为重启而变成空白
                    "login_at": now if is_login else None,
                    "last_seen": now,
                    "requests": 0,
                }
                _ENTRIES[key] = entry

            # 这几个字段每次都更新：它们本来就是最新的更准
            if display_name:
                entry["display_name"] = str(display_name)
            if role:
                entry["role"] = str(role)
            if ip:
                entry["ip"] = str(ip)
            entry["requests"] = int(entry.get("requests") or 0) + 1

            if is_login:
                entry["login_at"] = now
                entry["last_seen"] = now
            elif now - float(entry.get("last_seen") or 0.0) >= TOUCH_INTERVAL_SECONDS:
                entry["last_seen"] = now
    except Exception as exc:  # noqa: BLE001
        print("[警告][在线表] 更新失败（%s）：%s" % (username, exc), flush=True)


def forget(username: str) -> None:
    """
    把一个人从在线表里摘掉（登出时调用）。

    登出是**显式**的「我不在了」，这时候还留着他的 last_seen 会让管理员
    看到「刚还在线」的假象；持久记录由审计日志负责，这里删掉不丢信息。
    """
    key = _key(username)
    if not key:
        return
    with _LOCK:
        _ENTRIES.pop(key, None)


def _prune_locked(now: float) -> None:
    deadline = now - FORGET_AFTER_SECONDS
    stale = [key for key, entry in _ENTRIES.items()
             if float(entry.get("last_seen") or 0.0) < deadline]
    for key in stale:
        _ENTRIES.pop(key, None)


def snapshot(now: Optional[float] = None) -> List[Dict[str, Any]]:
    """
    当前在线情况，**在线的排前面**（其次按最后活动时间倒序）。

    每条带一个 `online` 布尔值：最近 ONLINE_WINDOW_SECONDS 内有活动的为 True。
    刻意把离线的人也返回（只要没超过 FORGET_AFTER_SECONDS），
    这样管理员能看到「他十分钟前还在」而不是「查无此人」。
    """
    current = time.time() if now is None else float(now)
    with _LOCK:
        _prune_locked(current)
        items = []
        for entry in _ENTRIES.values():
            last_seen = float(entry.get("last_seen") or 0.0)
            row = dict(entry)
            row["online"] = (current - last_seen) < ONLINE_WINDOW_SECONDS
            row["idle_seconds"] = max(0, int(current - last_seen))
            if row.get("login_at"):
                row["login_at"] = float(row["login_at"])
            items.append(row)

    items.sort(key=lambda row: (not row["online"],
                                -float(row.get("last_seen") or 0.0)))
    return items


def online_names(now: Optional[float] = None) -> List[str]:
    """在线用户名列表（管理界面筛选用）。"""
    return [row["username"] for row in snapshot(now) if row["online"]]


def count(now: Optional[float] = None) -> int:
    return len(online_names(now))


def reset() -> None:
    """清空（测试用）。"""
    with _LOCK:
        _ENTRIES.clear()

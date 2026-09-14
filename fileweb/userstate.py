# -*- coding: utf-8 -*-
"""
用户界面状态存储
================

需求背景：
    虚拟桌面的窗口布局（打开了哪些窗口、每个窗口的位置与大小、当前目录、
    视图模式）原先只存在前端内存里，**刷新页面就全没了**。
    用户真正想要的是「关掉浏览器标签页，过一会儿再打开，桌面还是原来的样子」。

因此这份状态必须由服务端落盘持久化，与 desktop_shortcuts.json 走同一套思路：
不能用浏览器 localStorage（换台电脑就没了、清缓存就没了）。

★ 多用户：这份状态是**按用户分开**存的（各人一份文件），否则「关掉浏览器再
打开还是原来的桌面」会变成「看到别人刚才的桌面」。管理员沿用原来的单文件，
子用户各用 `user_state.<用户名>.json`，细节见 peruser.py。

存储结构：
    文件里就是一份**不透明**的 JSON 对象，服务端不解释其中任何字段：

        {"version": 1, "windows": [...], "active": "win_3", "view": "icons"}

    ★ 之所以刻意不定义 schema：窗口布局属于纯前端概念，
      前端加一个新字段（比如窗口的 z 序、收纳盒折叠状态）不应该需要服务端改代码。
      服务端只保证「原样存取、不损坏」，这层解耦是刻意的设计而不是偷懒。

    但「不透明」不等于「不设防」：客户端可控的输入必须有体积上限
    （见 MAX_STATE_BYTES），否则任何人都能靠反复 PUT 把磁盘写满。

并发安全：
    所有读写都在同一把锁里完成，避免多个请求同时写文件造成内容损坏；
    写入采用「临时文件 + os.replace」的原子替换，断电也不会写坏文件。
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from typing import Any, Dict, Optional

from . import peruser

# 界面状态数据文件（与 config.json 同目录）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USER_STATE_PATH = os.path.join(BASE_DIR, "user_state.json")

# ★ 实际生效的路径。config.prepare() 会用配置里的配置项覆盖它。
#
# 为什么不在这里直接读配置对象：本模块会被 config.py 反过来引用（为了拿默认
# 路径常量），真的去 import config 会形成循环导入。
# 所以改成「config 启动时把解析好的绝对路径塞进来」这种单向依赖。
# 默认值保证「即使没人塞过」也指向项目根目录下那个文件，与 shortcuts.py 一致。
ACTIVE_PATH = USER_STATE_PATH

_LOCK = threading.RLock()

# ---------------------------------------------------------------------------
# 体积上限
# ---------------------------------------------------------------------------
# 256 KB 是「远超真实布局需要、又小到无法撑爆磁盘」的量级：
# 一份几十个窗口的布局序列化后通常只有几 KB，留两个数量级的余量足够应对
# 前端将来往里塞窗口内的滚动历史之类的额外字段。
# 上限按**序列化后的 UTF-8 字节数**算，而不是 Python 里的字符数——
# 中文一个字占 3 字节，按字符数算会低估三倍。
MAX_STATE_BYTES = 256 * 1024


class UserStateTooLargeError(ValueError):
    """
    状态文档超过 MAX_STATE_BYTES。调用方应回 **413**（Payload Too Large），
    而不是静默截断，也不是 400。

    ★ 注意它继承 ValueError：别的 ValueError（无法序列化等）语义上是
    「请求不合法」= 400，所以路由层必须**先**捕获本异常再捕获 ValueError，
    顺序反了就会把「太大」误报成 400。见 routers/desktop.py 的 put_desktop_state。
    """


def _path() -> str:
    """
    取当前生效的状态文件路径。

    路径是配置项（见 config.py 的 DEFAULT_CONFIG["user_state_path"]），
    由 config.prepare() 解析成绝对路径后写进 ACTIVE_PATH；
    这样测试把配置指向临时目录时，状态文件也会落在临时目录里，
    而不会污染项目根目录。
    """
    return ACTIVE_PATH or USER_STATE_PATH


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    """原子写 JSON：先写临时文件再替换，避免写一半导致文件损坏。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".userstate-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _serialize(data: Dict[str, Any]) -> str:
    """
    把状态对象序列化成 JSON 文本，并做必须的拒绝判断。

    allow_nan=False：前端理论上不会传 NaN/Infinity，但一旦传进来，
    标准 json 会写出 `NaN` 这种**不是合法 JSON** 的字面量，
    下次读取时 json.load 直接报错，等于把这份状态永久写坏。
    宁可在这里明确报错。
    """
    return json.dumps(data, ensure_ascii=False, allow_nan=False)


def load(path: Optional[str] = None,
         user: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    读取状态。

    文件缺失、不可读、损坏、根节点不是对象时一律返回空字典 {}，
    **绝不抛异常**：这份状态只影响「桌面看起来是否和上次一样」，
    让它把整个桌面加载流程搞崩是不划算的，前端拿到 {} 会走默认布局。

    :param user: 当前登录用户。管理员读基础文件（升级前后同一份），
                 子用户读自己的 `user_state.<用户名>.json`（见 peruser.py）。
                 显式给了 path 时以 path 为准（单元测试就是这么用的）。
    """
    target = path or peruser.state_path(_path(), user)

    with _LOCK:
        if not os.path.isfile(target):
            return {}
        try:
            with open(target, "r", encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except Exception as exc:  # noqa: BLE001
            _warn("界面状态文件无法读取，已退回空状态（%s）：%s" % (target, exc))
            return {}

        if not isinstance(data, dict):
            _warn("界面状态文件根节点不是对象，已退回空状态：%s" % target)
            return {}
        return data


def save(data: Dict[str, Any], path: Optional[str] = None,
         user: Optional[Dict[str, Any]] = None) -> None:
    """
    保存状态（原子替换）。

    非字典或含 JSON 不可序列化的值时抛 TypeError/ValueError（路由层翻译成 400），
    超过 MAX_STATE_BYTES 时抛 UserStateTooLargeError（路由层翻译成 413）。
    这里刻意**不静默截断**：截断后的 JSON 通常已经无法解析，
    前端再取回来只会更困惑。

    :param user: 与 load 同义，决定写进哪个用户的文件。
    """
    if not isinstance(data, dict):
        raise TypeError("界面状态必须是一个 JSON 对象")

    target = path or peruser.state_path(_path(), user)

    try:
        text = _serialize(data)
    except (TypeError, ValueError) as exc:
        raise ValueError("界面状态包含无法序列化的内容：%s" % exc)

    size = len(text.encode("utf-8"))
    if size > MAX_STATE_BYTES:
        raise UserStateTooLargeError(
            "界面状态过大（%d 字节，上限 %d 字节）" % (size, MAX_STATE_BYTES)
        )

    with _LOCK:
        _atomic_write(target, data)


def clear(path: Optional[str] = None,
          user: Optional[Dict[str, Any]] = None) -> bool:
    """
    删除状态文件（恢复默认布局用），返回是否真的删掉了。

    留着这个入口是为了「重置桌面布局」这类功能：
    与其写一个空对象进去，不如把文件删掉，语义更干净。
    """
    target = path or peruser.state_path(_path(), user)
    with _LOCK:
        try:
            os.unlink(target)
            return True
        except FileNotFoundError:
            return False
        except OSError as exc:
            _warn("删除界面状态文件失败（%s）：%s" % (target, exc))
            return False


def _warn(message: str) -> None:
    """
    打一行警告。

    flush=True 是必须的：以 Windows 服务（NSSM）方式运行时 stdout 被重定向，
    Python 用的是块缓冲，不 flush 的话这行警告会卡在缓冲区里，
    排查「桌面状态为什么丢了」时等于没有日志。
    """
    print("[警告][界面状态] %s" % message, flush=True)

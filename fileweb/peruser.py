# -*- coding: utf-8 -*-
"""
按用户分文件的状态存储路径
==========================

背景（多用户改造）
------------------
`desktop_shortcuts.json` 与 `user_state.json` 原先各是一个**单文件**，所有人共用。
多人一起用就会互相覆盖：学生 A 挪一下窗口、学生 B 刷新后桌面就变了；
A 建一个快捷方式，B 的桌面上也会冒出来。更糟的是「关掉浏览器再打开还是原来的
桌面」这个卖点会变成「看到的可能是别人刚才的桌面」。

命名规则
--------
* **管理员 / 未指定用户 → 沿用原文件名**（`user_state.json`）。
* **子用户 → 同目录下的 `user_state.<用户名>.json`**。

管理员沿用原名是刻意的，对应「现有那份归 admin」这条决定，好处有三个：

1. 现有部署升级后文件名、内容都不变 —— 管理员看到的桌面和升级前一模一样；
2. 沿用已有的备份 / 清理习惯，不会多出一个「不知道能不能删」的新文件；
3. 不需要任何迁移动作，也就不存在「迁移写坏了」这种失败模式。

★ 判据是**角色**而不是「用户名是不是 admin」：管理员的登录名可以改成别的，
将来也可能有第二个管理员。反过来，一个恰好叫 admin 的普通学生**绝不能**
拿到管理员那份文件 —— 那等于让他一登录就看到并改掉管理员的桌面布局。

安全
----
用户名会被拼进文件名，所以这里做**字符白名单**过滤。用户表本身已经把用户名
限制成 `[A-Za-z0-9_.-]{1,32}`（见 users.py 的 _USERNAME_RE），这里是第二道闸门：
万一旧文件里存着奇怪的名字、或将来放宽了规则，也不能让它跳出目录 ——
`../` 或 `..\\` 这种一旦拼进路径就是目录穿越，而状态文件是会被**写**的。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, Optional

# 允许出现在文件名里的字符。与 users.py 的用户名规则保持一致，
# 但这里**不是**校验器而是消毒器：遇到不允许的字符直接替换掉，
# 不抛异常 —— 状态文件读不出来也不该让整个桌面加载失败。
_UNSAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")

_MAX_NAME_LEN = 32


def safe_username(username: Any) -> str:
    """
    把用户名消毒成可以安全拼进文件名的片段。

    返回空串表示「这个名字没法用」，调用方应退回基础路径
    （宁可几个人共用一份，也不要在磁盘上乱造文件）。
    """
    clean = _UNSAFE_RE.sub("_", str(username or "").strip())
    # 去掉首尾的点：`.` 与 `..` 在路径里是特殊项，隐藏文件也不合适
    clean = clean.strip(".")
    return clean[:_MAX_NAME_LEN]


def state_path(base_path: str, user: Optional[Dict[str, Any]] = None) -> str:
    """
    算出**某个用户**该用哪个状态文件。

    :param base_path: 基础（管理员）路径，通常来自配置。
    :param user:      用户记录（含 username / role）。
                      None 或管理员 → 直接返回 base_path。
    """
    if not base_path:
        return base_path
    if not user or str(user.get("role") or "") == "admin":
        return base_path

    name = safe_username(user.get("username"))
    if not name:
        # 拿不到可用的用户名：退回基础路径。
        # 注意这是「共享」而不是「拒绝」—— 状态文件只影响界面外观，
        # 为此让桌面加载不出来是不划算的。
        return base_path

    directory, filename = os.path.split(base_path)
    stem, ext = os.path.splitext(filename)
    return os.path.join(directory, "%s.%s%s" % (stem, name, ext))

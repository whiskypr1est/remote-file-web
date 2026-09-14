# -*- coding: utf-8 -*-
"""
用户存储（多用户）
==================

多用户改造的基石：一张用户表，每个用户有角色、口令哈希、可见的根目录、
以及一个用来「按用户踢下线」的令牌版本号。

存哪儿、怎么写
--------------
独立文件 `users.json`（默认与 config.json 同级），沿用 `shortcuts.py` /
`userstate.py` 的既定做法：临时文件 + `os.replace` 原子替换，断电也不会写坏；
读不出来就当作「没有用户」并保留原文件，绝不因为一次解析失败把用户表清空。

与 `config.json` 的关系（★ 兼容老部署的关键）
---------------------------------------------
`config.json` 的 `auth` 段是**引导用**的：首次运行（还没有 users.json）时，
`ensure_bootstrap()` 会把它变成唯一的**管理员**账号 —— 这样老部署升级上来
口令继续有效、行为完全不变，267 条既有测试也不需要改。
一旦 users.json 存在，它就是这个系统**唯一的用户事实来源**：
之后改密码、加用户都只动 users.json，config 的 auth 段不再参与登录。

关于「token_version」
---------------------
会话是无状态签名 Cookie。要让「改密码 / 停用 / 强制下线」对**单个用户**生效，
就得有一个服务端能改、且校验时会比对的东西 —— 那就是每用户的 token_version：
签发时把它写进 token，校验时跟当前值比对，不一致即视为失效。
（全局轮换 session_secret 也能踢人，但会把所有人都踢掉，多用户下不可用。）

安全边界说明
------------
本模块的权限检查是**界面与接口层面**的。用户已确认子用户拥有全权限 cmd，
所以它不构成对抗恶意用户的边界 —— 详细讨论见 MULTIUSER.md 第〇节。
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from .security import hash_password, verify_password

# 用户名规则：只允许 ASCII 字母数字与 _ . -，长度 1..32。
# 刻意不允许中文：用户名会出现在 URL、文件名（每用户的状态文件）和日志里，
# 限制成 ASCII 能省掉一整类编码问题。显示名（display_name）可以随便用中文。
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,32}$")

# 与 auth.py 的 MIN_PASSWORD_LENGTH 保持一致
MIN_PASSWORD_LENGTH = 8

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)

# 密码哈希等敏感字段不下发给前端
_PUBLIC_EXCLUDE = ("password_hash",)

_LOCK = threading.RLock()

# 用户表路径。默认在 config.json 旁边；测试与多实例部署可以显式改。
USERS_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), os.pardir, "users.json")
USERS_PATH = os.path.normpath(USERS_PATH)


class UserError(Exception):
    """用户操作失败（用户名非法、重名、找不到等）。"""


def set_path(path: str) -> None:
    """改用户表位置（测试用；也让「用不同配置文件起多实例」各存各的）。"""
    global USERS_PATH
    USERS_PATH = os.path.abspath(path)


# ---------------------------------------------------------------------------
# 读写
# ---------------------------------------------------------------------------

def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    handle, tmp_path = tempfile.mkstemp(
        prefix=".users-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_raw() -> Dict[str, Any]:
    """
    读用户表；任何问题都退化成「空表」，**不抛异常**。

    调用方（登录流程）必须能在用户表损坏时给出可读的提示，而不是 500。
    原文件保持不动，方便人工修。
    """
    if not os.path.isfile(USERS_PATH):
        return {"version": 1, "users": []}

    try:
        with open(USERS_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "users": []}

    if not isinstance(data, dict):
        return {"version": 1, "users": []}
    if not isinstance(data.get("users"), list):
        data["users"] = []
    return data


def _normalize_roots(raw: Any) -> List[Dict[str, Any]]:
    """清洗根目录列表：丢掉缺 id/path 的项，补默认值。"""
    roots: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return roots

    seen = set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue
        root_id = str(item.get("id") or "").strip()
        if not root_id:
            # 用目录名兜底，保证 id 稳定且非空
            root_id = os.path.basename(path.rstrip("\\/")) or "root"
        if root_id in seen:
            continue
        seen.add(root_id)
        roots.append({
            "id": root_id,
            "name": str(item.get("name") or root_id),
            "path": os.path.normpath(path),
            "readonly": bool(item.get("readonly", False)),
        })
    return roots


# ---------------------------------------------------------------------------
# 可见目录的「文本格式」
# ---------------------------------------------------------------------------
# 管理界面用一块多行文本框来编辑「这个用户能看到哪些目录」——
# 那是最贴近管理员手上信息（他手上就是一个路径）的输入方式，
# 而做一个目录选择器等于把整棵文件树搬进管理窗口。
#
# ★ 解析放在**服务端**而不是前端：
#   * 格式只有一处定义，前端不需要懂；
#   * 能在本项目的 Python 测试里直接覆盖（前端解析逻辑没法在无 Node 的
#     测试环境里跑，而这段逻辑一旦出错就是「把权限分错人」，必须有测试）。
#   前端只负责把文本原样发过来、把服务端给的文本显示出来。
#
# 格式：每行一个目录，`路径 | 名称 | 只读`，后两段可省略；
#      以 # 开头的行是注释，空行忽略。
_READONLY_WORDS = ("只读", "ro", "readonly", "r")


def _is_readonly_word(text: str) -> bool:
    return str(text or "").strip().lower() in _READONLY_WORDS


def parse_roots_text(text: Any) -> List[Dict[str, Any]]:
    """
    把「每行一个目录」的文本解析成根目录列表（再过一遍 _normalize_roots）。

    宽容优先：管理员是在手写路径，多一个空格、少一段名称都不该报错 ——
    真正的把关（路径是否有意义）在运行时由解析器负责。
    """
    roots: List[Dict[str, Any]] = []

    for line in str(text or "").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue

        parts = [piece.strip() for piece in raw.split("|")]
        path = parts[0]
        if not path:
            continue

        name = parts[1] if len(parts) > 1 else ""
        # 只读可以从第二段之后**任意一段**写出来：
        # 既支持 `路径 | 只读`（省略名称），也支持 `路径 | 名称 | 只读`
        readonly = any(_is_readonly_word(piece) for piece in parts[1:])
        if readonly and _is_readonly_word(name):
            name = ""          # 那一格写的是「只读」，不是名称

        item: Dict[str, Any] = {"path": path, "readonly": readonly}
        if name:
            item["name"] = name
        roots.append(item)

    return _normalize_roots(roots)


def format_roots_text(roots: Any) -> str:
    """
    parse_roots_text 的逆操作（给管理界面回显用）。

    ★ 刻意**不输出 id**：id 是派生值（_normalize_roots 用目录名兜底），
    让管理员看见/编辑它只会带来「改了 id 却不知道会影响什么」的困惑。
    """
    lines = []
    for item in (roots or []):
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path:
            continue

        name = str(item.get("name") or "").strip()
        if name == os.path.basename(path.rstrip("\\/")):
            # 名称与目录名相同 = 自动派生的，显示出来只是噪音
            name = ""

        if item.get("readonly"):
            lines.append("%s | %s | 只读" % (path, name) if name
                         else "%s | 只读" % path)
        elif name:
            lines.append("%s | %s" % (path, name))
        else:
            lines.append(path)
    return "\n".join(lines)


def _normalize_permissions(raw: Any) -> Dict[str, bool]:
    defaults = {"terminal": True, "sysmon": True}
    if not isinstance(raw, dict):
        return defaults
    result = {}
    for key, default in defaults.items():
        result[key] = bool(raw.get(key, default))
    return result


def _normalize_user(raw: Any) -> Optional[Dict[str, Any]]:
    """把一条记录清洗成规范结构；无法拯救的返回 None（直接丢掉）。"""
    if not isinstance(raw, dict):
        return None

    username = str(raw.get("username") or "").strip()
    if not _USERNAME_RE.match(username):
        return None

    role = str(raw.get("role") or ROLE_USER).strip().lower()
    if role not in ROLES:
        role = ROLE_USER

    try:
        token_version = int(raw.get("token_version") or 1)
    except (TypeError, ValueError):
        token_version = 1

    try:
        created = float(raw.get("created") or 0)
    except (TypeError, ValueError):
        created = 0.0

    try:
        last_login = float(raw.get("last_login") or 0)
    except (TypeError, ValueError):
        last_login = 0.0

    try:
        max_sessions = int(raw.get("max_terminal_sessions") or 5)
    except (TypeError, ValueError):
        max_sessions = 5

    return {
        "username": username,
        "display_name": str(raw.get("display_name") or username),
        "role": role,
        "password_hash": str(raw.get("password_hash") or ""),
        "enabled": bool(raw.get("enabled", True)),
        "token_version": max(1, token_version),
        "created": created,
        "created_by": str(raw.get("created_by") or ""),
        "last_login": last_login,
        "note": str(raw.get("note") or ""),
        "roots": _normalize_roots(raw.get("roots")),
        "max_terminal_sessions": max(1, min(64, max_sessions)),
        "permissions": _normalize_permissions(raw.get("permissions")),
    }


def _load_users() -> List[Dict[str, Any]]:
    raw = _read_raw()
    users = []
    for item in raw.get("users") or []:
        normalized = _normalize_user(item)
        if normalized:
            users.append(normalized)
    return users


def _save_users(users: List[Dict[str, Any]]) -> None:
    _atomic_write(USERS_PATH, {"version": 1, "users": users})


def public(record: Dict[str, Any]) -> Dict[str, Any]:
    """去掉敏感字段后的副本，可以直接下发给前端。"""
    return {k: v for k, v in record.items() if k not in _PUBLIC_EXCLUDE}


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

def list_users() -> List[Dict[str, Any]]:
    """全部用户（含禁用），按创建时间排序。**含哈希**，路由层要过 public()。"""
    with _LOCK:
        users = _load_users()
    users.sort(key=lambda u: (u.get("created") or 0, u["username"]))
    return users


def get(username: str) -> Optional[Dict[str, Any]]:
    """按用户名取记录（含哈希）。用户名大小写不敏感。"""
    needle = str(username or "").strip().lower()
    if not needle:
        return None
    with _LOCK:
        for user in _load_users():
            if user["username"].lower() == needle:
                return user
    return None


def count() -> int:
    with _LOCK:
        return len(_load_users())


def has_admin() -> bool:
    """是否存在「启用中的管理员」——没有的话没人能进管理界面。"""
    return any(u["role"] == ROLE_ADMIN and u["enabled"] for u in list_users())


def admin_username() -> str:
    """第一个启用中的管理员名（引导与提示用）。"""
    for user in list_users():
        if user["role"] == ROLE_ADMIN and user["enabled"]:
            return user["username"]
    return ""


def authenticate(username: str, password: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """
    校验账号口令。

    :returns: (用户记录 or None, 失败原因)。失败原因给的是**给用户看的中文**，
              调用方可以直接拿去拼错误信息。
    """
    user = get(username)

    # 用户不存在时也走一遍口令校验，让耗时保持稳定（不泄露"这个用户名存在吗"）。
    # 注意：真实部署的威胁模型里子用户本来就能看磁盘，这里只是保持好习惯。
    if user is None:
        verify_password(password or "", hash_password("dummy-for-constant-time"))
        return None, "用户名或密码错误"

    if not user.get("password_hash"):
        return None, "该账号没有设置密码，请联系管理员重置"

    if not verify_password(password or "", user["password_hash"]):
        return None, "用户名或密码错误"

    if not user.get("enabled", True):
        return None, "该账号已被停用，请联系管理员"

    return user, ""


# ---------------------------------------------------------------------------
# 变更
# ---------------------------------------------------------------------------

def _require_valid_username(username: str) -> str:
    name = str(username or "").strip()
    if not _USERNAME_RE.match(name):
        raise UserError(
            "用户名只能用字母、数字、下划线、点或连字符，长度 1~32 位")
    return name


def _require_valid_password(password: str) -> str:
    text = password or ""
    if len(text) < MIN_PASSWORD_LENGTH:
        raise UserError("密码至少需要 %d 位" % MIN_PASSWORD_LENGTH)
    return text


def create(username: str, password: str, *, role: str = ROLE_USER,
           display_name: str = "", roots: Optional[List[Dict[str, Any]]] = None,
           note: str = "", created_by: str = "",
           max_terminal_sessions: int = 5,
           permissions: Optional[Dict[str, bool]] = None) -> Dict[str, Any]:
    """新建用户。用户名重名会抛 UserError。"""
    name = _require_valid_username(username)
    _require_valid_password(password)

    role_value = str(role or ROLE_USER).strip().lower()
    if role_value not in ROLES:
        raise UserError("角色只能是 %s" % " 或 ".join(ROLES))

    with _LOCK:
        users = _load_users()
        if any(u["username"].lower() == name.lower() for u in users):
            raise UserError("用户名已存在：%s" % name)

        record = _normalize_user({
            "username": name,
            "display_name": display_name or name,
            "role": role_value,
            "password_hash": hash_password(password),
            "enabled": True,
            "token_version": 1,
            "created": time.time(),
            "created_by": created_by,
            "note": note,
            "roots": roots or [],
            "max_terminal_sessions": max_terminal_sessions,
            "permissions": permissions or {},
        })
        users.append(record)
        _save_users(users)

    return record


def set_password(username: str, password: str,
                 bump_token: bool = True) -> Dict[str, Any]:
    """
    重设口令。

    默认**递增 token_version**：改完密码，该用户此前签发的所有会话立即失效
    （包括他自己的其它浏览器）。这正是上一轮「改密码要能踢掉旧会话」那条
    需求在多用户下的正确形态 —— 只踢他自己，不牵连别人。
    """
    _require_valid_password(password)
    return _mutate(username, lambda u: {
        "password_hash": hash_password(password),
        "token_version": (u["token_version"] + 1) if bump_token else u["token_version"],
    })


def set_enabled(username: str, enabled: bool) -> Dict[str, Any]:
    """启用 / 停用。停用同样递增 token_version，让在线会话立刻失效。"""
    return _mutate(username, lambda u: {
        "enabled": bool(enabled),
        "token_version": u["token_version"] + 1,
    })


def set_roots(username: str, roots: List[Dict[str, Any]]) -> Dict[str, Any]:
    """改可见根目录。不改 token_version（不需要把人踢下线，下次请求即生效）。"""
    return _mutate(username, lambda u: {"roots": _normalize_roots(roots)})


def update(username: str, **fields: Any) -> Dict[str, Any]:
    """通用更新（显示名/备注/角色/权限/会话上限）。"""
    allowed = {"display_name", "note", "role", "max_terminal_sessions", "permissions"}
    patch: Dict[str, Any] = {}
    for key, value in fields.items():
        if key not in allowed:
            continue
        patch[key] = value

    if "role" in patch:
        role_value = str(patch["role"]).strip().lower()
        if role_value not in ROLES:
            raise UserError("角色只能是 %s" % " 或 ".join(ROLES))
        patch["role"] = role_value

    return _mutate(username, lambda u: patch)


def bump_token_version(username: str) -> Dict[str, Any]:
    """把某个用户强制下线（不改密码、不停用）。"""
    return _mutate(username, lambda u: {"token_version": u["token_version"] + 1})


def record_login(username: str) -> None:
    """记下最近一次登录时间（失败不抛，登录流程不该因为这个报错）。"""
    try:
        _mutate(username, lambda u: {"last_login": time.time()})
    except UserError:
        pass


def delete(username: str) -> bool:
    """
    删除用户。

    注意这只是删掉**账号**，不动他的文件夹 —— 数据安全优先，
    要清理目录请人工确认后再删。日常「学生离开」应该用 set_enabled(False)。
    """
    name = str(username or "").strip().lower()
    with _LOCK:
        users = _load_users()
        remaining = [u for u in users if u["username"].lower() != name]
        if len(remaining) == len(users):
            return False
        _save_users(remaining)
    return True


def _mutate(username: str, patch_fn) -> Dict[str, Any]:
    """通用「读-改-写」，全程持锁。"""
    name = str(username or "").strip().lower()
    with _LOCK:
        users = _load_users()
        for index, user in enumerate(users):
            if user["username"].lower() != name:
                continue
            merged = dict(user)
            merged.update(patch_fn(user) or {})
            normalized = _normalize_user(merged)
            if normalized is None:
                raise UserError("更新后的记录不合法，已放弃本次修改")
            users[index] = normalized
            _save_users(users)
            return normalized
    raise UserError("用户不存在：%s" % username)


# ---------------------------------------------------------------------------
# 引导（老部署升级上来的入口）
# ---------------------------------------------------------------------------

def ensure_bootstrap(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    首次运行：没有用户表时，用 `config.json` 的 auth 段造出管理员。

    ★ 这是「升级后行为不变」的关键：老部署只有 `auth.username` / `password_hash`，
      迁移后它成为唯一的管理员 —— 口令继续有效，而且因为管理员的 roots 为空
      （走 mount_all_drives），他看到的仍然是全机。

    :returns: {"created": bool, "username": str, "warning": str}
    """
    with _LOCK:
        users = _load_users()

        if users:
            # 已有用户表：只在「一个启用中的管理员都没有」时给一句醒目告警。
            # 刻意**不自动提权** —— 静默把某个学生变成管理员比锁在外面更危险。
            if not any(u["role"] == ROLE_ADMIN and u["enabled"] for u in users):
                return {
                    "created": False,
                    "username": "",
                    "warning": "用户表里没有启用中的管理员，没人能进入管理界面。"
                               "请手工编辑 %s 修正。" % USERS_PATH,
                }
            return {"created": False, "username": admin_username(), "warning": ""}

        auth = (cfg or {}).get("auth") or {}
        username = str(auth.get("username") or "admin").strip() or "admin"
        if not _USERNAME_RE.match(username):
            # config 里的用户名不合规（例如用了中文）：换个合规的引导名，
            # 而不是让整个服务起不来。
            username = "admin"

        password_hash = str(auth.get("password_hash") or "")
        if not password_hash:
            # 兼容项：config 里只写了明文 auth.password 或者什么都没写
            plain = str(auth.get("password") or "")
            password_hash = hash_password(plain) if plain else ""

        record = _normalize_user({
            "username": username,
            "display_name": username,
            "role": ROLE_ADMIN,
            "password_hash": password_hash,
            "enabled": True,
            "token_version": 1,
            "created": time.time(),
            "created_by": "bootstrap",
            "note": "由 config.json 的 auth 段迁移而来",
            "roots": [],                 # 空 = 走 mount_all_drives（全机）
        })
        _save_users([record])

    return {"created": True, "username": username, "warning": ""}

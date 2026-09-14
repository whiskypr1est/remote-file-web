# -*- coding: utf-8 -*-
"""
安全工具模块
============

本模块是整个服务的安全基石，包含四部分：

1. 口令哈希：PBKDF2-HMAC-SHA256，配置文件中只保存哈希，绝不保存明文。
2. 会话令牌：HMAC-SHA256 签名的无状态 Token，放进 HttpOnly Cookie。
3. 路径穿越防护：所有文件 API 的唯一入口，确保访问永远不越出配置的根目录。
4. Windows 文件名合法性校验：保留设备名、非法字符、尾部点/空格。

设计要点（防越权的关键）：
    * 先做字符串层面的相对路径拆解，直接拒绝任何 ".." 段；
    * 再用 os.path.realpath 解析符号链接 / 目录联接（junction），
      然后按「根目录归一化前缀」做二次比对；
    * 两道防线叠加，才能同时防住 `..\\..\\windows\\system32` 和
      「根目录内放了指向 C:\\ 的 junction」这两种绕过方式。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

PBKDF2_ALGO = "pbkdf2_sha256"
PBKDF2_ITERATIONS = 260_000

# Windows 保留设备名（不区分大小写，且带任何扩展名都不允许，例如 CON.txt 也不行）
_WIN_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{i}" for i in range(1, 10)),
    *(f"lpt{i}" for i in range(1, 10)),
}

# Windows 文件名非法字符： < > : " / \ | ? * 以及所有控制字符
_INVALID_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class PathSecurityError(Exception):
    """路径越界或路径非法。调用方应转换为 HTTP 403。"""


class AuthError(Exception):
    """认证失败。调用方应转换为 HTTP 401。"""


# ---------------------------------------------------------------------------
# base64url 小工具
# ---------------------------------------------------------------------------

def b64e(raw: bytes) -> str:
    """bytes -> base64url 字符串（去掉 '=' 填充，可安全放进 cookie/URL）。"""
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def b64d(text: str) -> bytes:
    """base64url 字符串 -> bytes（自动补齐被去掉的 '='）。"""
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


# ---------------------------------------------------------------------------
# 1. 口令哈希
# ---------------------------------------------------------------------------

def hash_password(password: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    """
    生成口令哈希，格式：pbkdf2_sha256$迭代次数$salt_b64$hash_b64

    每次调用使用新的随机盐，因此同一口令两次调用结果不同，属正常现象。
    """
    if not isinstance(password, str) or not password:
        raise ValueError("口令不能为空")
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return "%s$%d$%s$%s" % (PBKDF2_ALGO, iterations, b64e(salt), b64e(dk))


def verify_password(password: str, stored: str) -> bool:
    """
    校验口令。任何格式异常都返回 False（而不是抛异常），避免把内部细节泄露给调用方。
    使用 hmac.compare_digest 做恒定时间比较，防止时序侧信道。
    """
    if not password or not stored:
        return False
    try:
        algo, iter_text, salt_b64, hash_b64 = stored.split("$")
        if algo != PBKDF2_ALGO:
            return False
        iterations = int(iter_text)
        salt = b64d(salt_b64)
        expected = b64d(hash_b64)
    except Exception:  # noqa: BLE001 - 配置写坏了也不能崩
        return False
    try:
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    except Exception:  # noqa: BLE001
        return False
    return hmac.compare_digest(dk, expected)


# ---------------------------------------------------------------------------
# 2. 会话令牌（无状态签名 Token）
# ---------------------------------------------------------------------------

def sign_token(payload: Dict[str, Any], secret: str) -> str:
    """
    生成签名令牌：base64url(JSON).base64url(HMAC-SHA256)

    载荷里会写入过期时间 exp，服务端无需存储任何会话状态，
    重启服务后旧令牌依然有效（只要没超时且密钥没变）。
    """
    body = b64e(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return body + "." + b64e(sig)


def verify_token(token: str, secret: str, max_age_seconds: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """
    校验并解析令牌。签名不对、格式不对、已过期都返回 None。
    max_age_seconds 为 None 时只信令牌里自带的 exp。
    """
    if not token or "." not in token:
        return None
    body, _, sig_b64 = token.rpartition(".")
    if not body or not sig_b64:
        return None

    expected = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    try:
        actual = b64d(sig_b64)
    except Exception:  # noqa: BLE001
        return None
    if not hmac.compare_digest(expected, actual):
        return None

    try:
        payload = json.loads(b64d(body).decode("utf-8"))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(payload, dict):
        return None

    now = time.time()
    exp = payload.get("exp")
    if exp is not None:
        try:
            if now > float(exp):
                return None
        except (TypeError, ValueError):
            return None
    if max_age_seconds is not None:
        iat = payload.get("iat")
        try:
            if iat is None or now - float(iat) > float(max_age_seconds):
                return None
        except (TypeError, ValueError):
            return None
    return payload


def csrf_token_for(session_token: str, secret: str) -> str:
    """
    由会话令牌派生出 CSRF 令牌。
    派生方式保证：令牌与会话一一对应，且无法从 CSRF 令牌反推会话令牌。
    """
    mac = hmac.new(secret.encode("utf-8"), ("csrf:" + session_token).encode("utf-8"), hashlib.sha256).digest()
    return b64e(mac)[:43]


def random_secret(nbytes: int = 32) -> str:
    """生成随机密钥（用于 session_secret）。"""
    return b64e(secrets.token_bytes(nbytes))


def random_password(length: int = 16) -> str:
    """
    生成易读性尚可的强随机口令。
    去掉了容易混淆的字符（0/O、1/l/I），方便用户手输。
    """
    alphabet = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    specials = "@#%+=?"
    while True:
        core = "".join(secrets.choice(alphabet) for _ in range(length - 2))
        pwd = core + secrets.choice(specials) + secrets.choice("0123456789")
        # 至少包含大写、小写、数字、符号各一个
        if (any(c.islower() for c in pwd) and any(c.isupper() for c in pwd)
                and any(c.isdigit() for c in pwd) and any(not c.isalnum() for c in pwd)):
            return pwd


# ---------------------------------------------------------------------------
# 3. 路径穿越防护
# ---------------------------------------------------------------------------

def norm_root(path: str) -> str:
    r"""
    归一化根目录：转绝对路径 + 解析符号链接/联接 + 去掉 \\?\ 长路径前缀。
    realpath 在路径不存在时也不会抛异常（strict=False），可放心使用。
    """
    if not path:
        raise PathSecurityError("根目录未配置")
    p = path.strip()
    # 去掉 Windows 长路径前缀，避免后续前缀比对被它干扰
    if p.startswith("\\\\?\\"):
        p = p[4:]
    p = os.path.expandvars(os.path.expanduser(p))
    return os.path.normpath(os.path.realpath(os.path.abspath(p)))


def is_within(root: str, target: str) -> bool:
    """
    判断 target 是否位于 root 之内（含 root 本身）。

    在 Windows 上使用 normcase 做大小写无关比较，
    并且对 root 追加分隔符后再比前缀，避免出现
    「D:\\Share 被 D:\\Share2 骗过」这类前缀误判。
    """
    try:
        r = os.path.normcase(os.path.normpath(os.path.realpath(root)))
        t = os.path.normcase(os.path.normpath(os.path.realpath(target)))
    except Exception:  # noqa: BLE001
        return False
    if r == t:
        return True
    if not r.endswith(os.sep):
        r = r + os.sep
    return t.startswith(r)


def is_protected(target: str, protected_paths: Iterable[str]) -> Optional[str]:
    """
    判断目标路径是否落在「受保护路径」内。

    受保护路径**允许浏览、禁止修改**（新建/重命名/删除/上传）。
    这是在开放整盘访问后的一道保险：C:\\Windows、C:\\Program Files 这类目录
    一旦被误删，Windows 可能直接起不来，而浏览它们往往是有正当需求的。

    返回命中的那条保护路径（便于提示用户），未命中返回 None。
    把 config.json 的 protected_paths 设为空数组即可完全关闭该保护。
    """
    if not protected_paths:
        return None

    for raw in protected_paths:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            guard = norm_root(text)
        except Exception:  # noqa: BLE001
            continue
        if is_within(guard, target):
            return os.path.normpath(text)
    return None


def split_rel_path(rel: str) -> List[str]:
    """
    把前端传来的相对路径拆成安全的路径段列表。

    这一步会拒绝：
      * 空字节（截断攻击）
      * 绝对路径 / 盘符（例如 C:\\Windows、\\\\server\\share）
      * 任何 ".." 段
    """
    rel = (rel or "").strip()
    if "\x00" in rel:
        raise PathSecurityError("路径包含非法字符")

    # 统一分隔符，前端可能传 a/b 也可能传 a\\b
    rel = rel.replace("\\", "/")

    # 拒绝 UNC（\\\\server\\share 转成 // 后会被这里拦下）
    if rel.startswith("//"):
        raise PathSecurityError("不允许访问 UNC 网络路径")

    # 拒绝带盘符的绝对路径：形如 "C:/xxx" 或 "C:xxx"
    if re.match(r"^[A-Za-z]:", rel):
        raise PathSecurityError("不允许使用绝对路径，请使用相对根目录的路径")

    parts: List[str] = []
    for seg in rel.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise PathSecurityError("路径中不允许出现 ..")
        parts.append(seg)
    return parts


def join_within_root(root_path: str, rel: str) -> str:
    """
    把相对路径安全地拼接到根目录下，返回归一化后的绝对路径。

    这是所有文件 API 必须调用的核心函数。
    任何越界都会抛 PathSecurityError。
    """
    root = norm_root(root_path)
    parts = split_rel_path(rel)
    candidate = os.path.join(root, *parts) if parts else root

    # 第二道防线：解析符号链接后再次确认没有跑出根目录
    real = os.path.realpath(candidate)
    if not is_within(root, real):
        raise PathSecurityError("访问越界：目标路径不在允许的根目录内")
    return real


# ---------------------------------------------------------------------------
# 4. Windows 文件名合法性校验
# ---------------------------------------------------------------------------

def is_reserved_name(name: str) -> bool:
    """是否为 Windows 保留设备名（CON、NUL、COM1…，带扩展名也算）。"""
    stem = name.split(".")[0].strip().lower()
    return stem in _WIN_RESERVED


def sanitize_filename(name: str) -> str:
    """
    把用户提供的「一个文件名」清洗成安全可用的单段名称。

    会做这些事：
      * 只保留最后一段，丢弃任何目录成分（防 a/../../b.txt 这种）
      * 替换 Windows 非法字符为下划线
      * 去掉首尾空格与结尾的点和空格（Windows 不允许以点/空格结尾）
      * 拦截保留设备名
      * 超长时截断主文件名但保留扩展名
    """
    if name is None:
        raise PathSecurityError("文件名不能为空")

    # 只取最后一段，杜绝夹带路径
    name = str(name).replace("\\", "/").split("/")[-1]

    name = _INVALID_FILENAME_CHARS.sub("_", name)
    name = name.strip().rstrip(". ")

    if not name or name in (".", ".."):
        raise PathSecurityError("文件名不合法")

    if is_reserved_name(name):
        raise PathSecurityError("“%s”是 Windows 保留设备名，不能使用" % name)

    # Windows 单段文件名上限 255，这里留些余量
    if len(name) > 200:
        stem, ext = os.path.splitext(name)
        keep = max(1, 200 - len(ext))
        name = stem[:keep] + ext

    return name


def is_blocked_extension(filename: str, blocked: Iterable[str]) -> Optional[str]:
    """
    判断文件是否命中上传黑名单，命中则返回命中的扩展名（供提示），否则返回 None。

    这里会同时检查「最后一级扩展名」和「完整后缀链」，用于拦截
    `evil.bat.txt` 这类看起来人畜无害、实则可能被某些程序误执行的名字之外的
    更常见形态（例如 `shell.php.jpg` 对某些旧服务器的绕过思路），
    对本地文件服务而言只要保证最终扩展名即可，故以最终扩展名为主、
    完整后缀链为辅。
    """
    blocked_set = {e.lower() for e in blocked or ()}
    if not blocked_set:
        return None

    base = os.path.basename(filename).lower()

    ext = os.path.splitext(base)[1]
    if ext and ext in blocked_set:
        return ext

    # 辅：检查所有出现过的后缀，例如 a.bat.exe 里的 .bat
    for part in base.split(".")[1:]:
        cand = "." + part
        if cand in blocked_set:
            return cand
    return None


# ---------------------------------------------------------------------------
# 5. 多根目录解析器
# ---------------------------------------------------------------------------

class PathResolver:
    """
    管理 config.json 中的 roots 列表，负责在「根标识 + 相对路径」与
    「真实绝对路径」之间安全地双向转换。

    根标识（id）是前端使用的稳定键名；即使根目录的显示名改了，
    也不会导致前端书签失效。
    """

    def __init__(
        self,
        roots: Iterable[Dict[str, Any]],
        auto_drives: bool = False,
        include_network_drives: bool = False,
        include_removable_drives: bool = True,
    ):
        self.roots: List[Dict[str, Any]] = []
        used_ids: set = set()
        used_paths: set = set()

        def _register(entry: Dict[str, Any], root_id: str) -> None:
            """登记一个根目录，自动保证 id 唯一、路径不重复。"""
            base_id, n = root_id, 2
            while root_id in used_ids:
                root_id = "%s%d" % (base_id, n)
                n += 1
            used_ids.add(root_id)
            used_paths.add(os.path.normcase(entry["path"]))
            entry["id"] = root_id
            self.roots.append(entry)

        # ---- 1) 配置里显式声明的根目录 ----
        for idx, item in enumerate(roots or ()):
            if not isinstance(item, dict):
                continue
            raw_path = str(item.get("path") or "").strip()
            if not raw_path:
                continue
            try:
                real = norm_root(raw_path)
            except Exception:  # noqa: BLE001
                continue

            _register({
                "name": str(item.get("name") or os.path.basename(real) or real),
                "path": real,
                # 配置里写的原始路径，用于界面展示更贴近用户预期
                "display_path": os.path.normpath(raw_path),
                "readonly": bool(item.get("readonly", False)),
                "exists": os.path.isdir(real),
                "kind": "folder",
                "type_label": "目录",
            }, str(item.get("id") or "").strip() or ("root%d" % idx))

        # ---- 2) 自动挂载本机所有磁盘 ----
        # 这样「此电脑」里能直接看到 C:/D:/E:…，插上 U 盘后重启服务也会自动出现，
        # 不需要每次去改 config.json。
        if auto_drives:
            from .drives import drive_id_for, enumerate_drives

            for drive in enumerate_drives(
                include_removable=include_removable_drives,
                include_network=include_network_drives,
            ):
                try:
                    real = norm_root(str(drive["path"]))
                except Exception:  # noqa: BLE001
                    continue

                # 该盘已经被显式配置的根目录覆盖时跳过（例如已单独配了 D:\Share）
                if os.path.normcase(real) in used_paths:
                    continue

                _register({
                    "name": str(drive["name"]),
                    "path": real,
                    "display_path": str(drive["path"]),
                    "readonly": False,
                    "exists": os.path.isdir(real),
                    "kind": "drive",
                    "type_label": str(drive["type_label"]),
                }, drive_id_for(str(drive["path"])))

    # -- 查询 ---------------------------------------------------------------

    def get(self, root_id: Optional[str]) -> Optional[Dict[str, Any]]:
        """按 id 取根目录配置。"""
        if not root_id:
            return None
        for r in self.roots:
            if r["id"] == root_id:
                return r
        return None

    def first(self) -> Optional[Dict[str, Any]]:
        return self.roots[0] if self.roots else None

    def public_list(self) -> List[Dict[str, Any]]:
        """给前端「此电脑」视图用的根目录列表。"""
        return [
            {
                "id": r["id"],
                "name": r["name"],
                "path": r["display_path"],
                "readonly": r["readonly"],
                "exists": r["exists"],
                # kind=drive 表示这是自动挂载的磁盘，前端会画成盘符图标
                "kind": r.get("kind", "folder"),
                "type_label": r.get("type_label", ""),
            }
            for r in self.roots
        ]

    # -- 解析 ---------------------------------------------------------------

    def resolve(self, root_id: Optional[str], rel: Optional[str]) -> Tuple[Dict[str, Any], str]:
        """
        「根标识 + 相对路径」-> (根配置, 绝对路径)。
        不传 root_id 时默认落到第一个根目录；传了但查不到则直接报错。
        """
        # ★ root_id 给了但查不到时必须报错，绝不能退回第一个根目录。
        # 改造前这里是 `self.get(root_id) or self.first()`，单用户时看不出问题；
        # 多用户下「拿着别人的根标识来访问」正好会走到这里，静默退回自己的根
        # 会让调用方以为操作成功了，页面显示的还是另一个目录，纯坑。
        if root_id:
            root = self.get(root_id)
            if root is None:
                raise PathSecurityError("没有这个可访问的根目录：%s" % root_id)
        else:
            root = self.first()
        if root is None:
            raise PathSecurityError("未配置任何可访问的根目录")
        abs_path = join_within_root(root["path"], rel or "")
        return root, abs_path

    def resolve_abs(self, abs_path: str) -> Tuple[Dict[str, Any], str]:
        """
        「绝对路径」-> (根配置, 绝对路径)。
        地址栏允许用户直接粘贴完整路径，这里负责判断它落在哪个根目录里。
        """
        if not abs_path or not str(abs_path).strip():
            raise PathSecurityError("路径不能为空")

        raw = str(abs_path).strip().strip('"')
        if raw.startswith("\\\\?\\"):
            raw = raw[4:]

        # 统一成系统分隔符后再归一化
        normalized = os.path.normpath(raw)
        real = os.path.realpath(normalized)

        for root in self.roots:
            if is_within(root["path"], real):
                return root, real

        raise PathSecurityError("该路径不在允许访问的根目录范围内")

    def to_rel(self, root: Dict[str, Any], abs_path: str) -> str:
        """绝对路径 -> 相对根目录的相对路径（统一用 '/' 分隔，方便前端拼 URL）。"""
        try:
            rel = os.path.relpath(abs_path, root["path"])
        except ValueError:
            raise PathSecurityError("路径与根目录不在同一盘符")
        if rel == ".":
            return ""
        return rel.replace("\\", "/")

    def parent_of(self, root: Dict[str, Any], abs_path: str) -> Optional[str]:
        """
        返回父目录的绝对路径。
        已经在根目录顶层时返回 None（前端据此禁用「向上」按钮）。
        """
        if os.path.normcase(os.path.normpath(abs_path)) == os.path.normcase(os.path.normpath(root["path"])):
            return None
        parent = os.path.dirname(abs_path)
        if not is_within(root["path"], parent):
            return None
        return parent

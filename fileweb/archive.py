# -*- coding: utf-8 -*-
"""
压缩与解压
==========

支持四种格式，读写能力与依赖各不相同：

    ZIP / TAR 系列（.zip .tar .tar.gz .tgz .tar.bz2 .tar.xz）—— 标准库，零依赖
    7z  —— py7zr（纯 Python，自带编解码器，**不依赖任何外部程序**）
    RAR —— WinRAR 的 UnRAR.exe（解压）/ Rar.exe（创建），需自动探测路径

为什么解压必须自己写而不是直接 extractall
========================================
解压是「把外部数据写进本地磁盘」，处理不当就是一个任意文件写入漏洞。
本项目已有的路径防护（security.is_within）是给「本来就在本地」的路径用的，
而压缩包里的条目名是**攻击者可以完全控制**的。所以这里逐条目校验：

  1. **Zip Slip（路径穿越）**：拒绝绝对路径、盘符（C:）、UNC（\\\\）、
     以及任何 `..` 段；再用 realpath 二次确认没跑出目标目录。
  2. **解压炸弹**：限制条目数与总解压体积，超过就中止（不是截断，是报错）。
  3. **符号链接 / 硬链接 / 设备文件**：ZIP 里靠 external_attr 判类型，
     TAR 里靠 member 类型，一律**跳过**，绝不按条目内容去创建链接 ——
     否则等于给了一个「写一个指向任意位置的链接」的跳板。
  4. **外部工具（RAR/7z）**：先把名称列表校验一遍，再解到目标目录下的
     暂存目录，最后按校验过的名字移动过去。
     这样即使外部工具自己不防穿越，也越不出暂存目录之外的地方。

中文文件名乱码（Windows 用户几乎必然遇到）
========================================
ZIP 规范要求文件名用 UTF-8 并在通用标志位里置 0x800；但 Windows 自带的
「发送到 → 压缩(zipped)文件夹」用的是**本地代码页**（简体中文即 GBK）
且**不置位**。Python 于是按 CP437 解码，"中文.txt" 会变成一串乱码。
这里在没有 UTF-8 标志时，尝试把 CP437 解出来的名字按 GBK 重解一次，
并且**只在结果确实含中日韩字符时**才采用 —— 避免把西欧语言的合法名字改坏。
"""

from __future__ import annotations

import bz2
import gzip
import lzma
import os
import re
import shutil
import stat as stat_module
import subprocess
import sys
import tarfile
import zipfile
from typing import Any, Dict, List, Optional, Tuple

from .security import PathSecurityError, is_within
from .fsops import OperationCancelled

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 扩展名 -> 格式。注意 .tar.gz 这种复合后缀要单独判。
_EXT_FORMAT: Dict[str, str] = {
    ".zip": "zip",
    ".tar": "tar",
    ".gz": "tar",       # 只要不是 .tar.gz 也会落到这里，交给内容和 tarfile 判
    ".tgz": "tar",
    ".bz2": "tar",
    ".tbz2": "tar",
    ".xz": "tar",
    ".txz": "tar",
    ".7z": "7z",
    ".rar": "rar",
}

# 创建时可选的目标格式（用户选的扩展名 -> 实际写出的格式）
_CREATE_FORMATS = ("zip", "tar", "tar.gz", "7z", "rar")

# 防御解压炸弹的默认上限（可用 config.json 的 archive 段覆盖）
DEFAULT_MAX_ENTRIES = 20000
DEFAULT_MAX_TOTAL_MB = 8192          # 单个压缩包解压后的总体积上限
DEFAULT_MAX_SINGLE_MB = 4096         # 单条目上限

# 暂存目录前缀（放在目标目录内，保证同卷 rename 是原子的）
_STAGING_PREFIX = ".fw_extract_"

# Windows 文件属性：带这个位的就是重解析点（符号链接 / 目录联接 / 挂载点）
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# WinRAR 命令行工具的常见位置
_RAR_TOOL_CANDIDATES = (
    r"C:\Program Files\WinRAR\Rar.exe",
    r"C:\Program Files (x86)\WinRAR\Rar.exe",
)
_UNRAR_TOOL_CANDIDATES = (
    r"C:\Program Files\WinRAR\UnRAR.exe",
    r"C:\Program Files (x86)\WinRAR\UnRAR.exe",
)


class ArchiveError(Exception):
    """压缩/解压相关错误的基类。路由层转成 4xx/5xx。"""


class ArchiveSecurityError(ArchiveError):
    """压缩包内容不安全（路径穿越、超限、非法链接）。"""


class ArchiveToolMissingError(ArchiveError):
    """缺少处理该格式所需的外部程序或库。"""


class ArchiveConflictError(ArchiveError):
    """目标位置已有同名项 —— 按约定直接拒绝，绝不覆盖。"""


# ---------------------------------------------------------------------------
# 外部工具探测
# ---------------------------------------------------------------------------

# 由路由层按 config.json 的 archive.rar_path / unrar_path 下发。
#
# 为什么做成模块级状态而不是给每个函数加参数：这两条路径在一次进程生命周期内
# 不会变，却要用在 list_entries / inspect / extract / create 四个入口里
# （解压走 rarfile，它必须知道 UnRAR.exe 在哪）。层层透传只会把签名弄脏，
# 而且很容易像之前那样漏掉一条路径 —— 当时的后果就是
# **archive.unrar_path 在解压时被完全忽略**，README 却写着可以配。
_RAR_PATH = ""
_UNRAR_PATH = ""


def configure_tools(rar_path: str = "", unrar_path: str = "") -> None:
    """把配置里的外部工具路径下发进来；留空表示按默认位置自动探测。"""
    global _RAR_PATH, _UNRAR_PATH
    _RAR_PATH = (rar_path or "").strip()
    _UNRAR_PATH = (unrar_path or "").strip()


def _explicit_tool_paths() -> Tuple[str, str]:
    """
    返回 (rar, unrar) 的「显式路径」，没有就返回空串交给自动探测。

    优先级：配置文件 > 环境变量（环境变量方便临时排障，不该压过配置）。
    """
    return (
        _RAR_PATH or os.environ.get("FW_RAR_PATH", ""),
        _UNRAR_PATH or os.environ.get("FW_UNRAR_PATH", ""),
    )


def find_rar_tools(rar_path: str = "", unrar_path: str = "") -> Tuple[Optional[str], Optional[str]]:
    """
    找出 Rar.exe（创建）与 UnRAR.exe（解压）的位置。

    优先用显式指定的路径（配置或环境变量）；没配就在常见安装位置里找；
    再找不到就退回 PATH（少数人会把 WinRAR 加进 PATH）。
    返回 (rar, unrar)，各自可能为 None。
    """
    def _pick(explicit: str, candidates) -> Optional[str]:
        if explicit:
            explicit = explicit.strip().strip('"')
            return explicit if os.path.isfile(explicit) else None
        for item in candidates:
            if os.path.isfile(item):
                return item
        name = os.path.basename(candidates[0])
        found = shutil.which(name)
        return found or None

    return _pick(rar_path, _RAR_TOOL_CANDIDATES), _pick(unrar_path, _UNRAR_TOOL_CANDIDATES)


# ---------------------------------------------------------------------------
# 条目名校验
# ---------------------------------------------------------------------------

def _check_member_name(name: str) -> Optional[List[str]]:
    """
    校验一个压缩包内条目名，返回安全的相对路径段列表。

    返回 None 表示这条应该被忽略（空名字、纯目录前缀之类）。
    不安全则抛 ArchiveSecurityError。
    """
    raw = str(name or "").replace("\\", "/")

    if not raw or raw in (".", "./"):
        return None
    # 绝对路径 / UNC
    if raw.startswith("/"):
        raise ArchiveSecurityError("条目使用了绝对路径，已拒绝：%s" % name)
    # 盘符（C:/ 或 C:）
    if re.match(r"^[A-Za-z]:", raw):
        raise ArchiveSecurityError("条目带盘符，已拒绝：%s" % name)

    parts: List[str] = []
    for seg in raw.split("/"):
        if seg in ("", "."):
            continue
        if seg == "..":
            raise ArchiveSecurityError("条目包含上级目录引用（..），已拒绝：%s" % name)
        if "\x00" in seg:
            raise ArchiveSecurityError("条目名含空字节，已拒绝")
        parts.append(seg)
    return parts or None


def _safe_target(dest_root: str, parts: List[str]) -> str:
    """把校验过的路径段拼成目标绝对路径，并做 realpath 二次确认。"""
    candidate = os.path.join(dest_root, *parts)
    real = os.path.realpath(candidate)
    if not is_within(dest_root, real):
        raise ArchiveSecurityError("条目越出目标目录，已拒绝：%s" % "/".join(parts))
    return real


# ---------------------------------------------------------------------------
# 中文文件名乱码修复
# ---------------------------------------------------------------------------

def _looks_like_cjk(text: str) -> bool:
    return any("\u4e00" <= ch <= "\u9fff" for ch in text)


def fix_zip_filename(info: zipfile.ZipInfo) -> str:
    """
    修正 Windows 资源管理器生成的 ZIP 里中文文件名的乱码。

    只在「没有 UTF-8 标志位」且「按本地代码页重解后确实得到中日韩字符」时才改，
    避免把西欧语言的合法名字弄坏。识别不了就原样返回。
    """
    name = info.filename
    if getattr(info, "flag_bits", 0) & 0x800:
        return name                      # 明确声明是 UTF-8，不动

    try:
        raw = name.encode("cp437")
    except UnicodeEncodeError:
        return name                      # 已经含非 CP437 字符，说明本来就是对的

    for encoding in ("gbk", "gb18030", "big5", "shift_jis"):
        try:
            fixed = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if fixed != name and _looks_like_cjk(fixed):
            return fixed
    return name


# ---------------------------------------------------------------------------
# 读取条目清单（用于预检与冲突判断）
# ---------------------------------------------------------------------------

def _entry_is_link_zip(info: zipfile.ZipInfo) -> bool:
    """ZIP 条目是不是符号链接（靠 external_attr 里的 Unix 模式位判断）。"""
    mode = info.external_attr >> 16
    return stat_module.S_ISLNK(mode)


# ---------------------------------------------------------------------------
# 单个文件的压缩流（裸 .gz / .bz2 / .xz）
# ---------------------------------------------------------------------------
#
# gzip/bzip2/xz 有两种用法：包一整个 tar（.tar.gz），或者只压**一个**文件
# （common：日志轮转出来的 app.log.gz）。后者不是 tar，tarfile 会报
# ReadError。以前这个异常不是 ArchiveError，会一路冒到路由层变成 500，
# 用户看到的是「服务器内部错误」，而他要的其实只是把日志解开。
# 所以这里补上单文件流：app.log.gz -> app.log。

_SINGLE_STREAM_SUFFIXES = (".gz", ".bz2", ".xz")

# 解压时用来开流的构造器；都是标准库，没有额外依赖
_SINGLE_STREAM_OPENERS = {
    ".gz": gzip.open,
    ".bz2": bz2.open,
    ".xz": lzma.open,
}

# 报给用户看的格式名（比 "gz"/"bz2" 更好认）
_SINGLE_STREAM_LABELS = {".gz": "gzip", ".bz2": "bzip2", ".xz": "xz"}


def _single_stream_kind(path: str) -> str:
    """
    判断路径看起来像不像「单个文件的压缩流」，像就返回压缩后缀，否则返回空串。

    这几个后缀必须排除掉：.tar.gz/.tar.bz2/.tar.xz 以及 .tgz/.tbz2/.txz
    都是明确的 tar 归档，不能按单文件解。
    （.tgz 之类本来就匹配不上这些后缀：它的结尾是 "tgz" 而不是 ".gz"。）
    """
    lower = (path or "").lower()
    for suffix in _SINGLE_STREAM_SUFFIXES:
        if lower.endswith(suffix) and not lower.endswith(".tar" + suffix):
            return suffix
    return ""


def _is_tar(path: str) -> bool:
    """能不能当 tar 打开。用来在「tar 还是单文件流」之间做判断。"""
    try:
        with tarfile.open(path, "r:*"):
            return True
    except (tarfile.TarError, OSError):
        return False


def _single_stream_name(path: str, suffix: str) -> str:
    """
    由压缩流文件名推出解压后的文件名：app.log.gz -> app.log。

    压缩流里没有存原始文件名（gz/bz2/xz 都不存，只有 .tar.gz 才有），
    所以只能去掉后缀。去掉后为空就退回「原名 + .out」，避免产出没名字的文件。
    """
    base = os.path.basename(path)
    stem = base[: -len(suffix)] if base.lower().endswith(suffix) else base
    stem = stem.strip() or base
    if stem == base:
        stem = base + ".out"
    return stem


def list_entries(archive_path: str, fmt: str,
                 max_entries: int = DEFAULT_MAX_ENTRIES) -> List[Dict[str, Any]]:
    """
    列出压缩包里的条目（名字已做校验与乱码修复）。

    返回 [{"name": 相对路径(用 /), "is_dir": bool, "size": int, "is_link": bool}]
    """
    entries: List[Dict[str, Any]] = []

    if fmt == "zip":
        try:
            handle = zipfile.ZipFile(archive_path)
        except zipfile.BadZipFile:
            # 不转成 ArchiveError 的话，BadZipFile 会一路冒到路由层变成 500，
            # 用户只看到「内部服务器错误」；而真实原因（这文件根本不是 zip）
            # 其实很容易说清楚 —— 「浏览压缩包」这种只读操作尤其不该 500。
            raise ArchiveError(
                "这不是有效的 ZIP 文件：可能已损坏、下载不完整，"
                "或者后缀与实际格式不符。")
        with handle as zf:
            infos = zf.infolist()
            # 加密包的内容根本读不出来。不提前拦的话，底层会抛
            # RuntimeError（"File ... is encrypted"）冒到路由层变成 500，
            # 而「这个包有密码」才是用户真正需要知道的信息。
            if any(info.flag_bits & 0x1 for info in infos):
                raise ArchiveError(
                    "这是一个加密的 ZIP，需要密码才能解压；本工具暂不支持加密压缩包。")
            for info in infos:
                if len(entries) >= max_entries:
                    raise ArchiveSecurityError("压缩包条目数超过上限 %d" % max_entries)
                name = fix_zip_filename(info)
                if _check_member_name(name) is None:
                    continue
                entries.append({
                    "name": name.replace("\\", "/"),
                    "is_dir": info.is_dir(),
                    "size": int(info.file_size),
                    "is_link": _entry_is_link_zip(info),
                })
        return entries

    if fmt == "tar":
        # 裸 .gz/.bz2/.xz 可能只是「一个文件的压缩流」而不是 tar。
        # 打不开 tar 时退到单文件模式，这样 app.log.gz 能解成 app.log，
        # 而不是把 tarfile.ReadError 冒到路由层变成 500。
        if not _is_tar(archive_path):
            suffix = _single_stream_kind(archive_path)
            if not suffix:
                raise ArchiveError(
                    "这不是一个有效的 TAR 归档：文件可能已损坏，"
                    "或后缀与实际格式不符。")
            name = _single_stream_name(archive_path, suffix)
            # 名字是从「压缩包自己的文件名」推出来的，理论上安全，
            # 但还是过一遍统一的校验（能挡住 "." / ".." 这种极端文件名）。
            if not _check_member_name(name):
                raise ArchiveError(
                    "无法从文件名推断出解压后的名字，请先给压缩包改名：%s"
                    % os.path.basename(archive_path))
            return [{
                "name": name,
                # 单文件压缩流不存原始大小（gz 只有 ISIZE，bz2/xz 没有），
                # 所以这里报 0；真正的体积上限靠解压时边读边限流来兜底。
                "size": 0,
                "is_dir": False,
                "is_link": False,
                "_single_stream": suffix,
            }]
        with tarfile.open(archive_path, "r:*") as tf:
            for member in tf.getmembers():
                if len(entries) >= max_entries:
                    raise ArchiveSecurityError("压缩包条目数超过上限 %d" % max_entries)
                if _check_member_name(member.name) is None:
                    continue
                entries.append({
                    "name": member.name.replace("\\", "/"),
                    "is_dir": member.isdir(),
                    "size": int(member.size),
                    # 链接/设备文件都标记出来，解压时跳过
                    "is_link": member.issym() or member.islnk()
                               or member.ischr() or member.isblk() or member.isfifo(),
                })
        return entries

    if fmt == "7z":
        import py7zr
        with py7zr.SevenZipFile(archive_path, "r") as zf:
            if _needs_password(zf):
                raise ArchiveError(
                    "这是一个加密的 7z，需要密码才能解压；本工具暂不支持加密压缩包。")
            for info in zf.list():
                if len(entries) >= max_entries:
                    raise ArchiveSecurityError("压缩包条目数超过上限 %d" % max_entries)
                if _check_member_name(info.filename) is None:
                    continue
                entries.append({
                    "name": info.filename.replace("\\", "/"),
                    "is_dir": bool(info.is_directory),
                    "size": int(info.uncompressed or 0),
                    # py7zr 的 FileInfo 带 is_symlink（不同版本字段名略有差异）
                    "is_link": bool(getattr(info, "is_symlink", False)),
                })
        return entries

    if fmt == "rar":
        import rarfile
        _configure_rarfile(rarfile)
        with rarfile.RarFile(archive_path) as rf:
            if _needs_password(rf):
                raise ArchiveError(
                    "这是一个加密的 RAR，需要密码才能解压；本工具暂不支持加密压缩包。")
            for info in rf.infolist():
                if len(entries) >= max_entries:
                    raise ArchiveSecurityError("压缩包条目数超过上限 %d" % max_entries)
                if _check_member_name(info.filename) is None:
                    continue
                entries.append({
                    "name": info.filename.replace("\\", "/"),
                    "is_dir": bool(info.isdir()),
                    "size": int(info.file_size),
                    "is_link": bool(info.is_symlink()),
                })
        return entries

    raise ArchiveError("不支持的压缩格式：%s" % fmt)


def _needs_password(handle: Any) -> bool:
    """
    判断 7z / RAR 是不是加密包。

    两种库都提供 needs_password()，但不同版本的可选性和返回时机不完全一致，
    所以这里做防御式调用：拿不到就当作「没加密」，让后面的正常流程去报错，
    总比因为我们自己探测失败而把一个正常压缩包拒之门外要好。
    """
    probe = getattr(handle, "needs_password", None)
    if not callable(probe):
        return False
    try:
        return bool(probe())
    except Exception:      # noqa: BLE001 - 探测失败不该影响正常解压
        return False


def _configure_rarfile(rarfile_module) -> None:
    """
    让 rarfile 找到 UnRAR.exe。

    rarfile 自己不实现解压算法，必须调用外部 unrar；而 WinRAR 默认
    不把安装目录加进 PATH，所以要显式告诉它完整路径。
    """
    unrar, _rar = find_rar_tools(*_explicit_tool_paths())
    if unrar:
        rarfile_module.UNRAR_TOOL = unrar


# ---------------------------------------------------------------------------
# 解压
# ---------------------------------------------------------------------------

def _check_limits(entries: List[Dict[str, Any]], max_total_mb: int,
                  max_single_mb: int) -> int:
    """体积上限检查，返回解压后总体积。超限直接抛错（不做截断）。"""
    total = 0
    single_limit = max_single_mb * 1024 * 1024
    for item in entries:
        size = item.get("size") or 0
        if size > single_limit:
            raise ArchiveSecurityError(
                "压缩包内单个文件超过上限 %d MB：%s" % (max_single_mb, item["name"]))
        total += size
        if total > max_total_mb * 1024 * 1024:
            raise ArchiveSecurityError(
                "压缩包解压后总体积超过上限 %d MB，已中止" % max_total_mb)
    return total


def _top_names(entries: List[Dict[str, Any]]) -> List[str]:
    """取所有条目的第一层名字（用于重名判断）。"""
    tops: List[str] = []
    for item in entries:
        first = item["name"].split("/")[0]
        if first and first not in tops:
            tops.append(first)
    return tops


def _check_conflicts(dest_root: str, entries: List[Dict[str, Any]]) -> List[str]:
    """
    检查目标目录里是否已有同名项。

    按需求「重名直接拒绝」：只要有一个顶层名字已存在就不解压，
    并把冲突的名字都列出来，让用户能改名后重试（而不是丢一句「解压失败」）。
    """
    conflicts = [name for name in _top_names(entries)
                 if os.path.exists(os.path.join(dest_root, name))]
    if conflicts:
        raise ArchiveConflictError(
            "目标位置已有同名项，为避免覆盖已中止解压：%s" % "、".join(conflicts[:10]))
    return conflicts


def top_level_conflicts(entries: List[Dict[str, Any]], dest_root: str) -> List[str]:
    """
    条目里的顶层名字与目标目录中已有项的重名列表。

    抽成公开函数，是为了让「只浏览、不解压」的接口也能复用同一套判断：
    解压遇到同名顶层项是**整体中止**的（见 extract），如果浏览时就能提前
    看到冲突，用户不必点完才发现被拒。判断逻辑只有这一份，两边不会走偏。
    """
    if not os.path.isdir(dest_root):
        return []
    return [name for name in _top_names(entries)
            if os.path.exists(os.path.join(dest_root, name))]


def inspect(archive_path: str, dest_root: str, *, fmt: str = "",
            max_entries: int = DEFAULT_MAX_ENTRIES,
            max_total_mb: int = DEFAULT_MAX_TOTAL_MB,
            max_single_mb: int = DEFAULT_MAX_SINGLE_MB) -> Dict[str, Any]:
    """
    解压前的预检：读条目、查体积上限、查目标重名。**不写任何文件**。

    单独抽出来是为了让路由层能在真正落盘之前就把「会撞名」「空间不够」
    这类可预期的问题报给用户，而不是解压到一半才失败。
    不安全（穿越/超限）会在这里就抛出去，此时磁盘还没被动过。
    """
    fmt = fmt or detect_format(archive_path)
    if not fmt:
        raise ArchiveError("无法识别的压缩格式：%s" % os.path.basename(archive_path))

    entries = list_entries(archive_path, fmt, max_entries=max_entries)
    total_bytes = _check_limits(entries, max_total_mb, max_single_mb)

    conflicts = top_level_conflicts(entries, dest_root)

    return {
        "format": fmt,
        "entries": len(entries),
        "top_names": _top_names(entries)[:50],
        "total_bytes": total_bytes,
        "conflicts": conflicts,
    }


def _extract_single_stream(archive_path: str, dest_root: str, suffix: str,
                           out_name: str, max_single_mb: int) -> Dict[str, Any]:
    """
    解开「单个文件的压缩流」：app.log.gz -> app.log。

    **必须边读边限流**：这是最容易做成解压炸弹的形态 —— 一个几十 KB 的 .gz
    可以膨胀到几百 GB，等解完再去看文件大小，磁盘已经满了。
    所以这里是流式写入，一超过上限就中断并删掉半成品。
    """
    opener = _SINGLE_STREAM_OPENERS[suffix]
    target = _safe_target(dest_root, [out_name])
    os.makedirs(os.path.dirname(target) or dest_root, exist_ok=True)

    limit = max(0, max_single_mb) * 1024 * 1024
    written = 0
    oversize = False

    try:
        with opener(archive_path, "rb") as src, open(target, "wb") as dst:
            while True:
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                written += len(chunk)
                if limit and written > limit:
                    oversize = True
                    break
                dst.write(chunk)
    except (OSError, EOFError, lzma.LZMAError) as exc:
        # 压缩流损坏（截断、非 gzip 数据……）要给明确说法，而不是 500
        try:
            if os.path.exists(target):
                os.unlink(target)
        except OSError:
            pass
        raise ArchiveError("解压失败：压缩流已损坏或不是有效的 %s 数据（%s）"
                           % (_SINGLE_STREAM_LABELS.get(suffix, suffix), exc))

    if oversize:
        try:
            os.unlink(target)
        except OSError:
            pass
        raise ArchiveSecurityError(
            "解压后的文件超过单个文件上限 %d MB，已中止（可能不是正常的日志/数据文件）"
            % max_single_mb)

    return {
        "format": _SINGLE_STREAM_LABELS.get(suffix, suffix),
        "entries": 1,
        "extracted": 1,
        "skipped": [],
        "total_bytes": written,
    }


def extract(archive_path: str, dest_root: str, *, fmt: str = "",
            max_entries: int = DEFAULT_MAX_ENTRIES,
            max_total_mb: int = DEFAULT_MAX_TOTAL_MB,
            max_single_mb: int = DEFAULT_MAX_SINGLE_MB,
            overwrite: bool = False,
            progress=None, should_cancel=None) -> Dict[str, Any]:
    """
    把压缩包解压到 dest_root。返回统计信息。

    overwrite=False（默认）时遇重名直接拒绝，绝不覆盖已有文件。

    progress / should_cancel 是给「后台任务」用的可选回调：:

        progress(items_delta, bytes_delta, name)
        should_cancel() -> bool

    ★ 精度不一致是**已知且有意的**：zip / tar 能在 Python 里逐条目写，
      所以能报细粒度进度；而 7z / rar 走的是外部库的 extractall，
      中间过程观察不到，只能报「开始 / 结束」两档。与其编一个假进度，
      不如让前端如实显示 —— 否则用户会以为卡死了。
    """
    fmt = fmt or detect_format(archive_path)
    if not fmt:
        raise ArchiveError("无法识别的压缩格式：%s" % os.path.basename(archive_path))

    entries = list_entries(archive_path, fmt, max_entries=max_entries)
    total_bytes = _check_limits(entries, max_total_mb, max_single_mb)
    if not overwrite:
        _check_conflicts(dest_root, entries)

    os.makedirs(dest_root, exist_ok=True)

    extracted = 0
    skipped: List[str] = []

    if fmt in ("zip", "tar"):
        # 这两种能在 Python 里逐条目写，直接落盘，不需要暂存目录，
        # 也因此能逐条目报进度、逐条目响应取消。
        if fmt == "zip":
            with zipfile.ZipFile(archive_path) as zf:
                for info in zf.infolist():
                    if should_cancel is not None and should_cancel():
                        raise OperationCancelled("解压已取消")
                    name = fix_zip_filename(info)
                    parts = _check_member_name(name)
                    if parts is None:
                        continue
                    target = _safe_target(dest_root, parts)
                    if _entry_is_link_zip(info):
                        skipped.append("%s（符号链接，已跳过）" % name)
                        continue
                    if info.is_dir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    extracted += 1
                    if progress is not None:
                        progress(1, int(info.file_size), name)
        else:
            # 单文件压缩流（app.log.gz）：走流式解压，别用 tarfile
            single = entries[0].get("_single_stream") if entries else ""
            if single:
                return _extract_single_stream(archive_path, dest_root, single,
                                              entries[0]["name"], max_single_mb)
            with tarfile.open(archive_path, "r:*") as tf:
                for member in tf.getmembers():
                    if should_cancel is not None and should_cancel():
                        raise OperationCancelled("解压已取消")
                    parts = _check_member_name(member.name)
                    if parts is None:
                        continue
                    target = _safe_target(dest_root, parts)
                    if member.issym() or member.islnk() or member.ischr() \
                            or member.isblk() or member.isfifo():
                        skipped.append("%s（链接/特殊文件，已跳过）" % member.name)
                        continue
                    if member.isdir():
                        os.makedirs(target, exist_ok=True)
                        continue
                    if not member.isfile():
                        skipped.append("%s（非常规文件，已跳过）" % member.name)
                        continue
                    os.makedirs(os.path.dirname(target), exist_ok=True)
                    src = tf.extractfile(member)
                    if src is None:
                        skipped.append("%s（读取失败，已跳过）" % member.name)
                        continue
                    with src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst, 1024 * 1024)
                    extracted += 1
                    if progress is not None:
                        progress(1, int(member.size or 0), member.name)
    else:
        # 7z / rar：先解到暂存目录，再按校验过的名字搬过去。
        # 这样即使外部工具（或多版本 py7zr）自己不防穿越，也越不出暂存目录。
        staging = os.path.join(dest_root, _STAGING_PREFIX + os.urandom(4).hex())
        os.makedirs(staging, exist_ok=True)
        try:
            if fmt == "7z":
                import py7zr
                with py7zr.SevenZipFile(archive_path, "r") as zf:
                    zf.extractall(path=staging)
            else:
                import rarfile
                _configure_rarfile(rarfile)
                unrar, _rar = find_rar_tools(*_explicit_tool_paths())
                if not unrar:
                    raise ArchiveToolMissingError(
                        "解压 RAR 需要 WinRAR 的 UnRAR.exe，未找到。"
                        "请安装 WinRAR，或在 config.json 的 archive.unrar_path 里指定完整路径。")
                rarfile.UNRAR_TOOL = unrar
                with rarfile.RarFile(archive_path) as rf:
                    rf.extractall(path=staging)

            for item in entries:
                parts = _check_member_name(item["name"])
                if parts is None:
                    continue
                if item.get("is_link"):
                    skipped.append("%s（符号链接，已跳过）" % item["name"])
                    continue
                src = os.path.join(staging, *parts)
                dst = _safe_target(dest_root, parts)
                if not os.path.exists(src):
                    continue
                if os.path.isdir(src):
                    os.makedirs(dst, exist_ok=True)
                else:
                    os.makedirs(os.path.dirname(dst), exist_ok=True)
                    shutil.move(src, dst)
                    extracted += 1
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    return {
        "format": fmt,
        "entries": len(entries),
        "extracted": extracted,
        "skipped": skipped,
        "total_bytes": total_bytes,
    }


# ---------------------------------------------------------------------------
# 创建压缩包
# ---------------------------------------------------------------------------

def detect_format(path: str) -> str:
    """按扩展名判断压缩格式；认不出来返回空串。"""
    lower = (path or "").lower()
    for suffix in (".tar.gz", ".tar.bz2", ".tar.xz"):
        if lower.endswith(suffix):
            return "tar"
    return _EXT_FORMAT.get(os.path.splitext(lower)[1], "")


def _is_reparse_point(path: str) -> bool:
    """
    判断路径是不是「重解析点」（符号链接、目录联接 junction、挂载点……）。

    os.path.islink() 在 Windows 上**认不出目录联接**，所以必须再看
    st_file_attributes 里的 FILE_ATTRIBUTE_REPARSE_POINT 标志位。
    非 Windows 平台上 st_file_attributes 不存在，getattr 会拿到 0，
    此时就只剩 islink 这一条判断（Linux 上链接本来就由 islink 覆盖）。
    """
    try:
        if os.path.islink(path):
            return True
        info = os.lstat(path)
    except OSError:
        return False
    attrs = getattr(info, "st_file_attributes", 0)
    return bool(attrs & _FILE_ATTRIBUTE_REPARSE_POINT)


def _walk_sources(sources: List[str]) -> Tuple[List[Tuple[str, str]], List[str]]:
    """
    把「源路径列表」展开成 ([(绝对路径, 包内相对名)], [被跳过的项])。

    规则：传入的每一项本身作为包内顶层名字，目录递归展开并保留结构；
    但**不进入任何重解析点**（符号链接 / 目录联接 junction / 挂载点）。

    为什么必须显式剪枝：Windows 上 os.path.islink() 对目录联接返回 False，
    而 os.walk 会把联接当普通目录继续往下走。于是一个指向 C:\\ 的联接就足以
    让打包无限递归，或者把整块盘的内容装进包里 —— 既不是用户想要的，
    也可能把不该带走的东西一起打包出去。所以直接在 dirnames 里剪掉，
    而不是靠 os.walk 的 followlinks 参数（它对联接不起作用）。
    """
    items: List[Tuple[str, str]] = []
    skipped: List[str] = []

    for src in sources:
        src = os.path.abspath(src)
        base = os.path.basename(src.rstrip("\\/")) or src

        if _is_reparse_point(src):
            # 用户直接选中了一个联接/符号链接本身，不跟进
            skipped.append("%s（符号链接/目录联接，已跳过）" % base)
            continue

        if os.path.isdir(src):
            parent = os.path.dirname(src)
            for dirpath, dirnames, filenames in os.walk(src):
                # 就地剪枝：把联接从待遍历列表里摘掉，os.walk 就不会进去了
                for name in list(dirnames):
                    full = os.path.join(dirpath, name)
                    if _is_reparse_point(full):
                        dirnames.remove(name)
                        skipped.append("%s（符号链接/目录联接，已跳过）"
                                       % os.path.relpath(full, parent).replace("\\", "/"))
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    if _is_reparse_point(full):
                        skipped.append("%s（符号链接，已跳过）"
                                       % os.path.relpath(full, parent).replace("\\", "/"))
                        continue
                    items.append((full, os.path.relpath(full, parent).replace("\\", "/")))
        else:
            items.append((src, base))

    return items, skipped


def create(sources: List[str], dest_path: str, *, fmt: str = "",
           rar_path: str = "", level: int = 3) -> Dict[str, Any]:
    """
    把 sources 打包成 dest_path。返回统计信息。

    level: 压缩级别 0-5（0 = 仅存储；各格式的实际含义略有差异）。
    """
    fmt = fmt or detect_format(dest_path)
    if fmt not in ("zip", "tar", "7z", "rar"):
        raise ArchiveError("不支持的压缩格式（支持 zip / tar / tar.gz / 7z / rar）")

    sources = [os.path.abspath(p) for p in sources if os.path.exists(p)]
    if not sources:
        raise ArchiveError("没有可压缩的内容")

    items, skipped = _walk_sources(sources)
    if not items:
        raise ArchiveError("选中的内容为空，或全部是无法打包的符号链接/目录联接")

    dest_path = os.path.abspath(dest_path)
    os.makedirs(os.path.dirname(dest_path), exist_ok=True)

    if fmt == "zip":
        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
            for full, arc in items:
                # 已经是压缩过的格式就不必再压，省 CPU（与下载打包一致）
                compress = zipfile.ZIP_STORED if os.path.splitext(full)[1].lower() in (
                    ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".jpg", ".jpeg",
                    ".png", ".gif", ".webp", ".mp3", ".aac", ".mp4", ".mkv", ".webm",
                    ".docx", ".xlsx", ".pptx", ".pdf",
                ) else zipfile.ZIP_DEFLATED
                zf.write(full, arc, compress_type=compress)
    elif fmt == "tar":
        # 按后缀决定压缩方式。这里只认「压缩后缀本身」，不要求前面必须有 .tar：
        # 之前只匹配 .tar.gz/.tgz 这类组合，于是用户写「备份.gz」时会落到
        # mode="w"，产出一个**没压缩的 tar 却叫 .gz** —— 任何解压工具都打不开它。
        # 现在「备份.gz」会老老实实输出 gzip 流。
        lower = dest_path.lower()
        # 每个压缩族都要把「别名后缀」一起认下来：
        # .tgz/.tbz2/.txz 并不以 .gz/.bz2/.xz 结尾（结尾分别是 tgz/tbz2/txz），
        # 只判 .gz 会把它们漏掉、产出没压缩的 tar。
        if lower.endswith((".gz", ".tgz")):
            mode = "w:gz"
        elif lower.endswith((".bz2", ".tbz2")):
            mode = "w:bz2"
        elif lower.endswith((".xz", ".txz")):
            mode = "w:xz"
        else:
            mode = "w"
        with tarfile.open(dest_path, mode) as tf:
            for full, arc in items:
                tf.add(full, arcname=arc, recursive=False)
    elif fmt == "7z":
        import py7zr
        filters = None
        if level <= 0:
            filters = [{"id": py7zr.FILTER_COPY}]
        with py7zr.SevenZipFile(dest_path, "w", filters=filters) as zf:
            for full, arc in items:
                zf.write(full, arcname=arc)
    else:  # rar —— 只能靠 WinRAR 的命令行压缩器
        # 显式传入的 rar_path 优先，其次才是配置下发的路径。
        # 创建只需要 Rar.exe，用不到 unrar。
        tool, _unrar = find_rar_tools(rar_path or _RAR_PATH, _UNRAR_PATH)
        if not tool:
            raise ArchiveToolMissingError(
                "创建 RAR 需要 WinRAR 的 Rar.exe，未找到。请安装 WinRAR，"
                "或在 config.json 的 archive.rar_path 里指定完整路径。")
        # Rar.exe 的 -ep1 表示不把源路径的父目录打进包内；
        # 我们用 (cwd, 相对名) 的方式调用，包内就是干净的名字。
        #
        # -ol 是必须的，不是可选优化：RAR 默认会**跟进**目录联接/符号链接，
        # 于是「打包一个含联接的目录」会把联接指向的外部内容一起装进包里
        # （实测：src\linkdir -> D:\outside，不加 -ol 时 SECRET.txt 会进包）。
        # 加了 -ol 之后 RAR 把链接本身存成链接条目、不再展开，
        # 包内结构与不加时一致。我们在 _walk_sources 里已经剪过一遍树，
        # 但 RAR 有自己的遍历逻辑，所以两边都要防。
        argv = [tool, "a", "-r", "-y", "-ep1", "-ol", "-m%d" % max(0, min(5, level)), dest_path]
        argv += [os.path.basename(p.rstrip("\\/")) or p for p in sources]
        proc = subprocess.run(argv, cwd=os.path.dirname(sources[0]) or None,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                              timeout=3600, check=False)
        if proc.returncode not in (0, 1):     # Rar 返回 1 表示有警告但成功
            detail = (proc.stdout or b"").decode("utf-8", "replace").strip()[:300]
            raise ArchiveError("Rar.exe 打包失败（代码 %d）：%s" % (proc.returncode, detail))

    try:
        size = os.path.getsize(dest_path)
    except OSError:
        size = 0

    return {
        "format": fmt,
        "dest": dest_path,
        "file_count": len(items),
        "source_count": len(sources),
        "size": size,
        "skipped": skipped,
    }

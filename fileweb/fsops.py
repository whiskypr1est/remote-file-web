# -*- coding: utf-8 -*-
"""
文件系统操作模块
================

集中实现所有目录/文件操作，供路由层调用。包含：

    * 文件类型识别（图标类型、是否可预览）
    * 目录列举与排序
    * 新建文件夹、重命名、删除（回收站 / 永久）
    * 重名自动改名、上传目标路径解析
    * 批量打包为 ZIP

本模块假定传入的路径**已经过 PathResolver 的安全校验**。
不过为了防御性编程，涉及创建/改名的地方仍会再调用
security.sanitize_filename 对「文件名部分」做一次清洗。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat as stat_module
import time
import zipfile
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .security import PathSecurityError, is_blocked_extension, sanitize_filename
from .thumbs import IMAGE_EXTENSIONS, is_image

# ---------------------------------------------------------------------------
# 文件类型表
# ---------------------------------------------------------------------------

# 扩展名 -> 前端图标类型
TYPE_MAP: Dict[str, str] = {}


def _reg(kind: str, extensions: Iterable[str]) -> None:
    for ext in extensions:
        TYPE_MAP[ext] = kind


_reg("image", IMAGE_EXTENSIONS)
_reg("video", {".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v", ".mpg", ".mpeg", ".3gp", ".ts", ".rmvb", ".rm"})
_reg("audio", {".mp3", ".wav", ".flac", ".aac", ".ogg", ".oga", ".m4a", ".wma", ".ape", ".opus", ".aiff", ".mid", ".midi"})
_reg("pdf", {".pdf"})
_reg("document", {".doc", ".docx", ".docm", ".dot", ".dotx", ".rtf", ".odt", ".txt", ".md", ".markdown", ".log", ".tex", ".wps"})
_reg("spreadsheet", {".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".ods", ".et"})
_reg("presentation", {".ppt", ".pptx", ".pptm", ".pot", ".potx", ".odp", ".dps"})
_reg("archive", {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz", ".tgz", ".cab", ".iso", ".jar", ".war"})
_reg("code", {
    ".json", ".xml", ".yml", ".yaml", ".ini", ".conf", ".cfg", ".toml", ".properties", ".env",
    ".html", ".htm", ".css", ".js", ".mjs", ".cjs", ".ts", ".jsx", ".tsx", ".vue",
    ".py", ".pyw", ".java", ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php",
    ".sh", ".bash", ".bat", ".cmd", ".ps1", ".sql", ".lua", ".pl", ".swift", ".kt", ".scala",
    ".gradle", ".makefile", ".dockerfile", ".gitignore", ".editorconfig",
})

# 可以直接在浏览器里预览的类型
PREVIEWABLE_TYPES = {"image", "video", "audio", "pdf", "document", "spreadsheet", "presentation", "code"}

# 这些扩展名的文件已经压过一次了，打包时不再压缩，省 CPU
_ALREADY_COMPRESSED = {
    ".zip", ".rar", ".7z", ".gz", ".bz2", ".xz", ".tgz", ".cab",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".heic",
    ".mp3", ".aac", ".ogg", ".m4a", ".flac", ".opus",
    ".mp4", ".mkv", ".webm", ".mov", ".m4v",
    ".docx", ".xlsx", ".pptx", ".pdf",
}


def classify(name: str, is_dir: bool) -> str:
    """返回文件类型标识（用于选图标、决定预览方式）。"""
    if is_dir:
        return "folder"
    ext = os.path.splitext(name or "")[1].lower()
    return TYPE_MAP.get(ext, "other")


def is_previewable(name: str, kind: str) -> bool:
    """该文件是否支持在窗口内预览。"""
    if kind not in PREVIEWABLE_TYPES:
        return False
    # 部分"文档"型扩展名（如 .wps/.et/.dps）我们没有实现预览，这里按类型放行，
    # 由预览层再决定给不给友好提示（例如提示下载后用本地软件打开）。
    return True


def human_size(size: int, is_dir: bool = False) -> str:
    """把字节数格式化成人类可读字符串。"""
    if is_dir:
        return ""
    try:
        size = int(size)
    except (TypeError, ValueError):
        return ""
    if size < 1024:
        return "%d B" % size
    units = ["KB", "MB", "GB", "TB", "PB"]
    value = float(size)
    for unit in units:
        value /= 1024.0
        if value < 1024 or unit == units[-1]:
            return ("%.1f %s" % (value, unit)) if value < 100 else ("%.0f %s" % (value, unit))
    return "%.1f PB" % value


def _fmt_time(timestamp: float) -> str:
    """格式化成 Windows 资源管理器风格的本地时间。"""
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(timestamp))
    except (OSError, ValueError, OverflowError):
        return ""


# ---------------------------------------------------------------------------
# 目录列举
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 协作式取消 + 分块复制
# ---------------------------------------------------------------------------

# 分块大小。1MB 是「进度更新够细」与「系统调用次数够少」之间的折中：
# 再小会让大文件的 write 次数暴涨，再大则进度条会一跳一跳的。
COPY_CHUNK_BYTES = 1024 * 1024


class OperationCancelled(Exception):
    """
    调用方要求中止这次操作（协作式取消）。

    刻意不做成「强杀」：Python 没法安全地打断一个正在写文件的线程，
    硬杀会留下写了一半的文件。所以取消的语义是「在下一个检查点尽快退出」，
    调用方拿到这个异常后应当把结果如实标成「已取消」而不是「失败」。
    """


def copy_file_tracked(source: str, target: str, progress=None,
                      should_cancel=None) -> None:
    """
    分块复制单个文件，每块回调一次进度、每个块边界检查一次取消。

    没有用 shutil.copy2：它一口气拷完，中途既报不了进度、也响应不了取消 ——
    而「复制一个几十 GB 的文件」恰恰是最需要这两样东西的场景，
    也正是本项目「界面看着像卡死」的主要来源。
    """
    with open(source, "rb") as src, open(target, "wb") as dst:
        while True:
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("复制已取消")
            chunk = src.read(COPY_CHUNK_BYTES)
            if not chunk:
                break
            dst.write(chunk)
            if progress is not None:
                progress(len(chunk))

    try:
        shutil.copystat(source, target)
    except OSError:
        # 时间戳/权限位复制失败不该让整次复制算失败：有的目标文件系统
        # 本来就不支持（FAT、部分网络盘），内容已经写对了才是关键。
        pass


# Windows 文件属性位
_FILE_ATTRIBUTE_HIDDEN = 0x2
_FILE_ATTRIBUTE_SYSTEM = 0x4
_FILE_ATTRIBUTE_READONLY = 0x1
# 重解析点：目录联接（junction）与符号链接都带这一位。
# ★ os.path.islink() 在 Windows 上**认不出目录联接**，而联接足以让递归遍历
#   成环（C:\Documents and Settings 就指向 C:\Users 那一类），所以遍历时
#   必须额外看这一位 —— 只看 islink 会漏掉联接。
_FILE_ATTRIBUTE_REPARSE_POINT = 0x400


def is_link_or_junction(path: str) -> bool:
    """
    判断路径是不是符号链接 / 目录联接（junction）。

    递归遍历（按文件名搜索、打包）必须先用它把这类目录剔掉：它们既可能成环，
    也可能把遍历带出根目录之外，而 os.walk(followlinks=False) 在 Windows 上
    拦不住目录联接（联接在 POSIX 语义里不算 symlink）。

    取不到状态时**保守返回 True**（当作「别进去」）：遍历少走一个目录只是漏结果，
    跟着一个坏链接钻进去却可能让请求再也回不来。
    """
    try:
        if os.path.islink(path):
            return True
        attributes = getattr(os.stat(path), "st_file_attributes", 0) or 0
    except OSError:
        return True
    return bool(attributes & _FILE_ATTRIBUTE_REPARSE_POINT)


def _entry_from_direntry(entry: os.DirEntry) -> Optional[Dict[str, Any]]:
    """把 scandir 的一条记录转成前端需要的结构。任何异常都跳过该条目。"""
    try:
        # 对目录联接 / 符号链接，用 follow_symlinks=True 才能正确识别成文件夹
        is_dir = entry.is_dir(follow_symlinks=True)
        try:
            st = entry.stat(follow_symlinks=False)
            if not is_dir:
                # 非目录时补一次跟随链接的 stat，拿到真实大小
                st = entry.stat(follow_symlinks=True)
        except OSError:
            st = entry.stat()

        name = entry.name
        ext = os.path.splitext(name)[1].lower()

        attributes = getattr(st, "st_file_attributes", 0) or 0
        hidden = bool(attributes & _FILE_ATTRIBUTE_HIDDEN) or name.startswith(".")
        is_link = entry.is_symlink()

        kind = classify(name, is_dir)

        return {
            "name": name,
            "is_dir": bool(is_dir),
            "size": 0 if is_dir else int(st.st_size),
            "size_text": human_size(st.st_size, is_dir),
            "mtime": float(st.st_mtime),
            "mtime_text": _fmt_time(st.st_mtime),
            "ctime": float(st.st_ctime),
            "ext": ext if not is_dir else "",
            "type": kind,
            "previewable": is_previewable(name, kind),
            "hidden": hidden,
            "readonly": bool(attributes & _FILE_ATTRIBUTE_READONLY) or not os.access(str(entry.path), os.W_OK),
            "link": is_link,
        }
    except OSError:
        return None
    except Exception:  # noqa: BLE001
        return None


def _sort_key(entry: Dict[str, Any], field: str):
    """生成排序键。名称排序使用大小写无关，且尽量让数字按自然顺序排。"""
    if field == "size":
        return entry["size"]
    if field == "mtime":
        return entry["mtime"]
    if field == "type":
        return (entry["type"], entry["name"].lower())

    name = entry["name"]
    # 自然排序：把数字片段变成 (0, 数值) 元组，字母片段变成 (1, 字符串)
    parts: List[Tuple[int, Any]] = []
    buffer = ""
    for ch in name:
        if ch.isdigit():
            buffer += ch
        else:
            if buffer:
                parts.append((0, int(buffer)))
                buffer = ""
            parts.append((1, ch.lower()))
    if buffer:
        parts.append((0, int(buffer)))
    return parts


def list_directory(abs_path: str, sort_field: str = "name", order: str = "asc",
                   show_hidden: bool = True) -> Dict[str, Any]:
    """
    列举目录内容。

    sort_field: name | size | mtime | type
    order:      asc | desc
    文件夹始终排在文件前面（与 Windows 资源管理器一致）。
    """
    if not os.path.isdir(abs_path):
        raise FileNotFoundError("目录不存在：%s" % abs_path)

    entries: List[Dict[str, Any]] = []
    skipped = 0

    try:
        with os.scandir(abs_path) as iterator:
            for direntry in iterator:
                item = _entry_from_direntry(direntry)
                if item is None:
                    skipped += 1
                    continue
                if item["hidden"] and not show_hidden:
                    continue
                entries.append(item)
    except PermissionError as exc:
        raise PermissionError("没有权限读取该目录") from exc

    reverse = str(order).lower() == "desc"
    field = sort_field if sort_field in ("name", "size", "mtime", "type") else "name"

    folders = [e for e in entries if e["is_dir"]]
    files = [e for e in entries if not e["is_dir"]]

    folders.sort(key=lambda e: _sort_key(e, field), reverse=reverse)
    files.sort(key=lambda e: _sort_key(e, field), reverse=reverse)

    ordered = folders + files

    return {
        "entries": ordered,
        "dir_count": len(folders),
        "file_count": len(files),
        "total": len(ordered),
        "skipped": skipped,
        "sort": field,
        "order": "desc" if reverse else "asc",
    }


# ---------------------------------------------------------------------------
# 重名处理与新建
# ---------------------------------------------------------------------------

def unique_path(directory: str, name: str, max_tries: int = 9999) -> str:
    """
    在目录内为 name 找一个不冲突的路径：
    已存在时会依次尝试 "name (1).ext"、"name (2).ext" …
    """
    candidate = os.path.join(directory, name)
    if not os.path.exists(candidate):
        return candidate

    stem, ext = os.path.splitext(name)
    for index in range(1, max_tries + 1):
        candidate = os.path.join(directory, "%s (%d)%s" % (stem, index, ext))
        if not os.path.exists(candidate):
            return candidate
    raise FileExistsError("同名文件过多，无法生成新文件名")


def make_directory(parent_abs: str, name: str) -> str:
    """在指定目录下新建文件夹，返回新目录的绝对路径。"""
    safe_name = sanitize_filename(name)
    target = os.path.join(parent_abs, safe_name)

    if os.path.exists(target):
        raise FileExistsError("已存在同名文件或文件夹：%s" % safe_name)

    os.mkdir(target)
    return target


def create_file(parent_abs: str, name: str) -> str:
    """
    在指定目录下新建一个**空文件**，返回新文件的绝对路径。

    为什么用 os.open(O_CREAT|O_EXCL) 而不是「先 exists() 再 open()」：
        「先查再建」之间有竞态窗口，两次调用之间文件可能已被别的程序创建；
        而 O_EXCL 让「不存在才创建」成为一次原子操作。

    为什么不用 open(..., "w")：
        该模式遇到同名文件会**直接清空**。新建文件覆盖掉别人的内容是最难
        挽回的一类事故，所以这里保证绝不覆盖任何已有文件（同名一律报错，
        由上层翻译成 409，前端会提示换个名字）。
    """
    safe_name = sanitize_filename(name)
    target = os.path.join(parent_abs, safe_name)

    # 同名目录单独报一句更贴切的提示：O_EXCL 只会给一个笼统的 EEXIST
    if os.path.isdir(target):
        raise FileExistsError("已存在同名文件夹：%s" % safe_name)

    try:
        handle = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        raise FileExistsError("已存在同名文件或文件夹：%s" % safe_name)
    os.close(handle)
    return target


def rename_entry(abs_path: str, new_name: str) -> str:
    """重命名文件或文件夹，返回新路径。"""
    safe_name = sanitize_filename(new_name)
    parent = os.path.dirname(abs_path)

    # 名称没变时直接返回，避免 Windows 上出现莫名的覆盖行为
    if os.path.basename(abs_path) == safe_name:
        return abs_path

    target = os.path.join(parent, safe_name)

    if os.path.exists(target):
        raise FileExistsError("已存在同名文件或文件夹：%s" % safe_name)

    os.rename(abs_path, target)
    return target


# ---------------------------------------------------------------------------
# 删除（回收站 / 永久）
# ---------------------------------------------------------------------------

# 回收站删除失败时的常见错误码 -> (中文原因, 建议)
#
# 为什么要这张表：早先这里把**所有**失败都归因成「目标盘不支持回收站
# （网络驱动器 / 非 NTFS 分区）」。实测证明那个归因是错的：D: 是 NTFS、
# $Recycle.Bin 存在、回收站配额 48GB、盘上还有 600GB 空闲，我自建的
# 探针目录（含中文名与嵌套）也能正常回收，但删一个 1GB 的目录仍然报
# WinError 161。那条提示把用户引向了错误的方向（跑去查盘格式），
# 所以改成按错误码给具体原因，认不出来就如实转述原始错误、不编造。
_DELETE_ERROR_HINTS: Dict[int, Tuple[str, str]] = {
    2: ("找不到指定的文件", "文件可能已被别的程序删除或改名，刷新目录后再看。"),
    3: ("找不到指定的路径", "路径可能已被移动或删除，刷新目录后再看。"),
    5: ("拒绝访问", "文件或目录是只读的、或当前账号权限不足，也可能正被别的程序占用。"),
    19: ("介质被写保护", "目标盘处于写保护状态，无法写入。"),
    32: ("文件正被其它程序使用", "请先关闭正在使用它的程序（播放器、下载工具、杀毒软件等）"
                                 "再重试；也可以直接改用永久删除。"),
    33: ("文件被部分锁定", "有程序正在读取它，关闭后重试；或改用永久删除。"),
    145: ("目录不是空的", "删除过程中目录内容又发生了变化，刷新目录后重试。"),
    161: ("路径无效", "最常见的原因是：该目录正被某个程序当作工作目录，"
                      "或内部有句柄被占用。请关闭可能在使用它的程序后重试；"
                      "永久删除不经过 Windows 外壳，成功率更高。"),
    1223: ("操作被系统取消", "通常是目标过大、或该盘的回收站不可用，可以改用永久删除。"),
}


def _explain_delete_error(exc: Exception) -> str:
    """
    把底层异常翻译成「人能看懂的原因 + 该怎么办」。

    认得错误码时给出中文原因与建议（同时保留错误码，便于排查）；
    认不出来时原样返回原始错误，绝不猜一个原因糊弄用户。
    """
    code = getattr(exc, "winerror", None)
    if not isinstance(code, int):
        code = getattr(exc, "errno", None)

    if isinstance(code, int):
        hint = _DELETE_ERROR_HINTS.get(code)
        if hint:
            return "%s（错误码 %s）。%s" % (hint[0], code, hint[1])
    return str(exc)


def _send_to_recycle_bin(paths: List[str]) -> List[str]:
    """
    把文件移入系统回收站。返回失败项的描述列表（空列表表示全部成功）。

    单项失败只记录这一项，不影响同批其它项；失败原因经 _explain_delete_error
    翻译成中文原因 + 建议，前端可以直接展示给用户。
    """
    failures: List[str] = []
    try:
        from send2trash import send2trash
    except ImportError:
        return ["服务端未安装 send2trash 库，无法使用回收站删除。"
                "请执行 pip install send2trash，或把 config.json 中 delete.use_recycle_bin 改为 false。"]

    for path in paths:
        try:
            send2trash(path)
        except Exception as exc:  # noqa: BLE001
            failures.append("%s：%s" % (os.path.basename(path), _explain_delete_error(exc)))
    return failures


def _permanent_delete(paths: List[str]) -> List[str]:
    """永久删除。返回失败项描述列表。"""
    failures: List[str] = []
    for path in paths:
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, onerror=_rmtree_onerror)
            else:
                os.chmod(path, stat_module.S_IWRITE)
                os.unlink(path)
        except Exception as exc:  # noqa: BLE001
            failures.append("%s：%s" % (os.path.basename(path), exc))
    return failures


def _rmtree_onerror(func, path, exc_info):
    """rmtree 出错时尝试去掉只读属性再删一次（Windows 常见问题）。"""
    try:
        os.chmod(path, stat_module.S_IWRITE)
        func(path)
    except Exception:  # noqa: BLE001
        raise


def delete_entries(abs_paths: List[str], use_recycle_bin: bool = True) -> Dict[str, Any]:
    """
    批量删除。

    返回 {"deleted": [...], "failures": [...], "mode": "recycle"|"permanent"}
    """
    if not abs_paths:
        raise ValueError("没有指定要删除的文件")

    if use_recycle_bin:
        failures = _send_to_recycle_bin(abs_paths)
        mode = "recycle"
        if failures and len(failures) == len(abs_paths):
            # 全部失败：不再武断地说是「盘不支持回收站」（见 _DELETE_ERROR_HINTS
            # 上面的说明），只给出真正可行的那条出路。
            failures.append(
                "以上项目都没能移入回收站。可以改用【永久删除】重试 —— "
                "永久删除不经过 Windows 回收站，即使回收站这条路走不通通常也能删掉，"
                "但删除之后无法再恢复。"
            )
    else:
        failures = _permanent_delete(abs_paths)
        mode = "permanent"

    # 判断哪些其实已经删掉了
    deleted: List[str] = []
    failed_names = {f.split("：", 1)[0] for f in failures}
    for path in abs_paths:
        if os.path.basename(path) in failed_names:
            continue
        if not os.path.exists(path):
            deleted.append(os.path.basename(path))

    return {
        "deleted": deleted,
        "failures": failures,
        "mode": mode,
        # 走回收站失败时，前端可以提示用户「改用永久删除重试」；
        # 已经是永久删除再失败就没有别的退路了。
        "can_retry_permanent": bool(failures and mode == "recycle"),
    }


# ---------------------------------------------------------------------------
# 上传相关
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# 按文件名搜索
# ---------------------------------------------------------------------------

# 三重刹车。全盘搜索在机械盘上可能要几分钟，而这是一个 HTTP 请求：
# 不能让浏览器一直挂着，更不能让一个请求把服务占住。三者任一触发都会
# **带着已有结果返回**并在 truncated/reason 里如实说明，而不是假装「就这些」。
DEFAULT_SEARCH_RESULTS = 100
DEFAULT_SEARCH_SCANNED = 200000
DEFAULT_SEARCH_SECONDS = 8.0


def search_files(roots: Iterable[Dict[str, Any]], query: str, *,
                 max_results: int = DEFAULT_SEARCH_RESULTS,
                 max_scanned: int = DEFAULT_SEARCH_SCANNED,
                 time_budget: float = DEFAULT_SEARCH_SECONDS) -> Dict[str, Any]:
    """
    在若干根目录下按**文件名**递归搜索（不搜文件内容）。

    几个关键取舍：

    * **不跟进符号链接 / 目录联接**（followlinks=False，并显式过滤掉它们）。
      Windows 上目录联接足以让遍历成环（C:\\Documents and Settings 就指向
      Users），一旦成环这个请求就再也回不来了 —— 与打包时是同一套判断。
    * **只搜文件名，不搜内容**。全盘 grep 是另一个量级的事，会真的把磁盘
      读穿；「我记得文件名里有个 xxx」才是找文件最常见的形态。
    * **攒够就返回**，不做全量统计。用户要的是列表，不是「共找到 N 条」，
      所以凑满 max_results 立刻收工，省下的是用户的时间。
    """
    needle = str(query or "").strip().lower()
    if not needle:
        return {"results": [], "scanned": 0, "truncated": False, "reason": ""}

    started = time.monotonic()
    results: List[Dict[str, Any]] = []
    scanned = 0
    truncated = False
    reason = ""

    for root_cfg in roots or ():
        base = str(root_cfg.get("path") or "")
        if not base or not os.path.isdir(base):
            continue
        root_id = str(root_cfg.get("id") or "")

        for current, dirs, files in os.walk(base, followlinks=False):
            if truncated:
                break

            # 先剔除目录联接/符号链接：它们既可能成环，也可能指向根目录之外
            dirs[:] = [name for name in dirs
                       if not is_link_or_junction(os.path.join(current, name))]

            for is_dir, bucket in ((True, dirs), (False, files)):
                for name in bucket:
                    scanned += 1
                    if scanned > max_scanned:
                        truncated, reason = True, "扫描的条目数已达上限"
                        break

                    if needle not in name.lower():
                        continue

                    full = os.path.join(current, name)
                    rel = os.path.relpath(full, base).replace(os.sep, "/")
                    parent = os.path.dirname(rel)

                    size = 0
                    mtime = 0.0
                    if not is_dir:
                        try:
                            info = os.stat(full)
                            size = int(info.st_size)
                            mtime = float(info.st_mtime)
                        except OSError:
                            # 权限不足/文件刚被删掉都很正常，跳过元信息即可，
                            # 没必要因为这个把整条结果丢掉
                            pass

                    results.append({
                        "root": root_id,
                        "name": name,
                        "rel": rel,
                        "dir": "" if parent in (".", "") else parent,
                        "is_dir": is_dir,
                        "size": size,
                        "mtime": mtime,
                    })

                    if len(results) >= max_results:
                        truncated, reason = True, "结果数已达上限"
                        break

                if truncated:
                    break

            if not truncated and (time.monotonic() - started) > time_budget:
                truncated, reason = True, "搜索时间已达上限（%.0f 秒）" % time_budget

    return {
        "results": results,
        "scanned": scanned,
        "truncated": truncated,
        "reason": reason,
    }


def resolve_upload_target(directory: str, filename: str, blocked_extensions: Iterable[str],
                          overwrite: bool = False) -> Tuple[str, str]:
    """
    校验并计算上传文件的落盘路径。

    返回 (清洗后的文件名, 目标绝对路径)。
    扩展名命中黑名单、文件名非法、磁盘空间不足都会抛异常。
    """
    safe_name = sanitize_filename(filename)

    hit = is_blocked_extension(safe_name, blocked_extensions)
    if hit:
        raise PathSecurityError(
            "出于安全考虑，禁止上传 %s 类型的可执行文件。"
            "如需放行，请修改 config.json 中 upload.blocked_extensions。" % hit
        )

    if overwrite:
        target = os.path.join(directory, safe_name)
    else:
        target = unique_path(directory, safe_name)

    return safe_name, target


def check_disk_space(directory: str, required_bytes: int) -> None:
    """检查目标磁盘剩余空间是否足够，不足则抛异常。"""
    try:
        usage = shutil.disk_usage(directory)
    except OSError:
        return
    # 预留 64MB 余量，避免刚好写满整块盘
    if usage.free < required_bytes + 64 * 1024 * 1024:
        raise OSError(
            "目标磁盘剩余空间不足：需要约 %s，当前可用 %s"
            % (human_size(required_bytes), human_size(usage.free))
        )


# ---------------------------------------------------------------------------
# 打包下载
# ---------------------------------------------------------------------------

def _zip_compression_for(path: str) -> int:
    """按扩展名选择压缩方式：已压缩过的文件直接存储，节省 CPU。"""
    ext = os.path.splitext(path)[1].lower()
    return zipfile.ZIP_STORED if ext in _ALREADY_COMPRESSED else zipfile.ZIP_DEFLATED


def create_zip(items: List[Tuple[str, str]], dest_zip: str, base_dir: str = "") -> Dict[str, Any]:
    """
    把若干文件/目录打包成 ZIP。

    items: [(绝对路径, 压缩包内相对名), ...]
    dest_zip: 生成的 zip 绝对路径
    base_dir: 用于做进度/路径显示的基准目录（可空）

    返回 {"file_count": n, "skipped": [...], "size": 字节数}
    """
    file_count = 0
    skipped: List[str] = []

    with zipfile.ZipFile(dest_zip, "w", allowZip64=True) as zf:
        for abs_path, arc_name in items:
            if not os.path.exists(abs_path):
                skipped.append("%s（文件不存在）" % arc_name)
                continue

            if os.path.isdir(abs_path) and not os.path.islink(abs_path):
                # 目录：递归加入，保留目录结构
                added_any = False
                for dirpath, dirnames, filenames in os.walk(abs_path):
                    rel_dir = os.path.relpath(dirpath, os.path.dirname(abs_path))
                    for filename in filenames:
                        full = os.path.join(dirpath, filename)
                        inner = os.path.join(rel_dir, filename).replace("\\", "/")
                        try:
                            zf.write(full, inner, compress_type=_zip_compression_for(full))
                            file_count += 1
                            added_any = True
                        except (OSError, ValueError) as exc:
                            skipped.append("%s：%s" % (inner, exc))
                if not added_any:
                    # 空目录也要能在压缩包里体现出来
                    zf.writestr(arc_name.rstrip("/") + "/", b"")
            else:
                try:
                    zf.write(abs_path, arc_name, compress_type=_zip_compression_for(abs_path))
                    file_count += 1
                except (OSError, ValueError) as exc:
                    skipped.append("%s：%s" % (arc_name, exc))

    try:
        size = os.path.getsize(dest_zip)
    except OSError:
        size = 0

    return {"file_count": file_count, "skipped": skipped, "size": size}


def safe_zip_name(name: str) -> str:
    """
    生成一个安全的 zip 文件名（用于 Content-Disposition）。
    去掉不适合出现在 HTTP 头里的字符。
    """
    cleaned = "".join(ch for ch in (name or "download") if ch not in '\\/:*?"<>|\r\n\t')
    cleaned = cleaned.strip().strip(".")
    return cleaned or "download"


def cache_key_for_listing(abs_paths: List[str]) -> str:
    """根据一组路径生成短哈希，用于给打包文件起唯一名字。"""
    raw = "|".join(os.path.normcase(p) for p in abs_paths) + "|%.6f" % time.time()
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]

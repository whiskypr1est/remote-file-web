# -*- coding: utf-8 -*-
"""
文件系统路由
============

    GET  /api/fs/roots          —— 根目录列表（「此电脑」视图）
    GET  /api/fs/list           —— 列目录
    POST /api/fs/mkdir          —— 新建文件夹
    POST /api/fs/newfile        —— 新建空文件（扩展名由用户自己决定）
    POST /api/fs/rename         —— 重命名
    POST /api/fs/delete         —— 删除（回收站 / 永久）
    POST /api/fs/copy           —— 复制（重名自动改名，绝不覆盖）
    POST /api/fs/move           —— 移动（同盘即改名，跨盘自动复制后删除）
    POST /api/fs/upload         —— 上传（原始请求体流式写入，支持 2GB）
    POST /api/fs/zip            —— 服务端打包，返回一次性下载令牌
    GET  /api/fs/zip/download   —— 用令牌下载打包好的 zip
    POST /api/fs/compress       —— 压缩成 zip / tar / 7z / rar（存到服务端）
    POST /api/fs/extract        —— 解压 zip / tar / 7z / rar（重名直接拒绝）

关于上传接口的设计：
    没有使用 multipart/form-data，而是把文件原始字节直接当作请求体，
    文件名放在 query 参数里。原因是 Starlette 解析 multipart 时会先把
    整个文件落到系统临时目录（SpooledTemporaryFile），再让我们拷贝一次，
    上传 2GB 就要多占 2GB 的 C 盘临时空间、多一倍磁盘 IO。
    改成原始体之后可以直接边收边写到目标文件，零额外空间。

关于路径安全：
    所有接口的路径参数都会经过 PathResolver 校验，
    任何越界（../、绝对路径、符号链接逃逸）都会抛 PathSecurityError 并被转成 403。
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import stat
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import archive, fsops, jobs
from ..deps import get_state
from ..http_utils import file_response
from ..security import PathSecurityError, is_blocked_extension, is_protected, is_within

router = APIRouter(prefix="/api/fs", tags=["文件"])

# 判断一个路径字符串是不是 Windows 绝对路径（C:\ 或 \\server\share）
_ABS_PATH_RE = re.compile(r"^([A-Za-z]:[\\/]|\\\\)")

# 打包时最多允许选择多少项，防止一次请求把服务器打满
MAX_ZIP_ITEMS = 5000

# 复制 / 移动时最多允许一次处理多少项
MAX_TRANSFER_ITEMS = 5000


# ---------------------------------------------------------------------------
# 公共工具
# ---------------------------------------------------------------------------

def _resolve(resolver, root_id: Optional[str], path_text: Optional[str]) -> Tuple[Dict[str, Any], str]:
    """
    统一的路径解析入口。

    地址栏允许用户直接输入完整路径，所以这里做了自动判断：
    path 看起来是绝对路径就走 resolve_abs，否则按「根标识 + 相对路径」解析。
    """
    text = (path_text or "").strip()
    if text and _ABS_PATH_RE.match(text):
        return resolver.resolve_abs(text)
    return resolver.resolve(root_id, text)


def _translate_error(exc: Exception) -> HTTPException:
    """把底层异常翻译成带中文提示的 HTTPException。"""
    if isinstance(exc, HTTPException):
        return exc
    if isinstance(exc, PathSecurityError):
        return HTTPException(status_code=403, detail=str(exc))
    if isinstance(exc, FileExistsError):
        return HTTPException(status_code=409, detail=str(exc) or "同名文件已存在")
    if isinstance(exc, FileNotFoundError):
        return HTTPException(status_code=404, detail=str(exc) or "文件或目录不存在")
    if isinstance(exc, PermissionError):
        return HTTPException(
            status_code=403,
            detail="没有权限访问该路径。如果目标文件正被其他程序占用，请先关闭后再试。",
        )
    if isinstance(exc, NotADirectoryError):
        return HTTPException(status_code=400, detail="该路径不是一个目录")
    if isinstance(exc, IsADirectoryError):
        return HTTPException(status_code=400, detail="该路径是一个目录，不能按文件处理")
    if isinstance(exc, OSError):
        return HTTPException(status_code=500, detail="文件系统操作失败：%s" % exc)
    return HTTPException(status_code=500, detail=str(exc) or "未知错误")


def _disk_usage(path: str) -> Dict[str, int]:
    """取磁盘容量信息，失败时返回空字典（前端会隐藏相应显示）。"""
    try:
        usage = shutil.disk_usage(path)
        return {"total": usage.total, "used": usage.used, "free": usage.free}
    except OSError:
        return {}


def _root_payload(root: Dict[str, Any]) -> Dict[str, Any]:
    """统一的根目录描述结构。"""
    return {
        "id": root["id"],
        "name": root["name"],
        "path": root["display_path"],
        "readonly": bool(root.get("readonly", False)),
        "exists": bool(os.path.isdir(root["path"])),
        # kind=drive 表示这是自动挂载的磁盘，前端据此画盘符图标
        "kind": root.get("kind", "folder"),
        "type_label": root.get("type_label", ""),
    }


def _entry_payload(entry: Dict[str, Any], root: Dict[str, Any], rel_dir: str,
                   thumbs_enabled: bool) -> Dict[str, Any]:
    """
    给目录条目补上前端需要的附加字段：
        rel        —— 相对根目录的路径，前端直接拿去调其它接口
        thumb_url  —— 图片类文件的缩略图地址，非图片为 None（前端用类型图标）
    """
    item = dict(entry)
    name = entry["name"]
    rel = ("%s/%s" % (rel_dir, name)) if rel_dir else name
    item["rel"] = rel

    if thumbs_enabled and entry["type"] == "image" and not entry["is_dir"]:
        # v 参数带修改时间，文件被覆盖后 URL 会变，浏览器不会复用旧缩略图
        item["thumb_url"] = "/api/fs/thumb?root=%s&path=%s&v=%d" % (
            quote(root["id"], safe=""),
            quote(rel, safe=""),
            int(entry["mtime"]),
        )
    else:
        item["thumb_url"] = None

    return item


# ---------------------------------------------------------------------------
# 请求体模型
# ---------------------------------------------------------------------------

class PathPayload(BaseModel):
    """带「根标识 + 相对路径」的通用请求体。"""
    root: str = ""
    path: str = ""


class MkdirPayload(BaseModel):
    root: str = ""
    path: str = ""
    name: str


class NewFilePayload(BaseModel):
    """新建空文件。name 是含扩展名的完整文件名，后缀由前端让用户自己选或填。"""
    root: str = ""
    path: str = ""
    name: str


class RenamePayload(BaseModel):
    root: str = ""
    path: str = ""
    new_name: str


class DeletePayload(BaseModel):
    root: str = ""
    paths: List[str] = []
    # 传 true 时强制永久删除（覆盖配置里的回收站设置）
    permanent: bool = False


class TransferPayload(BaseModel):
    """复制 / 移动的请求体：源（root + paths）与目标目录（target_root + target_path）。"""
    root: str = ""
    paths: List[str] = []
    # 目标根与源根可以不同，跨盘复制/移动就靠这两个字段
    target_root: str = ""
    target_path: str = ""
    # ★ 后台执行：立刻返回 job_id，前端轮询 /api/jobs/{id} 看进度。
    #   默认 false 保持原契约（请求挂到做完为止）—— 命令行/脚本仍然可以
    #   用「同步」这一档，不必自己写轮询。
    background: bool = False


class ZipPayload(BaseModel):
    root: str = ""
    paths: List[str] = []


# ---------------------------------------------------------------------------
# 根目录与目录列举
# ---------------------------------------------------------------------------

@router.get("/roots")
async def list_roots(request: Request) -> Dict[str, Any]:
    """返回所有允许访问的根目录，供「此电脑」视图使用。"""
    state = get_state(request)

    roots = []
    for root in state.resolver.roots:
        payload = _root_payload(root)
        payload["disk"] = _disk_usage(root["path"]) if payload["exists"] else {}
        roots.append(payload)

    return {"ok": True, "roots": roots}


@router.get("/search")
async def search_files(request: Request, q: str = "", root: str = "",
                       limit: int = 0) -> Dict[str, Any]:
    """
    按文件名搜索（递归，**不搜内容**）。

    root 留空 = 在所有可访问根目录里搜；开了 mount_all_drives 之后那就是
    「全盘搜索」。所以下面几道刹车不是可选项 —— 结果数、扫描条目数、时间预算，
    任一触发都会**带着已有结果**返回并把 truncated 置真，而不是把请求挂死。
    """
    state = get_state(request)
    query = (q or "").strip()
    if len(query) < 2:
        raise HTTPException(status_code=400, detail="请至少输入 2 个字符再搜索")

    settings = state.cfg.get("search") or {}

    def _int_setting(key: str, fallback: int) -> int:
        try:
            return int(settings.get(key) or fallback)
        except (TypeError, ValueError):
            return fallback

    def _float_setting(key: str, fallback: float) -> float:
        try:
            return float(settings.get(key) or fallback)
        except (TypeError, ValueError):
            return fallback

    if root:
        try:
            root_cfg, _abs = _resolve(state.resolver, root, "")
        except Exception as exc:  # noqa: BLE001
            raise _translate_error(exc)
        targets = [root_cfg]
    else:
        targets = list(state.resolver.roots)

    wanted = int(limit) if limit and limit > 0 else _int_setting(
        "max_results", fsops.DEFAULT_SEARCH_RESULTS)
    wanted = max(1, min(wanted, 500))

    result = await run_in_threadpool(
        fsops.search_files, targets, query,
        max_results=wanted,
        max_scanned=_int_setting("max_scanned", fsops.DEFAULT_SEARCH_SCANNED),
        time_budget=_float_setting("timeout_seconds", fsops.DEFAULT_SEARCH_SECONDS),
    )

    return {
        "ok": True,
        "query": query,
        "results": result["results"],
        "count": len(result["results"]),
        "scanned": result["scanned"],
        "truncated": result["truncated"],
        "reason": result["reason"],
    }


@router.get("/list")
async def list_directory(
    request: Request,
    root: str = "",
    path: str = "",
    sort: str = "name",
    order: str = "asc",
    show_hidden: bool = True,
) -> Dict[str, Any]:
    """
    列目录。

    path 可以是相对根目录的相对路径，也可以直接是绝对路径（地址栏粘贴场景）。
    """
    state = get_state(request)
    cfg = state.cfg
    resolver = state.resolver

    try:
        root_cfg, abs_path = _resolve(resolver, root, path)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="目录不存在：%s" % abs_path)
    if not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="该路径不是目录，无法打开")

    try:
        listing = await run_in_threadpool(
            fsops.list_directory, abs_path, sort, order, show_hidden
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    rel_dir = resolver.to_rel(root_cfg, abs_path)
    thumbs_enabled = bool((cfg.get("thumbs") or {}).get("enabled", True))

    entries = [
        _entry_payload(entry, root_cfg, rel_dir, thumbs_enabled)
        for entry in listing["entries"]
    ]

    parent_abs = resolver.parent_of(root_cfg, abs_path)

    return {
        "ok": True,
        "root": _root_payload(root_cfg),
        "rel": rel_dir,
        "abs": abs_path,
        "parent_abs": parent_abs,
        "at_root": parent_abs is None,
        "readonly": bool(root_cfg.get("readonly", False)),
        "entries": entries,
        "dir_count": listing["dir_count"],
        "file_count": listing["file_count"],
        "total": listing["total"],
        "skipped": listing["skipped"],
        "sort": listing["sort"],
        "order": listing["order"],
        "disk": _disk_usage(abs_path),
    }


# ---------------------------------------------------------------------------
# 新建 / 重命名 / 删除
# ---------------------------------------------------------------------------

def _ensure_writable(root_cfg: Dict[str, Any]) -> None:
    """只读根目录直接拒绝写操作。"""
    if root_cfg.get("readonly"):
        raise HTTPException(status_code=403, detail="该根目录已配置为只读，不允许修改")


def _is_root_path(abs_path: str) -> bool:
    """判断绝对路径是不是「盘符根 / 文件系统根」（例如 C:\\ 或 /）。"""
    if not abs_path:
        return False
    normalized = os.path.normpath(abs_path)
    return os.path.normcase(os.path.dirname(normalized)) == os.path.normcase(normalized)


def _ensure_not_root(root_cfg: Dict[str, Any], abs_path: str, action: str) -> None:
    """
    拒绝把「根目录本身」作为操作目标。

    开启 mount_all_drives 后每个盘符都是一个可访问根目录，而根目录
    既不在 protected_paths 里、也不是只读，于是「删除 C:\\」「把整个 C: 打包」
    这类请求能通过此前所有的校验（PathResolver 把根目录自身也算作「在根内」）。
    Windows 一般会拒绝删除卷根，但这里必须主动拦住，不能指望操作系统兜底。
    """
    if _is_root_path(abs_path):
        raise HTTPException(
            status_code=403,
            detail="不允许对磁盘根目录（%s）执行%s操作" % (abs_path, action),
        )
    if os.path.normcase(os.path.normpath(abs_path)) == os.path.normcase(os.path.normpath(root_cfg["path"])):
        raise HTTPException(
            status_code=403,
            detail="不允许对根目录「%s」本身执行%s操作" % (root_cfg.get("name") or abs_path, action),
        )


def _ensure_not_protected(cfg: Dict[str, Any], abs_path: str) -> None:
    """
    受保护路径禁止修改（但允许浏览）。

    开放整盘访问后，误删 C:\\Windows 这类目录可能让 Windows 直接起不来，
    所以默认拦住写操作（新建/重命名/删除/上传）。
    确需放开时，把 config.json 的 protected_paths 改成空数组即可。
    """
    hit = is_protected(abs_path, cfg.get("protected_paths") or [])
    if hit:
        raise HTTPException(
            status_code=403,
            detail="「%s」属于受保护的系统目录，禁止修改。\n"
                   "如需放开，请编辑 config.json 中的 protected_paths。" % hit,
        )


@router.post("/mkdir")
async def make_directory(request: Request, payload: MkdirPayload) -> Dict[str, Any]:
    """新建文件夹。"""
    state = get_state(request)

    try:
        root_cfg, abs_path = _resolve(state.resolver, payload.root, payload.path)
        _ensure_writable(root_cfg)

        if not os.path.isdir(abs_path):
            raise HTTPException(status_code=400, detail="目标位置不是有效目录")

        _ensure_not_protected(state.cfg, abs_path)

        new_path = await run_in_threadpool(fsops.make_directory, abs_path, payload.name)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    rel = state.resolver.to_rel(root_cfg, new_path)
    return {"ok": True, "message": "文件夹已创建", "rel": rel, "name": os.path.basename(new_path)}


@router.post("/newfile")
async def make_file(request: Request, payload: NewFilePayload) -> Dict[str, Any]:
    """
    新建空文件，扩展名由前端让用户自己选或直接输入。

    校验与「新建文件夹」一致（只读根目录 / 目标必须是目录 / 受保护路径），
    另外多一道**扩展名黑名单**，这是有意为之：
        新建文件等于在服务器上凭空造出一个文件，如果不受与上传相同的限制，
        「上传 .bat 被拦、但先新建一个空 .bat 再往里写内容」就绕过了黑名单。
        名单与上传共用 upload.blocked_extensions，放行方式也一致。
    """
    state = get_state(request)

    try:
        root_cfg, abs_path = _resolve(state.resolver, payload.root, payload.path)
        _ensure_writable(root_cfg)

        if not os.path.isdir(abs_path):
            raise HTTPException(status_code=400, detail="目标位置不是有效目录")

        _ensure_not_protected(state.cfg, abs_path)

        blocked = (state.cfg.get("upload") or {}).get("blocked_extensions") or []
        hit = is_blocked_extension(payload.name, blocked)
        if hit:
            raise PathSecurityError(
                "出于安全考虑，禁止新建 %s 类型的可执行文件。"
                "如需放行，请修改 config.json 中 upload.blocked_extensions。" % hit
            )

        new_path = await run_in_threadpool(fsops.create_file, abs_path, payload.name)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    rel = state.resolver.to_rel(root_cfg, new_path)
    return {"ok": True, "message": "文件已创建", "rel": rel, "name": os.path.basename(new_path)}


@router.post("/rename")
async def rename_entry(request: Request, payload: RenamePayload) -> Dict[str, Any]:
    """重命名文件或文件夹。"""
    state = get_state(request)

    try:
        root_cfg, abs_path = _resolve(state.resolver, payload.root, payload.path)
        _ensure_writable(root_cfg)

        if not os.path.exists(abs_path):
            raise FileNotFoundError("要重命名的对象不存在")

        _ensure_not_protected(state.cfg, abs_path)
        _ensure_not_root(root_cfg, abs_path, "重命名")

        new_path = await run_in_threadpool(fsops.rename_entry, abs_path, payload.new_name)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    rel = state.resolver.to_rel(root_cfg, new_path)
    return {"ok": True, "message": "重命名成功", "rel": rel, "name": os.path.basename(new_path)}


@router.post("/delete")
async def delete_entries(request: Request, payload: DeletePayload) -> Dict[str, Any]:
    """
    批量删除。

    默认删到回收站（可在 config.json 里改成永久删除），
    删除前由前端弹确认框，这里只负责执行并如实回报结果。
    """
    state = get_state(request)
    cfg = state.cfg

    if not payload.paths:
        raise HTTPException(status_code=400, detail="没有选择任何文件")

    try:
        root_cfg, _base = _resolve(state.resolver, payload.root, "")
        _ensure_writable(root_cfg)

        abs_paths: List[str] = []
        for rel in payload.paths:
            _r, abs_path = state.resolver.resolve(root_cfg["id"], rel)
            if not os.path.exists(abs_path):
                raise FileNotFoundError("对象不存在：%s" % rel)
            # 受保护的系统目录只允许浏览，不允许删除
            _ensure_not_protected(state.cfg, abs_path)
            # 根目录本身（C:\、D:\ 等）不能删除
            _ensure_not_root(root_cfg, abs_path, "删除")
            abs_paths.append(abs_path)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    # 永久删除的判定：请求显式要求，或配置里就是永久删除
    use_recycle = bool((cfg.get("delete") or {}).get("use_recycle_bin", True))
    if payload.permanent:
        use_recycle = False

    try:
        result = await run_in_threadpool(fsops.delete_entries, abs_paths, use_recycle)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    # 注意：这里**不再**「全部失败就抛 500」。
    # 抛 500 只能带一个字符串，前端拿不到结构化信息、也就无法提供
    # 「改用永久删除重试」这个出路；现在统一返回 200 + failures 列表
    # （与部分失败的口径一致），由 can_retry_permanent 决定要不要给重试按钮。
    if result["deleted"]:
        summary = "已删除 %d 项" % len(result["deleted"])
        if result["failures"]:
            summary += "，%d 项失败" % len(result["failures"])
    else:
        summary = "删除失败，%d 项都没能删除" % len(result["failures"])

    return {
        "ok": True,
        "deleted": result["deleted"],
        "failures": result["failures"],
        "mode": result["mode"],
        "can_retry_permanent": bool(result.get("can_retry_permanent")),
        "message": summary,
    }


# ---------------------------------------------------------------------------
# 复制 / 移动
# ---------------------------------------------------------------------------

def _same_dir(left: str, right: str) -> bool:
    """两个路径是否指向同一个目录（Windows 下大小写无关）。"""
    if not left or not right:
        return False
    return os.path.normcase(os.path.normpath(left)) == os.path.normcase(os.path.normpath(right))


def _is_reparse_point(path: str) -> bool:
    """
    判断路径是不是「重解析点」（符号链接 / 目录联接 / 挂载点）。

    Windows 上的目录联接（junction）不会被 os.path.islink 认出来，
    但复制时它一样能造成无限递归（比如 root\\back 指回 root），
    所以必须靠文件属性位单独识别。
    非 Windows 平台没有 st_file_attributes，退回 islink 判断。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    attributes = getattr(st, "st_file_attributes", None)
    if attributes is None:
        return os.path.islink(path)
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & flag)


def _copy_tree(src_dir: str, dst_dir: str, skipped: List[str], failures: List[str],
               label: str = "", progress=None, should_cancel=None) -> None:
    """
    递归复制目录。

    这里没有直接用 shutil.copytree(symlinks=True)：在 Windows 上目录联接
    不是符号链接，copytree 会一路跟进联接的目标目录，碰到指回上层的联接
    就会无限递归、把磁盘写满。改成自己遍历，遇到重解析点整条跳过并如实上报，
    宁可少复制一点也不能把服务拖死。
    单个文件出错只记进 failures，不影响同目录里的其它文件。

    progress / should_cancel 会一路透传给底层的分块复制，让「一个几十 GB 的
    文件」也能报进度、也能被取消。
    """
    os.makedirs(dst_dir, exist_ok=True)

    try:
        entries = list(os.scandir(src_dir))
    except OSError as exc:
        failures.append("%s：%s" % (label or src_dir, exc))
        return

    for entry in entries:
        source = entry.path
        target = os.path.join(dst_dir, entry.name)
        rel_label = ("%s/%s" % (label, entry.name)) if label else entry.name
        try:
            if _is_reparse_point(source):
                skipped.append("%s（符号链接/目录联接，已跳过）" % rel_label)
                continue
            if entry.is_dir(follow_symlinks=False):
                _copy_tree(source, target, skipped, failures, rel_label,
                           progress, should_cancel)
            else:
                fsops.copy_file_tracked(source, target, progress, should_cancel)
        except fsops.OperationCancelled:
            # 取消要一路冒到任务层，不能被下面那个「单项失败不影响整批」的
            # except Exception 吞掉 —— 否则点了取消、任务却继续跑完。
            raise
        except Exception as exc:  # noqa: BLE001 - 单个文件失败不能中断整棵树
            failures.append("%s：%s" % (rel_label, exc))


@router.post("/copy")
async def copy_entries(request: Request, payload: TransferPayload) -> Dict[str, Any]:
    """复制选中的文件/文件夹到目标目录；重名自动改名，绝不覆盖已有文件。"""
    return await _transfer_entries(request, payload, move=False)


@router.post("/move")
async def move_entries(request: Request, payload: TransferPayload) -> Dict[str, Any]:
    """移动选中的文件/文件夹到目标目录；同盘是改名，跨盘由 shutil.move 处理。"""
    return await _transfer_entries(request, payload, move=True)


async def _transfer_entries(request: Request, payload: TransferPayload, move: bool) -> Dict[str, Any]:
    """
    复制 / 移动的公共实现。

    两条硬性规则：
      1. 目标已有同名项时一律自动改名（报告.docx -> 报告 (1).docx），绝不覆盖；
      2. 单项失败只记进 failures，不影响同一批里的其它项（与 delete_entries 一致），
         整批全失败时才抛 500，把原因直接摊给前端显示。
    与 zip/delete 一样走线程池同步执行，完成后才返回，因此不需要任务队列。
    """
    state = get_state(request)
    action = "移动" if move else "复制"

    if not payload.paths:
        raise HTTPException(status_code=400, detail="没有选择任何文件")
    if len(payload.paths) > MAX_TRANSFER_ITEMS:
        raise HTTPException(status_code=400, detail="一次最多%s %d 项" % (action, MAX_TRANSFER_ITEMS))

    try:
        # 目标目录：先确认可写、不在保护名单里、确实是一个目录
        target_root_cfg, dst_dir = _resolve(state.resolver, payload.target_root, payload.target_path)
        _ensure_writable(target_root_cfg)
        if not os.path.isdir(dst_dir):
            raise HTTPException(status_code=400, detail="目标位置不是一个有效目录")
        _ensure_not_protected(state.cfg, dst_dir)

        # 源与目标允许属于不同的根目录（跨盘复制 / 移动）
        root_cfg, _base = _resolve(state.resolver, payload.root, "")

        sources: List[str] = []
        for rel in payload.paths:
            _r, src_abs = state.resolver.resolve(root_cfg["id"], rel)
            if not os.path.exists(src_abs):
                raise FileNotFoundError("对象不存在：%s" % rel)

            # 盘符根 / 根目录本身不能作为操作对象
            _ensure_not_root(root_cfg, src_abs, action)

            if move:
                # 移动会顺带删掉源，所以源侧也要按写操作校验
                _ensure_writable(root_cfg)
                _ensure_not_protected(state.cfg, src_abs)

            # 拒绝把目录复制/移动到它自己或它的子孙目录里，否则会无限递归。
            # 源和目标是同一个目录时 is_within 也返回 True（包含相等），
            # 但那种情况是合法的「原地复制一份」/「移动到自己这儿」，
            # 交给后面的循环按项处理（复制生成 (1)，移动则跳过）。
            if (os.path.isdir(src_abs)
                    and not _same_dir(os.path.dirname(src_abs), dst_dir)
                    and is_within(src_abs, dst_dir)):
                raise HTTPException(
                    status_code=400,
                    detail="不能把文件夹%s到它自己或它的子文件夹里面" % action,
                )

            sources.append(src_abs)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    copied: List[str] = []
    moved: List[str] = []
    renamed: List[Dict[str, str]] = []
    skipped: List[str] = []
    failures: List[str] = []

    def _transfer_one(src_abs: str, progress=None, should_cancel=None) -> None:
        """处理一项；异常只转成 failures 里的一条记录，不影响同批其它项。"""
        name = os.path.basename(src_abs) or src_abs

        def on_bytes(count: int) -> None:
            if progress is not None:
                progress(count, name)

        try:
            # 源与目标目录相同：移动没有意义，直接跳过并如实上报
            if move and _same_dir(os.path.dirname(src_abs), dst_dir):
                skipped.append("%s（已在目标位置）" % name)
                return

            # 顶层就是联接/符号链接时整条跳过，不跟进它的目标
            if _is_reparse_point(src_abs):
                skipped.append("%s（符号链接/目录联接，已跳过）" % name)
                return

            target = fsops.unique_path(dst_dir, name)

            if move:
                # 体积必须在移动**之前**量：移动完成后源就没了，
                # 那时再量只会得到 0，进度也就永远停在 0%。
                size = _sources_total_size([src_abs], 1 << 50)
                # shutil.move 跨卷时会自动退化成「复制 + 删除源」
                shutil.move(src_abs, target)
                moved.append(name)
                if progress is not None:
                    progress(size, name)
            elif os.path.isdir(src_abs):
                _copy_tree(src_abs, target, skipped, failures, name,
                           on_bytes, should_cancel)
                copied.append(name)
            else:
                fsops.copy_file_tracked(src_abs, target, on_bytes, should_cancel)
                copied.append(name)

            final_name = os.path.basename(target)
            if final_name != name:
                # 目标已有同名项，系统自动改了名，必须告诉前端
                renamed.append({"from": name, "to": final_name})
        except fsops.OperationCancelled:
            # 取消不能被下面那个「单项失败不影响整批」的分支吞掉，
            # 否则点了取消、任务还会一路跑完，取消就成了摆设。
            raise
        except Exception as exc:  # noqa: BLE001
            failures.append("%s：%s" % (name, exc))

    def _transfer_batch(progress=None, should_cancel=None,
                        on_item=None) -> Dict[str, Any]:
        """
        跑完整批并组装结果。

        同步接口与后台任务**共用这一份实现**，只是回调不同：
        同步时回调是 None（维持原来的行为），后台时接到 Job 上。
        """
        for src_path in sources:
            if should_cancel is not None and should_cancel():
                raise fsops.OperationCancelled("%s已取消" % action)
            _transfer_one(src_path, progress, should_cancel)
            if on_item is not None:
                on_item(os.path.basename(src_path) or src_path)
        return _transfer_result()

    def _transfer_result() -> Dict[str, Any]:
        done_count = len(copied) + len(moved)

        if failures and done_count == 0:
            # 一项都没成功，把原因直接抛给前端显示（与删除接口一致）
            raise HTTPException(status_code=500,
                                detail="%s失败：%s" % (action, "；".join(failures)))

        if done_count:
            summary = "已%s %d 项" % (action, done_count)
            if renamed:
                summary += "，%d 项因重名自动改名" % len(renamed)
            if skipped:
                summary += "，%d 项已跳过" % len(skipped)
            if failures:
                summary += "，%d 项失败" % len(failures)
        elif skipped:
            summary = "没有需要%s的项目" % action
        else:
            summary = "没有执行任何%s操作" % action

        return {
            "ok": True,
            "message": summary,
            # copied/moved 里是「源名字」，renamed 里给出改名前后的名字
            "copied": copied,
            "moved": moved,
            "renamed": renamed,
            "skipped": skipped,
            "failures": failures,
        }

    # ---- 后台模式：校验已经在上面同步做完了，搬字节的活儿丢给任务队列 ----
    if payload.background:
        def work(job) -> Dict[str, Any]:
            try:
                job.set_totals(items=len(sources),
                               total_bytes=_sources_total_size(sources, 1 << 50))
            except Exception:  # noqa: BLE001 - 量不出体积不算失败，退化成按条目计进度
                job.set_totals(items=len(sources))
            return _transfer_batch(
                progress=lambda count, current: job.advance(bytes=count, current=current),
                should_cancel=job.cancel_requested,
                on_item=lambda name: job.advance(items=1, current=name),
            )

        job = jobs.manager.submit(action, "%s %d 项" % (action, len(sources)), work)
        return {
            "ok": True,
            "background": True,
            "job_id": job.id,
            "message": "已加入后台队列（%s %d 项），可在任务列表里查看进度"
                       % (action, len(sources)),
        }

    try:
        # 整批丢进线程池同步跑完再返回（与 /api/fs/zip 的做法一致）
        return await run_in_threadpool(_transfer_batch)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)


# ---------------------------------------------------------------------------
# 上传（原始请求体流式写入）
# ---------------------------------------------------------------------------

@router.post("/upload")
async def upload_file(
    request: Request,
    root: str = "",
    path: str = "",
    filename: str = "",
    overwrite: bool = False,
) -> Dict[str, Any]:
    """
    上传单个文件。

    - 文件内容：请求体原始字节（application/octet-stream）
    - 文件名：query 参数 filename（前端做 URL 编码），也兼容 X-File-Name 头
    - 目标目录：root + path

    流程：先按 Content-Length 做「超限 / 空间不足」的快速失败，
    再边收边写入 <目标名>.part，全部收完后原子重命名为正式文件。
    中途任何异常都会清理掉 .part，不留下半截文件。
    """
    state = get_state(request)
    cfg = state.cfg
    upload_cfg = cfg.get("upload") or {}

    max_mb = int(upload_cfg.get("max_file_size_mb") or 2048)
    max_bytes = max_mb * 1024 * 1024
    blocked = upload_cfg.get("blocked_extensions") or []

    # 文件名：优先 query 参数，其次请求头
    raw_name = filename or request.headers.get("x-file-name") or ""
    if not raw_name:
        raise HTTPException(status_code=400, detail="缺少文件名参数 filename")
    try:
        from urllib.parse import unquote

        raw_name = unquote(raw_name)
    except Exception:  # noqa: BLE001
        pass

    try:
        root_cfg, directory = _resolve(state.resolver, root, path)
        _ensure_writable(root_cfg)

        if not os.path.isdir(directory):
            raise HTTPException(status_code=400, detail="上传目标不是一个有效目录")

        _ensure_not_protected(state.cfg, directory)

        safe_name, target = fsops.resolve_upload_target(
            directory, raw_name, blocked, overwrite=overwrite
        )
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    # 快速失败：先看 Content-Length，避免白传 2GB 才报错
    content_length = request.headers.get("content-length")
    declared = 0
    if content_length:
        try:
            declared = int(content_length)
        except ValueError:
            declared = 0
        if declared > max_bytes:
            raise HTTPException(
                status_code=413,
                detail="文件大小 %.1f MB 超过上限 %d MB" % (declared / 1024 / 1024, max_mb),
            )
        try:
            fsops.check_disk_space(directory, declared)
        except OSError as exc:
            raise HTTPException(status_code=507, detail=str(exc))

    part_path = target + ".part"
    written = 0

    try:
        with open(part_path, "wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="文件超过上限 %d MB，上传已中断" % max_mb,
                    )
                # 写盘放到线程池，避免大文件把事件循环堵死
                await run_in_threadpool(fh.write, chunk)

        if written == 0 and declared == 0:
            raise HTTPException(status_code=400, detail="上传内容为空")

        # 原子落地：同目录内 replace 在 Windows 上也是原子的
        os.replace(part_path, target)

    except HTTPException:
        _cleanup_part(part_path)
        raise
    except Exception as exc:  # noqa: BLE001
        _cleanup_part(part_path)
        raise _translate_error(exc)

    rel = state.resolver.to_rel(root_cfg, target)
    final_name = os.path.basename(target)

    return {
        "ok": True,
        "message": "上传完成",
        "name": final_name,
        "rel": rel,
        "size": written,
        "size_text": fsops.human_size(written),
        # 因重名而被自动改名时告知前端，界面上给出提示
        "renamed": final_name != safe_name,
    }


def _cleanup_part(part_path: str) -> None:
    """删除上传中断留下的临时文件。"""
    try:
        if os.path.isfile(part_path):
            os.unlink(part_path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 批量打包下载
# ---------------------------------------------------------------------------

@router.post("/zip")
async def make_zip(request: Request, payload: ZipPayload) -> Dict[str, Any]:
    """
    把选中的文件/文件夹打包成 zip。

    返回一次性令牌，前端再拿令牌去 /api/fs/zip/download 下载。
    之所以分两步：POST 需要带 CSRF 令牌头，而浏览器的下载跳转带不了自定义头；
    拆成「先 POST 打包拿令牌，再 GET 下载」就能同时满足安全与可用性。
    """
    state = get_state(request)

    if not payload.paths:
        raise HTTPException(status_code=400, detail="没有选择任何文件")
    if len(payload.paths) > MAX_ZIP_ITEMS:
        raise HTTPException(status_code=400, detail="一次最多打包 %d 项" % MAX_ZIP_ITEMS)

    try:
        root_cfg, _base = _resolve(state.resolver, payload.root, "")

        items: List[Tuple[str, str]] = []
        total_size = 0
        used_names: Dict[str, int] = {}

        for rel in payload.paths:
            _r, abs_path = state.resolver.resolve(root_cfg["id"], rel)
            if not os.path.exists(abs_path):
                continue

            # 把整个磁盘打包既没有实际意义，又会长时间拖住服务，直接拒绝
            _ensure_not_root(root_cfg, abs_path, "打包")

            # 压缩包内的条目名去重，避免同名文件互相覆盖
            base_name = os.path.basename(abs_path) or "item"
            if base_name in used_names:
                used_names[base_name] += 1
                stem, ext = os.path.splitext(base_name)
                base_name = "%s (%d)%s" % (stem, used_names[base_name], ext)
            else:
                used_names[base_name] = 1

            items.append((abs_path, base_name))

            if os.path.isfile(abs_path):
                try:
                    total_size += os.path.getsize(abs_path)
                except OSError:
                    pass
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    if not items:
        raise HTTPException(status_code=404, detail="选中的文件都已不存在")

    # 单文件直接以原名打包；多选则用「打包下载_时间戳」
    if len(items) == 1 and os.path.isfile(items[0][0]):
        zip_filename = fsops.safe_zip_name(os.path.splitext(items[0][1])[0] + ".zip")
    else:
        zip_filename = "打包下载_%s.zip" % time.strftime("%Y%m%d_%H%M%S")

    try:
        os.makedirs(state.zip_temp_dir, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法创建打包临时目录：%s" % exc)

    # 打包前检查空间：zip 大约需要「总大小」级别的剩余空间（已压缩文件占比高时更小）
    try:
        free = shutil.disk_usage(state.zip_temp_dir).free
        if free < min(total_size, 512 * 1024 * 1024):
            raise HTTPException(
                status_code=507,
                detail="磁盘剩余空间不足，无法打包（可用 %s）" % fsops.human_size(free),
            )
    except HTTPException:
        raise
    except OSError:
        pass

    token = secrets.token_urlsafe(24)
    dest_zip = os.path.join(state.zip_temp_dir, token + ".zip")

    try:
        result = await run_in_threadpool(fsops.create_zip, items, dest_zip)
    except Exception as exc:  # noqa: BLE001
        try:
            if os.path.isfile(dest_zip):
                os.unlink(dest_zip)
        except OSError:
            pass
        raise _translate_error(exc)

    if result["file_count"] == 0:
        try:
            os.unlink(dest_zip)
        except OSError:
            pass
        raise HTTPException(status_code=400, detail="选中的内容为空或无法读取，未生成压缩包")

    state.add_zip_token(token, dest_zip, zip_filename, result["size"])

    return {
        "ok": True,
        "token": token,
        "filename": zip_filename,
        "file_count": result["file_count"],
        "size": result["size"],
        "size_text": fsops.human_size(result["size"]),
        "skipped": result["skipped"],
        "download_url": "/api/fs/zip/download?token=%s" % quote(token, safe=""),
    }


@router.get("/zip/download")
async def download_zip(request: Request, token: str = ""):
    """用打包令牌下载 zip（支持 Range，可断点续传）。"""
    state = get_state(request)

    if not token:
        raise HTTPException(status_code=400, detail="缺少下载令牌")

    info = state.pop_zip_token(token)
    if not info:
        raise HTTPException(status_code=404, detail="下载链接已过期或不存在，请重新打包")

    if not os.path.isfile(info["path"]):
        raise HTTPException(status_code=404, detail="打包文件已被清理，请重新打包")

    return file_response(
        request,
        info["path"],
        filename=info["filename"],
        inline=False,
        media_type="application/zip",
    )


# ---------------------------------------------------------------------------
# 压缩 / 解压
# ---------------------------------------------------------------------------
#
# 与 /api/fs/zip 的区别：那个是「打包给浏览器下载」，产物放临时目录、用完即弃；
# 这里是「在服务端就地生成压缩文件」，产物落在用户的目录里，长期保留。
# 所以这里要多做几件事：目标目录写权限、受保护路径、重名、剩余空间。

class CompressPayload(BaseModel):
    root: str = ""
    paths: List[str] = []
    # 压缩包存哪儿；两个都为空时放在第一个源的同级目录
    target_root: str = ""
    target_path: str = ""
    # 压缩包文件名（含扩展名）；留空自动起名
    name: str = ""
    # zip / tar / tar.gz / 7z / rar；留空按 name 的扩展名推断
    format: str = ""
    level: int = 3


class ExtractPayload(BaseModel):
    root: str = ""
    path: str = ""
    # 解压到哪儿；两个都为空时就是压缩包所在目录
    target_root: str = ""
    target_path: str = ""
    overwrite: bool = False
    # ★ 后台执行：立刻返回 job_id，前端轮询 /api/jobs/{id} 看进度。
    #   默认 false 保持原契约（请求挂到做完为止），命令行/脚本仍然能用同步这档。
    background: bool = False


# 各格式的默认扩展名（给「用户只写了名字没写后缀」兜底）
_FORMAT_EXT = {
    "zip": ".zip",
    "tar": ".tar",
    "tar.gz": ".tar.gz",
    "7z": ".7z",
    "rar": ".rar",
}

# Windows 文件名里不允许出现的字符
_INVALID_NAME_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


def _archive_cfg(state) -> Dict[str, Any]:
    """读取 config.json 的 archive 段，缺项用模块里的默认值。"""
    raw = state.cfg.get("archive") or {}
    cfg = {
        "max_entries": int(raw.get("max_entries") or archive.DEFAULT_MAX_ENTRIES),
        "max_total_mb": int(raw.get("max_total_mb") or archive.DEFAULT_MAX_TOTAL_MB),
        "max_single_mb": int(raw.get("max_single_mb") or archive.DEFAULT_MAX_SINGLE_MB),
        "rar_path": str(raw.get("rar_path") or ""),
        "unrar_path": str(raw.get("unrar_path") or ""),
    }
    # 顺手把外部工具路径下发给 archive 模块。
    #
    # 放在这里而不是各个端点里，是为了「不可能有哪个端点忘记调用」——
    # archive.unrar_path 之前就是这么变成死配置的：创建走了 rar_path，
    # 解压却只读环境变量，于是配置里写了也不生效，README 还写着可以配。
    archive.configure_tools(cfg["rar_path"], cfg["unrar_path"])
    return cfg


def _sources_total_size(paths: List[str], cap: int) -> int:
    """
    累加待压缩内容的总大小，一旦超过 cap 就提前收手。

    只用于判断「剩余空间够不够」，没必要为了精确值去遍历几百 GB 的目录树。
    """
    total = 0
    for path in paths:
        try:
            if os.path.isfile(path):
                total += os.path.getsize(path)
            elif os.path.isdir(path):
                for dirpath, _dirnames, filenames in os.walk(path):
                    for filename in filenames:
                        try:
                            total += os.path.getsize(os.path.join(dirpath, filename))
                        except OSError:
                            pass
                    if total > cap:
                        return total
        except OSError:
            pass
        if total > cap:
            return total
    return total


def _ensure_enough_space(target_dir: str, need: int, action: str) -> None:
    """
    落盘前检查剩余空间；不够就返回 507，而不是写到一半才报 IO 错误。

    复用 fsops.check_disk_space 而不是自己调 shutil.disk_usage：
    它已经预留了 64MB 余量（避免刚好把盘写满），这个细节自己写容易漏。
    它抛的是 OSError，这里翻译成 HTTP 状态码。
    """
    try:
        fsops.check_disk_space(target_dir, need)
    except OSError as exc:
        raise HTTPException(status_code=507, detail="%s失败：%s" % (action, exc))


def _unique_archive_name(dest_dir: str, name: str) -> str:
    """
    压缩包重名时自动加序号，不覆盖已有文件。

    解压是「重名直接拒绝」，压缩这里则相反 —— 产物是我们新建的文件，
    自动改名既能保住旧文件又不会让操作白跑一趟。
    """
    if not os.path.exists(os.path.join(dest_dir, name)):
        return name
    stem, ext = os.path.splitext(name)
    for index in range(2, 1000):
        candidate = "%s (%d)%s" % (stem, index, ext)
        if not os.path.exists(os.path.join(dest_dir, candidate)):
            return candidate
    raise HTTPException(status_code=409, detail="同名文件过多，请换个名字")


def _normalize_archive_name(name: str, fmt: str) -> Tuple[str, str]:
    """
    清洗用户给的压缩包名，并让「名字后缀」与「目标格式」保持一致。

    返回 (最终文件名, 最终格式)。格式留空时按后缀推断；两边都给且矛盾时
    以后缀为准（用户写的文件名更直观）。
    """
    name = _INVALID_NAME_CHARS.sub("", (name or "").strip()).strip(". ")
    if not name:
        name = "新建压缩包_%s" % time.strftime("%Y%m%d_%H%M%S")

    guessed = archive.detect_format(name)
    if guessed:
        # 后缀能认出来就以后缀为准，并保留用户的原始大小写写法。
        # 注意返回的是 archive 模块认的格式名（tar.gz / tar.bz2 / tar.xz 都归成 "tar"），
        # 具体压缩方式由 archive.create 按文件后缀决定。
        return name, guessed

    fmt = (fmt or "zip").strip().lower()
    if fmt not in _FORMAT_EXT:
        raise HTTPException(status_code=400,
                            detail="不支持的压缩格式：%s（支持 zip / tar / tar.gz / 7z / rar）" % fmt)
    return name + _FORMAT_EXT[fmt], ("tar" if fmt.startswith("tar") else fmt)


def _translate_archive_error(exc: Exception) -> HTTPException:
    """把 archive 模块的异常翻译成合适的 HTTP 状态码。"""
    if isinstance(exc, archive.ArchiveConflictError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, archive.ArchiveSecurityError):
        return HTTPException(status_code=400, detail=str(exc))
    if isinstance(exc, archive.ArchiveToolMissingError):
        return HTTPException(status_code=501, detail=str(exc))
    if isinstance(exc, archive.ArchiveError):
        return HTTPException(status_code=400, detail=str(exc))
    return _translate_error(exc)


@router.post("/compress")
async def compress_entries(request: Request, payload: CompressPayload) -> Dict[str, Any]:
    """把选中的文件/文件夹压缩成 zip / tar / 7z / rar，产物存在服务端目录里。"""
    state = get_state(request)
    cfg = _archive_cfg(state)

    if not payload.paths:
        raise HTTPException(status_code=400, detail="请先选择要压缩的文件或文件夹")

    try:
        root_cfg, _first_dir = _resolve(state.resolver, payload.root, payload.paths[0])
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    _ensure_writable(root_cfg)

    sources: List[str] = []
    for rel in payload.paths:
        _rc, abs_path = _resolve(state.resolver, payload.root, rel)
        if not os.path.exists(abs_path):
            raise HTTPException(status_code=404, detail="文件不存在：%s" % rel)
        # 这里**故意不做**受保护路径检查：压缩只是「读」源文件，
        # 而受保护路径的语义是「允许浏览、禁止修改」。同目录的打包下载
        # （/api/fs/zip）也只对源做 _ensure_not_root，保持一致。
        # 真正要拦的是「往受保护目录里写产物」，那在下面判目标目录时做。
        # 不允许把整个盘根打包（既是误操作，也几乎必然撑爆磁盘）
        _ensure_not_root(root_cfg, abs_path, "压缩")
        if abs_path not in sources:
            sources.append(abs_path)

    if not sources:
        raise HTTPException(status_code=400, detail="没有可压缩的内容")

    name, fmt = _normalize_archive_name(payload.name, payload.format)

    # 目标目录：没指定就放在第一个源的同级目录
    if payload.target_root or payload.target_path:
        dest_root_cfg, dest_dir = _resolve(
            state.resolver, payload.target_root, payload.target_path)
    else:
        dest_root_cfg = root_cfg
        dest_dir = os.path.dirname(sources[0])

    if not os.path.isdir(dest_dir):
        raise HTTPException(status_code=404, detail="目标文件夹不存在")
    _ensure_writable(dest_root_cfg)
    _ensure_not_protected(state.cfg, dest_dir)

    final_name = _unique_archive_name(dest_dir, name)
    dest_path = os.path.join(dest_dir, final_name)

    total = _sources_total_size(sources, 512 * 1024 * 1024)
    _ensure_enough_space(dest_dir, min(total, 512 * 1024 * 1024), "创建压缩包")

    try:
        result = await run_in_threadpool(
            archive.create, sources, dest_path,
            fmt=fmt, rar_path=cfg["rar_path"], level=int(payload.level or 3),
        )
    except Exception as exc:  # noqa: BLE001
        # 失败时清掉半成品，别在用户目录里留个坏压缩包
        try:
            if os.path.isfile(dest_path):
                os.unlink(dest_path)
        except OSError:
            pass
        raise _translate_archive_error(exc)

    return {
        "ok": True,
        "name": final_name,
        "renamed": final_name != name,
        "format": result["format"],
        "file_count": result["file_count"],
        "size": result["size"],
        "size_text": fsops.human_size(result["size"]),
        # 被跳过的符号链接/目录联接。要如实告诉用户，否则他会以为文件丢了
        "skipped": result.get("skipped", []),
    }


@router.get("/archive")
async def archive_listing(request: Request, root: str = "", path: str = "",
                          limit: int = 0) -> Dict[str, Any]:
    """
    列出压缩包里的条目（**只读，不解压**）。

    为什么值得单独做一个接口：解压是有副作用的操作，而「看内容」没有。
    用户想确认「这个包里到底有什么、会不会盖掉我的东西」时，不该被迫先解压
    到磁盘上再手动删掉。

    这里刻意复用 archive.list_entries —— 它本来就是解压流程的第一道安全闸门
    （逐条目校验名字、识别加密包、修 GBK 乱码名）。因此**看到的条目名与真正
    解压时会落盘的名字完全一致**，不会出现「列表里叫 A、解出来却叫 B」。
    """
    state = get_state(request)
    cfg = _archive_cfg(state)

    if not path:
        raise HTTPException(status_code=400, detail="请先选择要查看的压缩包")

    try:
        _root_cfg, archive_path = _resolve(state.resolver, root, path)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    if not os.path.isfile(archive_path):
        raise HTTPException(status_code=404, detail="压缩包不存在")

    fmt = archive.detect_format(archive_path)
    if not fmt:
        raise HTTPException(
            status_code=400,
            detail="无法识别的压缩格式（支持 zip / tar / tar.gz / tar.bz2 / tar.xz / 7z / rar）",
        )

    try:
        entries = await run_in_threadpool(
            archive.list_entries, archive_path, fmt, cfg["max_entries"])
    except Exception as exc:  # noqa: BLE001
        raise _translate_archive_error(exc)

    file_count = sum(1 for item in entries if not item["is_dir"])
    total_bytes = sum(int(item.get("size") or 0)
                      for item in entries if not item["is_dir"])

    # 解压遇到同名顶层项是**整体中止**的，所以提前把冲突算出来给前端展示，
    # 免得用户点完「解压」才发现被拒。用的是与解压完全同一份判断。
    conflicts = archive.top_level_conflicts(entries, os.path.dirname(archive_path))

    # 一个包可能有上万条目，整份塞给浏览器既慢又没用。
    # 默认只回传前 500 条，总数单独给，前端据此提示「还有多少没显示」。
    cap = int(limit) if limit and limit > 0 else 500
    cap = max(1, min(cap, cfg["max_entries"]))

    return {
        "ok": True,
        "format": fmt,
        "name": os.path.basename(archive_path),
        "entries": entries[:cap],
        "shown": min(len(entries), cap),
        "entry_count": len(entries),
        "file_count": file_count,
        "dir_count": len(entries) - file_count,
        "link_count": sum(1 for item in entries if item.get("is_link")),
        "total_bytes": total_bytes,
        "total_text": fsops.human_size(total_bytes),
        "conflicts": conflicts[:50],
        "conflict_count": len(conflicts),
    }


@router.post("/extract")
async def extract_archive(request: Request, payload: ExtractPayload) -> Dict[str, Any]:
    """
    解压压缩包。

    重名策略是「直接拒绝」：只要目标目录里已有同名顶层项就整体中止，
    并在 detail 里列出撞名的项。解压这类批量写入一旦半途覆盖，
    用户几乎没有恢复手段，宁可让他改个目录重来。
    """
    state = get_state(request)
    cfg = _archive_cfg(state)

    if not payload.path:
        raise HTTPException(status_code=400, detail="请先选择要解压的压缩包")

    try:
        root_cfg, archive_path = _resolve(state.resolver, payload.root, payload.path)
    except Exception as exc:  # noqa: BLE001
        raise _translate_error(exc)

    if not os.path.isfile(archive_path):
        raise HTTPException(status_code=404, detail="压缩包不存在")

    fmt = archive.detect_format(archive_path)
    if not fmt:
        raise HTTPException(
            status_code=400,
            detail="无法识别的压缩格式（支持 zip / tar / tar.gz / tar.bz2 / tar.xz / 7z / rar）",
        )

    # 解压也是「写入」，目标目录必须可写
    if payload.target_root or payload.target_path:
        dest_root_cfg, dest_dir = _resolve(
            state.resolver, payload.target_root, payload.target_path)
    else:
        dest_root_cfg = root_cfg
        dest_dir = os.path.dirname(archive_path)

    if not os.path.isdir(dest_dir):
        raise HTTPException(status_code=404, detail="目标文件夹不存在")
    _ensure_writable(dest_root_cfg)
    _ensure_not_protected(state.cfg, dest_dir)

    if not payload.overwrite:
        try:
            info = await run_in_threadpool(
                archive.inspect, archive_path, dest_dir, fmt=fmt,
                max_entries=cfg["max_entries"], max_total_mb=cfg["max_total_mb"],
                max_single_mb=cfg["max_single_mb"],
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_archive_error(exc)

        if info["conflicts"]:
            shown = "、".join(info["conflicts"][:5])
            more = "" if len(info["conflicts"]) <= 5 else " 等 %d 项" % len(info["conflicts"])
            raise HTTPException(
                status_code=409,
                detail="目标文件夹里已有同名项（%s%s），为避免覆盖已中止解压。"
                       "请改到别的文件夹，或先重命名/移走这些项。" % (shown, more),
            )
        _ensure_enough_space(dest_dir, min(info["total_bytes"], 512 * 1024 * 1024), "解压")

    def _run_extract(progress=None, should_cancel=None) -> Dict[str, Any]:
        """同步与后台两条路径共用的解压实现。"""
        try:
            result = archive.extract(
                archive_path, dest_dir, fmt=fmt,
                max_entries=cfg["max_entries"], max_total_mb=cfg["max_total_mb"],
                max_single_mb=cfg["max_single_mb"], overwrite=payload.overwrite,
                progress=progress, should_cancel=should_cancel,
            )
        except Exception as exc:  # noqa: BLE001
            raise _translate_archive_error(exc)

        return {
            "ok": True,
            "format": result["format"],
            "entries": result["entries"],
            "extracted": result["extracted"],
            "skipped": result["skipped"],
            "size_text": fsops.human_size(result["total_bytes"]),
            "target": os.path.basename(dest_dir) or dest_dir,
        }

    # ---- 后台模式：冲突检查等校验已在上面同步做完，解压本身丢给任务队列 ----
    if payload.background:
        def work(job) -> Dict[str, Any]:
            # 先用条目表算出总量：这样进度条能显示「3/128 项」，
            # 而不是一个永远不知道还剩多久的转圈。
            try:
                listing = archive.list_entries(archive_path, fmt, cfg["max_entries"])
                job.set_totals(
                    items=len(listing),
                    total_bytes=sum(int(item.get("size") or 0) for item in listing),
                )
            except Exception:  # noqa: BLE001 - 量不出总量不算失败，退化成按条目计
                pass

            return _run_extract(
                progress=lambda items, size, name: job.advance(
                    items=items, bytes=size, current=name),
                should_cancel=job.cancel_requested,
            )

        job = jobs.manager.submit(
            "解压", "解压 %s" % os.path.basename(archive_path), work)
        return {
            "ok": True,
            "background": True,
            "job_id": job.id,
            "message": "已加入后台队列（解压 %s），可在任务列表里查看进度"
                       % os.path.basename(archive_path),
        }

    return await run_in_threadpool(_run_extract)

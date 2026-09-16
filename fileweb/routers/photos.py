# -*- coding: utf-8 -*-
"""
照片（时间轴相册）路由
======================

    GET  /api/photos/library       —— 整库一次取全（照片 + 统计 + 相册 + 偏好）
    GET  /api/photos/item          —— 单张详情（EXIF 原值 + 覆盖值 + 有效值）
    GET  /api/photos/thumb         —— 画廊缩略图（比资源管理器的大得多）
    GET  /api/photos/raw           —— 原图（支持 Range，大图能拖着看）
    GET  /api/photos/sources       —— 已纳入索引的目录清单
    POST /api/photos/import        —— 就地索引选中的文件/文件夹（可走后台任务）
    POST /api/photos/rescan        —— 重扫：核对、找回被移动的文件、发现新增
    POST /api/photos/edit          —— 改一批照片的时间 / 地点 / 标签 / 星级 / 备注
    POST /api/photos/batch/time    —— 批量平移时间（或统一设为同一个时间）
    POST /api/photos/undo          —— 撤销最近一次编辑
    POST /api/photos/albums        —— 新建相册（手动 / 按时间段自动归类）
    POST /api/photos/albums/rename —— 重命名相册
    POST /api/photos/albums/delete —— 删相册（不动照片）
    POST /api/photos/albums/items  —— 往手动相册里加/移出照片
    POST /api/photos/prefs         —— 保存浏览偏好（时间轴粒度 / 排序）
    POST /api/photos/forget        —— 把一个目录移出相册（不删任何文件）

★ 所有涉及文件的接口都走**当前用户的路径解析器**（resolver_of）：
  「能索引什么、能看到什么」与文件管理器完全一致，相册不是绕过可见性的旁路。

★ 照片标识（id）是内容指纹，只用来在索引里查表；查到的「根标识 + 相对路径」
  还要再过一遍解析器才变成绝对路径。所以就算有人手工改了 photo_index.json，
  也读不出根目录外的文件 —— 详见 fileweb/photos.py 头部的安全说明。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import jobs
from .. import photos
from .. import thumbs
from ..deps import get_state, get_user, resolver_of, owner_of
from ..http_utils import file_response

router = APIRouter(prefix="/api/photos", tags=["照片"])


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

class ImportPayload(BaseModel):
    """就地索引：root + 相对路径列表（文件或文件夹，文件夹会递归）。"""
    root: str = ""
    paths: List[str] = []
    # true = 立刻返回 job_id，实际索引在后台任务里跑（右下角面板能看进度、能取消）
    background: bool = False


class EditPayload(BaseModel):
    """
    改一批照片。

    patch 是「要改哪些字段」的字典（只出现的键才会动）：
        {"taken_at": "...", "place": {...}, "tags": [...], "rating": 5, "caption": "..."}
    为了前端写起来顺手，也接受同名的扁平字段，它们会覆盖到 patch 上。
    """
    ids: List[str] = []
    patch: Dict[str, Any] = {}
    taken_at: Optional[str] = None
    place: Optional[Dict[str, Any]] = None
    tags: Optional[Any] = None
    rating: Optional[int] = None
    caption: Optional[str] = None


class ShiftTimePayload(BaseModel):
    """批量平移时间。delta_seconds / delta_hours 二选一；set_to 表示统一设为某个时间。"""
    ids: List[str] = []
    delta_seconds: Optional[int] = None
    delta_hours: Optional[float] = None
    set_to: Optional[str] = None


class AlbumPayload(BaseModel):
    name: str = ""
    kind: str = "manual"          # manual | smart
    start: str = ""               # smart 用：起始时间
    end: str = ""                 # smart 用：结束时间
    items: List[str] = []


class AlbumRenamePayload(BaseModel):
    id: str = ""
    name: str = ""


class AlbumDeletePayload(BaseModel):
    id: str = ""


class AlbumItemsPayload(BaseModel):
    id: str = ""
    ids: List[str] = []
    action: str = "add"           # add | remove


class PrefsPayload(BaseModel):
    level: Optional[str] = None
    sort: Optional[str] = None


class ForgetPayload(BaseModel):
    root: str = ""
    path: str = ""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _settings(request: Request) -> None:
    """功能被服务端关掉时一律 403（前端据此隐藏入口，两边同一个判断）。"""
    cfg = get_state(request).cfg
    if not (cfg.get("photos") or {}).get("enabled", True):
        raise HTTPException(
            status_code=403,
            detail="照片应用已在服务端关闭（config.json 的 photos.enabled = false）",
        )


def _cfg(request: Request) -> Dict[str, Any]:
    return get_state(request).cfg


def _photo_error(exc: Exception) -> HTTPException:
    """把库层的可读错误翻成 400，而不是 500。"""
    if isinstance(exc, photos.PhotoError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=400, detail=str(exc))


def _safe_unlink(path: str) -> None:
    """
    删掉半成品文件（上传中断、索引失败时用）。

    ★ 必须吞掉异常：这一步是在**出错路径**上执行的，如果它自己再抛一个，
      就会把真正的原因（比如「超过大小上限」）盖成「删除失败」。
    """
    try:
        os.unlink(path)
    except OSError:
        pass


def _thumb_cache(cfg: Dict[str, Any], state) -> tuple:
    """缩略图缓存目录与体积上限（复用 thumbs 那套配置与 LRU 淘汰）。"""
    thumb_cfg = cfg.get("thumbs") or {}
    cache_dir = thumb_cfg.get("cache_dir") or os.path.join(state.base_dir, "thumb_cache")
    max_mb = int(thumb_cfg.get("max_cache_mb") or 512)
    return cache_dir, max_mb


# ---------------------------------------------------------------------------
# 库
# ---------------------------------------------------------------------------

@router.get("/library")
async def get_library(request: Request) -> Dict[str, Any]:
    """
    整库一次取全（前端自己分组 / 排序 / 筛选）。

    为什么不分页：目标是单用户几千张，这个量级的 JSON 只有 1~2MB；
    一次取全能让「时间轴分组、跨月筛选、批量选择」全在前端做，简单得多。
    真要上万张时再按 ceiling 参数平滑升级。
    """
    _settings(request)
    cfg = _cfg(request)
    state = get_state(request)
    user = get_user(request)

    try:
        data = await run_in_threadpool(
            photos.build_library, cfg, user, resolver_of(request))
    except photos.PhotoError as exc:
        raise _photo_error(exc)

    return {
        "ok": True,
        "library": {
            "indexed": data["stats"]["total"],
            "inaccessible": data["inaccessible"],
            "sources": data["sources"],
            "limits": data["limits"],
        },
        "photos": data["photos"],
        "stats": data["stats"],
        "truncated": data["truncated"],
        "albums": data["albums"],
        "prefs": data["prefs"],
    }


@router.get("/item")
async def get_item(request: Request, id: str = "") -> Dict[str, Any]:
    """单张详情：EXIF 原值 / 用户覆盖值 / 最终有效值都在这里。"""
    _settings(request)
    cfg = _cfg(request)
    try:
        detail = await run_in_threadpool(
            photos.get_photo_detail, cfg, get_user(request),
            resolver_of(request), id)
    except photos.PhotoError as exc:
        raise _photo_error(exc)
    return {"ok": True, "photo": detail}


# ---------------------------------------------------------------------------
# 看图
# ---------------------------------------------------------------------------

@router.get("/thumb")
async def get_thumb(request: Request, id: str = "", box: int = 0):
    """
    画廊缩略图（默认 320，比资源管理器的 100 大得多）。

    ★ 缓存键里带着尺寸，所以与资源管理器共用同一个 thumb_cache/ 也不会打架：
      100 的那批是缩略图视图用的，320 的是相册网格用的，各自命中各自的。
    """
    _settings(request)
    cfg = _cfg(request)
    state = get_state(request)

    try:
        _entry, abs_path = await run_in_threadpool(
            photos.resolve_photo, cfg, get_user(request), resolver_of(request), id)
    except photos.PhotoError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if not thumbs.is_image(abs_path):
        raise HTTPException(status_code=404, detail="不是支持的图片类型")

    cache_dir, max_mb = _thumb_cache(cfg, state)
    size = int(box) if box else photos.thumb_size(cfg)
    size = min(1024, max(64, size))

    try:
        ok, thumb_path = await run_in_threadpool(
            thumbs.ensure_thumb, abs_path, cache_dir, size, max_mb)
    except Exception:  # noqa: BLE001
        ok, thumb_path = False, None

    if not ok or not thumb_path:
        # 404 会让前端换成占位块；坏图、超大图都属于这一档，不是错误
        raise HTTPException(status_code=404, detail="无法生成缩略图")

    return file_response(
        request, thumb_path, filename="thumb.jpg", inline=True,
        media_type="image/jpeg",
        extra_headers={"Cache-Control": "private, max-age=86400"},
    )


@router.get("/raw")
async def get_raw(request: Request, id: str = "", download: int = 0):
    """
    原图。复用 file_response（Range / ETag / 中文名 / 内联安全头全都处理好了）。

    download=1 时作为附件下载，其余情况内联显示（大图查看就是这个接口）。
    """
    _settings(request)
    cfg = _cfg(request)

    try:
        entry, abs_path = await run_in_threadpool(
            photos.resolve_photo, cfg, get_user(request), resolver_of(request), id)
    except photos.PhotoError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return file_response(
        request, abs_path,
        filename=os.path.basename(entry["relpath"]),
        inline=not bool(download),
    )


# ---------------------------------------------------------------------------
# 索引
# ---------------------------------------------------------------------------

@router.post("/import")
async def import_photos(request: Request, payload: ImportPayload) -> Dict[str, Any]:
    """
    把文件管理器里的照片「就地索引」进相册。

    ★ 与音乐播放器的导入**不同**：这里不复制任何文件，只记录位置与元数据。
      照片库动辄几十上百 GB，复制一份在时间与空间上都不可接受。
      代价（文件被移走/改名）由内容指纹与重扫兜住，见 fileweb/photos.py。
    """
    _settings(request)
    cfg = _cfg(request)
    user = get_user(request)
    resolver = resolver_of(request)

    # ★ 保留空串：`paths: [""]` 表示「就是根目录本身」（用户在资源管理器里
    #   选中某个根再导入时就是这个形状）。只有「一个路径都没给」才算没选东西 ——
    #   早先这里顺手把空串一起过滤掉了，结果「导入整个根目录」永远返回 400。
    if not payload.paths:
        raise HTTPException(status_code=400, detail="没有选择要索引的文件或文件夹")
    sources = [str(p or "").strip() for p in payload.paths]

    # ---- 后台模式：立刻返回 job_id，进度显示在右下角的任务面板里 ----
    if payload.background:
        def work(job) -> Dict[str, Any]:
            return photos.import_paths(cfg, user, resolver, payload.root, sources, job)

        job = jobs.manager.submit(
            "photos-import", "索引 %d 项照片" % len(sources), work,
            owner=owner_of(request))
        return {
            "ok": True,
            "background": True,
            "job_id": job.id,
            "message": "已加入后台队列，正在索引照片（可在任务面板查看进度）",
        }

    try:
        result = await run_in_threadpool(
            photos.import_paths, cfg, user, resolver, payload.root, sources)
    except photos.PhotoError as exc:
        raise _photo_error(exc)

    return {"ok": True, "background": False, **result}


@router.post("/upload")
async def upload_photo(request: Request, filename: str = "") -> Dict[str, Any]:
    """
    从**浏览器所在的电脑**上传一张照片（原始请求体 + `?filename=`，流式落盘）。

    为什么要有它：照片默认是从**服务器上已有的文件**里就地索引的，但用户手上
    的照片往往在自己那台电脑里（手机导出的、相机卡里的）。没有这个接口就只能
    先想办法把文件弄到服务器上 —— 那正是文件管理器上传能做的事，但用户不该
    为此先去开一个资源管理器窗口。

    ★ 与壁纸、文件上传、音乐上传同一套做法：**流式**写盘、边写边数、
      超限立刻中止并删掉半成品 —— 不用先把整个文件读进内存。
    ★ 落盘位置与命名（不覆盖、只收图片）由 photos.prepare_upload_target 决定。
    """
    from urllib.parse import unquote

    _settings(request)
    cfg = _cfg(request)
    user = get_user(request)
    max_bytes = photos.max_upload_bytes(cfg)
    blocked = (cfg.get("upload") or {}).get("blocked_extensions") or ()

    raw_name = filename or request.headers.get("x-file-name") or ""
    if not raw_name:
        raise HTTPException(status_code=400, detail="缺少文件名参数 filename")
    try:
        raw_name = unquote(raw_name)
    except Exception:  # noqa: BLE001
        pass

    # 先看 Content-Length：既用于快速拒绝过大的文件，也用于**写入前**检查磁盘空间
    declared_bytes = 0
    declared = request.headers.get("content-length")
    if declared:
        try:
            declared_bytes = int(declared)
        except ValueError:
            declared_bytes = 0
        if declared_bytes > max_bytes:
            raise HTTPException(
                status_code=413,
                detail="单张照片不能超过 %d MB" % (max_bytes // (1024 * 1024)),
            )

    try:
        final_name, target = photos.prepare_upload_target(
            cfg, user, raw_name, declared_size=declared_bytes,
            blocked_extensions=blocked)
    except photos.PhotoError as exc:
        raise _photo_error(exc)

    part_path = target + ".part"
    written = 0
    try:
        with open(part_path, "wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                # 边写边数：Content-Length 可能缺失或说谎，真正的上限在这里兜底
                if written > max_bytes:
                    raise HTTPException(
                        status_code=413,
                        detail="单张照片不能超过 %d MB" % (max_bytes // (1024 * 1024)),
                    )
                await run_in_threadpool(fh.write, chunk)

        if written == 0:
            raise HTTPException(status_code=400, detail="上传内容为空")

        os.replace(part_path, target)
    except HTTPException:
        _safe_unlink(part_path)
        raise
    except Exception as exc:  # noqa: BLE001
        _safe_unlink(part_path)
        raise HTTPException(status_code=500, detail="保存照片失败：%s" % exc)

    # 落盘成功就立刻索引进去 —— 用户下一眼就该在时间轴上看到它
    try:
        item = await run_in_threadpool(
            photos.index_upload, cfg, user, resolver_of(request), target, final_name)
    except photos.PhotoError as exc:
        _safe_unlink(target)
        raise _photo_error(exc)
    except Exception as exc:  # noqa: BLE001
        _safe_unlink(target)
        raise HTTPException(status_code=500, detail="索引上传的照片失败：%s" % exc)

    # ★ 内容重复：照片的身份就是内容指纹，同一张传两遍只该在相册里出现一次。
    #   刚写下的那份要删掉 —— 留着它在磁盘上就是个永远不会被索引的副本
    #   （用户看不到、也不会去删），而且还占着空间。这里必须明说，
    #   不能让用户以为「我传了两张，怎么只有一张」。
    if item.get("duplicate"):
        _safe_unlink(target)
        return {
            "ok": True,
            "photo": item,
            "size": written,
            "duplicate": True,
            "message": "「%s」和相册里已有的那张内容完全相同，已跳过（没有重复保存）"
                       % final_name,
        }

    return {
        "ok": True,
        "photo": item,
        "size": written,
        "duplicate": False,
        "message": "已上传「%s」" % final_name,
    }


@router.post("/rescan")
async def rescan(request: Request) -> Dict[str, Any]:
    """
    重扫：核对已知条目、按内容指纹找回被移动/改名的文件、发现新增。

    ★ 找回之后用户改过的时间与地点**自动跟着走**（编辑是按指纹存的），
      这正是「就地索引」这套设计能不能用的关键。
    """
    _settings(request)
    cfg = _cfg(request)
    user = get_user(request)
    resolver = resolver_of(request)

    def work(job) -> Dict[str, Any]:
        return photos.rescan(cfg, user, resolver, job)

    try:
        result = await run_in_threadpool(work, None)
    except photos.PhotoError as exc:
        raise _photo_error(exc)

    return {"ok": True, **result}


@router.get("/sources")
async def get_sources(request: Request) -> Dict[str, Any]:
    """已纳入索引的目录清单，并标出哪些当前不可访问（例如移动硬盘没插）。"""
    _settings(request)
    cfg = _cfg(request)
    user = get_user(request)
    resolver = resolver_of(request)

    state = photos.load_state(cfg, user)
    items = []
    for source in state["sources"]:
        item = dict(source)
        try:
            _root, abs_path = resolver.resolve(source["root"], source["path"])
            item["exists"] = os.path.isdir(abs_path)
        except Exception:  # noqa: BLE001 - 越权/根不存在都归为「不可访问」
            item["exists"] = False
        items.append(item)

    return {"ok": True, "sources": items}


@router.post("/forget")
async def forget_source(request: Request, payload: ForgetPayload) -> Dict[str, Any]:
    """
    把一个目录移出相册（连同它的照片条目与编辑记录）。

    ★ **不会删除任何照片文件** —— 这是最容易让人误解的一步，所以提示语里
      明确写出来，前端也会再确认一次。
    """
    _settings(request)
    cfg = _cfg(request)
    try:
        result = await run_in_threadpool(
            photos.forget_source, cfg, get_user(request),
            payload.root, payload.path)
    except photos.PhotoError as exc:
        raise _photo_error(exc)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------

def _collect_patch(payload: EditPayload) -> Dict[str, Any]:
    """把 patch 与扁平字段合成一份「要改什么」。"""
    patch: Dict[str, Any] = dict(payload.patch or {})
    for key in ("taken_at", "place", "tags", "rating", "caption"):
        value = getattr(payload, key)
        if value is not None:
            patch[key] = value
    return patch


@router.post("/edit")
async def edit_photos(request: Request, payload: EditPayload) -> Dict[str, Any]:
    """
    改一批照片的时间 / 地点 / 标签 / 星级 / 备注。

    ★ 修改只写进 photos_state.json，**原图一个字节都不动**。
      这是刻意的：写回 EXIF 会重编码 JPEG（画质损失、丢失厂商 MakerNote），
      而这个项目一贯的取向是绝不静默覆盖原始资料。界面上也明确告诉用户
      「改的是相册里的记录，不会改照片文件本身」。
    """
    _settings(request)
    cfg = _cfg(request)

    patch = _collect_patch(payload)
    if not patch:
        raise HTTPException(status_code=400, detail="没有要修改的内容")

    try:
        result = await run_in_threadpool(
            photos.apply_edits, cfg, get_user(request), payload.ids, patch)
    except photos.PhotoError as exc:
        raise _photo_error(exc)

    return {"ok": True, **result, "message": "已更新 %d 张照片" % result["updated"]}


@router.post("/batch/time")
async def batch_time(request: Request, payload: ShiftTimePayload) -> Dict[str, Any]:
    """
    批量时间：整体平移（保留相对先后）或统一设为同一个时间。

    平移是照片整理里最常用的那一个动作：相机时区设错了，一次旅行拍的
    几百张全部偏了 8 小时 —— 与其一张张改，不如整体挪。
    """
    _settings(request)
    cfg = _cfg(request)
    user = get_user(request)

    # 统一设为同一个时间：就是一次普通的覆盖写入
    if payload.set_to:
        try:
            result = await run_in_threadpool(
                photos.apply_edits, cfg, user, payload.ids,
                {"taken_at": payload.set_to}, "set_time", "统一设置时间")
        except photos.PhotoError as exc:
            raise _photo_error(exc)
        return {"ok": True, **result, "mode": "set",
                "message": "已把 %d 张照片的时间设为同一个值" % result["updated"]}

    delta = payload.delta_seconds
    if delta is None and payload.delta_hours is not None:
        try:
            delta = int(round(float(payload.delta_hours) * 3600))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="时间增量不合法")
    if delta is None:
        raise HTTPException(status_code=400, detail="请提供 delta_seconds 或 delta_hours")

    try:
        result = await run_in_threadpool(
            photos.shift_time, cfg, user, payload.ids, delta)
    except photos.PhotoError as exc:
        raise _photo_error(exc)

    hours = result["delta"] / 3600.0
    return {"ok": True, **result, "mode": "shift",
            "message": "已把 %d 张照片整体平移 %+.2f 小时" % (result["updated"], hours)}


@router.post("/undo")
async def undo(request: Request) -> Dict[str, Any]:
    """撤销最近一次编辑（批量平移改错了可以一键回退）。"""
    _settings(request)
    cfg = _cfg(request)
    try:
        result = await run_in_threadpool(photos.undo_last, cfg, get_user(request))
    except photos.PhotoError as exc:
        raise _photo_error(exc)
    return {"ok": True, **result,
            "message": "已撤销「%s」（恢复 %d 张）"
                       % (result.get("label") or "上一次操作", result["restored"])}


# ---------------------------------------------------------------------------
# 相册
# ---------------------------------------------------------------------------

@router.post("/albums")
async def create_album(request: Request, payload: AlbumPayload) -> Dict[str, Any]:
    """新建相册：manual（挑照片）或 smart（按时间段自动归类）。"""
    _settings(request)
    cfg = _cfg(request)
    try:
        album = await run_in_threadpool(
            photos.create_album, cfg, get_user(request), payload.name,
            payload.kind, payload.start, payload.end, payload.items)
    except photos.PhotoError as exc:
        raise _photo_error(exc)
    return {"ok": True, "album": album, "message": "已创建相册「%s」" % album["name"]}


@router.post("/albums/rename")
async def rename_album(request: Request, payload: AlbumRenamePayload) -> Dict[str, Any]:
    _settings(request)
    cfg = _cfg(request)
    try:
        album = await run_in_threadpool(
            photos.rename_album, cfg, get_user(request), payload.id, payload.name)
    except photos.PhotoError as exc:
        raise _photo_error(exc)
    return {"ok": True, "album": album}


@router.post("/albums/delete")
async def delete_album(request: Request, payload: AlbumDeletePayload) -> Dict[str, Any]:
    """删相册。★ 只是删掉这个归类，照片与编辑记录一张都不动。"""
    _settings(request)
    cfg = _cfg(request)
    try:
        await run_in_threadpool(
            photos.delete_album, cfg, get_user(request), payload.id)
    except photos.PhotoError as exc:
        raise _photo_error(exc)
    return {"ok": True, "message": "已删除相册（照片本身没有被删除）"}


@router.post("/albums/items")
async def album_items(request: Request, payload: AlbumItemsPayload) -> Dict[str, Any]:
    """往手动相册里加照片 / 移出照片。"""
    _settings(request)
    cfg = _cfg(request)
    try:
        result = await run_in_threadpool(
            photos.album_items, cfg, get_user(request),
            payload.id, payload.ids, payload.action)
    except photos.PhotoError as exc:
        raise _photo_error(exc)
    return {"ok": True, **result}


# ---------------------------------------------------------------------------
# 偏好
# ---------------------------------------------------------------------------

@router.post("/prefs")
async def save_prefs(request: Request, payload: PrefsPayload) -> Dict[str, Any]:
    """保存浏览偏好（时间轴粒度 / 排序），按用户各存一份。"""
    _settings(request)
    cfg = _cfg(request)
    patch = {k: v for k, v in (("level", payload.level), ("sort", payload.sort))
             if v is not None}
    try:
        prefs = await run_in_threadpool(
            photos.save_prefs, cfg, get_user(request), patch)
    except photos.PhotoError as exc:
        raise _photo_error(exc)
    return {"ok": True, "prefs": prefs}

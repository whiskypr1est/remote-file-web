# -*- coding: utf-8 -*-
"""
音乐播放器路由
==============

    GET  /api/music/library          —— 库信息 + 全部歌曲 + 歌单 + 播放偏好
    POST /api/music/import           —— 从「我能看到的文件」里导入到音乐库
    POST /api/music/upload           —— 直接上传音频文件（原始请求体 + ?filename=）
    POST /api/music/delete           —— 从库里删除一首歌（连同歌词）
    GET  /api/music/stream           —— 播放音频（支持 Range，进度条能拖）
    GET  /api/music/lyrics           —— 读歌词（自动识别同名 .lrc）
    POST /api/music/lyrics           —— 上传 / 粘贴歌词
    POST /api/music/playlists        —— 新建歌单
    POST /api/music/playlists/rename —— 重命名歌单
    POST /api/music/playlists/delete —— 删除歌单
    POST /api/music/playlists/songs  —— 往歌单里加歌 / 移出歌单
    POST /api/music/prefs            —— 保存播放偏好（音量 / 模式 / 上次播到哪）

★ 导入走的是**当前用户的路径解析器**（resolver_of），所以「能导入什么」与
  「文件管理器能看到什么」完全一致 —— 不存在「播放器能读到文件管理器看不到的
  东西」这种旁路。子用户导入不了别人的目录，也导入不了未分配的盘。

★ 歌词与歌曲都只落在**当前用户的库目录**里；所有接受歌曲标识的接口都过
  music._safe_song_id（只认裸文件名 + 音频扩展名），因此删不掉、也写不到库外面。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import music
from ..deps import get_state, get_user, resolver_of
from ..http_utils import file_response
from ..security import PathSecurityError

router = APIRouter(prefix="/api/music", tags=["音乐播放器"])


# ---------------------------------------------------------------------------
# 请求体
# ---------------------------------------------------------------------------

class ImportPayload(BaseModel):
    """从可见目录导入：root + 相对路径列表（与文件管理器的复制接口同一套写法）。"""
    root: str = ""
    paths: List[str] = []


class SongPayload(BaseModel):
    id: str = ""


class LyricsPayload(BaseModel):
    id: str = ""
    text: str = ""


class PlaylistPayload(BaseModel):
    name: str = ""


class PlaylistRenamePayload(BaseModel):
    id: str = ""
    name: str = ""


class PlaylistSongPayload(BaseModel):
    id: str = ""            # 歌单 id
    song: str = ""          # 歌曲标识（文件名）
    action: str = "add"     # add | remove


class PrefsPayload(BaseModel):
    volume: Optional[float] = None
    mode: Optional[str] = None
    muted: Optional[bool] = None
    last: Optional[str] = None
    source: Optional[str] = None


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _settings(request: Request) -> Dict[str, Any]:
    cfg = get_state(request).cfg
    settings = cfg.get("music") or {}

    if not settings.get("enabled", True):
        raise HTTPException(
            status_code=403,
            detail="音乐播放器已在服务端关闭（config.json 的 music.enabled = false）",
        )
    return cfg


def _max_upload_bytes(cfg: Dict[str, Any]) -> int:
    settings = cfg.get("music") or {}
    try:
        mb = int(settings.get("max_upload_mb") or 200)
    except (TypeError, ValueError):
        mb = 200
    return max(1, mb) * 1024 * 1024


def _user(request: Request) -> Dict[str, Any]:
    return get_user(request)


def _music_error(exc: Exception) -> HTTPException:
    """把库层的可读错误翻译成 400（而不是 500）。"""
    return HTTPException(status_code=400, detail=str(exc))


# ---------------------------------------------------------------------------
# 库
# ---------------------------------------------------------------------------

@router.get("/library")
async def get_library(request: Request) -> Dict[str, Any]:
    """
    整个曲库（一次取全，前端自己排序 / 搜索 / 过滤）。

    曲库不会大到需要分页：几百首歌的 JSON 只有几十 KB，
    而分页会让「搜索 / 排序 / 播放列表翻页」都变复杂，得不偿失。
    """
    cfg = _settings(request)
    user = _user(request)

    try:
        summary = music.library_summary(cfg, user)
        songs = music.list_songs(cfg, user)
        state = music.load_state(cfg, user)
    except music.MusicError as exc:
        raise _music_error(exc)

    return {
        "ok": True,
        "library": summary,
        "songs": songs,
        "playlists": state["playlists"],
        "prefs": state["prefs"],
        "limits": {
            "max_upload_mb": _max_upload_bytes(cfg) // (1024 * 1024),
        },
    }


# ---------------------------------------------------------------------------
# 导入 / 上传 / 删除
# ---------------------------------------------------------------------------

@router.post("/import")
async def import_songs(request: Request, payload: ImportPayload) -> Dict[str, Any]:
    """
    把文件管理器里的文件「导入」到音乐库（复制一份，源文件不动）。

    为什么是复制而不是引用原位置：曲库要能独立于用户目录存在 ——
    学生把一个学期的东西删掉时，不该顺手把自己的歌单也清空；
    而「引用」还要处理文件被改名 / 移动 / 移出可见范围之后的各种失效。
    复制一份语义最简单，也和「导入」这个词的直觉一致。
    """
    cfg = _settings(request)
    user = _user(request)
    resolver = resolver_of(request)
    max_bytes = _max_upload_bytes(cfg)

    sources: List[str] = []
    for rel in (payload.paths or []):
        text = str(rel or "").strip()
        if text:
            sources.append(text)

    if not sources:
        raise HTTPException(status_code=400, detail="没有选择要导入的文件")

    imported: List[Dict[str, Any]] = []
    skipped: List[Dict[str, str]] = []

    for rel in sources:
        try:
            _root_cfg, abs_path = resolver.resolve(payload.root, rel)
        except PathSecurityError as exc:
            # 看不见的路径：与文件管理器同一套拒绝口径
            skipped.append({"name": rel, "reason": str(exc)})
            continue

        if not abs_path or not os.path.isfile(abs_path):
            skipped.append({"name": rel, "reason": "不是文件（或已不存在）"})
            continue

        name = rel.replace("\\", "/").rstrip("/").split("/")[-1] or abs_path
        try:
            # 复制是阻塞 IO，丢到线程池，别把事件循环占住
            item = await run_in_threadpool(
                music.import_file, cfg, user, abs_path, name, max_bytes)
            imported.append(item)
        except music.MusicError as exc:
            skipped.append({"name": name, "reason": str(exc)})

    if not imported and skipped:
        # 一个都没成：直接把第一个原因当错误返回，比让前端自己去翻列表清楚
        raise HTTPException(status_code=400, detail=skipped[0]["reason"])

    message = "已导入 %d 首" % len(imported)
    if skipped:
        message += "，%d 个跳过" % len(skipped)
    return {
        "ok": True,
        "imported": imported,
        "skipped": skipped,
        "message": message,
    }


@router.post("/upload")
async def upload_song(request: Request, filename: str = "") -> Dict[str, Any]:
    """
    上传一个音频文件（原始请求体 + ?filename=）。

    与壁纸、文件上传同一套做法：**流式**落盘，边写边记字节数，
    超限立刻中止并删掉半成品 —— 不用先把整个文件读进内存。
    """
    from urllib.parse import unquote

    cfg = _settings(request)
    user = _user(request)
    max_bytes = _max_upload_bytes(cfg)

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
                detail="单个音频不能超过 %d MB" % (max_bytes // (1024 * 1024)),
            )

    try:
        # 目标路径还没写字节就先算好：文件名 / 扩展名 / 重名 / 剩余空间都在这一步定下来
        final_name, target = music.prepare_target(
            cfg, user, raw_name, declared_size=declared_bytes)
    except music.MusicError as exc:
        raise _music_error(exc)

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
                        detail="单个音频不能超过 %d MB" % (max_bytes // (1024 * 1024)),
                    )
                await run_in_threadpool(fh.write, chunk)

        if written == 0:
            raise HTTPException(status_code=400, detail="上传内容为空")

        os.replace(part_path, target)
    except HTTPException:
        music.safe_unlink(part_path)
        raise
    except Exception as exc:  # noqa: BLE001
        music.safe_unlink(part_path)
        raise HTTPException(status_code=500, detail="保存音频失败：%s" % exc)

    title, artist = music.parse_title(final_name)
    return {
        "ok": True,
        "song": {
            "id": final_name,
            "name": final_name,
            "title": title,
            "artist": artist,
            "size": written,
            "has_lyrics": False,
        },
        "message": "已上传「%s」" % final_name,
    }


@router.post("/delete")
async def delete_song(request: Request, payload: SongPayload) -> Dict[str, Any]:
    """从库里删掉一首歌（连同它的 .lrc）。源文件不受影响 —— 导入本来就是复制。"""
    cfg = _settings(request)
    user = _user(request)

    try:
        removed = music.delete_song(cfg, user, payload.id)
    except music.MusicError as exc:
        raise _music_error(exc)

    if not removed:
        raise HTTPException(status_code=404, detail="这首歌不在库里")

    return {"ok": True, "message": "已从音乐库移除「%s」" % payload.id}


# ---------------------------------------------------------------------------
# 播放
# ---------------------------------------------------------------------------

@router.get("/stream")
async def stream_song(request: Request, id: str = "") -> Any:
    """
    输出音频本身（供 <audio> 播放）。

    复用 http_utils.file_response：它已经处理好了 Range（拖动进度条）、
    ETag / Last-Modified 条件请求、以及内联输出时的安全头。
    没有 Range 支持的话，进度条只能从头播到尾、拖一下就重来。
    """
    cfg = _settings(request)
    user = _user(request)

    try:
        path = music.song_path(cfg, user, id)
    except music.MusicError as exc:
        raise _music_error(exc)

    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="歌曲不存在或已被移除")

    return file_response(request, path, filename=id, inline=True)


# ---------------------------------------------------------------------------
# 歌词
# ---------------------------------------------------------------------------

@router.get("/lyrics")
async def get_lyrics(request: Request, id: str = "") -> Dict[str, Any]:
    """
    读歌词：自动识别歌曲旁边的同名 .lrc（大小写不敏感）。

    「没有歌词」是正常状态（不是 404）：前端据此显示「暂无歌词，可上传」。
    """
    cfg = _settings(request)
    user = _user(request)

    try:
        data = music.read_lyrics(cfg, user, id)
    except music.MusicError as exc:
        raise _music_error(exc)

    return {"ok": True, "id": id, **data}


@router.post("/lyrics")
async def put_lyrics(request: Request, payload: LyricsPayload) -> Dict[str, Any]:
    """
    保存歌词（上传 .lrc 文件，或直接粘贴文本）。

    ★ 落成歌曲旁边的 `<同名>.lrc`，而不是塞进某个数据库：这样歌词在文件管理器里
    看得见、能备份、能直接用记事本改，换到任何别的播放器也照样能用。
    """
    cfg = _settings(request)
    user = _user(request)

    try:
        music.save_lyrics(cfg, user, payload.id, payload.text)
    except music.MusicError as exc:
        raise _music_error(exc)

    return {"ok": True, "message": "歌词已保存"}


# ---------------------------------------------------------------------------
# 歌单
# ---------------------------------------------------------------------------

@router.post("/playlists")
async def create_playlist(request: Request, payload: PlaylistPayload) -> Dict[str, Any]:
    cfg = _settings(request)
    user = _user(request)
    try:
        item = music.create_playlist(cfg, user, payload.name)
    except music.MusicError as exc:
        raise _music_error(exc)
    return {"ok": True, "playlist": item, "message": "已建歌单「%s」" % item["name"]}


@router.post("/playlists/rename")
async def rename_playlist(request: Request, payload: PlaylistRenamePayload) -> Dict[str, Any]:
    cfg = _settings(request)
    user = _user(request)
    try:
        item = music.rename_playlist(cfg, user, payload.id, payload.name)
    except music.MusicError as exc:
        raise _music_error(exc)
    return {"ok": True, "playlist": item, "message": "已重命名为「%s」" % item["name"]}


@router.post("/playlists/delete")
async def delete_playlist(request: Request, payload: SongPayload) -> Dict[str, Any]:
    cfg = _settings(request)
    user = _user(request)
    try:
        removed = music.delete_playlist(cfg, user, payload.id)
    except music.MusicError as exc:
        raise _music_error(exc)
    if not removed:
        raise HTTPException(status_code=404, detail="歌单不存在")
    return {"ok": True, "message": "歌单已删除"}


@router.post("/playlists/songs")
async def edit_playlist_songs(request: Request, payload: PlaylistSongPayload) -> Dict[str, Any]:
    """往歌单里加歌 / 把歌移出歌单（不动库里的文件）。"""
    cfg = _settings(request)
    user = _user(request)

    action = str(payload.action or "add").lower()
    try:
        if action == "remove":
            item = music.remove_from_playlist(cfg, user, payload.id, payload.song)
            message = "已从歌单移除"
        else:
            item = music.add_to_playlist(cfg, user, payload.id, payload.song)
            message = "已加入歌单"
    except music.MusicError as exc:
        raise _music_error(exc)

    return {"ok": True, "playlist": item, "message": message}


# ---------------------------------------------------------------------------
# 播放偏好
# ---------------------------------------------------------------------------

@router.post("/prefs")
async def save_prefs(request: Request, payload: PrefsPayload) -> Dict[str, Any]:
    """
    保存播放偏好（音量 / 播放模式 / 上次播到哪）。

    按用户分开存：同学之间互不干扰 —— 我把音量调到 20%，不该影响别人的耳朵。
    """
    cfg = _settings(request)
    user = _user(request)

    patch = {key: value for key, value in payload.model_dump().items()
             if value is not None}

    try:
        prefs = music.save_prefs(cfg, user, patch)
    except music.MusicError as exc:
        raise _music_error(exc)

    return {"ok": True, "prefs": prefs}

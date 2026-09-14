# -*- coding: utf-8 -*-
"""
音乐库（服务端）
================

虚拟桌面内置音乐播放器的「后端一半」：管库目录、导入、歌词、歌单与偏好。
HTTP 层在 routers/music.py，这里只做文件与数据的事（与 fsops/fs、users/routers.users
的分工一致）。

库目录
------
    <music.library_dir>/<用户名>/      （music.per_user = true，默认）
    <music.library_dir>/               （music.per_user = false，全机共用一个库）

默认**每人一个子目录**，与「每个子用户只看到自己的文件夹」保持一致：
学生导入的歌不会出现在同学的播放器里。想搞一个公共曲库就把 per_user 改成 false
（那时所有人看到同一批歌；歌单与播放偏好**仍然是各人各一份**）。

★ 导入走的是**当前用户的路径解析器**（在路由层完成），所以子用户只能导入
  他能看到的文件 —— 与文件管理器是同一套可见性规则，不存在「播放器能读到
  文件管理器看不到的东西」这种旁路。

歌曲标识
--------
就是**文件名**（库目录是平的，不建子目录）。因此凡是接受 song_id 的地方都要
先过 _safe_song_id()：只允许「裸文件名 + 受支持的音频扩展名」，
绝不允许 `..`、路径分隔符或指向库外的绝对路径 —— 删除与写歌词都靠它兜底。

歌词
----
* **识别**：自动找与歌曲同名的 `.lrc`（大小写不敏感），放在歌曲旁边；
* **上传**：把 LRC 文本写成那个 `.lrc` 文件。

  ★ 之所以落成 sidecar 文件而不是塞进数据库：这样歌词在文件管理器里**看得见、
  能备份、能直接改**，而且换播放器（甚至换到本机任何播放器）都还能用。
  代价是共享曲库时歌词也是共享的 —— 歌词本来就不算私密数据，可以接受。

只接受浏览器**真的能放**的格式
------------------------------
wma / ape / midi / aiff 这些浏览器解不了，导进来只会得到一个「点了没反应」的条目。
所以导入时就明确拒绝并说清楚原因，而不是先收下再让用户困惑。
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import fsops, peruser
from .security import PathSecurityError, sanitize_filename

# 浏览器能直接播放的音频格式。
# 这份清单是**按浏览器能力**定的，不是按「是不是音频文件」定的：
# 收下放不出来的格式，用户只会得到一个点了没反应的条目。
PLAYABLE_EXTENSIONS = (
    ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".webm",
)

LYRICS_EXTENSION = ".lrc"

# LRC 时间标签：[mm:ss.xx] / [mm:ss] / [mm:ss:xx]（有的老歌词用冒号分隔毫秒）
_LRC_TIME_RE = re.compile(r"\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\]")

# 文件名长度上限（含扩展名）。Windows 路径整体有 260 字符限制，
# 库目录本身已经占掉一段，这里再留出余量。
_MAX_NAME_LEN = 120

# 歌词文本上限。LRC 再长也不会超过几十 KB，超过基本就是传错东西了。
MAX_LYRICS_BYTES = 256 * 1024

_LOCK = threading.RLock()


class MusicError(Exception):
    """音乐库相关的可读错误（路由层翻译成 4xx）。"""


# ---------------------------------------------------------------------------
# 目录与文件名
# ---------------------------------------------------------------------------

def library_dir(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> str:
    """
    当前用户该用哪个库目录（只算路径，不创建）。

    ★ 每人一个子目录时要过 peruser.safe_username：用户名会被拼进路径，
      而库目录是会被**写**的（导入的音频就落在里面）。
    """
    settings = cfg.get("music") or {}
    base = str(settings.get("library_dir") or "").strip()
    if not base:
        raise MusicError("音乐库目录没有配置（config.json 的 music.library_dir）")

    if settings.get("per_user", True):
        name = peruser.safe_username((user or {}).get("username"))
        if name:
            return os.path.join(base, name)
    return base


def ensure_library(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> str:
    """拿到库目录并确保它存在。"""
    path = library_dir(cfg, user)
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise MusicError("无法创建音乐库目录：%s" % exc)
    return path


def _safe_song_id(song_id: Any) -> str:
    """
    校验歌曲标识（就是库里的文件名）。

    ★ 这是本模块的安全闸门：删除歌曲、写歌词都拿它去拼路径，
      所以必须挡掉 `..`、路径分隔符、以及指向库外的绝对路径。
      只允许「裸文件名 + 受支持的音频扩展名」。
    """
    name = str(song_id or "").strip()
    if not name or name != os.path.basename(name):
        raise MusicError("歌曲标识不合法")
    if name in (".", "..") or "/" in name or "\\" in name:
        raise MusicError("歌曲标识不合法")
    if os.path.splitext(name)[1].lower() not in PLAYABLE_EXTENSIONS:
        raise MusicError("不是受支持的音频文件：%s" % name)
    return name


def song_path(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], song_id: Any) -> str:
    """歌曲的绝对路径（标识已校验）。"""
    return os.path.join(ensure_library(cfg, user), _safe_song_id(song_id))


def lyrics_path(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], song_id: Any) -> str:
    """同名 .lrc 的绝对路径（歌词与歌曲一一对应）。"""
    base = os.path.splitext(_safe_song_id(song_id))[0]
    return os.path.join(ensure_library(cfg, user), base + LYRICS_EXTENSION)


def parse_title(filename: str) -> Tuple[str, str]:
    """
    从文件名猜「歌名 / 歌手」。

    采用的约定是下载站最常见的 `歌手 - 歌名`（也认全角/长破折号）。
    猜错也只是列表里两列对调，用户看得出来、不影响播放，所以不值得为它做配置项。

    ★ 刻意**不**拿单独的 `-` 当分隔符：`My-Song.mp3` 这类名字会被切得莫名其妙。
    """
    stem = os.path.splitext(os.path.basename(str(filename or "")))[0].strip()
    for sep in (" - ", " – ", " — ", " − "):
        if sep in stem:
            left, right = stem.split(sep, 1)
            left, right = left.strip(), right.strip()
            if left and right:
                # 左边是歌手、右边是歌名（下载站约定）
                return right, left
    return stem or str(filename or ""), ""


def _find_lyrics_file(directory: str, song_id: str) -> str:
    """
    找与歌曲同名的 .lrc（大小写不敏感）。

    为什么要大小写不敏感：从各种渠道拿到的歌词有 `.lrc`、`.LRC`，甚至 `.Lrc`，
    在 Windows 上它们其实是同一个文件，但字面比较会漏掉。
    """
    base = os.path.splitext(song_id)[0]
    wanted = (base + LYRICS_EXTENSION).lower()
    try:
        for entry in os.listdir(directory):
            if entry.lower() == wanted and os.path.isfile(os.path.join(directory, entry)):
                return os.path.join(directory, entry)
    except OSError:
        pass
    return ""


def list_songs(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """库里的全部歌曲（按文件名排序，界面自己再排）。"""
    directory = ensure_library(cfg, user)
    songs: List[Dict[str, Any]] = []

    try:
        entries = os.listdir(directory)
    except OSError:
        return songs

    for entry in entries:
        if os.path.splitext(entry)[1].lower() not in PLAYABLE_EXTENSIONS:
            continue
        path = os.path.join(directory, entry)
        if not os.path.isfile(path):
            continue

        try:
            size = os.path.getsize(path)
            # Windows 上是**创建时间**，也就是「导入进来的时刻」，
            # 用来做「最近添加」排序；用 mtime 的话（copy2 会保留源文件的
            # 修改时间）排出来的是源文件的岁数，不是用户期望的顺序。
            added = os.path.getctime(path)
        except OSError:
            size, added = 0, 0.0

        title, artist = parse_title(entry)
        songs.append({
            "id": entry,
            "name": entry,
            "title": title,
            "artist": artist,
            "ext": os.path.splitext(entry)[1].lower(),
            "size": size,
            "added": added,
            "has_lyrics": bool(_find_lyrics_file(directory, entry)),
        })

    songs.sort(key=lambda item: item["id"].lower())
    return songs


# ---------------------------------------------------------------------------
# 导入 / 上传 / 删除
# ---------------------------------------------------------------------------

def prepare_target(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                   filename: str, declared_size: int = 0) -> Tuple[str, str]:
    """
    算出一个可以安全写入的库内目标路径（返回 (最终文件名, 绝对路径)）。

    ★ 导入与上传都必须走这里，好让两条路径的规则**完全一致**：
      * 清洗文件名（非法字符、Windows 保留设备名由 sanitize_filename 负责）；
      * 只允许浏览器放得出来的音频扩展名；
      * 重名自动改名（与文件管理器同一条原则：**绝不覆盖**已有的歌）；
      * 写入前检查剩余空间 —— 把盘写满的代价远大于提前拒绝。
    """
    directory = ensure_library(cfg, user)

    name = sanitize_filename(str(filename or "").strip())
    if not name:
        raise MusicError("文件名不合法")

    extension = os.path.splitext(name)[1].lower()
    if extension not in PLAYABLE_EXTENSIONS:
        raise MusicError(
            "不支持这种格式：%s。播放器只能播放浏览器能解码的音频（%s）"
            % (extension or "（无扩展名）",
               "、".join(sorted(e.lstrip(".") for e in PLAYABLE_EXTENSIONS))))

    if len(name) > _MAX_NAME_LEN:
        stem, ext = os.path.splitext(name)
        name = stem[:_MAX_NAME_LEN - len(ext)] + ext

    try:
        _, target = fsops.resolve_upload_target(
            directory, name, cfg.get("upload", {}).get("blocked_extensions") or [])
    except PathSecurityError as exc:
        raise MusicError(str(exc))

    if declared_size:
        try:
            fsops.check_disk_space(directory, declared_size)
        except OSError as exc:
            raise MusicError(str(exc))

    return os.path.basename(target), target


def import_file(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                source_path: str, source_name: str,
                max_bytes: int = 0) -> Dict[str, Any]:
    """
    把**一个已经在本机**的文件复制进音乐库（导入）。

    调用方负责用当前用户的解析器校验过 source_path（可见性、穿越），
    这里只管「是不是能放的音频」+「复制进去、绝不覆盖」。

    :param max_bytes: 超过就拒绝（0 = 不限制）。默认给的是上传的限额 ——
                      导入同样是往库里写字节，没有理由比上传宽松。
    """
    directory = ensure_library(cfg, user)

    if not os.path.isfile(source_path):
        raise MusicError("源文件不存在：%s" % source_name)

    try:
        size = os.path.getsize(source_path)
    except OSError:
        size = 0

    if size == 0:
        raise MusicError("文件是空的：%s" % source_name)
    if max_bytes and size > max_bytes:
        raise MusicError("文件太大（%s），超过音乐库的单曲上限"
                         % fsops.human_size(size))

    final_name, target = prepare_target(
        cfg, user, source_name or os.path.basename(source_path), declared_size=size)

    temp_path = target + ".part"
    try:
        shutil.copyfile(source_path, temp_path)
        os.replace(temp_path, target)
    except OSError as exc:
        _safe_unlink(temp_path)
        raise MusicError("导入失败：%s" % exc)

    # 源文件旁边若有同名 .lrc，一并带过来 —— 这是「识别歌词」最自然的时机：
    # 用户把歌和歌词一起导入，不该还要再手动上传一次。
    copied_lyrics = _copy_sidecar_lyrics(directory, source_path, final_name)

    title, artist = parse_title(final_name)
    return {
        "id": final_name,
        "name": final_name,
        "title": title,
        "artist": artist,
        "size": size,
        "renamed": final_name != os.path.basename(str(source_name or "")),
        "lyrics": copied_lyrics,
    }


def _copy_sidecar_lyrics(directory: str, source_path: str, song_id: str) -> bool:
    """导入歌曲时顺手把源目录里的同名 .lrc 也复制过来（有就复制，没有就算）。"""
    source_dir = os.path.dirname(source_path)
    found = _find_lyrics_file(source_dir, os.path.basename(source_path))
    if not found:
        return False
    try:
        with open(found, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read(MAX_LYRICS_BYTES + 1)
    except OSError:
        return False
    if not text.strip():
        return False
    try:
        _write_lyrics_file(directory, song_id, text)
        return True
    except MusicError:
        return False


def delete_song(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], song_id: Any) -> bool:
    """
    从库里删掉一首歌（连同它的 .lrc）。

    ★ 只删库目录里的文件 —— song_id 已经过 _safe_song_id 校验（裸文件名），
      所以不存在「删到库外面去」的可能。歌词是附属物，一起删掉更符合预期
      （否则下次导入同名歌会莫名带上旧歌词）。
    """
    directory = ensure_library(cfg, user)
    name = _safe_song_id(song_id)
    path = os.path.join(directory, name)

    if not os.path.isfile(path):
        return False

    try:
        os.unlink(path)
    except OSError as exc:
        raise MusicError("删除失败：%s" % exc)

    lyrics = _find_lyrics_file(directory, name)
    if lyrics:
        _safe_unlink(lyrics)
    return True


# ---------------------------------------------------------------------------
# 歌词
# ---------------------------------------------------------------------------

def _write_lyrics_file(directory: str, song_id: str, text: str) -> str:
    """原子写 .lrc（临时文件 + replace，断电不会留下半个文件）。"""
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_LYRICS_BYTES:
        raise MusicError("歌词太大了（上限 %d KB）" % (MAX_LYRICS_BYTES // 1024))

    target = os.path.join(directory, os.path.splitext(song_id)[0] + LYRICS_EXTENSION)
    fd, temp_path = tempfile.mkstemp(prefix=".lrc-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(encoded)
        os.replace(temp_path, target)
    except Exception:
        _safe_unlink(temp_path)
        raise
    return target


def save_lyrics(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                song_id: Any, text: str) -> None:
    """
    保存歌词（上传 / 粘贴）。

    文本里常见的 BOM 会被去掉：浏览器读本地 .lrc 文件时经常带上它，
    留着会在第一行时间标签前面多出一个看不见的字符，导致那一行匹配不上。
    """
    directory = ensure_library(cfg, user)
    name = _safe_song_id(song_id)

    if not os.path.isfile(os.path.join(directory, name)):
        raise MusicError("歌曲不在库里：%s" % name)

    clean = str(text or "").lstrip("\ufeff")
    if not clean.strip():
        raise MusicError("歌词内容是空的")

    try:
        _write_lyrics_file(directory, name, clean)
    except MusicError:
        raise
    except OSError as exc:
        raise MusicError("歌词写入失败（库目录可能不可写）：%s" % exc)


def read_lyrics(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                song_id: Any) -> Dict[str, Any]:
    """读歌词：返回 {found, text, has_timestamps, source}。"""
    directory = ensure_library(cfg, user)
    name = _safe_song_id(song_id)
    path = _find_lyrics_file(directory, name)
    if not path:
        return {"found": False, "text": "", "has_timestamps": False, "source": ""}

    try:
        with open(path, "r", encoding="utf-8-sig", errors="replace") as fh:
            text = fh.read(MAX_LYRICS_BYTES + 1)
    except OSError as exc:
        raise MusicError("歌词读取失败：%s" % exc)

    return {
        "found": True,
        "text": text,
        # 有时间标签才是「逐句滚动」的那种歌词；纯文本歌词只做静态展示
        "has_timestamps": bool(_LRC_TIME_RE.search(text)),
        "source": os.path.basename(path),
    }


# ---------------------------------------------------------------------------
# 歌单与播放偏好（每人一份）
# ---------------------------------------------------------------------------

def state_path(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> str:
    """
    歌单/偏好的落盘文件。

    和界面状态、桌面快捷方式一样**按用户分文件**（管理员沿用不带后缀的那份），
    复用 peruser 里那套命名规则 —— 歌单是「我的」东西，不该被同学看到或覆盖。
    """
    base = str((cfg.get("music") or {}).get("state_path") or "").strip()
    if not base:
        return ""
    return peruser.state_path(base, user)


def _read_state(path: str) -> Dict[str, Any]:
    """容错读取：文件坏了就退回空结构，绝不让播放器打不开。"""
    if not path or not os.path.isfile(path):
        return {"version": 1, "playlists": [], "prefs": {}}
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return {"version": 1, "playlists": [], "prefs": {}}

    if not isinstance(data, dict):
        return {"version": 1, "playlists": [], "prefs": {}}
    if not isinstance(data.get("playlists"), list):
        data["playlists"] = []
    if not isinstance(data.get("prefs"), dict):
        data["prefs"] = {}
    data.setdefault("version", 1)
    return data


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".musicstate-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(temp_path, path)
    except Exception:
        _safe_unlink(temp_path)
        raise


def _normalize_playlist(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:60]
    if not name:
        return None
    songs = []
    for item in (raw.get("songs") or []):
        try:
            songs.append(_safe_song_id(item))
        except MusicError:
            continue          # 已经不在库里的条目直接丢掉
    return {
        "id": str(raw.get("id") or ("pl_" + secrets.token_hex(6))),
        "name": name,
        "songs": songs,
        "created": float(raw.get("created") or time.time()),
    }


def load_state(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """读歌单与偏好（顺带清洗一遍，坏条目不会传出去）。"""
    path = state_path(cfg, user)
    with _LOCK:
        data = _read_state(path)
    playlists = [pl for pl in (_normalize_playlist(x) for x in data["playlists"]) if pl]
    return {
        "playlists": playlists,
        "prefs": _normalize_prefs(data["prefs"]),
    }


def _normalize_prefs(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    try:
        volume = float(data.get("volume", 0.8))
    except (TypeError, ValueError):
        volume = 0.8

    mode = str(data.get("mode") or "list")
    if mode not in ("list", "single", "order", "shuffle"):
        mode = "list"

    return {
        "volume": min(1.0, max(0.0, volume)),
        "mode": mode,
        "muted": bool(data.get("muted", False)),
        "last": str(data.get("last") or ""),
        "source": str(data.get("source") or "all"),
    }


def save_prefs(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
               patch: Dict[str, Any]) -> Dict[str, Any]:
    """保存播放偏好（音量、播放模式、上次播到哪）。"""
    path = state_path(cfg, user)
    if not path:
        raise MusicError("没有配置歌单存储位置（config.json 的 music.state_path）")

    with _LOCK:
        data = _read_state(path)
        merged = dict(data.get("prefs") or {})
        for key, value in (patch or {}).items():
            if key in ("volume", "mode", "muted", "last", "source"):
                merged[key] = value
        data["prefs"] = _normalize_prefs(merged)
        _atomic_write(path, data)
        return data["prefs"]


def _mutate_playlists(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], mutator):
    path = state_path(cfg, user)
    if not path:
        raise MusicError("没有配置歌单存储位置（config.json 的 music.state_path）")

    with _LOCK:
        data = _read_state(path)
        playlists = [pl for pl in (_normalize_playlist(x) for x in data["playlists"]) if pl]
        result = mutator(playlists)
        data["playlists"] = playlists
        _atomic_write(path, data)
        return result


def create_playlist(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], name: str) -> Dict[str, Any]:
    clean = str(name or "").strip()[:60]
    if not clean:
        raise MusicError("歌单名不能为空")

    def add(playlists):
        if any(pl["name"] == clean for pl in playlists):
            raise MusicError("已经有同名歌单了：%s" % clean)
        item = {"id": "pl_" + secrets.token_hex(6), "name": clean,
                "songs": [], "created": time.time()}
        playlists.append(item)
        return item

    return _mutate_playlists(cfg, user, add)


def rename_playlist(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                    playlist_id: str, name: str) -> Dict[str, Any]:
    clean = str(name or "").strip()[:60]
    if not clean:
        raise MusicError("歌单名不能为空")
    target = str(playlist_id or "").strip()

    def rename(playlists):
        for pl in playlists:
            if pl["id"] == target:
                pl["name"] = clean
                return pl
        raise MusicError("歌单不存在")

    return _mutate_playlists(cfg, user, rename)


def delete_playlist(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                    playlist_id: str) -> bool:
    target = str(playlist_id or "").strip()

    def drop(playlists):
        before = len(playlists)
        playlists[:] = [pl for pl in playlists if pl["id"] != target]
        return len(playlists) != before

    return _mutate_playlists(cfg, user, drop)


def add_to_playlist(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                    playlist_id: str, song_id: Any) -> Dict[str, Any]:
    """把歌加进歌单（同一首歌不会重复加）。"""
    name = _safe_song_id(song_id)

    def add(playlists):
        for pl in playlists:
            if pl["id"] == str(playlist_id or "").strip():
                if name not in pl["songs"]:
                    pl["songs"].append(name)
                return pl
        raise MusicError("歌单不存在")

    return _mutate_playlists(cfg, user, add)


def remove_from_playlist(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                         playlist_id: str, song_id: Any) -> Dict[str, Any]:
    name = _safe_song_id(song_id)

    def drop(playlists):
        for pl in playlists:
            if pl["id"] == str(playlist_id or "").strip():
                pl["songs"] = [s for s in pl["songs"] if s != name]
                return pl
        raise MusicError("歌单不存在")

    return _mutate_playlists(cfg, user, drop)


# ---------------------------------------------------------------------------

def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


# 公开别名：路由层清理「写了一半的 .part」时也要用它
safe_unlink = _safe_unlink


def library_summary(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """库的整体信息（给界面显示「库在哪、有几首、多大」）。"""
    directory = ensure_library(cfg, user)
    songs = list_songs(cfg, user)
    total = sum(int(item.get("size") or 0) for item in songs)
    return {
        "dir": directory,
        "per_user": bool((cfg.get("music") or {}).get("per_user", True)),
        "count": len(songs),
        "total_bytes": total,
        "total_text": fsops.human_size(total),
        "extensions": sorted(e.lstrip(".") for e in PLAYABLE_EXTENSIONS),
    }

# -*- coding: utf-8 -*-
"""
照片（时间轴相册）
==================

这个模块给虚拟桌面提供「照片」应用的后端：**就地索引**用户可见目录里的图片，
按拍摄时间在时间轴上归类，并允许用户改写时间 / 地点 / 标签。

为什么是「就地索引」而不是像音乐那样复制进库
--------------------------------------------
音乐播放器的做法是**复制**一份进 `music/<用户>/`，因为一首歌几 MB，复制代价
很小，而且库能独立于源目录存在（学生删掉一个学期的文件，不该顺手清空歌单）。

照片不能照抄这个设计：一个照片库动辄几十上百 GB，复制一份在时间和空间上都
不可接受。所以这里只记录「文件在哪 + 它的元数据是什么」。

代价必须正面处理，一共有三条：

1. **文件会被移走 / 改名 / 删除。** 每条索引都存一个「快速指纹」
   （文件**前 256KB** + 大小做 sha1）。指纹只读头部，几毫秒一张，
   而不是把整个文件读一遍。用户改完时间之后把照片挪个目录，
   编辑记录依然跟着走 —— 因为**编辑是按指纹存的**，不是按路径。
2. **文件会变。** 索引里存了 size/mtime 当凭据，读取时比对；
   不一致就标成 stale 让用户重扫，绝不用着旧元数据装作没事
   （和文本编辑器保存前比对 mtime/size 是同一个思路）。
3. **设备可能不在线。** 移动硬盘没插时，那批照片会标成「找不到」，
   但**用户的编辑记录一个都不会丢** —— 找不到文件从来不是删除数据的理由。

★ 两张表，职责严格分开
----------------------
* **覆盖层** `photos_state.json` —— 用户改的时间 / 地点 / 标签 / 相册 / 撤销日志。
  这是**用户数据**，是唯一的真相。
* **索引层** `photo_index.json` —— 指纹 / EXIF / 尺寸。这是**派生缓存**。

分开之后「重建索引」永远安全。合成一张表的话，一次扫描失败就可能顺手把用户
辛苦改了半天的 500 个时间戳冲掉 —— 那是不可挽回的数据损失，不能靠小心避免，
要靠结构避免。

★ 安全：绝不存绝对路径
----------------------
索引里只保存 `根标识 + 相对路径`。每次读取都要重新过当前用户的路径解析器
（`deps.resolver_of`），于是：

* 子用户索引不到、也读不到没分配给它的目录（与文件管理器同一套可见性）；
* 配置文件被手工改坏（比如把 relpath 写成 `../../`）也读不出根目录外的文件 ——
  如果索引里存的是绝对路径，**编辑一个 JSON 就等于任意文件读取**。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
import warnings
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

from PIL import Image

from . import fsops
from . import peruser
from . import thumbs
from .fsops import is_link_or_junction
from .security import (PathSecurityError, is_blocked_extension, is_within,
                       sanitize_filename)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

# 认哪些扩展名。★ 直接复用缩略图模块那一份，而不是在这里再抄一遍：
# 两处各写一份的话，将来 thymbs 支持了新格式（比如装上 pillow-heif 之后的
# .heic），画廊会莫名其妙地漏掉它，而且这种不一致没有任何症状。
# 只去掉 .ico —— 那是图标，不是照片。
PHOTO_EXTENSIONS = tuple(sorted(thumbs.IMAGE_EXTENSIONS - {".ico"}))

# 指纹只读文件头部这么多字节。256KB 足够区分海量照片，
# 又不会让「索引 3000 张」变成一次几百 GB 的读取。
FINGERPRINT_BYTES = 256 * 1024

# 名字长度上限（展示用，不参与落盘路径拼接）
MAX_NAME_LEN = 200

# 一批最多处理多少张（防止一次误选把服务占住太久）
MAX_BATCH = 20000

# 撤销日志保留多少条
JOURNAL_LIMIT = 50

# 相册名长度上限
MAX_ALBUM_NAME = 60

# ★ 「上传进来的照片」用的伪根标识。
#
# 为什么需要它：照片是**就地索引**的（索引里存「根标识 + 相对路径」，读取时
# 再过一遍用户解析器），而从浏览器所在的电脑上传进来的文件必须先落到服务器
# 的某个目录里 —— 那个目录未必在用户的可见根范围内（子用户只被分配了几个
# 特定目录）。用一个伪根把它跟「用户自己的目录」区分开，解析时改走
# `photos.upload_dir/<用户名>/`，于是：
#   * 子用户只能看到**自己**上传的那一份（每人一个子目录）；
#   * 一个被改坏的索引也没法借它读到上传目录之外（见 upload_file_path）。
#
# 这个标识以 `__` 开头，而且带下划线 —— users.py 的用户名规则是
# `[A-Za-z0-9_.-]`，驱动器的根标识（drives.drive_id_for）也不会长这样，
# 所以它不可能跟真实的根标识撞上。
UPLOAD_ROOT = "__uploads__"

# 从浏览器上传单个照片的大小上限（MB）
DEFAULT_MAX_UPLOAD_MB = 200

# 一次最多上传多少张（前端逐张上传，这里是服务端的兜底口径）
MAX_UPLOAD_BATCH = 2000

_LOCK = threading.RLock()

# EXIF 标签号。写数字而不是依赖 ExifTags 的键名，是因为不同 Pillow 版本
# 对个别标签的命名有过变化，而这些号是 EXIF 标准里定死的。
_TAG_DATETIME = 306
_TAG_DATETIME_ORIGINAL = 36867
_TAG_DATETIME_DIGITIZED = 36868
_TAG_OFFSET_ORIGINAL = 36881
_TAG_MAKE = 271
_TAG_MODEL = 272
_TAG_LENS_MODEL = 42036
_TAG_ISO = 34855
_TAG_FNUMBER = 33437
_TAG_EXPOSURE = 33434
_TAG_ORIENTATION = 274
_GPS_IFD = 34853

# GPS 子标签
_GPS_LAT_REF = 1
_GPS_LAT = 2
_GPS_LON_REF = 3
_GPS_LON = 4
_GPS_ALT_REF = 5
_GPS_ALT = 6

_EXIF_DT_RE = re.compile(
    r"^(\d{4})[:\-/](\d{1,2})[:\-/](\d{1,2})[ T](\d{1,2}):(\d{1,2}):(\d{1,2})")

# 用户输入的时间：允许 "2023-08-15 12:34:56" / "2023-08-15T12:34" / "2023-08-15"
_USER_DT_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?$")

# 指纹的合法形状（索引键，也是接口入参 —— 必须挡住乱七八糟的字符串）
_ID_RE = re.compile(r"^[0-9a-f]{16,64}$")


class PhotoError(Exception):
    """照片库相关的可读错误（路由层翻译成 4xx，而不是 500）。"""


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def is_photo(filename: str) -> bool:
    """按扩展名判断是不是本模块认的照片。"""
    return os.path.splitext(filename or "")[1].lower() in PHOTO_EXTENSIONS


def norm_id(photo_id: Any) -> str:
    """
    校验照片标识（指纹）。

    ★ 这是本模块的**安全闸门之一**：id 会作为字典键去索引里查表、也会进相册的
      列表，形状不对就直接拒绝，而不是拿去做任何路径拼接。
    """
    text = str(photo_id or "").strip().lower()
    if not _ID_RE.match(text):
        raise PhotoError("照片标识不合法")
    return text


def mtime_iso(mtime: float) -> str:
    """文件时间 -> 本地时间的 ISO 串（不带时区，见 format_time 的说明）。"""
    try:
        return datetime.fromtimestamp(float(mtime)).strftime("%Y-%m-%dT%H:%M:%S")
    except (OverflowError, OSError, ValueError, TypeError):
        return ""


def format_time(year: int, month: int, day: int,
                hour: int = 0, minute: int = 0, second: int = 0) -> str:
    """
    拼成 `YYYY-MM-DDTHH:MM:SS`。

    ★ 刻意**不带时区**：EXIF 的 DateTimeOriginal 本身就是「相机当时的墙上时间」，
      没有时区信息，硬按 UTC 解释会让所有照片整体偏移。这里统一按
      「本地墙上时间」处理，用户看到什么就是什么。
      个别机型带 OffsetTimeOriginal（tag 36881），单独存下来给界面显示。
    """
    return "%04d-%02d-%02dT%02d:%02d:%02d" % (year, month, day, hour, minute, second)


def _exif_datetime(value: Any) -> str:
    """
    EXIF 时间字符串 -> ISO。

    EXIF 用的是 `2023:08:15 12:34:56`（冒号分隔日期）这种古怪写法。
    没有 EXIF 的机型会写 `0000:00:00 00:00:00`，必须当作「没有」而不是
    当成公元 0 年 —— 否则时间轴上会冒出一堆排在最前面的幽灵照片。
    """
    text = str(value or "").strip()
    if not text:
        return ""
    match = _EXIF_DT_RE.match(text)
    if not match:
        return ""
    try:
        year, month, day, hour, minute, second = (int(x) for x in match.groups())
    except ValueError:
        return ""
    if year < 1900 or not (1 <= month <= 12):
        return ""
    if not (1 <= day <= 31):
        return ""
    # 相机时区/夏令时写疯的时候会出现 24:00:00 这种值；
    # 夹到合法区间比丢掉整条时间更有用（时间轴还在，只是那一秒不准）。
    hour = min(23, max(0, hour))
    minute = min(59, max(0, minute))
    second = min(59, max(0, second))
    return format_time(year, month, day, hour, minute, second)


def parse_user_time(text: Any) -> str:
    """
    解析用户在界面上填的时间。

    接受 `2023-08-15 12:34:56` / `2023-08-15T12:34` / `2023-08-15`
    （只给日期时按当天 00:00:00 处理）。空串 = 清空覆盖值。
    """
    raw = str(text or "").strip()
    if not raw:
        return ""
    match = _USER_DT_RE.match(raw)
    if not match:
        raise PhotoError("时间格式不对，请用 2023-08-15 12:34:56 这样的写法")
    year, month, day, hour, minute, second = match.groups()
    year, month, day = int(year), int(month), int(day)
    hour = int(hour or 0)
    minute = int(minute or 0)
    second = int(second or 0)
    if not (1 <= month <= 12) or not (1 <= day <= 31):
        raise PhotoError("日期不合法")
    if hour > 23 or minute > 59 or second > 59:
        raise PhotoError("时间不合法")
    return format_time(year, month, day, hour, minute, second)


def _gps_get(gps: Any, tag_id: int, name: str) -> Any:
    """
    从 GPS 字典里取一个值。

    ★ 不同 Pillow 版本给的键不一样：有的用整数标签号，有的用
      `GPSTAGS` 里的名字。两种都试，取不到就当没有 —— 相册绝不该
      因为读不出 GPS 就打不开。
    """
    if not isinstance(gps, dict):
        return None
    if tag_id in gps:
        return gps[tag_id]
    return gps.get(name)


def _gps_degrees(value: Any, ref: Any) -> Optional[float]:
    """EXIF 的「度分秒三元组」-> 十进制度数（南纬/西经取负）。"""
    if value is None:
        return None
    try:
        parts = [float(x) for x in value]
    except (TypeError, ValueError):
        return None
    if len(parts) != 3:
        return None
    degrees = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
    text = str(ref or "").strip().upper()[:1]
    if text in ("S", "W"):
        degrees = -degrees
    if not (-180.0 <= degrees <= 180.0):
        return None
    return degrees


def _read_gps(exif: Any) -> Optional[Dict[str, float]]:
    """读 GPS 子表 -> {lat, lon, alt?}；读不到返回 None。"""
    gps: Any = None
    # Pillow 8+ 的正确入口是 get_ifd；老版本只有 get 能拿到子字典
    try:
        gps = exif.get_ifd(_GPS_IFD)
    except Exception:  # noqa: BLE001 - 坏 EXIF 不该让整张图失败
        gps = None
    if not isinstance(gps, dict) or not gps:
        try:
            raw = exif.get(_GPS_IFD)
            gps = raw if isinstance(raw, dict) else None
        except Exception:  # noqa: BLE001
            gps = None
    if not isinstance(gps, dict) or not gps:
        return None

    lat = _gps_degrees(_gps_get(gps, _GPS_LAT, "GPSLatitude"),
                       _gps_get(gps, _GPS_LAT_REF, "GPSLatitudeRef"))
    lon = _gps_degrees(_gps_get(gps, _GPS_LON, "GPSLongitude"),
                       _gps_get(gps, _GPS_LON_REF, "GPSLongitudeRef"))
    if lat is None or lon is None:
        return None
    # (0,0) 在几内亚湾，实际基本都是「相机的坐标没写好」，当成没有更诚实
    if abs(lat) < 1e-6 and abs(lon) < 1e-6:
        return None

    result: Dict[str, float] = {"lat": round(lat, 6), "lon": round(lon, 6)}

    alt = _gps_degrees(_gps_get(gps, _GPS_ALT, "GPSAltitude"), None)
    if alt is not None:
        ref = str(_gps_get(gps, _GPS_ALT_REF, "GPSAltitudeRef") or "0").strip()
        if ref.startswith("1"):
            alt = -alt
        result["alt"] = round(alt, 1)
    return result


def _format_exposure(value: Any) -> str:
    """快门速度：EXIF 里是个分数（1/2000），显示成 `1/2000s`。"""
    if value is None:
        return ""
    try:
        num = float(value)
    except (TypeError, ValueError):
        return ""
    if num <= 0:
        return ""
    if num >= 1:
        return "%gs" % round(num, 1)
    return "1/%d" % int(round(1.0 / num))


# ---------------------------------------------------------------------------
# 指纹与元数据
# ---------------------------------------------------------------------------

def quick_fingerprint(abs_path: str, size: Optional[int] = None) -> str:
    """
    计算「快速指纹」= sha1(文件前 256KB + 文件大小)。

    为什么不用完整哈希：一张 5MB 的照片读全文件要几十毫秒，索引一万张就是
    十几分钟；而只读头部 256KB 大约 1 毫秒。为什么头部够用：照片的头部包含
    完整的 EXIF 与起始图像数据，两台相机拍出「前 256KB 与大小都完全相同」的
    两张不同照片，实际上不可能。

    ★ 指纹是**编辑记录的挂靠点**：文件被移动/改名之后，只要内容没变，
      指纹就不变，用户设过的时间与地点就还在。
    """
    digest = hashlib.sha1()
    try:
        with open(abs_path, "rb") as fh:
            digest.update(fh.read(FINGERPRINT_BYTES))
            if size is None:
                size = os.path.getsize(abs_path)
    except OSError as exc:
        raise PhotoError("无法读取文件：%s" % exc)
    digest.update(("|%d" % int(size or 0)).encode("ascii"))
    return digest.hexdigest()


def read_image_meta(abs_path: str) -> Dict[str, Any]:
    """
    读一张图的尺寸与 EXIF（拍摄时间 / GPS / 相机 / 快门 / 光圈 / ISO）。

    ★ 这里**从不抛异常**。坏图、被截断的文件、微信和截图那种完全没有 EXIF 的
      图，全都是常态：它们应该得到一条「时间来自文件」的记录，
      而不是让整个导入失败。
    """
    meta: Dict[str, Any] = {
        "w": 0, "h": 0,
        "exif_taken": "", "exif_tz": "",
        "gps": None,
        "camera": "", "lens": "", "iso": 0, "fnum": 0.0, "exposure": "",
    }

    try:
        with warnings.catch_warnings():
            # 与 thumbs 同样的理由：我们自己的像素上限比 Pillow 的告警阈值更严，
            # 这里只读头部，不需要为超大图刷一屏告警。
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)

            with Image.open(abs_path) as im:
                try:
                    meta["w"], meta["h"] = int(im.size[0]), int(im.size[1])
                except Exception:  # noqa: BLE001
                    pass

                exif = None
                try:
                    exif = im.getexif()
                except Exception:  # noqa: BLE001 - 没有 EXIF 是常态
                    exif = None

                if not exif:
                    return meta

                # 优先 DateTimeOriginal（真正的拍摄时刻），
                # 退到 DateTimeDigitized，最后才是 DateTime（那可能只是修改时间）
                meta["exif_taken"] = (
                    _exif_datetime(exif.get(_TAG_DATETIME_ORIGINAL))
                    or _exif_datetime(exif.get(_TAG_DATETIME_DIGITIZED))
                    or _exif_datetime(exif.get(_TAG_DATETIME))
                )

                offset = str(exif.get(_TAG_OFFSET_ORIGINAL) or "").strip()
                if offset:
                    meta["exif_tz"] = offset[:8]

                make = str(exif.get(_TAG_MAKE) or "").strip()
                model = str(exif.get(_TAG_MODEL) or "").strip()
                # 佳能的 Make 就是 "Canon"、Model 是 "Canon EOS R6"，
                # 直接拼会变成 "Canon Canon EOS R6"，所以去个重
                if model and make and not model.lower().startswith(make.lower()):
                    meta["camera"] = ("%s %s" % (make, model))[:80]
                else:
                    meta["camera"] = (model or make)[:80]

                meta["lens"] = str(exif.get(_TAG_LENS_MODEL) or "").strip()[:80]

                try:
                    meta["iso"] = int(float(exif.get(_TAG_ISO) or 0))
                except (TypeError, ValueError):
                    meta["iso"] = 0

                try:
                    meta["fnum"] = round(float(exif.get(_TAG_FNUMBER) or 0), 1)
                except (TypeError, ValueError):
                    meta["fnum"] = 0.0

                meta["exposure"] = _format_exposure(exif.get(_TAG_EXPOSURE))
                meta["gps"] = _read_gps(exif)
    except Exception:  # noqa: BLE001 - 坏图/权限/格式不支持统统归为「没有元数据」
        pass

    return meta


# ---------------------------------------------------------------------------
# 落盘位置与读写
# ---------------------------------------------------------------------------

def _base_path(cfg: Dict[str, Any], key: str) -> str:
    value = str((cfg.get("photos") or {}).get(key) or "").strip()
    return value


def state_path(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> str:
    """用户数据（编辑 / 相册 / 撤销日志）的落盘文件，**按用户分文件**。"""
    base = _base_path(cfg, "state_path")
    if not base:
        return ""
    return peruser.state_path(base, user)


def index_path(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> str:
    """
    索引缓存的落盘文件，同样按用户分文件。

    ★ 必须分：索引里是「这个人能看到的那些文件」，而两个子用户的可见目录
      完全不同。共用一份的话，A 的相册会列出 B 的文件（虽然读取时还会被
      解析器挡住，但界面已经泄露了文件名）。
    """
    base = _base_path(cfg, "index_path")
    if not base:
        return ""
    return peruser.state_path(base, user)


def thumb_size(cfg: Dict[str, Any]) -> int:
    """画廊缩略图边长（夹到合理区间，配置写错了也不能让界面崩）。"""
    try:
        value = int((cfg.get("photos") or {}).get("thumb_size") or 320)
    except (TypeError, ValueError):
        value = 320
    return min(1024, max(64, value))


def upload_dir(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> str:
    """
    「从浏览器上传的照片」该落在哪个目录（只算路径，不创建）。

    ★ 每人一个子目录时要过 peruser.safe_username：用户名会被拼进路径，
      而这个目录是会被**写**的（上传的照片就落在里面）。
      不分子目录的话，同学上传的照片会混进同一个目录，而且互相看得到文件名。
    """
    settings = cfg.get("photos") or {}
    base = str(settings.get("upload_dir") or "").strip()
    if not base:
        return ""

    if settings.get("upload_per_user", True):
        name = peruser.safe_username((user or {}).get("username"))
        if name:
            return os.path.join(base, name)
    return base


def ensure_upload_dir(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> str:
    """拿到上传目录并确保它存在。"""
    path = upload_dir(cfg, user)
    if not path:
        raise PhotoError("没有配置照片上传目录（config.json 的 photos.upload_dir）")
    try:
        os.makedirs(path, exist_ok=True)
    except OSError as exc:
        raise PhotoError("无法创建照片上传目录：%s" % exc)
    return path


def max_upload_bytes(cfg: Dict[str, Any]) -> int:
    """单张照片的上传上限（字节）。"""
    try:
        mb = int((cfg.get("photos") or {}).get("max_upload_mb") or DEFAULT_MAX_UPLOAD_MB)
    except (TypeError, ValueError):
        mb = DEFAULT_MAX_UPLOAD_MB
    return max(1, mb) * 1024 * 1024


def _safe_rel(rel: Any) -> str:
    """
    校验上传目录内的相对路径。

    ★ 与 norm_id 一样是**安全闸门**：上传目录里的文件名会参与路径拼接，
      所以必须挡掉 `..`、绝对路径与盘符。上传时写下去的名字由我们自己
      生成（sanitize_filename + unique_path），但读取时会经过这里，
      于是「索引文件被手工改坏」也读不出上传目录之外。

    ★ 开头是 `/` 或 `\\` 的也要拒掉，**不能靠 os.path.isabs**：
      它在 Windows 上对 `/etc/passwd` 返回 False（那是「相对当前盘」的写法），
      于是同一个字符串在 Linux 上被拒、在 Windows 上被放行 —— 这种
      「随平台变的安全判断」正是最不该出现在闸门里的东西。
      反正一个**相对**路径本来就不该以分隔符开头。
    """
    text = str(rel or "").replace("\\", "/").strip()
    if not text or text.startswith("/") or ":" in text:
        raise PhotoError("照片路径不合法")

    parts = [p for p in text.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." for p in parts):
        raise PhotoError("照片路径不合法")
    return "/".join(parts)


def upload_file_path(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                     rel: Any) -> str:
    """上传目录里某个文件的绝对路径（相对路径已校验，且必须落在目录内）。"""
    directory = upload_dir(cfg, user)
    if not directory:
        raise PhotoError("没有配置照片上传目录（config.json 的 photos.upload_dir）")

    target = os.path.join(directory, _safe_rel(rel))
    # 双保险：拼出来的路径必须还在**这个用户自己的**上传目录里。
    # 上面已经挡掉了 `..` 与绝对路径，这一层是防「将来放宽了 _safe_rel」
    # 或者符号链接把路径带到别处。
    if not is_within(os.path.realpath(directory), os.path.realpath(target)):
        raise PhotoError("照片路径不合法")
    return target


def max_items(cfg: Dict[str, Any]) -> int:
    try:
        value = int((cfg.get("photos") or {}).get("max_items") or 5000)
    except (TypeError, ValueError):
        value = 5000
    return max(1, min(MAX_BATCH, value))


def max_import(cfg: Dict[str, Any]) -> int:
    try:
        value = int((cfg.get("photos") or {}).get("max_import") or 20000)
    except (TypeError, ValueError):
        value = 20000
    return max(1, min(MAX_BATCH, value))


def _empty_state() -> Dict[str, Any]:
    return {
        "version": 1,
        "sources": [],
        "edits": {},
        "albums": [],
        "journal": [],
        "prefs": {},
    }


def _empty_index() -> Dict[str, Any]:
    return {"version": 1, "entries": {}}


def _read_json(path: str, fallback: Dict[str, Any]) -> Dict[str, Any]:
    """
    容错读取。

    ★ 坏文件一律退回空结构，**绝不让相册打不开**。注意这不适用于覆盖层：
      覆盖层读坏了会退成空，但我们会把它留在磁盘上不动（不覆盖写），
      所以用户还有机会手工抢救 —— 详见 _mutate_state 的说明。
    """
    if not path or not os.path.isfile(path):
        return fallback
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return fallback
    if not isinstance(data, dict):
        return fallback
    return data


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    """先写临时文件再 os.replace 原子落地，避免读到写了一半的 JSON。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, temp_path = tempfile.mkstemp(prefix=".photo-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(temp_path, path)
    except Exception:
        try:
            os.unlink(temp_path)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------

def _normalize_edit(raw: Any) -> Optional[Dict[str, Any]]:
    """把一条编辑记录洗成固定形状；完全空白的记录返回 None（不占空间）。"""
    if not isinstance(raw, dict):
        return None

    edit: Dict[str, Any] = {}

    taken = str(raw.get("taken_at") or "").strip()
    if taken:
        # 存进来的必须已经是 ISO；不合法就丢掉这一项而不是原样存下去
        try:
            edit["taken_at"] = parse_user_time(taken.replace("T", " "))
        except PhotoError:
            pass

    tz = str(raw.get("tz") or "").strip()
    if tz:
        edit["tz"] = tz[:8]

    place = raw.get("place")
    if isinstance(place, dict):
        name = str(place.get("name") or "").strip()[:120]
        entry: Dict[str, Any] = {}
        if name:
            entry["name"] = name
        for key in ("lat", "lon"):
            try:
                value = place.get(key)
                if value is not None and str(value).strip() != "":
                    entry[key] = round(float(value), 6)
            except (TypeError, ValueError):
                continue
        if entry:
            edit["place"] = entry

    tags: List[str] = []
    for item in (raw.get("tags") or []):
        text = str(item or "").strip()[:40]
        if text and text not in tags:
            tags.append(text)
        if len(tags) >= 30:
            break
    if tags:
        edit["tags"] = tags

    try:
        rating = int(raw.get("rating") or 0)
    except (TypeError, ValueError):
        rating = 0
    if rating:
        edit["rating"] = max(0, min(5, rating))

    caption = str(raw.get("caption") or "").strip()[:500]
    if caption:
        edit["caption"] = caption

    try:
        edited_at = float(raw.get("edited_at") or 0)
    except (TypeError, ValueError):
        edited_at = 0.0
    if edited_at:
        edit["edited_at"] = edited_at

    return edit or None


def _normalize_album(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    name = str(raw.get("name") or "").strip()[:MAX_ALBUM_NAME]
    if not name:
        return None

    kind = str(raw.get("kind") or "manual")
    if kind not in ("manual", "smart"):
        kind = "manual"

    album: Dict[str, Any] = {
        "id": str(raw.get("id") or ("al_" + secrets.token_hex(6))),
        "name": name,
        "kind": kind,
        "created": float(raw.get("created") or time.time()),
    }

    if kind == "smart":
        start = str(raw.get("start") or "").strip()[:19]
        end = str(raw.get("end") or "").strip()[:19]
        album["start"] = start
        album["end"] = end
    else:
        items: List[str] = []
        for item in (raw.get("items") or []):
            try:
                pid = norm_id(item)
            except PhotoError:
                continue
            if pid not in items:
                items.append(pid)
        album["items"] = items

    return album


def _normalize_source(raw: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(raw, dict):
        return None
    root = str(raw.get("root") or "").strip()
    if not root:
        return None
    return {
        "root": root,
        "path": str(raw.get("path") or "").replace("\\", "/").strip("/"),
        "added": float(raw.get("added") or time.time()),
    }


def _normalize_prefs(raw: Any) -> Dict[str, Any]:
    data = raw if isinstance(raw, dict) else {}
    level = str(data.get("level") or "day")
    if level not in ("year", "month", "day"):
        level = "day"
    sort = str(data.get("sort") or "taken_desc")
    if sort not in ("taken_desc", "taken_asc", "name_asc", "name_desc"):
        sort = "taken_desc"
    return {"level": level, "sort": sort}


def load_state(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """读覆盖层（顺带清洗一遍，坏条目不会传出去）。"""
    path = state_path(cfg, user)
    with _LOCK:
        data = _read_json(path, _empty_state())

    state = _empty_state()
    if isinstance(data.get("edits"), dict):
        for pid, raw in data["edits"].items():
            try:
                key = norm_id(pid)
            except PhotoError:
                continue
            edit = _normalize_edit(raw)
            if edit:
                state["edits"][key] = edit
    state["sources"] = [s for s in (_normalize_source(x) for x in (data.get("sources") or [])) if s]
    state["albums"] = [a for a in (_normalize_album(x) for x in (data.get("albums") or [])) if a]
    if isinstance(data.get("journal"), list):
        state["journal"] = [x for x in data["journal"] if isinstance(x, dict)][-JOURNAL_LIMIT:]
    state["prefs"] = _normalize_prefs(data.get("prefs"))
    return state


def load_index(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """读索引层。条目形状在这里统一，坏条目直接丢掉。"""
    path = index_path(cfg, user)
    with _LOCK:
        data = _read_json(path, _empty_index())

    entries: Dict[str, Dict[str, Any]] = {}
    raw_entries = data.get("entries")
    if isinstance(raw_entries, dict):
        for pid, raw in raw_entries.items():
            if not isinstance(raw, dict):
                continue
            try:
                key = norm_id(pid)
            except PhotoError:
                continue
            root = str(raw.get("root") or "").strip()
            relpath = str(raw.get("relpath") or "").replace("\\", "/").strip("/")
            if not root or not relpath:
                continue
            entries[key] = {
                "id": key,
                "root": root,
                "relpath": relpath,
                "size": int(raw.get("size") or 0),
                "mtime": float(raw.get("mtime") or 0),
                "w": int(raw.get("w") or 0),
                "h": int(raw.get("h") or 0),
                "exif_taken": str(raw.get("exif_taken") or "")[:19],
                "exif_tz": str(raw.get("exif_tz") or "")[:8],
                "gps": raw.get("gps") if isinstance(raw.get("gps"), dict) else None,
                "camera": str(raw.get("camera") or "")[:80],
                "lens": str(raw.get("lens") or "")[:80],
                "iso": int(raw.get("iso") or 0),
                "fnum": float(raw.get("fnum") or 0),
                "exposure": str(raw.get("exposure") or "")[:20],
                "added": float(raw.get("added") or 0),
            }
    return {"version": 1, "entries": entries}


def _mutate_state(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], mutator):
    """
    读-改-写覆盖层（全程持锁、原子落地）。

    ★ 这里必须在锁里重新读一遍再写：两个请求同时改不同的照片时，
      如果各自拿的是进入函数前读到的快照，后写的那个会把前一个的改动抹掉
      （表现是「改了 A 的照片，B 的改动没了」）。
    """
    path = state_path(cfg, user)
    if not path:
        raise PhotoError("没有配置照片数据的存储位置（config.json 的 photos.state_path）")

    with _LOCK:
        data = _read_json(path, _empty_state())
        if not isinstance(data, dict):
            data = _empty_state()
        # 用规范化后的结构作为写入骨架，顺带把坏条目清洗掉
        normalized = load_state(cfg, user)
        result = mutator(normalized)
        normalized["journal"] = normalized["journal"][-JOURNAL_LIMIT:]
        normalized["version"] = 1
        _atomic_write(path, normalized)
        return result


def _mutate_index(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], mutator):
    """读-改-写索引层（原子落地）。索引坏了最多重扫一次。"""
    path = index_path(cfg, user)
    if not path:
        raise PhotoError("没有配置照片索引的存储位置（config.json 的 photos.index_path）")

    with _LOCK:
        index = load_index(cfg, user)
        result = mutator(index)
        index["version"] = 1
        _atomic_write(path, index)
        return result


# ---------------------------------------------------------------------------
# 扫描
# ---------------------------------------------------------------------------

def collect_images(resolver, root_id: str, rel_paths: Iterable[str],
                   limit: int = MAX_BATCH
                   ) -> Tuple[List[Tuple[str, str, str]], List[Dict[str, str]], int]:
    """
    把用户选中的「文件或文件夹」展开成待索引的图片清单。

    返回 ([(根标识, 相对路径, 绝对路径)], [跳过原因], 忽略掉的非图片文件数)。

    ★ 走的是**当前用户的路径解析器**，所以「能索引什么」与「文件管理器能看到
      什么」完全一致 —— 相册不是绕过可见性的旁路。子用户索引不到别人的目录，
      也索引不到没分配给它的盘。

    ★ 递归时**不进入符号链接与目录联接**：Windows 上 os.path.islink() 认不出
      目录联接，跟随它会把根目录外的整棵树（甚至成环）拖进来。
      项目里打包与搜索都踩过这个坑，这里复用同一套判断。

    ★ 遍历目录时遇到的非图片文件**只计数、不逐条上报**：一个照片文件夹里
      混着几十个 .txt/.docx 是常态，逐条塞进「跳过原因」会把真正要看的问题
      （比如某个目录读不了）淹掉。但也不能不吭声 —— 那个数字会出现在
      导入结果里，用户能看出「我选了 100 个文件，为什么只进来 80 张」。
    """
    found: List[Tuple[str, str, str]] = []
    skipped: List[Dict[str, str]] = []
    ignored = 0
    seen: set = set()

    def _add(rel: str, abs_path: str) -> None:
        key = os.path.normcase(os.path.abspath(abs_path))
        if key in seen:
            return
        seen.add(key)
        found.append((root_id, rel.replace("\\", "/"), abs_path))

    for raw in (rel_paths or ()):
        rel = str(raw or "").strip()
        try:
            _root, abs_path = resolver.resolve(root_id, rel)
        except PathSecurityError as exc:
            skipped.append({"name": rel, "reason": str(exc)})
            continue

        if os.path.isdir(abs_path):
            base_rel = rel.replace("\\", "/").strip("/")
            for current, dirs, files in os.walk(abs_path, followlinks=False):
                # 显式剪枝：联接在 Windows 上不会被 followlinks=False 挡住
                dirs[:] = [d for d in dirs
                           if not is_link_or_junction(os.path.join(current, d))]
                for name in files:
                    if not is_photo(name):
                        ignored += 1
                        continue
                    if len(found) >= limit:
                        skipped.append({"name": base_rel or name,
                                        "reason": "一次最多索引 %d 张，其余的请分批导入" % limit})
                        return found, skipped, ignored
                    full = os.path.join(current, name)
                    if is_link_or_junction(full):
                        continue
                    try:
                        sub = os.path.relpath(full, abs_path).replace("\\", "/")
                    except ValueError:
                        continue
                    _add(("%s/%s" % (base_rel, sub)) if base_rel else sub, full)
        elif os.path.isfile(abs_path):
            if not is_photo(os.path.basename(abs_path)):
                skipped.append({"name": rel, "reason": "不是受支持的图片格式"})
                continue
            _add(rel.replace("\\", "/").strip("/"), abs_path)
        else:
            skipped.append({"name": rel, "reason": "不是文件也不是文件夹（或已不存在）"})

    return found, skipped, ignored


def _build_entry(root_id: str, relpath: str, abs_path: str,
                 size: Optional[int] = None, mtime: Optional[float] = None,
                 fingerprint: Optional[str] = None) -> Dict[str, Any]:
    """给一个文件造索引条目（读指纹 + EXIF）。"""
    try:
        stat = os.stat(abs_path)
    except OSError as exc:
        raise PhotoError("无法读取文件：%s" % exc)

    real_size = int(stat.st_size) if size is None else int(size)
    real_mtime = float(stat.st_mtime) if mtime is None else float(mtime)
    pid = fingerprint or quick_fingerprint(abs_path, real_size)
    meta = read_image_meta(abs_path)

    return {
        "id": pid,
        "root": root_id,
        "relpath": relpath.replace("\\", "/").strip("/"),
        "size": real_size,
        "mtime": real_mtime,
        "w": meta["w"],
        "h": meta["h"],
        "exif_taken": meta["exif_taken"],
        "exif_tz": meta["exif_tz"],
        "gps": meta["gps"],
        "camera": meta["camera"],
        "lens": meta["lens"],
        "iso": meta["iso"],
        "fnum": meta["fnum"],
        "exposure": meta["exposure"],
        "added": time.time(),
    }


def import_paths(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], resolver,
                 root_id: str, rel_paths: Iterable[str],
                 job: Any = None) -> Dict[str, Any]:
    """
    索引用户选中的文件/文件夹（**不移动、不复制任何文件**）。

    参数 job 给了就把它当作 jobs.Job 用来报进度（导入几千张会跑一会儿，
    右下角的任务面板要能看到进度条，也要能取消）。
    """
    limit = max_import(cfg)
    targets, skipped, ignored = collect_images(resolver, root_id, rel_paths, limit)
    if job is not None:
        job.set_totals(items=len(targets))

    if not targets:
        raise PhotoError(skipped[0]["reason"] if skipped else "没有找到可以索引的图片")

    imported: List[Dict[str, Any]] = []
    updated = 0
    entries_to_add: Dict[str, Dict[str, Any]] = {}

    # 先读一次现有索引，用于「同一个文件已索引过就跳过」的判断
    existing = load_index(cfg, user)["entries"]
    by_location = {(e["root"], e["relpath"]): pid for pid, e in existing.items()}

    cancelled = False
    for root, rel, abs_path in targets:
        if job is not None and job.cancel_requested():
            cancelled = True
            break
        if job is not None:
            job.advance(items=1, current=os.path.basename(abs_path))

        try:
            stat = os.stat(abs_path)
        except OSError as exc:
            skipped.append({"name": rel, "reason": "无法读取：%s" % exc})
            continue

        known = by_location.get((root, rel))
        if known and known in existing:
            old = existing[known]
            # 位置没变时，只有 size/mtime 也一致才算「还是原来那张」——
            # 否则说明文件被换过内容，必须重新指纹与读 EXIF
            if old.get("size") == int(stat.st_size) and abs(old.get("mtime", 0) - stat.st_mtime) < 1e-6:
                continue

        try:
            entry = _build_entry(root, rel, abs_path,
                                 size=int(stat.st_size), mtime=stat.st_mtime)
        except PhotoError as exc:
            skipped.append({"name": rel, "reason": str(exc)})
            continue

        if entry["id"] in existing and entry["id"] != known:
            # 同一张图已经在库里（可能从另一个目录索引过）——更新它的位置
            updated += 1
        entries_to_add[entry["id"]] = entry
        imported.append({
            "id": entry["id"],
            "name": os.path.basename(rel),
            "relpath": entry["relpath"],
            "taken_at": entry["exif_taken"],
        })

    if entries_to_add:
        def _merge(index):
            index["entries"].update(entries_to_add)
            return None
        _mutate_index(cfg, user, _merge)

    # 记住这次导入的**具体目录**，重扫时才知道该去哪些地方找被移动的文件。
    # ★ 绝不能图省事一律记成整个根：从 C: 里挑了一个文件夹导入，重扫就会去遍历
    #   整个 C: —— 那是分钟级的空转，而且会顺带把没打算纳入的照片全扫进来。
    # ★ 但也**不能把空串跳过**：`paths: [""]` 就是「导入这个根目录」，
    #   跳过它等于一条来源都没记下，重扫就再也找不回被移动的文件了
    #   （这个坑真的踩过：导入整个根之后挪文件，重扫一律报「暂时找不到」）。
    sources: List[str] = []
    for raw in (rel_paths or ()):
        rel = str(raw or "").strip().replace("\\", "/").strip("/")
        try:
            _root, abs_path = resolver.resolve(root_id, rel)
        except PathSecurityError:
            continue
        if os.path.isdir(abs_path):
            sources.append(rel)
        else:
            parent = os.path.dirname(rel).replace("\\", "/").strip("/")
            sources.append(parent)
    _remember_sources(cfg, user, root_id, sources)

    message = "已索引 %d 张照片" % len(entries_to_add)
    if ignored:
        message += "，忽略 %d 个非图片文件" % ignored
    if skipped:
        message += "，%d 个跳过" % len(skipped)
    if cancelled:
        message += "（已取消）"

    return {
        "imported": len(entries_to_add),
        "updated": updated,
        "ignored": ignored,
        "skipped": skipped[:50],
        "skipped_count": len(skipped),
        "message": message,
    }


def _remember_sources(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                      root_id: str, rel_dirs: Iterable[str]) -> None:
    """记下「这些目录已纳入索引」，重扫时按它们去找被移动的文件。"""
    if not root_id:
        return
    cleaned = []
    for rel in (rel_dirs or ()):
        value = str(rel or "").replace("\\", "/").strip("/")
        if value not in cleaned:
            cleaned.append(value)
    if not cleaned:
        return

    def _mutate(state):
        for rel in cleaned:
            if any(s["root"] == root_id and s["path"] == rel for s in state["sources"]):
                continue
            state["sources"].append({"root": root_id, "path": rel, "added": time.time()})

    try:
        _mutate_state(cfg, user, _mutate)
    except PhotoError:
        # 索引已经写进去了；来源清单记不上不该让整个导入报失败
        pass


def rescan(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], resolver,
           job: Any = None) -> Dict[str, Any]:
    """
    重扫：核对已知条目、找回被移动的文件、发现新文件。

    分三步，每一步都刻意选最省的做法：

    1. **原地核对**：条目记的路径还在、且 size 一致 —— 什么都不用做
       （不重新哈希、不重新读 EXIF）。绝大多数情况都走这一条。
    2. **找回被移动的**：只在第 1 步失败时才去算指纹，再拿指纹到
       「还没对上号的条目」里找。找到了就把 relpath 改过来 ——
       ★ 编辑记录是按指纹存的，所以用户设过的时间/地点**自动跟着走**。
    3. **发现新增**：来源目录里冒出来的新图片建成新条目。

    ★ 找不到文件的条目**不会被删除**。移动硬盘没插、临时改了目录名都会
      走到这里，而「文件暂时不在」从来不是丢掉用户编辑记录的理由。
      它们会被标成 missing，界面灰度显示。
    """
    index = load_index(cfg, user)
    known: Dict[str, Dict[str, Any]] = dict(index["entries"])
    if job is not None:
        job.set_totals(items=len(known) or 1)

    verified = 0
    missing = 0
    relinked = 0
    added = 0
    refreshed = 0

    # 还没对上号的条目（= 候选「走丢了」），键就是指纹本身
    unresolved: Dict[str, Dict[str, Any]] = {}
    # 已经确认还活着的（原地核对通过，或在目录里被找了回来）
    located: set = set()
    # 同一路径上内容变了的老 id -> 新 id，用来迁移用户的编辑记录
    migrate: Dict[str, str] = {}

    # ★ 必须遍历快照：下面会往 known 里 pop / 新增（内容变了要换指纹），
    #   直接遍历 known.items() 会抛「dictionary keys changed during iteration」。
    for pid, entry in list(known.items()):
        if job is not None and job.cancel_requested():
            break
        if job is not None:
            job.advance(items=1, current=os.path.basename(entry["relpath"]))

        try:
            abs_path = resolve_entry(cfg, user, resolver, entry)
        except (PathSecurityError, PhotoError):
            # 根已经不在这个人的可见范围里了（管理员改了分配）、
            # 或者上传条目的文件名被改坏了 ——
            # 不当成「文件丢了」，只是这次没法核对
            unresolved[pid] = entry
            continue

        try:
            stat = os.stat(abs_path)
        except OSError:
            unresolved[pid] = entry
            continue

        if int(stat.st_size) == entry["size"] and abs(stat.st_mtime - entry["mtime"]) < 1e-6:
            verified += 1
            continue

        # 内容可能变了：重新指纹 + 重新读 EXIF
        try:
            fresh = _build_entry(entry["root"], entry["relpath"], abs_path,
                                 size=int(stat.st_size), mtime=stat.st_mtime)
        except PhotoError:
            unresolved[pid] = entry
            continue

        if fresh["id"] == pid:
            # 同一张图、只是被重新保存过（mtime 变了）：刷新元数据
            fresh["added"] = entry.get("added") or time.time()
            known[pid] = fresh
            refreshed += 1
        else:
            # 同一个路径上内容变了（多半是重新导出 / 修过图）。
            # ★ 当作**同一张照片**处理：把用户的编辑记录迁到新指纹上。
            #   否则「我重新导出了一遍，之前改好的时间全没了」会非常恼人 ——
            #   而路径没变这件事本身就是「这还是那张照片」的强证据。
            known.pop(pid, None)
            known[fresh["id"]] = fresh
            migrate[pid] = fresh["id"]
            refreshed += 1

    # ---- 第 2 / 3 步：走一遍来源目录 ----
    # ★ 即使一张都没走丢也要走：**发现新增文件**本身就是重扫的职责。
    #   代价靠下面的快速通道压住 —— 位置与大小都没变的文件不重新哈希，
    #   真正要读字节的只有「新出现的」和「位置变了的」。
    by_fp = dict(unresolved)
    sources = load_state(cfg, user)["sources"]

    if sources:
        # (根标识, 相对路径) -> (id, 大小)：判断「这个位置我们已经索引过了」
        by_location: Dict[Tuple[str, str], Tuple[str, int]] = {
            (e["root"], e["relpath"]): (pid, e["size"]) for pid, e in known.items()
        }

        for source in sources:
            root_id = source["root"]
            base_rel = source["path"]
            try:
                _root, base_abs = resolver.resolve(root_id, base_rel)
            except PathSecurityError:
                continue
            if not os.path.isdir(base_abs):
                continue

            for current, dirs, files in os.walk(base_abs, followlinks=False):
                dirs[:] = [d for d in dirs
                           if not is_link_or_junction(os.path.join(current, d))]
                for name in files:
                    if job is not None and job.cancel_requested():
                        break
                    if not is_photo(name):
                        continue
                    full = os.path.join(current, name)
                    if is_link_or_junction(full):
                        continue
                    try:
                        rel = os.path.relpath(full, base_abs).replace("\\", "/")
                    except ValueError:
                        continue
                    rel = ("%s/%s" % (base_rel, rel)) if base_rel else rel

                    try:
                        stat = os.stat(full)
                    except OSError:
                        continue
                    size = int(stat.st_size)

                    # ★ 快速通道：这个位置已经索引过、大小也没变 —— 什么都不用做。
                    #   没有这条的话，每次重扫都要把库里每一张图的头部重新哈希
                    #   一遍（几千张就是几百 MB 的读取），白白慢一大截。
                    known_here = by_location.get((root_id, rel))
                    if known_here is not None and known_here[1] == size:
                        located.add(known_here[0])
                        continue

                    # 需要指纹才能判断「这是不是某个走丢了的老朋友」
                    try:
                        fp = quick_fingerprint(full, size)
                    except PhotoError:
                        continue

                    if fp in by_fp and fp not in located:
                        # ★ 命中「走丢的条目」：指纹就是 id，说明这张图本来
                        #   就在索引里，只是位置变了 —— 要认回来，用户的编辑
                        #   记录是按指纹存的，认回来就自动跟着走。
                        moved = dict(by_fp[fp])
                        moved["root"] = root_id
                        moved["relpath"] = rel
                        moved["size"] = size
                        moved["mtime"] = float(stat.st_mtime)
                        known[fp] = moved
                        by_location[(root_id, rel)] = (fp, size)
                        located.add(fp)
                        relinked += 1
                        continue

                    if known_here is not None or fp in known:
                        # 内容已经在库里了（同一个位置换了内容，或同一张图
                        # 被从另一个目录索引过）—— 不做重复条目
                        continue

                    try:
                        fresh = _build_entry(root_id, rel, full,
                                             size=size,
                                             mtime=stat.st_mtime, fingerprint=fp)
                    except PhotoError:
                        continue
                    known[fresh["id"]] = fresh
                    by_location[(root_id, rel)] = (fresh["id"], size)
                    added += 1

    # ---- 收尾：找不到的条目**保留下来**，只做标记 ----
    # ★「文件暂时不在」从来不是丢掉用户编辑记录的理由。移动硬盘没插、
    #   目录临时改名都会走到这里；条目留着，下次插上盘重扫就自动回来了。
    #
    # ★ 「最终没找到」要在这里才算：第 1 步里 stat 失败只说明「不在原位」，
    #   它很可能在第 2 步就被找回来了。直接累加第 1 步的次数会报出一个
    #   假警报（「1 张找不到」，而那张其实刚刚被认回来）。
    missing = sum(1 for pid in unresolved if pid not in located)

    final: Dict[str, Dict[str, Any]] = {}
    for pid, entry in known.items():
        item = dict(entry)
        item["missing"] = pid in unresolved and pid not in located
        final[pid] = item

    def _replace(index):
        index["entries"] = final
        return None
    _mutate_index(cfg, user, _replace)

    # 内容变过的照片：把编辑记录与相册引用迁到新指纹上
    # （见上面 else 分支的说明 —— 否则「重新导出一次，改好的时间就没了」）
    if migrate:
        def _migrate_state(state):
            for old_id, new_id in migrate.items():
                old_edit = state["edits"].pop(old_id, None)
                if old_edit and new_id not in state["edits"]:
                    state["edits"][new_id] = old_edit
            for album in state["albums"]:
                if album.get("kind") == "manual":
                    album["items"] = [migrate.get(x, x)
                                      for x in (album.get("items") or [])]
            return None

        try:
            _mutate_state(cfg, user, _migrate_state)
        except PhotoError:
            pass

    message = "核对 %d 张" % verified
    if refreshed:
        message += "，刷新 %d 张" % refreshed
    if relinked:
        message += "，找回 %d 张（位置变了，编辑记录已跟随）" % relinked
    if added:
        message += "，新增 %d 张" % added
    if missing:
        message += "，%d 张暂时找不到" % missing

    return {
        "verified": verified,
        "refreshed": refreshed,
        "relinked": relinked,
        "added": added,
        "missing": missing,
        "message": message,
    }


# ---------------------------------------------------------------------------
# 有效值（覆盖层 ?? EXIF ?? 文件时间）
# ---------------------------------------------------------------------------

def effective_time(entry: Dict[str, Any], edit: Optional[Dict[str, Any]]) -> Tuple[str, str]:
    """
    算出这张照片「实际该显示在时间轴的哪个位置」，以及**这个时间的可信度**。

    ★ 三态是刻意的，不是实现细节：
      * `user` —— 用户自己改过，最可信；
      * `exif` —— 相机写的拍摄时间，可信；
      * `file` —— 兜底用了文件修改时间。微信图片、截图、从网上下载的图
        基本都没有 EXIF，它们会全部落到这一档，而这个时间是**下载/保存时间**，
        不是拍摄时间。时间轴会因此散成一团。

      所以界面必须把 file 档显式标出来，并把它们汇到「待整理」里 ——
      否则「按时间轴归类」这件事从一开始就建立在一批不可信的时间上。
    """
    if edit and edit.get("taken_at"):
        return str(edit["taken_at"]), "user"
    if entry.get("exif_taken"):
        return str(entry["exif_taken"]), "exif"
    return mtime_iso(entry.get("mtime") or 0), "file"


def _photo_payload(entry: Dict[str, Any], edit: Optional[Dict[str, Any]],
                   missing: bool = False, stale: bool = False) -> Dict[str, Any]:
    """索引条目 + 覆盖层 -> 给前端的一条照片记录。"""
    taken_at, source = effective_time(entry, edit)

    place = None
    if edit and isinstance(edit.get("place"), dict):
        place = dict(edit["place"])
    gps = entry.get("gps")

    # 用户没填名字、但 EXIF 里有坐标时，把坐标带给界面，
    # 让它提示「这里有一批同坐标的照片，要不要起个名字」
    if place is None and isinstance(gps, dict):
        place = {"name": "", "lat": gps.get("lat"), "lon": gps.get("lon"),
                 "from_gps": True}

    return {
        "id": entry["id"],
        "name": os.path.basename(entry["relpath"]),
        "relpath": entry["relpath"],
        "root": entry["root"],
        "size": entry["size"],
        "mtime": entry["mtime"],
        "w": entry.get("w") or 0,
        "h": entry.get("h") or 0,
        "taken_at": taken_at,
        "source": source,
        "tz": (edit or {}).get("tz") or entry.get("exif_tz") or "",
        "place": place,
        "gps": gps,
        "tags": list((edit or {}).get("tags") or []),
        "rating": int((edit or {}).get("rating") or 0),
        "caption": str((edit or {}).get("caption") or ""),
        "camera": entry.get("camera") or "",
        "lens": entry.get("lens") or "",
        "iso": entry.get("iso") or 0,
        "fnum": entry.get("fnum") or 0,
        "exposure": entry.get("exposure") or "",
        "edited": bool(edit),
        "missing": bool(missing),
        "stale": bool(stale),
    }


def build_library(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], resolver,
                  limit: Optional[int] = None) -> Dict[str, Any]:
    """
    组装前端要的整份数据（一次取全，前端自己分组/排序/筛选）。

    为什么不做服务端分页：目标是单用户几千张，这个量级的 JSON 只有 1~2MB，
    一次取全能让「时间轴分组、跨月筛选、批量选择」全部在前端做，
    简单得多。接口保留 limit 参数是为了将来真的上万张时能平滑过渡。
    """
    state = load_state(cfg, user)
    index = load_index(cfg, user)
    ceiling = limit if limit is not None else max_items(cfg)

    photos: List[Dict[str, Any]] = []
    stats = {"total": 0, "user": 0, "exif": 0, "file": 0, "missing": 0, "stale": 0,
             "undated": 0, "with_gps": 0}

    inaccessible = 0
    for pid, entry in index["entries"].items():
        try:
            abs_path = resolve_entry(cfg, user, resolver, entry)
        except (PathSecurityError, PhotoError):
            # 这个根已经不在当前用户的可见范围内（管理员改了分配），
            # 或者上传条目的文件名被改坏了。
            # ★ 直接跳过：相册绝不能成为绕过可见性的旁路，
            #   也绝不能因为一条坏记录就让整库打不开。
            inaccessible += 1
            continue

        missing = False
        stale = False
        try:
            stat = os.stat(abs_path)
            if int(stat.st_size) != entry["size"] or abs(stat.st_mtime - entry["mtime"]) > 1e-6:
                stale = True
        except OSError:
            missing = True

        item = _photo_payload(entry, state["edits"].get(pid), missing=missing, stale=stale)
        photos.append(item)

        stats["total"] += 1
        stats[item["source"]] = stats.get(item["source"], 0) + 1
        if missing:
            stats["missing"] += 1
        if stale:
            stats["stale"] += 1
        if not item["taken_at"]:
            stats["undated"] += 1
        if item["gps"]:
            stats["with_gps"] += 1

        if len(photos) >= ceiling:
            break

    # 默认按拍摄时间倒序（新的在前），前端可以再排
    photos.sort(key=lambda p: (p["taken_at"] or "", p["name"].lower()), reverse=True)

    return {
        "photos": photos,
        "stats": stats,
        "truncated": stats["total"] >= ceiling,
        "ceiling": ceiling,
        "inaccessible": inaccessible,
        "sources": state["sources"],
        "albums": state["albums"],
        "prefs": state["prefs"],
        "limits": {
            "thumb_size": thumb_size(cfg),
            "max_import": max_import(cfg),
            "max_items": max_items(cfg),
        },
    }


# ---------------------------------------------------------------------------
# 取单个 / 解析路径
# ---------------------------------------------------------------------------

def resolve_entry(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], resolver,
                  entry: Dict[str, Any]) -> str:
    """
    索引条目 -> 绝对路径。**所有**读文件的地方都必须走这里。

    条目有两种来源，闸门也相应有两道：

      * 普通条目（用户在服务器上挑的目录）：走**当前用户的路径解析器**。
        索引里存的是「根标识 + 相对路径」，所以手工把 relpath 改成
        `../../etc/passwd` 也读不出根目录外的文件 —— 如果索引里存的是绝对
        路径，编辑一个 JSON 就等于任意文件读取。
      * 上传进来的照片（root = UPLOAD_ROOT）：走 `upload_dir/<用户名>/`。
        那里面的文件名由服务端生成，且必须落在这个用户自己的上传目录内。

    ★ 集中成一个函数是刻意的：早先这三处（重扫、列库、取单张）各写了一遍
      `resolver.resolve`，加上上传之后必须三处同时改对，漏掉任何一处就会
      出现「上传的照片在相册里显示找不到」这种很难查的问题。
    """
    root = str(entry.get("root") or "")
    rel = str(entry.get("relpath") or "")

    if root == UPLOAD_ROOT:
        return upload_file_path(cfg, user, rel)

    _root_cfg, abs_path = resolver.resolve(root, rel)
    return abs_path


def resolve_photo(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], resolver,
                  photo_id: Any) -> Tuple[Dict[str, Any], str]:
    """
    照片标识 -> (索引条目, 绝对路径)。

    ★ 这里是**第二道安全闸门**（第一道是 norm_id）。索引里存的是
      「根标识 + 相对路径」，所以就算有人手工改了 photo_index.json，
      也要再过一遍 resolve_entry 的两道校验才变成绝对路径。
    """
    pid = norm_id(photo_id)
    entry = load_index(cfg, user)["entries"].get(pid)
    if entry is None:
        raise PhotoError("这张照片不在索引里（可能还没导入，或已被移除）")

    try:
        abs_path = resolve_entry(cfg, user, resolver, entry)
    except PathSecurityError as exc:
        raise PhotoError("这张照片不在你能访问的目录里：%s" % exc)

    if not os.path.isfile(abs_path):
        raise PhotoError("文件已经不在了：%s" % entry["relpath"])
    return entry, abs_path


# ---------------------------------------------------------------------------
# 上传（从**浏览器所在的电脑**传进来）
# ---------------------------------------------------------------------------

def prepare_upload_target(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                          filename: str, declared_size: int = 0,
                          blocked_extensions: Iterable[str] = ()) -> Tuple[str, str]:
    """
    校验并算出上传照片的落盘路径，返回 (清洗后的文件名, 目标绝对路径)。

    与文件管理器、音乐播放器同一套做法，三条一致的口径：

      * **只收图片**：不是图片扩展名直接拒绝（比黑名单更严，也更贴合本功能）——
        收下打不开的文件，用户只会得到一个点不动的条目；
      * **绝不覆盖**：同名一律自动改名（`照片.jpg` → `照片 (1).jpg`），
        上传是最容易踩到覆盖的一步，而覆盖掉别人的照片几乎无法挽回；
      * **落盘前先看磁盘**：Content-Length 给了就先查一次剩余空间。
    """
    directory = ensure_upload_dir(cfg, user)

    safe_name = sanitize_filename(filename)
    if not safe_name:
        raise PhotoError("文件名不合法")
    if not is_photo(safe_name):
        raise PhotoError(
            "只支持图片格式：%s" % "、".join(PHOTO_EXTENSIONS))

    hit = is_blocked_extension(safe_name, blocked_extensions)
    if hit:
        raise PhotoError("出于安全考虑，禁止上传 %s 类型的文件" % hit)

    if declared_size:
        try:
            fsops.check_disk_space(directory, int(declared_size))
        except OSError as exc:
            raise PhotoError(str(exc))

    try:
        _requested, target = fsops.resolve_upload_target(
            directory, safe_name, blocked_extensions, overwrite=False)
    except (PathSecurityError, FileExistsError) as exc:
        raise PhotoError(str(exc))

    # ★ 必须用**磁盘上真正的那个名字**：resolve_upload_target 回的名字是
    #   「用户原本请求的名字」，而重名时 unique_path 会把文件改成
    #   `照片 (1).jpg` —— 两者并不一致。这个名字会被存进索引当 relpath 用，
    #   用错的话条目就指向了另一个文件（表现是缩略图和实际内容对不上，
    #   或者干脆显示「找不到」）。文件管理器那边只是提示语不好看，
    #   在这里却是数据正确性问题。
    final_name = os.path.basename(target)
    return final_name, target


def index_upload(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], resolver,
                 abs_path: str, name: str) -> Dict[str, Any]:
    """
    把刚上传落盘的文件就地索引进去（root 用 UPLOAD_ROOT）。

    与「服务器上导入」走的是同一条索引结构，所以指纹、EXIF、时间三态、
    编辑、重扫这些能力对它一律适用，不需要另一套代码。

    ★ 返回里的 `duplicate` 是**内容重复**：照片的身份就是内容指纹，所以
      同一张照片传两遍时，两份文件的指纹完全一样，索引里只能有一条记录。
      如果不处理，第二次上传会**悄悄把第一条的位置顶掉**，磁盘上留下一个
      永远不会被索引的副本（用户看不到、也不会去删）。
      所以这里如实报出来，由调用方把刚写下的那份删掉并明确告诉用户。
    """
    try:
        stat = os.stat(abs_path)
    except OSError as exc:
        raise PhotoError("无法读取刚上传的文件：%s" % exc)

    entry = _build_entry(UPLOAD_ROOT, name, abs_path,
                         size=int(stat.st_size), mtime=stat.st_mtime)

    existing = load_index(cfg, user)["entries"].get(entry["id"])
    if existing is not None:
        # 这条指纹已经在库里了。先看它记的是不是**同一个位置**：
        # 同位置说明 unique_path 把刚写下的文件又放回了原处（原来那份被删过），
        # 那就是同一张照片回来了，刷新元数据即可，不算重复。
        same_location = (str(existing.get("root") or "") == entry["root"]
                         and str(existing.get("relpath") or "") == entry["relpath"])

        if not same_location:
            # 位置不同，再看老位置的文件还在不在：
            #   * 还在 → 这次是重复上传，位置**不动**（不然第一条就成了孤儿）；
            #   * 不在（被删/被挪走）→ 正好用这一份把位置补回来，相当于恢复。
            # ★ 必须先比位置再看文件：只查 isfile 的话，刚写在**原位置**上的
            #   文件会把「已经删掉的那份」误判成还在，于是「恢复」变成「重复」。
            still_there = False
            try:
                still_there = os.path.isfile(
                    resolve_entry(cfg, user, resolver, existing))
            except (PathSecurityError, PhotoError):
                still_there = False

            if still_there:
                return {
                    "id": entry["id"],
                    "name": name,
                    "relpath": entry["relpath"],
                    "size": entry["size"],
                    "w": entry["w"],
                    "h": entry["h"],
                    "taken_at": entry["exif_taken"],
                    "duplicate": True,
                }

    def _merge(index):
        index["entries"][entry["id"]] = entry
        return None

    _mutate_index(cfg, user, _merge)

    return {
        "id": entry["id"],
        "name": name,
        "relpath": entry["relpath"],
        "size": entry["size"],
        "w": entry["w"],
        "h": entry["h"],
        "taken_at": entry["exif_taken"],
        "duplicate": False,
    }


def get_photo_detail(cfg: Dict[str, Any], user: Optional[Dict[str, Any]], resolver,
                     photo_id: Any) -> Dict[str, Any]:
    """单张照片的完整信息（EXIF 原值 + 用户覆盖值 + 有效值）。"""
    pid = norm_id(photo_id)
    entry, abs_path = resolve_photo(cfg, user, resolver, pid)
    state = load_state(cfg, user)
    edit = state["edits"].get(pid)

    try:
        stat = os.stat(abs_path)
        stale = (int(stat.st_size) != entry["size"]
                 or abs(stat.st_mtime - entry["mtime"]) > 1e-6)
    except OSError:
        stale = False

    detail = _photo_payload(entry, edit, stale=stale)
    detail["abs_hint"] = entry["relpath"]
    detail["exif"] = {
        "taken_at": entry.get("exif_taken") or "",
        "tz": entry.get("exif_tz") or "",
        "camera": entry.get("camera") or "",
        "lens": entry.get("lens") or "",
        "iso": entry.get("iso") or 0,
        "fnum": entry.get("fnum") or 0,
        "exposure": entry.get("exposure") or "",
        "gps": entry.get("gps"),
        "w": entry.get("w") or 0,
        "h": entry.get("h") or 0,
    }
    detail["edit"] = edit or {}
    return detail


# ---------------------------------------------------------------------------
# 编辑
# ---------------------------------------------------------------------------

def _ensure_edit(state: Dict[str, Any], pid: str) -> Dict[str, Any]:
    edit = state["edits"].get(pid)
    if not isinstance(edit, dict):
        edit = {}
        state["edits"][pid] = edit
    return edit


def _journal_push(state: Dict[str, Any], op: str, before: Dict[str, Any],
                  label: str = "") -> None:
    """
    记一条撤销日志。

    ★ 为什么要撤销：批量把 137 张照片整体 +8 小时，是一个**不可逆**的操作
      （改错了要一张张改回来）。项目对不可逆操作的一贯态度是要么二次确认、
      要么可恢复 —— 这里选「可恢复」，比每次弹确认框顺手得多。
    """
    state["journal"].append({
        "op": op,
        "label": label,
        "at": time.time(),
        "before": before,
    })
    del state["journal"][:-JOURNAL_LIMIT]


def apply_edits(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                photo_ids: Iterable[Any], patch: Dict[str, Any],
                op: str = "edit", label: str = "") -> Dict[str, Any]:
    """
    对一批照片套用同一组修改（单张编辑就是只有一个元素的这一批）。

    patch 里出现的键才会被改：
      * `taken_at`：字符串（空串 = 清除覆盖值，回到 EXIF/文件时间）
      * `place`：{name, lat, lon}
      * `tags`：字符串列表
      * `rating`：0~5
      * `caption`：字符串

    ★ 只有**真正存在于索引里**的 id 才会被接受：否则接口可以被用来往
      state 文件里灌任意垃圾（久而久之把文件撑爆）。
    """
    ids: List[str] = []
    for raw in (photo_ids or []):
        try:
            pid = norm_id(raw)
        except PhotoError:
            continue
        if pid not in ids:
            ids.append(pid)
    if not ids:
        raise PhotoError("没有选中任何照片")
    if len(ids) > MAX_BATCH:
        raise PhotoError("一次最多修改 %d 张" % MAX_BATCH)

    index = load_index(cfg, user)["entries"]
    valid = [pid for pid in ids if pid in index]
    if not valid:
        raise PhotoError("选中的照片都不在索引里")

    def _mutate(state):
        before: Dict[str, Any] = {}
        for pid in valid:
            old = state["edits"].get(pid)
            before[pid] = dict(old) if isinstance(old, dict) else None

            edit = _ensure_edit(state, pid)
            _merge_patch(edit, patch)

            if not edit:
                state["edits"].pop(pid, None)
            else:
                edit["edited_at"] = time.time()

        _journal_push(state, op, before, label)
        return None

    _mutate_state(cfg, user, _mutate)
    return {"updated": len(valid), "skipped": len(ids) - len(valid)}


def _merge_patch(edit: Dict[str, Any], patch: Dict[str, Any]) -> None:
    """把 patch 合进一条编辑记录（只有 patch 里出现的键会被动）。"""
    if "taken_at" in patch:
        text = str(patch.get("taken_at") or "").strip()
        if text:
            edit["taken_at"] = parse_user_time(text.replace("T", " "))
        else:
            edit.pop("taken_at", None)

    if "tz" in patch:
        text = str(patch.get("tz") or "").strip()
        if text:
            edit["tz"] = text[:8]
        else:
            edit.pop("tz", None)

    if "place" in patch:
        raw = patch.get("place")
        if raw is None:
            edit.pop("place", None)
        elif isinstance(raw, dict):
            name = str(raw.get("name") or "").strip()[:120]
            entry: Dict[str, Any] = {}
            if name:
                entry["name"] = name
            for key in ("lat", "lon"):
                value = raw.get(key)
                if value is None or str(value).strip() == "":
                    continue
                try:
                    entry[key] = round(float(value), 6)
                except (TypeError, ValueError):
                    raise PhotoError("经纬度必须是数字")
            if entry:
                edit["place"] = entry
            else:
                edit.pop("place", None)

    if "tags" in patch:
        raw = patch.get("tags")
        tags: List[str] = []
        if isinstance(raw, str):
            raw = re.split(r"[,，、\s]+", raw)
        for item in (raw or []):
            text = str(item or "").strip()[:40]
            if text and text not in tags:
                tags.append(text)
            if len(tags) >= 30:
                break
        if tags:
            edit["tags"] = tags
        else:
            edit.pop("tags", None)

    if "rating" in patch:
        try:
            rating = int(patch.get("rating") or 0)
        except (TypeError, ValueError):
            raise PhotoError("星级必须是 0~5 的整数")
        if rating:
            edit["rating"] = max(0, min(5, rating))
        else:
            edit.pop("rating", None)

    if "caption" in patch:
        text = str(patch.get("caption") or "").strip()[:500]
        if text:
            edit["caption"] = text
        else:
            edit.pop("caption", None)


def shift_time(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
               photo_ids: Iterable[Any], delta_seconds: int) -> Dict[str, Any]:
    """
    批量把时间平移一个增量（**保留彼此的相对先后**）。

    这是照片整理里真正常用的那一个操作：相机的时区设错了，一次旅行拍的
    几百张全部偏了 8 小时。与其一张张改，不如整体挪。

    ★ 平移的基准是「当前有效时间」，而且把结果**写进用户覆盖值**：
      对原本没有 EXIF 的照片，平移它的文件时间同样会得到一个新时间。
      这符合直觉（用户看到的是时间轴，挪的就是时间轴上的位置）。
    """
    try:
        delta = int(delta_seconds)
    except (TypeError, ValueError):
        raise PhotoError("时间增量必须是整数秒")
    if delta == 0:
        raise PhotoError("时间增量是 0，没有需要修改的内容")
    if abs(delta) > 366 * 24 * 3600 * 20:
        raise PhotoError("时间增量过大（最多 20 年）")

    ids: List[str] = []
    for raw in (photo_ids or []):
        try:
            pid = norm_id(raw)
        except PhotoError:
            continue
        if pid not in ids:
            ids.append(pid)
    if not ids:
        raise PhotoError("没有选中任何照片")
    if len(ids) > MAX_BATCH:
        raise PhotoError("一次最多修改 %d 张" % MAX_BATCH)

    index = load_index(cfg, user)["entries"]

    def _mutate(state):
        before: Dict[str, Any] = {}
        changed = 0
        for pid in ids:
            entry = index.get(pid)
            if entry is None:
                continue
            current, _source = effective_time(entry, state["edits"].get(pid))
            if not current:
                continue

            try:
                base = datetime.strptime(current, "%Y-%m-%dT%H:%M:%S")
            except ValueError:
                continue

            old = state["edits"].get(pid)
            before[pid] = dict(old) if isinstance(old, dict) else None

            moved = base.timestamp() + delta
            try:
                shifted = datetime.fromtimestamp(moved).strftime("%Y-%m-%dT%H:%M:%S")
            except (OverflowError, OSError, ValueError):
                continue

            edit = _ensure_edit(state, pid)
            edit["taken_at"] = shifted
            edit["edited_at"] = time.time()
            changed += 1

        if not changed:
            raise PhotoError("选中的照片都没有可用的时间，无法平移")

        hours = delta / 3600.0
        _journal_push(state, "shift_time", before,
                      "整体平移 %+.2f 小时" % hours)
        state["journal"][-1]["delta"] = delta
        return changed

    changed = _mutate_state(cfg, user, _mutate)
    return {"updated": changed, "delta": delta}


def undo_last(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """撤销最近一次编辑（回放撤销日志里记下的「改动前」快照）。"""
    def _mutate(state):
        if not state["journal"]:
            raise PhotoError("没有可以撤销的操作")

        record = state["journal"].pop()
        before = record.get("before") or {}
        restored = 0
        for pid, old in before.items():
            try:
                pid = norm_id(pid)
            except PhotoError:
                continue
            if isinstance(old, dict):
                state["edits"][pid] = old
            else:
                state["edits"].pop(pid, None)
            restored += 1
        return {"restored": restored, "label": record.get("label") or record.get("op") or ""}

    return _mutate_state(cfg, user, _mutate)


# ---------------------------------------------------------------------------
# 相册
# ---------------------------------------------------------------------------

def create_album(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                 name: str, kind: str = "manual",
                 start: str = "", end: str = "",
                 items: Optional[Iterable[Any]] = None) -> Dict[str, Any]:
    """新建相册：manual（手动挑的）或 smart（按时间段自动归类）。"""
    clean = str(name or "").strip()[:MAX_ALBUM_NAME]
    if not clean:
        raise PhotoError("相册名不能为空")

    if kind == "smart":
        start_iso = parse_user_time(start) if str(start or "").strip() else ""
        end_iso = parse_user_time(end) if str(end or "").strip() else ""
        if not start_iso and not end_iso:
            raise PhotoError("按时间段归类时，至少要给一个开始或结束时间")
        if start_iso and end_iso and start_iso > end_iso:
            raise PhotoError("开始时间不能晚于结束时间")
    else:
        start_iso = end_iso = ""

    index = load_index(cfg, user)["entries"]
    picked: List[str] = []
    if kind != "smart":
        for raw in (items or []):
            try:
                pid = norm_id(raw)
            except PhotoError:
                continue
            if pid in index and pid not in picked:
                picked.append(pid)

    album = {
        "id": "al_" + secrets.token_hex(6),
        "name": clean,
        "kind": "smart" if kind == "smart" else "manual",
        "created": time.time(),
    }
    if album["kind"] == "smart":
        album["start"] = start_iso
        album["end"] = end_iso
    else:
        album["items"] = picked

    def _mutate(state):
        state["albums"].append(album)
        return album

    _mutate_state(cfg, user, _mutate)
    return album


def delete_album(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                 album_id: Any) -> bool:
    """删相册。★ 只是删掉这个归类，照片和它们的编辑记录一张都不动。"""
    target = str(album_id or "").strip()
    if not target:
        raise PhotoError("缺少相册标识")

    def _mutate(state):
        before = len(state["albums"])
        state["albums"] = [a for a in state["albums"] if a["id"] != target]
        return before != len(state["albums"])

    removed = _mutate_state(cfg, user, _mutate)
    if not removed:
        raise PhotoError("没有这个相册")
    return True


def rename_album(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                 album_id: Any, name: str) -> Dict[str, Any]:
    target = str(album_id or "").strip()
    clean = str(name or "").strip()[:MAX_ALBUM_NAME]
    if not target:
        raise PhotoError("缺少相册标识")
    if not clean:
        raise PhotoError("相册名不能为空")

    def _mutate(state):
        for album in state["albums"]:
            if album["id"] == target:
                album["name"] = clean
                return album
        raise PhotoError("没有这个相册")

    return _mutate_state(cfg, user, _mutate)


def album_items(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                album_id: Any, photo_ids: Iterable[Any],
                action: str = "add") -> Dict[str, Any]:
    """往手动相册里加照片 / 移出照片。★ 移出相册**不会**删照片、也不会丢编辑。"""
    target = str(album_id or "").strip()
    if not target:
        raise PhotoError("缺少相册标识")
    mode = "remove" if str(action or "").strip().lower() == "remove" else "add"

    ids: List[str] = []
    for raw in (photo_ids or []):
        try:
            pid = norm_id(raw)
        except PhotoError:
            continue
        if pid not in ids:
            ids.append(pid)
    if not ids:
        raise PhotoError("没有选中任何照片")

    def _mutate(state):
        for album in state["albums"]:
            if album["id"] != target:
                continue
            if album.get("kind") != "manual":
                raise PhotoError("智能相册按时间段自动归类，不能手工加减照片")
            items = list(album.get("items") or [])
            if mode == "add":
                for pid in ids:
                    if pid not in items:
                        items.append(pid)
            else:
                items = [pid for pid in items if pid not in ids]
            album["items"] = items
            return {"count": len(items)}
        raise PhotoError("没有这个相册")

    result = _mutate_state(cfg, user, _mutate)
    return {"count": result["count"], "action": mode, "updated": len(ids)}


def save_prefs(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
               patch: Dict[str, Any]) -> Dict[str, Any]:
    """保存浏览偏好（时间轴粒度 / 排序）。"""
    def _mutate(state):
        merged = dict(state.get("prefs") or {})
        for key in ("level", "sort"):
            if key in (patch or {}):
                merged[key] = patch[key]
        state["prefs"] = _normalize_prefs(merged)
        return state["prefs"]

    return _mutate_state(cfg, user, _mutate)


def forget_source(cfg: Dict[str, Any], user: Optional[Dict[str, Any]],
                  root_id: str, rel: str = "") -> Dict[str, Any]:
    """
    把一个来源目录移出索引（**不删任何文件**）。

    默认连带把该目录下的照片条目与编辑记录一起清掉 —— 这是「我不想再看
    这个目录」的语义。用户如果只是想暂时断开（比如移动硬盘拔了），
    不该用这个接口，那批照片会自动标成 missing 保留着。
    """
    target_root = str(root_id or "").strip()
    clean = str(rel or "").replace("\\", "/").strip("/")
    if not target_root:
        raise PhotoError("缺少根标识")

    prefix = (clean + "/") if clean else ""

    index = load_index(cfg, user)["entries"]
    doomed = [pid for pid, entry in index.items()
              if entry["root"] == target_root
              and (entry["relpath"] == clean
                   or (prefix and entry["relpath"].startswith(prefix)))]

    def _drop_entries(index_data):
        for pid in doomed:
            index_data["entries"].pop(pid, None)
        return len(doomed)

    # ★ 闭包名不能叫 _mutate_index —— 那会遮蔽模块级的同名函数，
    #   于是 `_mutate_index(cfg, user, _mutate_index)` 变成调用闭包本身，
    #   直接抛 TypeError（这个坑真的踩过一次）。
    removed = _mutate_index(cfg, user, _drop_entries)

    def _drop_state(state):
        state["sources"] = [s for s in state["sources"]
                            if not (s["root"] == target_root and s["path"] == clean)]
        for pid in doomed:
            state["edits"].pop(pid, None)
        return None

    _mutate_state(cfg, user, _drop_state)

    return {"removed": removed,
            "message": "已从相册移出 %d 张（照片文件本身没有被删除）" % removed}


def library_summary(cfg: Dict[str, Any], user: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """库的概况（不枚举照片，给界面做轻量提示用）。"""
    index = load_index(cfg, user)["entries"]
    state = load_state(cfg, user)
    return {
        "indexed": len(index),
        "sources": len(state["sources"]),
        "albums": len(state["albums"]),
        "edits": len(state["edits"]),
        "undoable": len(state["journal"]),
    }

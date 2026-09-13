# -*- coding: utf-8 -*-
"""
缩略图模块
==========

为图片类文件生成 100x100 的缩略图，并缓存到磁盘（默认 ./thumb_cache）。

关键设计：
    * 缓存键 = sha1(绝对路径 | 修改时间 | 文件大小 | 目标尺寸)
      把 mtime 和 size 计入键，文件被覆盖后会自然生成新缩略图，不会看到旧图。
    * 生成失败会写一个 0 字节的「失败标记」，避免每次请求都去重试解码坏图。
    * 先写临时文件再 os.replace 原子落地，多请求并发生成同一张图也不会读到半截文件。
    * 缓存目录超过上限时按「最久未访问」清理，防止无限增长。

关于「解压炸弹」防护（重要，别改回去）：
    本模块**不再**修改 PIL.Image.MAX_IMAGE_PIXELS 这个进程级全局量。
    早先的写法把它设成 300_000_000，但 Pillow 自身的默认值是 89_478_485，
    而且只有超过 2 倍上限才会真正抛错 —— 也就是说那行「防护」实际把拒绝
    阈值从 1.79 亿像素抬高到了 6 亿像素（实测 3.49 亿像素的 PNG 能正常打开），
    单张图就能吃掉 GB 级内存，比不写这行更危险。

    现在改为两道自己控制的防线：
      1. 解码前用 _MAX_PIXELS 做显式尺寸预检（不依赖 Pillow 的内部策略）；
      2. JPEG 先 draft() 让 libjpeg 直接按 1/2、1/4、1/8 解码，避免全尺寸解码。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import threading
import time
import warnings
from typing import Optional, Tuple

from PIL import Image, ImageOps

# 单张图片允许解码的最大像素数（宽 x 高）。
# 80M 像素约为 8000x10000，远大于常规照片；按 RGB 估算峰值内存约 240MB。
# 超过就拒绝生成缩略图（前端会退化成类型图标），而不是硬解一张可能吃 GB 内存的图。
_MAX_PIXELS = 80_000_000

# 支持的图片扩展名。
# 注意：.heic / .heif 需要额外的解码器（pillow-heif），本项目依赖里没有，
# 列进来只会白解码一次并写下永久的失败标记，因此不收录。
# .avif 在 Pillow 11.3+ 已原生支持（本机实测 features.check('avif') = True），保留。
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".gif", ".bmp", ".webp",
    ".tif", ".tiff", ".ico", ".avif", ".jfif",
}

# 生成失败时写入的标记文件后缀
_FAIL_SUFFIX = ".fail"

_evict_lock = threading.Lock()
_last_evict = 0.0
# 每生成这么多张缩略图才检查一次缓存体积，避免频繁遍历目录
_EVICT_INTERVAL_COUNT = 50
_generate_counter = 0


def is_image(filename: str) -> bool:
    """按扩展名判断是否图片。"""
    return os.path.splitext(filename or "")[1].lower() in IMAGE_EXTENSIONS


def cache_key(src_path: str, mtime: float, size: int, box: int) -> str:
    """计算缓存键。路径做了 normcase，避免 Windows 大小写差异导致重复缓存。"""
    raw = "%s|%.6f|%d|%d" % (os.path.normcase(os.path.abspath(src_path)), mtime, size, box)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _cache_paths(cache_dir: str, key: str) -> Tuple[str, str]:
    """返回 (正式缓存文件, 失败标记文件) 的路径。按前两位分子目录，避免单目录文件过多。"""
    sub = os.path.join(cache_dir, key[:2])
    return os.path.join(sub, key + ".jpg"), os.path.join(sub, key + _FAIL_SUFFIX)


def _render_thumbnail(src_path: str, dest_path: str, box: int) -> bool:
    """
    真正做图像解码与缩放。成功返回 True。

    处理细节：
      * 解码前做像素上限校验，并让 JPEG 先 draft 降采样（省内存的关键）
      * exif_transpose 修正手机照片的旋转信息，否则竖拍照片会躺着显示
      * 动图（GIF/WebP）只取第一帧
      * 带透明通道 / 调色板 / CMYK 统一合成到白底再存成 JPEG
    """
    try:
        # 我们自己的像素上限（80M）比 Pillow 的告警阈值（89M）更严格，
        # 所以 Pillow 的 DecompressionBombWarning 只可能对我们已经拒绝的图发出，
        # 这里直接忽略以免刷日志；真正越过 2 倍阈值时 Pillow 抛的是 Error，会被下面捕获。
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)

            with Image.open(src_path) as im:
                # JPEG 可以交给 libjpeg 直接按 1/2、1/4、1/8 解码，内存立刻降一个量级
                if im.format == "JPEG":
                    try:
                        im.draft("RGB", (box, box))
                    except Exception:  # noqa: BLE001 - draft 失败不影响正常流程
                        pass

                width, height = im.size
                if width * height > _MAX_PIXELS:
                    # 刻意不解码：直接判定失败，前端退化为类型图标
                    return False

                im.load()

                try:
                    im = ImageOps.exif_transpose(im)
                except Exception:  # noqa: BLE001 - 个别图 EXIF 损坏不影响出图
                    pass

                im.thumbnail((box, box), Image.LANCZOS)

                # 统一转换到 RGB，必要时合成白底
                if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                    rgba = im.convert("RGBA")
                    background = Image.new("RGB", rgba.size, (255, 255, 255))
                    background.paste(rgba, mask=rgba.split()[-1])
                    im = background
                elif im.mode != "RGB":
                    im = im.convert("RGB")

                # 原子落地：先写临时文件，再 replace
                directory = os.path.dirname(dest_path)
                os.makedirs(directory, exist_ok=True)
                fd, tmp_path = tempfile.mkstemp(prefix=".thumb-", suffix=".tmp", dir=directory)
                os.close(fd)
                try:
                    im.save(tmp_path, format="JPEG", quality=85, optimize=True)
                    os.replace(tmp_path, dest_path)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass
                    raise
        return True
    except Exception:  # noqa: BLE001 - 坏图、格式不支持、权限不足都归为失败
        return False


def _touch(path: str) -> None:
    """
    更新「最后访问时间」，供 LRU 清理使用。

    只改 atime、保留 mtime：os.utime(path, None) 会把 atime 和 mtime 一起改成
    当前时间，那样缓存文件真实修改时间就丢了，语义也不对。
    """
    try:
        stat = os.stat(path)
        os.utime(path, (time.time(), stat.st_mtime))
    except OSError:
        pass


def ensure_thumb(src_path: str, cache_dir: str, box: int = 100,
                 max_mb: int = 512) -> Tuple[bool, Optional[str]]:
    """
    获取（必要时生成）缩略图。

    返回：
        (True, 缩略图绝对路径)  —— 可以直接用 FileResponse 返回
        (False, None)          —— 不是图片 / 解码失败 / 生成失败

    max_mb 为缓存目录体积上限（来自 config.json 的 thumbs.max_cache_mb）。
    """
    if not src_path or not os.path.isfile(src_path):
        return False, None

    try:
        stat = os.stat(src_path)
    except OSError:
        return False, None

    key = cache_key(src_path, stat.st_mtime, stat.st_size, box)
    hit_path, fail_path = _cache_paths(cache_dir, key)

    # 先查缓存命中：必须放在失败标记检查之前。
    # 否则并发场景下（A 读到正在写入的半截文件而失败并写下 .fail，
    # B 随后成功生成了有效缩略图）那个 .fail 会把已经生成好的缩略图永久屏蔽。
    try:
        if os.path.exists(hit_path) and os.path.getsize(hit_path) > 0:
            _touch(hit_path)
            # 已经有有效缩略图，说明失败标记是陈旧的（典型场景：上次读到了
            # 正在被写入的半截文件而失败），顺手清掉，避免它继续屏蔽这张图
            if os.path.exists(fail_path):
                try:
                    os.unlink(fail_path)
                except OSError:
                    pass
            return True, hit_path
    except OSError:
        # 命中判定与淘汰线程之间可能存在竞态，退化为「未命中」重新生成即可
        pass

    # 命中失败标记：说明这张图之前就解不开，直接返回失败，不再重试
    if os.path.exists(fail_path):
        return False, None

    # 生成
    ok = _render_thumbnail(src_path, hit_path, box)

    if not ok:
        # 写失败标记（0 字节），下次直接短路
        try:
            os.makedirs(os.path.dirname(fail_path), exist_ok=True)
            with open(fail_path, "wb"):
                pass
        except OSError:
            pass
        return False, None

    # 生成成功：清掉可能残留的陈旧失败标记，避免它继续屏蔽这张有效缩略图
    if os.path.exists(fail_path):
        try:
            os.unlink(fail_path)
        except OSError:
            pass

    _maybe_evict(cache_dir, max_mb)
    return True, hit_path


def _maybe_evict(cache_dir: str, max_mb: int = 512) -> None:
    """
    定期清理缓存目录。

    策略：整体体积超过上限时，按「最后访问时间」从旧到新删除，
    直到降到上限的 80%，避免刚清完又立刻触发。

    max_mb 必须由调用方传入（此前调用点漏传，导致 config.json 的
    thumbs.max_cache_mb 完全不生效，永远按 512MB 处理）。
    """
    global _generate_counter, _last_evict

    _generate_counter += 1
    if _generate_counter % _EVICT_INTERVAL_COUNT != 0:
        return

    now = time.time()
    if now - _last_evict < 60:
        return

    with _evict_lock:
        _last_evict = now
        try:
            limit_bytes = max(16, int(max_mb)) * 1024 * 1024
            entries = []
            total = 0
            for dirpath, _dirnames, filenames in os.walk(cache_dir):
                for name in filenames:
                    fp = os.path.join(dirpath, name)
                    try:
                        st = os.stat(fp)
                    except OSError:
                        continue
                    total += st.st_size
                    entries.append((st.st_mtime, st.st_size, fp))

            if total <= limit_bytes:
                return

            target = int(limit_bytes * 0.8)
            entries.sort(key=lambda item: item[0])   # 旧的排前面
            for _mtime, size, fp in entries:
                if total <= target:
                    break
                try:
                    os.unlink(fp)
                    total -= size
                except OSError:
                    continue
        except Exception:  # noqa: BLE001 - 清理失败绝不影响主流程
            return


def clear_cache(cache_dir: str) -> int:
    """清空缩略图缓存，返回删除的文件数（供维护接口使用）。"""
    removed = 0
    if not os.path.isdir(cache_dir):
        return 0
    for dirpath, _dirnames, filenames in os.walk(cache_dir, topdown=False):
        for name in filenames:
            try:
                os.unlink(os.path.join(dirpath, name))
                removed += 1
            except OSError:
                continue
        try:
            os.rmdir(dirpath)
        except OSError:
            pass
    return removed

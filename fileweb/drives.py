# -*- coding: utf-8 -*-
"""
磁盘驱动器枚举
==============

用于「此电脑」视图：把本机所有可访问的盘符自动纳入可浏览范围，
这样插上 U 盘、移动硬盘、挂载新分区之后，重启服务即可看到，不用改配置。

实现要点：
    * 优先使用 Python 3.12+ 的 os.listdrives()，老版本回退到 A-Z 盘符探测；
    * 用 Win32 的 GetDriveTypeW 区分固定盘 / 可移动盘 / 光驱 / 网络盘，
      默认排除光驱（读不到盘时会卡住）与网络盘（可能长时间无响应）；
    * 用 GetVolumeInformationW 取卷标，让界面显示成「本地磁盘 (C:)」这样；
    * 非 Windows 平台退化成「根目录 /」一项，保证代码跨平台可用。
"""

from __future__ import annotations

import ctypes
import os
import string
import sys
from typing import Dict, List, Optional

# Win32 驱动器类型常量
DRIVE_UNKNOWN = 0
DRIVE_NO_ROOT_DIR = 1
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3
DRIVE_REMOTE = 4        # 网络驱动器
DRIVE_CDROM = 5
DRIVE_RAMDISK = 6

# 类型 -> 中文描述
_TYPE_LABEL = {
    DRIVE_REMOVABLE: "可移动磁盘",
    DRIVE_FIXED: "本地磁盘",
    DRIVE_REMOTE: "网络位置",
    DRIVE_CDROM: "光盘驱动器",
    DRIVE_RAMDISK: "内存盘",
    DRIVE_UNKNOWN: "磁盘",
}

_IS_WINDOWS = sys.platform == "win32"


def _get_drive_type(root: str) -> int:
    """取驱动器类型；失败时按「固定盘」处理，保证不会因为 API 异常而漏掉盘符。"""
    if not _IS_WINDOWS:
        return DRIVE_FIXED
    try:
        return int(ctypes.windll.kernel32.GetDriveTypeW(ctypes.c_wchar_p(root)))
    except Exception:  # noqa: BLE001
        return DRIVE_UNKNOWN


def _get_volume_label(root: str) -> str:
    """取卷标（例如「系统」「数据盘」）；没有卷标时返回空串。"""
    if not _IS_WINDOWS:
        return ""
    try:
        name_buffer = ctypes.create_unicode_buffer(261)
        fs_buffer = ctypes.create_unicode_buffer(261)
        ok = ctypes.windll.kernel32.GetVolumeInformationW(
            ctypes.c_wchar_p(root),
            name_buffer,
            len(name_buffer),
            None, None, None,
            fs_buffer,
            len(fs_buffer),
        )
        return name_buffer.value.strip() if ok else ""
    except Exception:  # noqa: BLE001
        return ""


def list_drive_roots() -> List[str]:
    """列出本机存在的盘符根路径，例如 ['C:\\\\', 'D:\\\\']。"""
    roots: List[str] = []

    # Python 3.12+ 自带 os.listdrives()
    listdrives = getattr(os, "listdrives", None)
    if callable(listdrives):
        try:
            return list(listdrives())
        except Exception:  # noqa: BLE001
            pass

    if _IS_WINDOWS:
        for letter in string.ascii_uppercase:
            root = "%s:\\" % letter
            if os.path.exists(root):
                roots.append(root)
    else:
        roots.append("/")

    return roots


def enumerate_drives(
    include_removable: bool = True,
    include_network: bool = False,
) -> List[Dict[str, object]]:
    """
    枚举可浏览的驱动器。

    返回 [{letter, path, name, label, drive_type, type_label, removable, network}, ...]
    顺序固定按盘符字母排序，保证界面顺序稳定。
    """
    result: List[Dict[str, object]] = []

    for root in list_drive_roots():
        drive_type = _get_drive_type(root)
        is_network = drive_type == DRIVE_REMOTE
        is_removable = drive_type == DRIVE_REMOVABLE

        # 光驱默认跳过：没放盘的时候访问会长时间无响应
        if drive_type == DRIVE_CDROM:
            continue
        if is_network and not include_network:
            continue
        if is_removable and not include_removable:
            continue

        letter = root.rstrip("\\/")
        label = _get_volume_label(root)
        type_label = _TYPE_LABEL.get(drive_type, "磁盘")

        if label:
            name = "%s (%s)" % (label, letter)
        elif drive_type == DRIVE_FIXED and letter.upper() == "C:":
            name = "系统盘 (C:)"
        else:
            name = "%s (%s)" % (type_label, letter)

        result.append({
            "letter": letter,
            "path": root,
            "name": name,
            "label": label,
            "drive_type": drive_type,
            "type_label": type_label,
            "removable": is_removable,
            "network": is_network,
        })

    result.sort(key=lambda item: str(item["letter"]).upper())
    return result


def drive_id_for(path: str) -> str:
    """
    由盘符路径生成稳定的根目录 id，例如 'C:\\\\' -> 'drive-c'。
    用 id 而不是盘符本身，是为了让前端的书签/快捷方式在配置调整后依然可用。
    """
    letter = path.rstrip("\\/").replace(":", "").replace("\\", "").replace("/", "")
    return "drive-%s" % letter.lower() if letter else "drive-root"

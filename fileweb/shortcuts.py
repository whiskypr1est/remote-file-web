# -*- coding: utf-8 -*-
"""
虚拟桌面快捷方式存储
====================

需求背景：
    在文件资源管理器里右键文件夹 ->「发送到桌面快捷方式」，
    快捷方式**只出现在 Web 虚拟桌面上**，不会写到 Windows 真实桌面；
    并且服务重启后依然存在。

因此不能用浏览器 localStorage（换台电脑就没了、清缓存就没了），
必须由服务端落盘持久化。这里把数据存到项目目录下的 desktop_shortcuts.json。

存储结构：
    {
      "version": 1,
      "items": [
        {"id": "sc_xxx", "name": "项目资料", "root": "drive-d",
         "path": "Work/项目", "is_dir": true, "created": 1730000000}
      ]
    }

并发安全：
    所有读写都在同一把锁里完成，避免多个请求同时写文件造成内容损坏；
    写入采用「临时文件 + os.replace」的原子替换，断电也不会写坏。
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import threading
import time
from typing import Any, Dict, List, Optional

# 快捷方式数据文件（与 config.json 同目录）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHORTCUTS_PATH = os.path.join(BASE_DIR, "desktop_shortcuts.json")

_LOCK = threading.RLock()

# 名称长度上限，避免界面上出现超长标题
_MAX_NAME_LEN = 80


def _atomic_write(path: str, data: Dict[str, Any]) -> None:
    """原子写 JSON：先写临时文件再替换，避免写一半导致文件损坏。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".shortcuts-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _read_raw() -> Dict[str, Any]:
    """读取原始数据；文件不存在或损坏时返回空结构（不抛异常，保证桌面能正常加载）。"""
    if not os.path.isfile(SHORTCUTS_PATH):
        return {"version": 1, "items": []}
    try:
        with open(SHORTCUTS_PATH, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return {"version": 1, "items": []}

    if not isinstance(data, dict):
        return {"version": 1, "items": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []
    data.setdefault("version", 1)
    return data


def _normalize(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把一条记录规整成统一结构；缺关键字段则丢弃。"""
    if not isinstance(item, dict):
        return None

    root = str(item.get("root") or "").strip()
    rel = str(item.get("path") or "").strip().replace("\\", "/")
    name = str(item.get("name") or "").strip()

    if not root or not name:
        return None

    return {
        "id": str(item.get("id") or ("sc_" + secrets.token_hex(8))),
        "name": name[:_MAX_NAME_LEN],
        "root": root,
        "path": rel,
        "is_dir": bool(item.get("is_dir", True)),
        "created": float(item.get("created") or time.time()),
    }


def list_items() -> List[Dict[str, Any]]:
    """返回所有快捷方式（按创建时间正序，界面上顺序稳定）。"""
    with _LOCK:
        data = _read_raw()
        items = []
        for raw in data["items"]:
            normalized = _normalize(raw)
            if normalized:
                items.append(normalized)
        items.sort(key=lambda entry: entry["created"])
        return items


def add(name: str, root: str, rel: str, is_dir: bool = True) -> Dict[str, Any]:
    """
    新增（或更新同名同路径的）快捷方式。

    同一个 root + path 已存在时不会重复添加，而是直接返回已有项，
    避免用户反复点击产生一堆一模一样的图标。
    """
    clean_name = str(name or "").strip()[:_MAX_NAME_LEN]
    if not clean_name:
        raise ValueError("快捷方式名称不能为空")

    clean_root = str(root or "").strip()
    clean_rel = str(rel or "").strip().replace("\\", "/").strip("/")
    if not clean_root:
        raise ValueError("缺少根目录标识")

    with _LOCK:
        data = _read_raw()
        items = [entry for entry in (_normalize(x) for x in data["items"]) if entry]

        for existing in items:
            if existing["root"] == clean_root and existing["path"] == clean_rel:
                return existing

        item = {
            "id": "sc_" + secrets.token_hex(8),
            "name": clean_name,
            "root": clean_root,
            "path": clean_rel,
            "is_dir": bool(is_dir),
            "created": time.time(),
        }
        items.append(item)
        _atomic_write(SHORTCUTS_PATH, {"version": 1, "items": items})
        return item


def remove(shortcut_id: str) -> bool:
    """删除指定快捷方式，返回是否真的删掉了。"""
    target = str(shortcut_id or "").strip()
    if not target:
        return False

    with _LOCK:
        data = _read_raw()
        items = [entry for entry in (_normalize(x) for x in data["items"]) if entry]
        remaining = [entry for entry in items if entry["id"] != target]

        if len(remaining) == len(items):
            return False

        _atomic_write(SHORTCUTS_PATH, {"version": 1, "items": remaining})
        return True


def rename(shortcut_id: str, new_name: str) -> Optional[Dict[str, Any]]:
    """重命名快捷方式（只改显示名，不影响指向的真实路径）。"""
    clean_name = str(new_name or "").strip()[:_MAX_NAME_LEN]
    if not clean_name:
        raise ValueError("名称不能为空")

    target = str(shortcut_id or "").strip()

    with _LOCK:
        data = _read_raw()
        items = [entry for entry in (_normalize(x) for x in data["items"]) if entry]

        updated = None
        for entry in items:
            if entry["id"] == target:
                entry["name"] = clean_name
                updated = entry
                break

        if updated is None:
            return None

        _atomic_write(SHORTCUTS_PATH, {"version": 1, "items": items})
        return updated


def clear() -> int:
    """清空全部快捷方式，返回删除数量。"""
    with _LOCK:
        data = _read_raw()
        count = len([1 for x in data["items"] if _normalize(x)])
        _atomic_write(SHORTCUTS_PATH, {"version": 1, "items": []})
        return count

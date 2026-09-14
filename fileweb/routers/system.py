# -*- coding: utf-8 -*-
"""
系统信息路由
============

    GET  /api/system/info          —— 桌面初始化所需的一切元信息
    POST /api/system/soffice/rescan —— 重新探测 LibreOffice（装完后不用重启服务）

前端用 /api/system/info 来：
    * 生成「此电脑」里的根目录列表
    * 在任务栏托盘显示服务器 IP 与时间
    * 决定是否显示「Office 预览不可用」的提示
    * 展示「关于」对话框里的版本信息
"""

from __future__ import annotations

import json
import os
import platform
import socket
import sys
import time
from typing import Any, Dict, List

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .. import APP_NAME, __version__
from ..config import BASE_DIR
from ..deps import (
    feature_allowed_for,
    get_state,
    get_user,
    local_ip_addresses,
    resolver_of,
)
from ..office import find_soffice, reset_soffice_cache

router = APIRouter(prefix="/api/system", tags=["系统"])

# 前端离线资源的版本清单（由 tools/fetch_vendor.py 生成）
_VENDOR_MANIFEST = os.path.join(BASE_DIR, "static", "vendor", "VERSIONS.json")


def _read_vendor_versions() -> Dict[str, str]:
    """读取前端离线资源版本号，读不到就返回空字典（不影响功能）。"""
    try:
        with open(_VENDOR_MANIFEST, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        pass
    return {}


def _recycle_bin_available() -> bool:
    """检测回收站删除能力是否可用（依赖 send2trash）。"""
    try:
        import send2trash  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


@router.get("/info")
async def system_info(request: Request) -> Dict[str, Any]:
    """桌面初始化元信息。"""
    state = get_state(request)
    cfg = state.cfg
    server_cfg = cfg.get("server") or {}
    auth_cfg = cfg.get("auth") or {}
    upload_cfg = cfg.get("upload") or {}
    office_cfg = cfg.get("office") or {}
    ui_cfg = cfg.get("ui") or {}

    soffice = find_soffice(office_cfg.get("soffice_path") or "") if office_cfg.get("enabled", True) else None

    # ★ 多用户：这段信息要**按当前登录的人**给，而不是按 config 里的 auth 段。
    #   config 的用户名只在首次引导时用过一次，之后谁是管理员由用户表说了算。
    account = get_user(request)
    is_admin_user = str(account.get("role") or "") == "admin"

    def _allowed(key: str, cfg_enabled: bool) -> bool:
        """
        功能对这个用户是否开放 = 配置里开着 **且** 这个人没被单独关掉。

        ★ 判断走 deps.feature_allowed_for，与 routers/terminal.py、
          routers/sysmon.py 里那道 403 是**同一份实现** —— 两边跑偏就会出现
          「界面藏了入口、接口却照收」，或者反过来「入口在、点了就 403」。
        """
        return bool(cfg_enabled) and feature_allowed_for(account, key)

    return {
        "ok": True,
        "app": {
            "name": APP_NAME,
            "version": __version__,
            "title": server_cfg.get("title") or APP_NAME,
        },
        "server": {
            "hostname": socket.gethostname(),
            "ips": local_ip_addresses(),
            "platform": "%s %s" % (platform.system(), platform.release()),
            "python": platform.python_version(),
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "timestamp": time.time(),
            "uptime_seconds": int(time.time() - state.started_at),
        },
        "user": {
            "username": account.get("username") or "",
            "display_name": (account.get("display_name")
                             or account.get("username") or ""),
            "role": account.get("role") or "user",
            # 前端据此决定是否显示「用户管理」等管理员入口
            "is_admin": is_admin_user,
        },
        "roots": resolver_of(request).public_list(),
        "ui": {
            "wallpaper": ui_cfg.get("wallpaper") or "",
            "default_view": ui_cfg.get("default_view") or "icons",
        },
        "limits": {
            "max_upload_mb": int(upload_cfg.get("max_file_size_mb") or 2048),
            "blocked_extensions": list(upload_cfg.get("blocked_extensions") or []),
            "session_hours": int(auth_cfg.get("session_hours") or 12),
            # 命令行输出在浏览器侧的保留上限（KB），前端据此裁剪滚动缓冲
            "terminal_output_kb": int((cfg.get("terminal") or {}).get("max_output_kb") or 512),
            # 任务管理器：把配置里的刷新间隔与进程条数下发给前端，
            # 免得界面自己写死一套、与 config.json 对不上
            "sysmon_refresh_seconds": float((cfg.get("sysmon") or {}).get("refresh_seconds") or 2),
            "sysmon_top_n": int((cfg.get("sysmon") or {}).get("top_n") or 30),
        },
        "features": {
            # 是否装了 LibreOffice：没装的话 doc/xls/ppt 只能降级预览或提示
            "libreoffice": bool(soffice),
            "recycle_bin": bool((cfg.get("delete") or {}).get("use_recycle_bin", True)) and _recycle_bin_available(),
            "thumbnails": bool((cfg.get("thumbs") or {}).get("enabled", True)),
            # 命令行功能：默认开启，前端据此决定是否显示入口
            "terminal": _allowed("terminal",
                                 bool((cfg.get("terminal") or {}).get("enabled", True))),
            # 任务管理器（只读系统监控）：同样据此决定开始菜单里是否出现入口
            "sysmon": _allowed("sysmon",
                               bool((cfg.get("sysmon") or {}).get("enabled", True))),
            # ★ 用户管理（用户列表 / 在线情况 / 审计日志）：**仅管理员**。
            #   前端据此决定开始菜单里是否出现入口 —— 子用户看不到它。
            "users": is_admin_user,
            "text_encodings": ["utf-8", "gb18030", "big5", "latin-1"],
        },
        "versions": _read_vendor_versions(),
    }


@router.post("/soffice/rescan")
async def rescan_soffice(request: Request) -> Dict[str, Any]:
    """
    重新探测 LibreOffice。

    场景：用户看完提示后去装了 LibreOffice，不想重启服务。
    探测缓存由 office.reset_soffice_cache() 清空。
    """
    state = get_state(request)
    office_cfg = (state.cfg.get("office") or {})

    reset_soffice_cache()
    found = find_soffice(office_cfg.get("soffice_path") or "")

    return {
        "ok": True,
        "libreoffice": bool(found),
        "path": found or "",
        "message": ("已找到 LibreOffice：%s" % found) if found
                   else "仍未检测到 LibreOffice，请确认已安装并重启过命令行窗口。",
    }


# ---------------------------------------------------------------------------
# 桌面壁纸
# ---------------------------------------------------------------------------

# 壁纸保存目录：放在 static 下，浏览器可以直接按 URL 取用
WALLPAPER_DIR = os.path.join(BASE_DIR, "static", "wallpapers")

# 允许的壁纸格式与体积上限
WALLPAPER_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}
WALLPAPER_MAX_BYTES = 20 * 1024 * 1024


def _safe_unlink(path: str) -> None:
    """删除单个文件，失败忽略（清理逻辑不能影响主流程）。"""
    try:
        if os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


def _clear_wallpaper_files(keep: str = "") -> None:
    """清空壁纸目录（保留 keep 指定的那一个），避免换壁纸后残留垃圾文件。"""
    if not os.path.isdir(WALLPAPER_DIR):
        return
    keep_name = os.path.basename(keep) if keep else ""
    for name in os.listdir(WALLPAPER_DIR):
        if name == keep_name:
            continue
        _safe_unlink(os.path.join(WALLPAPER_DIR, name))


@router.post("/wallpaper")
async def upload_wallpaper(request: Request, filename: str = "") -> Dict[str, Any]:
    """
    上传自定义桌面壁纸。

    与文件上传一致，使用「原始请求体 + filename 查询参数」的方式流式落盘，
    成功后把 URL 写进 config.json 的 ui.wallpaper 并立即生效。
    """
    state = get_state(request)
    cfg = state.cfg

    raw_name = filename or request.headers.get("x-file-name") or ""
    if not raw_name:
        raise HTTPException(status_code=400, detail="缺少文件名参数 filename")

    try:
        from urllib.parse import unquote

        raw_name = unquote(raw_name)
    except Exception:  # noqa: BLE001
        pass

    extension = os.path.splitext(raw_name)[1].lower()
    if extension not in WALLPAPER_EXTENSIONS:
        raise HTTPException(
            status_code=400,
            detail="壁纸仅支持 %s 格式" % "、".join(sorted(WALLPAPER_EXTENSIONS)),
        )

    # 快速失败：先看 Content-Length
    declared = request.headers.get("content-length")
    if declared:
        try:
            if int(declared) > WALLPAPER_MAX_BYTES:
                raise HTTPException(status_code=413, detail="壁纸文件不能超过 20MB")
        except ValueError:
            pass

    try:
        os.makedirs(WALLPAPER_DIR, exist_ok=True)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="无法创建壁纸目录：%s" % exc)

    # 固定文件名（custom + 扩展名），覆盖式保存
    target = os.path.join(WALLPAPER_DIR, "custom" + extension)
    part_path = target + ".part"
    written = 0

    try:
        with open(part_path, "wb") as fh:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > WALLPAPER_MAX_BYTES:
                    raise HTTPException(status_code=413, detail="壁纸文件不能超过 20MB")
                await run_in_threadpool(fh.write, chunk)

        if written == 0:
            raise HTTPException(status_code=400, detail="上传内容为空")

        os.replace(part_path, target)
    except HTTPException:
        _safe_unlink(part_path)
        raise
    except Exception as exc:  # noqa: BLE001
        _safe_unlink(part_path)
        raise HTTPException(status_code=500, detail="保存壁纸失败：%s" % exc)

    # 清掉旧格式的壁纸文件
    _clear_wallpaper_files(keep=target)

    url = "/static/wallpapers/" + os.path.basename(target)
    cfg.setdefault("ui", {})["wallpaper"] = url
    state.persist()

    return {
        "ok": True,
        "wallpaper": url,
        "size": written,
        "message": "壁纸已更新",
    }


@router.post("/wallpaper/reset")
async def reset_wallpaper(request: Request) -> Dict[str, Any]:
    """恢复内置的蓝色渐变壁纸。"""
    state = get_state(request)
    cfg = state.cfg

    _clear_wallpaper_files()
    cfg.setdefault("ui", {})["wallpaper"] = ""
    state.persist()

    return {"ok": True, "wallpaper": "", "message": "已恢复默认壁纸"}

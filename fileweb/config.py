# -*- coding: utf-8 -*-
"""
配置模块
========

负责 config.json 的读取、默认值补全、原子保存，以及首次运行时自动生成配置。

设计原则：
    * 代码里带一份完整的默认配置（DEFAULT_CONFIG），
      配置文件中缺字段时自动用默认值补齐，便于版本升级时新增配置项；
    * 密码只以 PBKDF2 哈希形式保存；若用户在配置里直接写了明文 password，
      服务会正常使用但会在启动时高亮警告，并提示改用哈希；
    * 所有相对路径（缓存目录等）都以项目根目录为基准解析成绝对路径，
      避免因为工作目录不同而把缓存写到莫名其妙的地方。
"""

from __future__ import annotations

import copy
import json
import os
import sys
import tempfile
from typing import Any, Dict, Optional

from .security import hash_password, random_password, random_secret

# 项目根目录（app.py 所在目录）
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
EXAMPLE_CONFIG_PATH = os.path.join(BASE_DIR, "config.example.json")
FIRST_RUN_PASSWORD_PATH = os.path.join(BASE_DIR, "FIRST_RUN_PASSWORD.txt")

# ---------------------------------------------------------------------------
# 默认配置
# ---------------------------------------------------------------------------

DEFAULT_CONFIG: Dict[str, Any] = {
    "server": {
        # 0.0.0.0 表示监听本机所有网卡，局域网内其他电脑才能访问
        "host": "0.0.0.0",
        "port": 8000,
        "title": "远程文件管理",
    },

    # 允许访问的根目录列表。可以配置多个（多盘符 / 多目录）。
    # id 是前端使用的稳定标识，改名不会导致前端书签失效。
    "roots": [
        {"id": "share", "name": "共享目录", "path": "D:\\Share", "readonly": False},
    ],

    # 自动把本机所有磁盘（C:/D:/E: …）挂载为可访问根目录。
    # 打开后「此电脑」里能直接看到所有盘符；以后插入 U 盘/移动硬盘，
    # 重启服务也会自动出现，不用再改配置。
    "mount_all_drives": True,
    # 是否也挂载网络驱动器（网络盘无响应时可能让界面卡顿，默认关闭）
    "mount_network_drives": False,
    # 是否挂载可移动磁盘（U 盘、移动硬盘）
    "mount_removable_drives": True,

    # 受保护路径：允许浏览，但禁止新建/重命名/删除/上传。
    # 开放整盘访问后这道保险很有用 —— 误删这些目录可能让 Windows 直接起不来。
    # 想完全放开就把它改成空数组 []。
    "protected_paths": [
        "C:\\Windows",
        "C:\\Program Files",
        "C:\\Program Files (x86)",
        "C:\\ProgramData",
        "C:\\$Recycle.Bin",
        "C:\\System Volume Information",
        "C:\\Recovery",
        "C:\\PerfLogs",
    ],

    "auth": {
        "username": "admin",
        # 优先使用 password_hash（PBKDF2）。空字符串表示尚未设置。
        "password_hash": "",
        # 兼容项：若只想图省事直接写明文，可以填这里，但不推荐。
        "password": "",
        # 会话签名密钥，首次生成配置时自动随机生成
        "session_secret": "",
        "session_hours": 12,
        # 登录失败锁定策略
        "max_login_fails": 5,
        "lockout_seconds": 300,
        # ★ 可信反向代理列表。X-Forwarded-For 是客户端可随意伪造的头，
        # 只有请求的直连对端 IP 命中这个列表时才采信它来判定客户端 IP
        # （登录失败锁定按该 IP 计数）。默认空数组 = 完全不信任该头。
        # 若前面挂了 Nginx/Caddy，把代理所在机器的 IP 填进来，
        # 支持单个 IP（10.0.0.5）或网段（10.0.0.0/8）。
        "trusted_proxies": [],
    },

    "ui": {
        # 空字符串 = 使用内置蓝色渐变壁纸；
        # 也可以填图片 URL（例如 /static/wallpapers/xxx.jpg）
        "wallpaper": "",
        "default_view": "icons",      # icons | list
    },

    # 虚拟桌面的界面状态（窗口位置/大小、当前目录、视图模式）落盘位置。
    # 前端内存里那份布局刷新一次就没了，所以由服务端持久化一份**不透明**的
    # JSON 文档（服务端不解释其中任何字段），关掉浏览器再打开就能恢复原样。
    # 与 desktop_shortcuts.json 同目录，方便一起备份/清理。
    #
    # ★ 多用户：这两个值是**基础路径**，也就是管理员用的那一份（沿用原名，
    #   升级前后完全不变）。子用户各自落在同目录的
    #   user_state.<用户名>.json / desktop_shortcuts.<用户名>.json，
    #   否则多人共用一个文件会互相覆盖对方桌面。见 fileweb/peruser.py。
    "user_state_path": "user_state.json",

    # 桌面快捷方式文件位置。与 user_state_path 同样解析（相对路径以配置文件
    # 所在目录为基准），同样只是「基础路径」。
    "desktop_shortcuts_path": "desktop_shortcuts.json",

    # ★ 审计日志：登录成功/失败、登出、改密码、管理员改账号等**账号层面的动作**
    # 落盘留痕（一行一个 JSON，见 fileweb/audit.py）。
    # 不记录文件浏览、上传下载这些高频动作 —— 那会把日志淹掉，而且真实的
    # 取证需求集中在账号动作上。写失败绝不影响业务（磁盘满也不该让人登不进来）。
    "audit": {
        "path": "audit.log.jsonl",
        # 单个文件的体积上限，超出后轮转成 audit.log.jsonl.1 / .2 / …
        "max_bytes": 4 * 1024 * 1024,
        # 保留几个历史文件（0 = 不保留，直接丢弃旧的）
        "backups": 3,
    },

    "upload": {
        "max_file_size_mb": 2048,     # 单文件上限 2GB
        "blocked_extensions": [
            ".exe", ".com", ".scr", ".pif", ".cpl", ".msi", ".msp",
            ".bat", ".cmd", ".ps1", ".psm1", ".psd1", ".vbs", ".vbe",
            ".wsf", ".wsh", ".hta", ".reg", ".inf", ".msc", ".sct",
            ".dll", ".sys", ".drv", ".ocx", ".gadget", ".job", ".lnk",
            ".appx", ".msix", ".appxbundle",
        ],
    },

    "delete": {
        # True = 删除到回收站（更安全，可恢复）；False = 永久删除
        "use_recycle_bin": True,
    },

    # ★ 虚拟桌面里的「命令提示符」窗口
    # 安全警告：这等于把服务器的命令行交给任何能登录本服务的人。
    # 以 Windows 服务（NSSM）方式运行时进程身份是 SYSTEM，即最高权限。
    # 不需要这个功能时把 enabled 改成 false（改完重启服务生效）。
    "terminal": {
        "enabled": True,
        # 要启动的 shell，也可以换成 powershell.exe
        "shell": "cmd.exe",
        # ★ 每人同时允许打开的命令行会话数上限（多用户：按用户计，不是全机）。
        # 用户表里的 max_terminal_sessions 是「这个人的额度」（默认 5），
        # 本项是「全站政策上限」，两者取小生效（见 routers/terminal.py）。
        # 想给某人多开，把这里也一起调大。
        "max_sessions": 5,
        # ★ 全机命令行会话总数上限（0 = 不设限）。只防「每人 5 个 × 很多人」
        # 把机器拖垮；真的触顶时会顶掉闲置最久的分离会话，否则才拒绝新建。
        "max_sessions_total": 64,
        # 空闲多久自动关闭会话（秒），0 = 不限制。
        # ★ 多用户下定为 0（用户决定）：学生关掉浏览器后 cmd 一直留着，
        # 方便回来接着看输出；总量由上面的每用户名額与全机上限管住，
        # 不再靠「半小时后自动回收」兜底。
        "idle_timeout_seconds": 0,
        # 单个会话在浏览器侧保留的输出上限（KB）
        "max_output_kb": 512,
        # ★ 分离会话至少闲置多久才允许被「挤掉」以腾出 max_sessions 名额（秒）。
        # 「分离」指的是浏览器已经断开、但进程还在跑（等用户重连）的会话。
        # 这些会话会一直占着名额，本项决定「占多久之后可以被新开的命令行挤掉」。
        # 设得太短会让「刷新页面」这种瞬时重连有被误杀的风险，
        # 设得太长则被彻底遗弃的窗口会长期占着名额。20 秒是两者的折中。
        "evict_grace_seconds": 20,
        # 启动目录，留空 = 第一个可访问的根目录
        "start_dir": "",
    },

    # ★ 虚拟桌面内置的音乐播放器
    # 曲库是**文件系统里的目录**（不是数据库）：导入 = 把文件复制进去，
    # 所以歌曲在文件管理器里看得见、能备份、能直接用别的播放器打开。
    "music": {
        "enabled": True,
        # 曲库根目录。相对路径以**本配置文件所在目录**为基准（与状态文件同一套规则）。
        "library_dir": "music",
        # true = 每个用户一个子目录（<library_dir>/<用户名>）；
        # false = 全机共用一个曲库（所有人看到同一批歌）。
        # 默认每人一份，与「每个子用户只看到自己的文件夹」保持一致。
        "per_user": True,
        # 单曲上限（导入与上传都按它算）。默认 200MB，无损音频也放得下。
        "max_upload_mb": 200,
        # 歌单与播放偏好的存储位置（同样相对本文件解析，按用户分文件）。
        "state_path": "music_state.json",
    },

    # ★ 虚拟桌面内置的「照片」应用（时间轴相册）
    # 与音乐播放器最大的不同：照片**就地索引**，不复制进库 —— 照片库动辄几十
    # 上百 GB，复制一份不可接受。代价是文件可能被移走/改名/删除，所以每条索引
    # 都存一个「快速指纹」（文件前 256KB + 大小），重扫时按指纹把用户编辑过的
    # 时间/地点重新接回去（见 fileweb/photos.py）。
    "photos": {
        "enabled": True,
        # ★ 用户编辑的时间 / 地点 / 标签都落在这里，**按用户分文件**。
        #   这是唯一的用户数据真相：索引坏了可以重建，它绝不能丢
        #   （所以 index_path 与它是两个文件，重建索引永远不动用户数据）。
        "state_path": "photos_state.json",
        # 派生缓存（指纹 / EXIF / 尺寸），删掉重扫即可重建。
        "index_path": "photo_index.json",
        # 画廊缩略图边长。比资源管理器的 100 大得多才像相册；
        # ★ 缓存键里带着这个尺寸，所以与 thumbs.size 共用同一个缓存目录
        #   也不会互相覆盖。
        "thumb_size": 320,
        # 一次导入最多索引多少张。防止误选一个几十万文件的盘把服务占住。
        "max_import": 20000,
        # 单次 /library 最多返回多少条。几千张的规模下一次取全没问题，
        # 这个上限是防止异常大的库把浏览器打爆。
        "max_items": 5000,
        # ★ 从**浏览器所在的电脑**上传进来的照片落在哪里。
        #   照片本身是「就地索引」的（不复制），但从别的机器上传时总得有个
        #   落点，所以单独给一个目录。相对路径以本配置文件所在目录为基准。
        "upload_dir": "photos_uploads",
        # true = 每人一个子目录（<upload_dir>/<用户名>）。
        # ★ 与曲库同样的理由：不分开的话，同学上传的照片会混在一个目录里，
        #   而且互相看得到文件名。
        "upload_per_user": True,
        # 单张上传的大小上限（MB）。手机原图一般 3~10MB，留足余量。
        "max_upload_mb": 200,
    },

    # ★ 桌面歌词（局域网转发服务）
    # 网页播放器把「现在在放什么」上报给服务端，服务端再广播给桌面悬浮窗
    # 客户端（desktop-lyrics/），于是关掉浏览器也能继续看歌词。
    # 状态**只在内存里**，重启即忘 —— 这本来就是实时状态而不是数据
    # （对比：曲库与歌单是数据，所以它们落盘）。因此这里没有任何路径配置项。
    "lyrics": {
        "enabled": True,
        # 多久没收到上报就认为「那个源已经不在了」（秒）。
        # 播放器播放时每 0.5 秒报一次、暂停时每 5 秒报一次心跳，
        # 所以默认 10 秒既容忍网络抖动，又能在一关掉浏览器后就让歌词消失。
        "stale_seconds": 10,
        # ★ 预留的鉴权扩展点：留空 = 不校验（局域网内互相信任的默认部署）。
        # 配成任意字符串后，上报与订阅都必须带上同一个 token
        # （?token=xxx 或上报体里的 "token" 字段），悬浮窗在托盘菜单里填。
        # 详见 fileweb/routers/lyrics.py 头部的安全说明。
        "token": "",
    },

    "thumbs": {
        "enabled": True,
        "cache_dir": "./thumb_cache",
        "size": 100,                  # 缩略图边长（像素）
        "max_cache_mb": 512,          # 缓存目录体积上限，超出后按最旧优先清理
    },

    "office": {
        "enabled": True,
        # 留空则自动探测 LibreOffice；也可以写死 soffice.exe 的完整路径
        "soffice_path": "",
        "timeout_seconds": 120,
        "cache_dir": "./office_cache",
        # 转换结果（PDF）缓存体积上限（MB），超出后按最旧优先清理。
        # 此前完全没有淘汰机制，缓存只增不减。
        "max_cache_mb": 1024,
    },

    "archive": {
        # 解压时的安全上限。压缩包内部条目名是外部可控的，
        # 所以这几个值同时也是「解压炸弹」的防线，别随意调得太大。
        "max_entries": 20000,         # 单个压缩包最多多少个条目
        "max_total_mb": 8192,         # 解压后总体积上限（MB）
        "max_single_mb": 4096,        # 解压后单个文件上限（MB）
        # RAR 需要 WinRAR 的命令行工具。留空则自动探测
        # C:\Program Files\WinRAR\ 下的 Rar.exe / UnRAR.exe。
        # 7z 走 py7zr，纯 Python，不依赖外部程序。
        "rar_path": "",
        "unrar_path": "",
    },

    # ★ 虚拟桌面里的「任务管理器」窗口（只读监控：CPU / 内存 / 网络 / GPU / 进程）
    "sysmon": {
        # 是否启用。它**只读**，不提供结束进程的能力；但进程列表会暴露
        # 服务器上正在跑哪些程序（含所属用户名），不需要这个信息面就关掉。
        "enabled": True,
        # 一次最多返回多少个进程（按 CPU 或内存排序后取前 N）
        "top_n": 30,
        # 前端自动刷新的默认间隔（秒）。只影响界面默认值，
        # 真正的最小采集间隔由下面的 min_interval_seconds 决定。
        "refresh_seconds": 2,
        # 服务端两次**真正采集**之间的最小间隔（秒）。多个浏览器同时开着
        # 任务管理器时，靠它可以避免各自触发一次完整的进程枚举。
        "min_interval_seconds": 0.5,
    },

    # ★ 按文件名搜索（开始菜单搜索框用）
    # 这是**全盘递归遍历**，所以下面几道刹车不是可选项：任一触发都会带着
    # 已有结果返回并标记 truncated，而不是让一个请求把服务占住好几分钟。
    "search": {
        # 一次最多返回多少条结果（前端可用 limit 覆盖，硬上限 500）
        "max_results": 100,
        # 扫过多少个条目就收工，防止在超大目录树里空转
        "max_scanned": 200000,
        # 墙钟时间预算（秒）。到点带着已有结果返回
        "timeout_seconds": 8,
    },

    "preview": {
        # 文本预览单次最多读取多少 KB（防止打开几个 GB 的日志卡死浏览器）
        "text_max_kb": 2048,
    },

    "log": {
        "level": "info",              # debug | info | warning | error
    },
}


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------

def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """
    递归合并配置：以 base 为骨架，用 override 覆盖。
    override 中未出现的新字段会保留 base 的默认值（便于版本升级）。
    """
    result = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def abs_from_base(path_text: str) -> str:
    """把配置里的相对路径按项目根目录解析成绝对路径。"""
    if not path_text:
        return BASE_DIR
    if os.path.isabs(path_text):
        return os.path.normpath(path_text)
    return os.path.normpath(os.path.join(BASE_DIR, path_text))


def _atomic_write_json(path: str, data: Dict[str, Any]) -> None:
    """原子写 JSON：先写临时文件再 replace，避免写一半断电导致配置损坏。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=directory)
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


# ---------------------------------------------------------------------------
# 首次运行：生成配置
# ---------------------------------------------------------------------------

def _build_fresh_config() -> Dict[str, Any]:
    """
    生成一份全新的配置（含随机口令与会话密钥），
    同时把明文口令写到 FIRST_RUN_PASSWORD.txt，方便用户首次登录。
    """
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    password = random_password(16)

    cfg["auth"]["password_hash"] = hash_password(password)
    cfg["auth"]["password"] = ""
    cfg["auth"]["session_secret"] = random_secret(32)

    return cfg, password


def _write_first_run_password(username: str, password: str, path: str) -> None:
    """
    把首次生成的账号口令写到文件里，并尽量收紧权限。

    path 必须由调用方给出（**不能**在这里读模块常量 FIRST_RUN_PASSWORD_PATH）：
    那个常量锚在 BASE_DIR，也就是「代码所在目录」，跟当前用的是哪份配置无关。
    于是只要有人拿一份「没有口令的临时配置」跑一次 prepare()，
    就会把真实部署旁边的口令文件覆盖掉 —— 实测发生过一次。
    现在由调用方按「当前配置文件所在目录」决定位置，裸调 prepare() 干脆不写。
    """
    content = (
        "本文件由服务首次启动时自动生成，记录了初始登录信息。\n"
        "登录成功并确认无误后，建议删除本文件。\n"
        "\n"
        "用户名：%s\n"
        "密  码：%s\n"
        "\n"
        "修改密码：python tools/gen_password.py --set\n"
    ) % (username, password)
    try:
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
    except OSError:
        pass


def _password_file_beside(cfg_path: str) -> str:
    """
    口令文件放在「当前使用的配置文件」旁边。

    默认部署下 CONFIG_PATH 就在 BASE_DIR 里，所以路径与以前完全一致，
    用户看不出区别；只有用别的配置文件跑时才落到那个文件旁边，
    不会再去污染真实部署。
    """
    if not cfg_path:
        return ""
    folder = os.path.dirname(os.path.abspath(cfg_path))
    return os.path.join(folder or BASE_DIR, "FIRST_RUN_PASSWORD.txt")


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def load(path: Optional[str] = None, create_if_missing: bool = True) -> Dict[str, Any]:
    """
    读取配置。

    文件不存在时（且允许创建）会用随机口令生成一份新配置，
    并把明文口令写入 FIRST_RUN_PASSWORD.txt 同时在控制台醒目提示。
    """
    cfg_path = path or CONFIG_PATH

    if not os.path.exists(cfg_path):
        if not create_if_missing:
            return copy.deepcopy(DEFAULT_CONFIG)

        fresh, password = _build_fresh_config()
        # 首次生成时也要把来源路径记上，否则紧接着的 persist()（例如用户
        # 立刻去改密码）会写回模块默认路径，而那是另一个文件。
        fresh["_cfg_path"] = cfg_path
        _atomic_write_json(cfg_path, fresh)
        _write_first_run_password(fresh["auth"]["username"], password,
                                  _password_file_beside(cfg_path))

        banner = (
            "\n" + "=" * 64 + "\n"
            "  已生成新配置文件: %s\n"
            "  初始用户名: %s\n"
            "  初始密  码: %s\n"
            "\n"
            "  该密码同时保存在: FIRST_RUN_PASSWORD.txt\n"
            "  请登录后尽快使用 python tools/gen_password.py --set 修改。\n"
            + "=" * 64 + "\n"
        ) % (cfg_path, fresh["auth"]["username"], password)
        print(banner)

        return prepare(fresh, cfg_path)

    with open(cfg_path, "r", encoding="utf-8-sig") as fh:
        try:
            user_cfg = json.load(fh)
        except json.JSONDecodeError as exc:
            raise SystemExit(
                "配置文件解析失败: %s\n位置: 第 %d 行第 %d 列\n"
                "提示：请检查 JSON 格式（多余的逗号、缺少引号等）。"
                % (cfg_path, exc.lineno, exc.colno)
            )

    if not isinstance(user_cfg, dict):
        raise SystemExit("配置文件根节点必须是一个 JSON 对象: %s" % cfg_path)

    # 记录用户真正写了哪些字段，用于「是否配置过密码」的判断
    raw_auth = user_cfg.get("auth") or {}
    merged = _deep_merge(DEFAULT_CONFIG, user_cfg)
    merged["_raw_auth_keys"] = sorted(raw_auth.keys())

    # ★ 记住这份配置是从哪个文件读出来的。
    #   persist()（改壁纸、改密码）必须写回**同一个文件**；cfg 被传给
    #   create_app() 之后路径就丢了，只能靠这里带着走。
    #   不带这一项时，用 --config 起的实例会把真实部署的 config.json 覆盖掉。
    merged["_cfg_path"] = cfg_path

    return prepare(merged, cfg_path)


def save(cfg: Dict[str, Any], path: Optional[str] = None) -> None:
    """
    保存配置（会自动剔除内部字段）。

    ★ 目标路径的优先级：显式参数 > 配置里记着的来源路径 > 模块默认。

    中间那一档不是可有可无的：`deps.AppState.persist()`（改壁纸、改密码）
    拿到的是 `load()` 读出来的那份 cfg，因此必须写回**它被读出来的那个文件**。
    少了这一档，用 `--config 临时配置` 起一个实例时，persist() 会写到模块默认
    的 config.json 上 —— 也就是**真实部署的那一份**。这不是假想：
    测试里起的服务就是这么把真实 config.json 覆盖成测试配置的
    （端口变成随机值、标题变成 fileweb-test），服务一重启就再也访问不到。
    """
    cfg_path = path or cfg.get("_cfg_path") or CONFIG_PATH
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    _atomic_write_json(cfg_path, clean)


def prepare(cfg: Dict[str, Any], cfg_path: str = "") -> Dict[str, Any]:
    """
    配置后处理：
      * 兜底补齐 auth 里的密钥（老配置可能没有）
      * 把所有缓存目录解析成绝对路径
      * 校验根目录配置，去掉明显无效的项

    cfg_path 是「当前正在处理的配置文件」的路径，只有 load() 知道。
    它决定了首次运行口令文件写到哪里：**留空就不写**。
    这一点很关键 —— prepare() 会被测试和诊断脚本用临时配置直接调用，
    如果那时还去写到 BASE_DIR 旁边的口令文件，就会把真实部署的初始口令
    覆盖掉（实测发生过）。所以这里的原则是：
    「没有配置文件上下文，就不产生文件系统副作用」。
    """
    auth = cfg.setdefault("auth", {})
    if not auth.get("session_secret"):
        auth["session_secret"] = random_secret(32)

    # 口令兜底：既没有 hash 也没有明文时，按首次运行逻辑生成一个
    if not auth.get("password_hash") and not auth.get("password"):
        password = random_password(16)
        auth["password_hash"] = hash_password(password)
        # 只有确实在处理某个配置文件时才落盘（见本函数 docstring）
        password_file = _password_file_beside(cfg_path)
        if password_file:
            _write_first_run_password(auth.get("username") or "admin", password,
                                      password_file)
        print("\n[!] 配置中未设置任何口令，已自动生成初始密码。")
        print("    用户名: %s" % (auth.get("username") or "admin"))
        print("    密  码: %s" % password)
        if password_file:
            print("    已写入: %s\n" % password_file)
        else:
            print("    （未指定配置文件位置，本次不落盘；"
                  "正常启动服务时会写到 config.json 旁边）\n")

    # 数值字段兜底，防止配置里写成字符串导致运行期出错
    try:
        auth["session_hours"] = max(1, int(auth.get("session_hours") or 12))
    except (TypeError, ValueError):
        auth["session_hours"] = 12
    try:
        auth["max_login_fails"] = max(0, int(auth.get("max_login_fails") or 5))
    except (TypeError, ValueError):
        auth["max_login_fails"] = 5
    try:
        auth["lockout_seconds"] = max(0, int(auth.get("lockout_seconds") or 300))
    except (TypeError, ValueError):
        auth["lockout_seconds"] = 300

    # 可信反向代理：必须是字符串列表，非列表一律退回空（= 不信任 X-Forwarded-For）
    if not isinstance(auth.get("trusted_proxies"), list):
        auth["trusted_proxies"] = list(DEFAULT_CONFIG["auth"]["trusted_proxies"])

    server = cfg.setdefault("server", {})
    try:
        server["port"] = int(server.get("port") or 8000)
    except (TypeError, ValueError):
        server["port"] = 8000
    server.setdefault("host", "0.0.0.0")

    # 缓存目录 -> 绝对路径
    thumbs = cfg.setdefault("thumbs", {})
    thumbs["cache_dir"] = abs_from_base(thumbs.get("cache_dir") or "./thumb_cache")
    try:
        thumbs["size"] = max(32, min(512, int(thumbs.get("size") or 100)))
    except (TypeError, ValueError):
        thumbs["size"] = 100

    office = cfg.setdefault("office", {})
    office["cache_dir"] = abs_from_base(office.get("cache_dir") or "./office_cache")
    try:
        office["timeout_seconds"] = max(10, int(office.get("timeout_seconds") or 120))
    except (TypeError, ValueError):
        office["timeout_seconds"] = 120
    try:
        office["max_cache_mb"] = max(64, int(office.get("max_cache_mb") or 1024))
    except (TypeError, ValueError):
        office["max_cache_mb"] = 1024

    # 命令提示符窗口：补齐缺省字段，数值字段做范围兜底
    terminal = cfg.setdefault("terminal", {})
    terminal["enabled"] = bool(terminal.get("enabled", DEFAULT_CONFIG["terminal"]["enabled"]))
    terminal["shell"] = str(terminal.get("shell") or DEFAULT_CONFIG["terminal"]["shell"])
    for key, default, low in (
        ("max_sessions", 5, 1),
        ("max_sessions_total", 64, 0),
        ("idle_timeout_seconds", 0, 0),
        ("max_output_kb", 512, 32),
        ("evict_grace_seconds", 20, 0),
    ):
        raw = terminal.get(key)
        try:
            terminal[key] = max(low, int(default if raw is None else raw))
        except (TypeError, ValueError):
            terminal[key] = default
    terminal["start_dir"] = str(terminal.get("start_dir") or "")

    preview = cfg.setdefault("preview", {})
    try:
        preview["text_max_kb"] = max(16, int(preview.get("text_max_kb") or 2048))
    except (TypeError, ValueError):
        preview["text_max_kb"] = 2048

    # 状态文件（界面状态 / 桌面快捷方式）-> 绝对路径。
    #
    # ★ 相对路径的基准是**当前配置文件所在目录**，而不是代码目录（BASE_DIR）。
    #   这与上面 FIRST_RUN_PASSWORD.txt 的处置是同一条教训，而且后果更重：
    #   状态文件存的是用户真实的桌面布局，一旦被「拿临时配置跑起来的实例」
    #   写坏，用户下次打开看到的就是别人的窗口。
    #
    #   默认部署下 CONFIG_PATH 就在 BASE_DIR 里，两者是同一个目录，
    #   所以路径与以前完全一致，用户看不出区别；只有用了别的配置文件
    #   （测试脚手架、临时实例、多实例部署）时才会落到那份配置旁边，
    #   不会去覆盖真实部署的 user_state.json。
    #
    #   显式写的绝对路径一律尊重（用户明确指定位置就该听他的）。
    #
    # ★ 多用户：这里解析出来的两个都只是**基础路径**（管理员用的那一份）。
    #   子用户的文件由 peruser.state_path 在同一目录下按用户名派生，
    #   所以「每个学生的桌面各存一份」不需要再多两个配置项。
    def _resolve_under_config(raw_value: Any, default: str) -> str:
        raw = str(raw_value or "").strip() or default
        if os.path.isabs(raw):
            return os.path.normpath(raw)
        if cfg_path:
            return os.path.normpath(os.path.join(
                os.path.dirname(os.path.abspath(cfg_path)), raw))
        # 没有配置文件上下文（裸调 prepare）：只能退回代码目录。
        # 这里不产生任何文件系统副作用，真正的写入发生在起服务之后。
        return abs_from_base(raw)

    cfg["user_state_path"] = _resolve_under_config(
        cfg.get("user_state_path"), DEFAULT_CONFIG["user_state_path"])
    cfg["desktop_shortcuts_path"] = _resolve_under_config(
        cfg.get("desktop_shortcuts_path"), DEFAULT_CONFIG["desktop_shortcuts_path"])

    # 音乐库目录与歌单文件也走同一套解析（相对本配置文件所在目录）。
    # ★ 这两个默认值同样落在项目根目录下，所以 tests/_harness.py 的
    #   STATE_PATH_KEYS 必须带上它们 —— 否则用临时配置起的服务会把测试
    #   导入的歌写进真实部署的曲库（那条结构性守卫就是为了这个）。
    music_cfg = cfg.setdefault("music", {})
    if not isinstance(music_cfg, dict):
        music_cfg = dict(DEFAULT_CONFIG["music"])
        cfg["music"] = music_cfg
    music_cfg["library_dir"] = _resolve_under_config(
        music_cfg.get("library_dir"), DEFAULT_CONFIG["music"]["library_dir"])
    music_cfg["state_path"] = _resolve_under_config(
        music_cfg.get("state_path"), DEFAULT_CONFIG["music"]["state_path"])

    # ★ 通知这两个模块实际该读写哪个文件。走这种「单向下发」而不是让它们
    # 反过来 import config，是为了避开两个模块的循环导入。
    try:
        from . import userstate as userstate_module

        userstate_module.ACTIVE_PATH = cfg["user_state_path"]
    except Exception:  # noqa: BLE001 - 下发失败时 userstate 会用自己的默认路径
        pass
    try:
        from . import shortcuts as shortcuts_module

        shortcuts_module.set_path(cfg["desktop_shortcuts_path"])
    except Exception:  # noqa: BLE001 - 下发失败时 shortcuts 会用自己的默认路径
        pass

    # 审计日志：路径同样落在配置文件旁边（多实例各记各的），
    # 轮转参数做范围兜底后下发给 audit 模块。
    audit_cfg = cfg.setdefault("audit", {})
    if not isinstance(audit_cfg, dict):
        audit_cfg = {}
        cfg["audit"] = audit_cfg
    audit_cfg["path"] = _resolve_under_config(
        audit_cfg.get("path"), DEFAULT_CONFIG["audit"]["path"])
    try:
        audit_cfg["max_bytes"] = max(
            64 * 1024, int(audit_cfg.get("max_bytes")
                           or DEFAULT_CONFIG["audit"]["max_bytes"]))
    except (TypeError, ValueError):
        audit_cfg["max_bytes"] = DEFAULT_CONFIG["audit"]["max_bytes"]
    try:
        audit_cfg["backups"] = max(
            0, min(20, int(audit_cfg.get("backups")
                           if audit_cfg.get("backups") is not None
                           else DEFAULT_CONFIG["audit"]["backups"])))
    except (TypeError, ValueError):
        audit_cfg["backups"] = DEFAULT_CONFIG["audit"]["backups"]

    try:
        from . import audit as audit_module

        audit_module.set_path(audit_cfg["path"])
        audit_module.configure(max_bytes=audit_cfg["max_bytes"],
                               backups=audit_cfg["backups"])
    except Exception:  # noqa: BLE001 - 下发失败时 audit 会用自己的默认路径
        pass

    upload = cfg.setdefault("upload", {})
    try:
        upload["max_file_size_mb"] = max(1, int(upload.get("max_file_size_mb") or 2048))
    except (TypeError, ValueError):
        upload["max_file_size_mb"] = 2048
    if not isinstance(upload.get("blocked_extensions"), list):
        upload["blocked_extensions"] = list(DEFAULT_CONFIG["upload"]["blocked_extensions"])

    # 新增开关的默认值兜底
    for bool_key in ("mount_all_drives", "mount_network_drives", "mount_removable_drives"):
        cfg[bool_key] = bool(cfg.get(bool_key, DEFAULT_CONFIG[bool_key]))

    if not isinstance(cfg.get("protected_paths"), list):
        cfg["protected_paths"] = list(DEFAULT_CONFIG["protected_paths"])

    # 根目录列表清洗
    roots = cfg.get("roots")
    if not isinstance(roots, list):
        cfg["roots"] = copy.deepcopy(DEFAULT_CONFIG["roots"])
    elif not roots and not cfg.get("mount_all_drives"):
        # 既没有显式配置根目录、又没开启自动挂载磁盘时，
        # 服务将没有任何可访问目录，这里退回默认值兜底
        cfg["roots"] = copy.deepcopy(DEFAULT_CONFIG["roots"])

    return cfg


def ensure_runtime_dirs(cfg: Dict[str, Any]) -> Dict[str, str]:
    """
    确保运行期需要的目录都存在（缩略图缓存、Office 转换缓存）。
    返回各目录的绝对路径，方便日志里打印。
    """
    created: Dict[str, str] = {}
    for key in ("thumbs", "office"):
        directory = (cfg.get(key) or {}).get("cache_dir")
        if not directory:
            continue
        try:
            os.makedirs(directory, exist_ok=True)
            created[key] = directory
        except OSError as exc:
            print("[!] 无法创建缓存目录 %s: %s" % (directory, exc))
    return created


def ensure_roots_exist(cfg: Dict[str, Any]) -> list:
    """
    确保配置的根目录存在，不存在则自动创建（需求要求：默认 D:\\Share 不存在时自动创建）。
    返回 [(路径, 是否新建成功, 错误信息), ...] 便于启动日志展示。
    """
    results = []
    for item in cfg.get("roots") or []:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path") or "").strip()
        if not raw:
            continue
        target = os.path.normpath(os.path.expandvars(os.path.expanduser(raw)))
        if os.path.isdir(target):
            results.append((target, True, ""))
            continue
        try:
            os.makedirs(target, exist_ok=True)
            results.append((target, True, "已自动创建"))
        except OSError as exc:
            results.append((target, False, str(exc)))
    return results


def write_example_config(path: Optional[str] = None) -> str:
    """
    导出一份示例配置（不含真实口令），用于交付与文档。
    """
    example = copy.deepcopy(DEFAULT_CONFIG)
    example["auth"]["password_hash"] = "pbkdf2_sha256$260000$请用-tools/gen_password.py-生成$请用-tools/gen_password.py-生成"
    example["auth"]["password"] = ""
    example["auth"]["session_secret"] = "请填入随机字符串，留空则启动时自动生成"
    example["_说明"] = [
        "roots 可以配置多个根目录，前端会以「此电脑」形式展示。",
        "password_hash 请用 python tools/gen_password.py 生成，不要明文保存密码。",
        "upload.blocked_extensions 为上传黑名单，按需增删。",
        "delete.use_recycle_bin=true 时删除进回收站，可恢复。",
        "thumbs.cache_dir / office.cache_dir 支持相对路径（相对本文件所在目录）。",
    ]

    target = path or EXAMPLE_CONFIG_PATH
    _atomic_write_json(target, example)
    return target

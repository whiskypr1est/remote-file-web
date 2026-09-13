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
    "user_state_path": "user_state.json",

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
        # 同时允许打开的命令行会话数上限
        "max_sessions": 4,
        # 空闲多久自动关闭会话（秒），0 = 不限制
        "idle_timeout_seconds": 1800,
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

    return prepare(merged, cfg_path)


def save(cfg: Dict[str, Any], path: Optional[str] = None) -> None:
    """保存配置（会自动剔除内部字段与未变更的敏感信息处理）。"""
    cfg_path = path or CONFIG_PATH
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
        ("max_sessions", 4, 1),
        ("idle_timeout_seconds", 1800, 0),
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

    # 界面状态文件 -> 绝对路径。
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
    _raw_state_path = str(cfg.get("user_state_path") or "").strip()
    if not _raw_state_path:
        _raw_state_path = DEFAULT_CONFIG["user_state_path"]
    if os.path.isabs(_raw_state_path):
        cfg["user_state_path"] = os.path.normpath(_raw_state_path)
    elif cfg_path:
        cfg["user_state_path"] = os.path.normpath(os.path.join(
            os.path.dirname(os.path.abspath(cfg_path)), _raw_state_path))
    else:
        # 没有配置文件上下文（裸调 prepare）：只能退回代码目录。
        # 这里不产生任何文件系统副作用，真正的写入发生在起服务之后。
        cfg["user_state_path"] = abs_from_base(_raw_state_path)
    # ★ 通知 userstate 模块实际该读写哪个文件。走这种「单向下发」而不是
    # 让 userstate 反过来 import config，是为了避开两个模块的循环导入。
    try:
        from . import userstate as userstate_module

        userstate_module.ACTIVE_PATH = cfg["user_state_path"]
    except Exception:  # noqa: BLE001 - 下发失败时 userstate 会用自己的默认路径
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

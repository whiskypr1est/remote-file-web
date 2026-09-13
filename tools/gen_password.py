# -*- coding: utf-8 -*-
"""
口令工具
========

用来生成 / 修改登录口令。口令在配置文件中**只保存 PBKDF2 哈希**，
本工具是唯一需要接触明文口令的地方，且不会把明文写进 config.json。

常见用法：

    # 只生成一个随机口令（打印口令和对应哈希，不修改任何文件）
    python tools/gen_password.py

    # 交互式设置新口令（会提示输入两次，输入时不回显）
    python tools/gen_password.py --set

    # 直接指定新口令
    python tools/gen_password.py --set --password "MyNewPass123!"

    # 随机生成一个新口令并写入配置，同时在控制台显示
    python tools/gen_password.py --set --random

    # 顺便修改用户名
    python tools/gen_password.py --set --username boss --random

    # 只把某个口令转成哈希（用于手工编辑 config.json）
    python tools/gen_password.py --hash "MyPass123!"
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys

# 统一用 UTF-8 输出，避免中文在 GBK 控制台/管道里变成乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

# 允许脚本从项目根目录导入 fileweb 包
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from fileweb import config as config_module  # noqa: E402
from fileweb.security import hash_password, random_password  # noqa: E402

FIRST_RUN_PASSWORD_PATH = config_module.FIRST_RUN_PASSWORD_PATH


def _print_header(title: str) -> None:
    print("\n" + "=" * 62)
    print("  " + title)
    print("=" * 62)


def _ask_password() -> str:
    """交互式读取新口令（输入两次确认，且不回显）。"""
    while True:
        first = getpass.getpass("请输入新口令（输入时不显示）: ")
        if len(first) < 6:
            print("  [!] 口令太短，至少 6 位，请重试。")
            continue
        second = getpass.getpass("请再次输入以确认: ")
        if first != second:
            print("  [!] 两次输入不一致，请重试。")
            continue
        return first


def _write_hint_file(username: str, password: str) -> None:
    """把新口令写到提示文件，便于用户查找；同时更新该文件内容。"""
    content = (
        "本文件由 tools/gen_password.py 生成，记录了当前的登录信息。\n"
        "确认无误后建议删除本文件，避免明文口令留在磁盘上。\n"
        "\n"
        "用户名：%s\n"
        "密  码：%s\n"
    ) % (username, password)
    try:
        with open(FIRST_RUN_PASSWORD_PATH, "w", encoding="utf-8") as fh:
            fh.write(content)
        print("  已把当前口令写入: %s" % FIRST_RUN_PASSWORD_PATH)
    except OSError as exc:
        print("  [!] 写入提示文件失败（不影响服务运行）: %s" % exc)


def cmd_generate() -> int:
    """生成随机口令 + 哈希，不改动配置。"""
    password = random_password(16)
    _print_header("随机口令已生成（未修改任何文件）")
    print("  明文口令 : %s" % password)
    print("  对应哈希 : %s" % hash_password(password))
    print("\n  若要把它应用到配置中，可执行：")
    print('    python tools/gen_password.py --set --password "%s"' % password)
    return 0


def cmd_hash(password: str) -> int:
    """把给定口令转成哈希。"""
    _print_header("口令哈希")
    print("  明文口令 : %s" % password)
    print("  对应哈希 : %s" % hash_password(password))
    print("\n  把上面这串哈希填到 config.json 的 auth.password_hash 即可。")
    return 0


def cmd_set(args) -> int:
    """把新口令写入 config.json。"""
    cfg_path = args.config or config_module.CONFIG_PATH

    # load() 在文件不存在时会自动创建一份带随机口令的配置
    cfg = config_module.load(cfg_path)

    # 决定新口令
    if args.password:
        password = args.password
    elif args.random:
        password = random_password(16)
    else:
        _print_header("修改登录口令")
        password = _ask_password()

    username = args.username or (cfg.get("auth") or {}).get("username") or "admin"

    auth = cfg.setdefault("auth", {})
    auth["username"] = username
    auth["password_hash"] = hash_password(password)
    # 清掉可能残留的明文口令，确保配置里只剩哈希
    auth["password"] = ""

    config_module.save(cfg, cfg_path)

    _print_header("修改成功")
    print("  配置文件 : %s" % cfg_path)
    print("  用户名   : %s" % username)
    if args.password:
        # 用户自己提供的口令，不再回显，避免泄露到终端历史里
        print("  密  码   : （使用你提供的口令）")
    else:
        print("  密  码   : %s" % password)
    print("\n  提示：正在运行的服务需要重启后才会读取新口令。")
    print("        已登录的浏览器会话仍然有效，直到会话超时。")

    if not args.password:
        _write_hint_file(username, password)
    else:
        # 用户自定的口令也写一份提示文件，方便日后查找
        _write_hint_file(username, password)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成或修改本服务的登录口令（配置文件只保存 PBKDF2 哈希）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--set", action="store_true", help="把新口令写入配置文件")
    parser.add_argument("--password", default=None, help="直接指定新口令（配合 --set）")
    parser.add_argument("--random", action="store_true", help="随机生成新口令（配合 --set）")
    parser.add_argument("--username", default=None, help="同时修改用户名（配合 --set）")
    parser.add_argument("--config", default=None, help="指定配置文件路径，默认 config.json")
    parser.add_argument("--hash", dest="hash_value", default=None, help="仅计算某个口令的哈希")
    args = parser.parse_args()

    try:
        if args.hash_value is not None:
            return cmd_hash(args.hash_value)
        if args.set:
            return cmd_set(args)
        return cmd_generate()
    except KeyboardInterrupt:
        print("\n已取消。")
        return 130
    except Exception as exc:  # noqa: BLE001
        print("\n[错误] %s" % exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())

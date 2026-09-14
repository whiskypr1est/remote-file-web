# -*- coding: utf-8 -*-
"""
口令工具
========

用来生成 / 修改登录口令。口令**只保存 PBKDF2 哈希**，
本工具是唯一需要接触明文口令的地方，且不会把明文写进任何文件。

★ 多用户改造后，口令改的是**用户表 users.json**，不是 config.json。
  config.json 的 auth 段只在「还没有用户表」时被用来派生第一个管理员
  （见 fileweb/users.ensure_bootstrap），之后不再参与登录。
  所以本工具同时把哈希**同步**写回 config.json 的 auth 段：
  万一将来 users.json 被删掉重建，引导出来的管理员口令仍然是你设的这一个。

常见用法：

    # 列出所有账号（忘了有谁、谁被停用时用这个）
    python tools/gen_password.py --list

    # 只生成一个随机口令（打印口令和对应哈希，不修改任何文件）
    python tools/gen_password.py

    # 交互式设置新口令（会提示输入两次，输入时不回显）
    python tools/gen_password.py --set

    # 给指定账号设置新口令
    python tools/gen_password.py --set --username stu01 --password "NewPass123!"

    # 随机生成一个新口令并写入，同时在控制台显示
    python tools/gen_password.py --set --random

    # 只把某个口令转成哈希（用于手工编辑 users.json / config.json）
    python tools/gen_password.py --hash "MyPass123!"

改动**立即生效**，不需要重启服务：用户表是每个请求现读的。
被改口令的那个账号，已登录的浏览器会立即失效（这正是「口令泄露后重置」想要的）。
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
from fileweb import users as users_module  # noqa: E402
from fileweb.security import hash_password, random_password  # noqa: E402

FIRST_RUN_PASSWORD_PATH = config_module.FIRST_RUN_PASSWORD_PATH

ROLE_LABELS = {users_module.ROLE_ADMIN: "管理员",
               users_module.ROLE_USER: "普通用户"}


def _print_header(title: str) -> None:
    print("\n" + "=" * 62)
    print("  " + title)
    print("=" * 62)


def _ask_password() -> str:
    """交互式读取新口令（输入两次确认，且不回显）。"""
    while True:
        first = getpass.getpass("请输入新口令（输入时不显示）: ")
        if len(first) < users_module.MIN_PASSWORD_LENGTH:
            print("  [!] 口令太短，至少 %d 位，请重试。"
                  % users_module.MIN_PASSWORD_LENGTH)
            continue
        second = getpass.getpass("请再次输入以确认: ")
        if first != second:
            print("  [!] 两次输入不一致，请重试。")
            continue
        return first


def _hint_file_for(cfg_path: str) -> str:
    """
    口令提示文件放在**配置文件旁边**。

    ★ 不能直接用 config_module.FIRST_RUN_PASSWORD_PATH（那是代码目录下的常量）：
      用 --config 操作别的实例时，提示文件会写到真实部署的项目根目录去，
      把**当前部署的明文口令**覆盖成另一个实例的 —— 这正是本项目出过的
      「测试写脏真实部署」那类事故。config.py 里已有同规则的私有助手。
    """
    beside = config_module._password_file_beside(cfg_path)
    return beside or config_module.FIRST_RUN_PASSWORD_PATH


def _write_hint_file(username: str, password: str, cfg_path: str) -> None:
    """把新口令写到提示文件，便于用户查找；同时更新该文件内容。"""
    target = _hint_file_for(cfg_path)
    content = (
        "本文件由 tools/gen_password.py 生成，记录了当前的登录信息。\n"
        "确认无误后建议删除本文件，避免明文口令留在磁盘上。\n"
        "\n"
        "用户名：%s\n"
        "密  码：%s\n"
    ) % (username, password)
    try:
        with open(target, "w", encoding="utf-8") as fh:
            fh.write(content)
        print("  已把当前口令写入: %s" % target)
    except OSError as exc:
        print("  [!] 写入提示文件失败（不影响服务运行）: %s" % exc)


def _users_path_for(cfg_path: str, override: str = "") -> str:
    """
    用户表放在**配置文件旁边**（与 app.py 的 create_app 同一套规则），
    这样用 --config 操作别的实例时，改的也是那个实例的用户。
    """
    if override:
        return os.path.abspath(override)
    return os.path.join(os.path.dirname(os.path.abspath(cfg_path)), "users.json")


def cmd_generate() -> int:
    """生成随机口令 + 哈希，不改动任何文件。"""
    password = random_password(16)
    _print_header("随机口令已生成（未修改任何文件）")
    print("  明文口令 : %s" % password)
    print("  对应哈希 : %s" % hash_password(password))
    print("\n  若要把它应用到某个账号，可执行：")
    print('    python tools/gen_password.py --set --username <账号> --password "%s"'
          % password)
    return 0


def cmd_hash(password: str) -> int:
    """把给定口令转成哈希。"""
    _print_header("口令哈希")
    print("  明文口令 : %s" % password)
    print("  对应哈希 : %s" % hash_password(password))
    print("\n  可用于手工编辑 users.json 中某个账号的 password_hash，")
    print("  或填到 config.json 的 auth.password_hash（仅在还没有用户表时生效）。")
    return 0


def cmd_list(args) -> int:
    """列出全部账号（含停用的），不显示任何口令信息。"""
    cfg_path = args.config or config_module.CONFIG_PATH
    users_path = _users_path_for(cfg_path, args.users)
    users_module.set_path(users_path)

    _print_header("账号列表")
    print("  用户表   : %s" % users_path)
    if not os.path.isfile(users_path):
        print("  （还没有用户表 —— 服务首次启动时会从 config.json 的 auth 段派生管理员）")
        return 0

    records = users_module.list_users()
    print("")
    print("  %-20s %-10s %-8s %s" % ("用户名", "角色", "状态", "可见目录 / 显示名"))
    print("  " + "-" * 66)
    for record in records:
        roots = record.get("roots") or []
        note = "%d 个目录" % len(roots) if roots else "（无：看不到任何文件）"
        if record.get("display_name") and record["display_name"] != record["username"]:
            note += "  " + record["display_name"]
        print("  %-20s %-10s %-8s %s" % (
            record["username"],
            ROLE_LABELS.get(record.get("role"), record.get("role") or "?"),
            "启用" if record.get("enabled", True) else "停用",
            note,
        ))
    print("")
    if not users_module.has_admin():
        print("  [!] 当前没有**启用中的管理员**，谁也进不了管理界面。")
        print("      用 --set --username <账号> --password ... 给某个管理员账号重设口令，")
        print("      或直接编辑 users.json 把某个管理员的 enabled 改成 true。")
    return 0


def cmd_set(args) -> int:
    """
    把新口令写入**用户表**，并同步 config.json 的 auth 段。

    ★ 为什么必须写用户表：多用户改造之后登录只查 users.json，config 的 auth
      段只在「首次引导」时被用过一次。继续只改 config 会出现**显示修改成功、
      但口令根本没变** —— 比直接报错糟糕得多（用户会以为是自己记错了）。
    """
    cfg_path = args.config or config_module.CONFIG_PATH

    # load() 在文件不存在时会自动创建一份带随机口令的配置
    cfg = config_module.load(cfg_path)

    if args.password:
        password = args.password
    elif args.random:
        password = random_password(16)
    else:
        _print_header("修改登录口令")
        password = _ask_password()

    if len(password) < users_module.MIN_PASSWORD_LENGTH:
        print("\n[错误] 口令至少需要 %d 位。"
              % users_module.MIN_PASSWORD_LENGTH)
        return 1

    users_path = _users_path_for(cfg_path, args.users)
    users_module.set_path(users_path)

    # 还没有用户表时先引导出管理员（与 app.py 启动时做的事完全一致），
    # 否则「刚部署、还没登录过」的机器上会找不到任何账号可改。
    if not os.path.isfile(users_path):
        users_module.ensure_bootstrap(cfg)

    # 决定改谁：命令行指定的 > 现有的第一个管理员 > config 里的用户名
    username = (args.username
                or users_module.admin_username()
                or ((cfg.get("auth") or {}).get("username") or "")
                or "admin")

    existing = users_module.get(username)
    created = False
    token_version = None

    if existing is None:
        # ★ 刻意**不**做「改名」：用户名是任务归属、终端归属、审计日志的键，
        #   改名会让这些记录对不上号。所以「换一个名字」= 建一个新管理员，
        #   旧账号保留（要停用请去管理界面，或手工编辑 users.json）。
        try:
            users_module.create(
                username, password,
                role=(args.role or users_module.ROLE_ADMIN),
                display_name=username,
                created_by="gen_password.py",
            )
        except users_module.UserError as exc:
            print("\n[错误] %s" % exc)
            return 1
        created = True
    else:
        users_module.set_password(username, password)
        updated = users_module.get(username) or {}
        token_version = updated.get("token_version")

    # 同步写回 config 的 auth 段：它是「还没有用户表」时的引导种子。
    # 让它与当前口令保持一致，将来 users.json 丢了重建也不会把口令换掉。
    auth = cfg.setdefault("auth", {})
    auth_was = auth.get("username") or ""
    if not created or not auth_was:
        auth["username"] = username
    auth["password_hash"] = hash_password(password)
    # 清掉可能残留的明文口令，确保配置里只剩哈希
    auth["password"] = ""
    config_module.save(cfg, cfg_path)

    _print_header("修改成功")
    print("  配置文件 : %s" % cfg_path)
    print("  用户表   : %s" % users_path)
    print("  用户名   : %s%s" % (username, "（新建）" if created else ""))
    if args.password:
        # 用户自己提供的口令，不再回显，避免泄露到终端历史里
        print("  密  码   : （使用你提供的口令）")
    else:
        print("  密  码   : %s" % password)
    if token_version is not None:
        print("  会话版本 : %s（递增 —— 该账号已登录的浏览器立即失效）"
              % token_version)
    print("\n  ★ 不需要重启服务：用户表是每个请求现读的，改动立即生效。")

    if created and auth_was and auth_was != username:
        print("  提示：原来的账号「%s」仍然存在。要停用它，请登录后到"
              "「用户管理」里停用，或手工编辑 users.json。" % auth_was)

    _write_hint_file(username, password, cfg_path)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="生成或修改本服务的登录口令（只保存 PBKDF2 哈希）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--set", action="store_true", help="把新口令写入用户表")
    parser.add_argument("--list", action="store_true", help="列出全部账号")
    parser.add_argument("--password", default=None, help="直接指定新口令（配合 --set）")
    parser.add_argument("--random", action="store_true", help="随机生成新口令（配合 --set）")
    parser.add_argument("--username", default=None,
                        help="指定账号名（配合 --set；不存在则新建为管理员）")
    parser.add_argument("--role", default=None,
                        help="新建账号时的角色：admin 或 user（默认 admin）")
    parser.add_argument("--config", default=None, help="指定配置文件路径，默认 config.json")
    parser.add_argument("--users", default=None,
                        help="指定用户表路径，默认与配置文件同目录的 users.json")
    parser.add_argument("--hash", dest="hash_value", default=None, help="仅计算某个口令的哈希")
    args = parser.parse_args()

    try:
        if args.hash_value is not None:
            return cmd_hash(args.hash_value)
        if args.list:
            return cmd_list(args)
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

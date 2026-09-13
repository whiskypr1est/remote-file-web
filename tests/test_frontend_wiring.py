# -*- coding: utf-8 -*-
"""
前端「调用了不存在的成员」这一类的静态回归测试。

为什么需要它（这不是假想的风险，是真实踩过的坑）：
    sessionstate.js 曾经写 `wm.onWindowsChanged(...)`，而 onWindowsChanged 是
    wins.js 的**模块级导出**、不是 WindowManager 的方法。结果是 TypeError，
    桌面布局持久化从第一行就断了 —— 整个功能等于没做。

    而这个 bug 骗过了当时所有的验证手段：
      * `node --check` 只查语法，查不出成员是否存在；
      * 「把 ES 模块图 link 起来」也查不出来 —— import 的 `wm` 本身存在，
        link 会成功；只有真正**调用**那一刻才炸；
      * 作者自己的逻辑测试用了替身，替身上「恰好」有这个方法，于是全绿。

    换句话说：**只有真跑浏览器才能发现它**。浏览器验证当然要跑（那是最终防线），
    但那种反馈很慢，所以这里补一道快的静态闸门，把「名字对不上」这类问题
    在提交前就挡掉。

覆盖三类跨模块调用：wm.*（对象方法）、api.* / ui.*（模块导出）。
"""

from __future__ import annotations

import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JS_DIR = os.path.join(ROOT, "static", "js")


def _read(name: str) -> str:
    with io.open(os.path.join(JS_DIR, name), encoding="utf-8") as fh:
        return fh.read()


def _js_files():
    for name in sorted(os.listdir(JS_DIR)):
        if name.endswith(".js"):
            yield name


def _code_lines(src: str):
    """
    产出 (行号, 原文, 可用于匹配的文本)。

    做两件事，都是为了让正则**只看到真实调用**：
      * 跳过整行注释，并去掉行尾注释；
      * 把字符串字面量抹掉 —— 否则 `from './api.js'` 里的 `api.js`
        会被 `api\\.(\\w+)` 匹配成「调用了 api.js 这个成员」（我第一版就踩了）。
    """
    for index, line in enumerate(src.splitlines(), 1):
        stripped = line.strip()
        if stripped.startswith("//") or stripped.startswith("*") or stripped.startswith("/*"):
            continue
        code = line.split("//", 1)[0]
        code = re.sub(r"'[^']*'", "''", code)
        code = re.sub(r'"[^"]*"', '""', code)
        code = re.sub(r"`[^`]*`", "``", code)
        yield index, line, code


def _module_exports(name: str) -> set:
    """模块的具名导出：export function/const/let/class + export { a, b }。"""
    src = _read(name)
    names = set(re.findall(r"^export\s+(?:async\s+)?(?:function|const|let|var|class)\s+([A-Za-z_$][\w$]*)",
                           src, re.M))
    for group in re.findall(r"^export\s*\{([^}]*)\}", src, re.M):
        for piece in group.split(","):
            piece = piece.strip()
            if not piece:
                continue
            names.add(piece.split(" as ")[-1].strip())
    return names


def _window_manager_members() -> set:
    """WindowManager 类的方法、访问器与构造函数里赋值的实例字段。"""
    src = _read("wins.js")
    match = re.search(r"class WindowManager\s*\{(.*)\n\}", src, re.S)
    body = match.group(1) if match else src
    members = set(re.findall(r"^\s{2}(?:async\s+)?([A-Za-z_$][\w$]*)\s*\(", body, re.M))
    members |= set(re.findall(r"^\s{2}(?:get|set)\s+([A-Za-z_$][\w$]*)\s*\(", body, re.M))
    members |= set(re.findall(r"this\.([A-Za-z_$][\w$]*)\s*=", body))
    return members


class FrontendCrossModuleCallTests(unittest.TestCase):
    """跨模块调用名必须真的存在。"""

    def test_no_calls_to_missing_window_manager_members(self):
        """
        `wm.xxx` 必须是 WindowManager 真的有的成员。

        这条直接对应当年那个 TypeError：wm.onWindowsChanged 并不存在，
        应该从 wins.js 具名导入 onWindowsChanged 再调用。
        """
        members = _window_manager_members()
        self.assertIn("create", members, "没能解析出 WindowManager 成员，正则可能失配了")
        self.assertNotIn("onWindowsChanged", members,
                         "onWindowsChanged 不该是 wm 的方法（它是模块级导出），"
                         "如果它变成方法了，请同步更新这条断言")

        offenders = []
        for name in _js_files():
            for lineno, raw, code in _code_lines(_read(name)):
                # (?<![\w$.]) 是必要的：否则 this.info.ui.wallpaper 这种
                # 「别的对象的同名属性」会被当成模块引用（第一版就误报过）
                for used in re.findall(r"(?<![\w$.])wm\.([A-Za-z_$][\w$]*)", code):
                    if used not in members:
                        offenders.append("%s:%d wm.%s" % (name, lineno, used))
        self.assertEqual(offenders, [],
                         "调用了 WindowManager 上不存在的成员：\n  " + "\n  ".join(offenders))

    def test_no_calls_to_missing_api_exports(self):
        """`api.xxx` 必须是 api.js 真的导出的名字（拼错只在运行时炸）。"""
        exports = _module_exports("api.js")
        self.assertTrue(exports, "没能解析出 api.js 的导出")

        offenders = []
        for name in _js_files():
            for lineno, raw, code in _code_lines(_read(name)):
                for used in re.findall(r"(?<![\w$.])api\.([A-Za-z_$][\w$]*)", code):
                    if used not in exports:
                        offenders.append("%s:%d api.%s" % (name, lineno, used))
        self.assertEqual(offenders, [], "调用了 api.js 未导出的名字：\n  " + "\n  ".join(offenders))

    def test_no_calls_to_missing_ui_exports(self):
        """`ui.xxx` 必须是 ui.js 真的导出的名字。"""
        exports = _module_exports("ui.js")
        self.assertTrue(exports, "没能解析出 ui.js 的导出")

        offenders = []
        for name in _js_files():
            for lineno, raw, code in _code_lines(_read(name)):
                for used in re.findall(r"(?<![\w$.])ui\.([A-Za-z_$][\w$]*)", code):
                    if used not in exports:
                        offenders.append("%s:%d ui.%s" % (name, lineno, used))
        self.assertEqual(offenders, [], "调用了 ui.js 未导出的名字：\n  " + "\n  ".join(offenders))

    def test_relative_imports_resolve_to_real_files(self):
        """相对 import 指向的文件必须存在（改名/挪文件时最容易漏）。"""
        missing = []
        for name in _js_files():
            for lineno, raw, code in _code_lines(_read(name)):
                for spec in re.findall(r"""from\s+['"](\./[^'"]+)['"]""", code):
                    target = os.path.normpath(os.path.join(JS_DIR, spec))
                    if not os.path.isfile(target):
                        missing.append("%s:%d -> %s" % (name, lineno, spec))
        self.assertEqual(missing, [], "相对导入指向了不存在的文件：\n  " + "\n  ".join(missing))


if __name__ == "__main__":
    unittest.main(verbosity=2)

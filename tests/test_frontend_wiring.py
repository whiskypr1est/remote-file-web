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


def _template_html(src: str) -> str:
    """
    粗略还原某个窗口类 template() 里拼出来的 HTML 字符串。

    只为「选择器里的 class 在不在模板里」这一件事服务，所以不追求精确：
    把 `return [ ... ].join('')` 之间的单引号字符串按顺序拼起来即可
    （`icon('x')` 这类调用不是字符串，会被自然忽略）。
    """
    match = re.search(r"template\(\)\s*\{(.*?)\n  \}", src, re.S)
    if not match:
        return ""
    body = match.group(1)
    start = body.find("return [")
    end = body.find("].join('')", start)
    if start < 0 or end < 0:
        return ""
    return "".join(re.findall(r"'([^']*)'", body[start:end]))


def _class_names(src: str) -> set:
    """
    源码里出现过的全部 class 名。

    扫的是整个文件（不只是 template()）：模板里写死的、渲染函数里拼出来的
    HTML 字符串里都算 —— 两者都是带 `class="..."` 的 JS 字符串。
    按空白拆开，所以 `class="ms-time ms-duration"` 会贡献两个名字。
    """
    names = set()
    for value in re.findall(r'class="([^"]*)"', src):
        for piece in value.split():
            names.add(piece)
    return names


class FrontendDomLookupTests(unittest.TestCase):
    """
    ★ 窗口类里的 DOM 查找必须真的能找到东西。

    这一组是补一个**真实出过的 bug**（用户报的「导入了歌但列表空白」）：
    music.js 的 `this.$` 表定义的是 `listHead`，而 renderList() 写的是
    `this.$.listTitle` —— 键不存在 → undefined → 赋值 .textContent 当场抛
    「Cannot set properties of undefined (setting 'textContent')」，
    而它在 renderList 的**第一行**，后面的 innerHTML 根本执行不到。

    为什么原来的闸门没拦住：`node --check` 只查语法（键名写错是合法语法），
    而 test_frontend_wiring 只管**跨模块**成员（api./ui./wm.）——
    `this.$.xxx` 与选择器字符串都是**同一个文件内部**的事，谁都没管。
    这类错误不会在加载时暴露，只在跑到那一行时才炸，正是最该静态挡住的。
    """

    def test_dom_lookup_maps_only_use_defined_keys(self):
        """`this.$.xxx` 用到的每个键，都必须在 `this.$ = {...}` 里定义过。"""
        offenders = []
        for name in _js_files():
            src = _read(name)
            match = re.search(r"this\.\$ = \{(.*?)\n\s*\};", src, re.S)
            if not match:
                continue
            defined = set(re.findall(r"(\w+)\s*:\s*this\.root\.querySelector", match.group(1)))
            used = set(re.findall(r"this\.\$\.(\w+)", src))
            for key in sorted(used - defined):
                offenders.append("%s 用了 this.$.%s，但它不在 this.$ 映射里"
                                 % (name, key))
        self.assertEqual(offenders, [],
                         "DOM 查找表里没有这个键（运行时会是 undefined）：\n  "
                         + "\n  ".join(offenders))

    def test_dollar_map_entries_are_all_used(self):
        """反向：表里定义了却没人用的键，通常意味着改名只改了一半。"""
        offenders = []
        for name in _js_files():
            src = _read(name)
            match = re.search(r"this\.\$ = \{(.*?)\n\s*\};", src, re.S)
            if not match:
                continue
            defined = set(re.findall(r"(\w+)\s*:\s*this\.root\.querySelector", match.group(1)))
            used = set(re.findall(r"this\.\$\.(\w+)", src))
            for key in sorted(defined - used):
                offenders.append("%s 的 this.$ 里 `%s` 定义了却没用到" % (name, key))
        self.assertEqual(offenders, [],
                         "DOM 查找表里有没人用的键（可能改名漏改）：\n  "
                         + "\n  ".join(offenders))

    def test_class_selectors_exist_in_the_template(self):
        """
        `querySelector('.ms-xxx')` 里的 class，必须在**同一个文件里**被用过
        （模板里写死的、或渲染函数里拼出来的都算）。

        写错的后果和上面那条一样（拿到 null 再赋值就抛），
        而且同样只在运行到那一行时才暴露。

        ★ 为什么是「本文件里出现过」而不是「模板里出现过」：
          表格行、任务管理器的卡片这类 DOM 是在渲染函数里拼出来的，
          它们**不在** template() 里 —— 只看模板会一片误报
          （.ms-dur / .tm-bar 等）。而真正的拼写错误在任何地方都不会出现，
          所以这个口径照样抓得住 typos。
        """
        # 少数 class 归别的模块或 HTML 所有，本文件只是使用者
        external = {
            'wb-body',        # winbox 自己的窗口主体
            'ctx-menu',       # ui.js 的右键菜单
            'hidden',         # index.html 的通用类
        }
        offenders = []
        for name in _js_files():
            src = _read(name)
            if not _template_html(src):
                continue          # 不是「窗口类」的文件，跳过
            known = _class_names(src)
            selectors = set(re.findall(r"querySelector\('\.([\w-]+)'\)", src))
            for cls in sorted(selectors):
                if cls in known or cls in external:
                    continue
                offenders.append("%s 查了 .%s，但这个 class 在本文件里从没出现过"
                                 % (name, cls))
        self.assertEqual(offenders, [],
                         "选择器指向了不存在的 class（运行时会拿到 null）：\n  "
                         + "\n  ".join(offenders))


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

    def test_named_imports_resolve_to_real_exports(self):
        """
        `import { foo } from './x.js'` 里的 foo 必须是 x.js 真的导出的名字。

        这一条补的是上面三条的空档：它们只查 `api.xxx` / `ui.xxx` / `wm.xxx`
        这种「命名空间成员访问」，而**具名导入**写错时是**模块链接阶段**就失败 ——
        整个页面的模块图都建不起来，用户看到的是一片白屏、控制台只有一行
        "does not provide an export named"。比运行到那一行才炸更难查，
        所以值得在静态闸门里挡掉。

        用原始行（raw）而不是抹掉字符串后的文本匹配：这里**需要**那个模块
        路径字面量，而 _code_lines 会把字符串抹成空引号。
        局限：只认单行写法（本项目的 import 都是单行的）。
        """
        offenders = []
        for name in _js_files():
            for lineno, raw, code in _code_lines(_read(name)):
                match = re.search(
                    r"""import\s*\{([^}]*)\}\s*from\s*['"](\./[^'"]+)['"]""", raw)
                if not match:
                    continue
                target = os.path.basename(match.group(2))
                exports = _module_exports(target)
                for piece in match.group(1).split(","):
                    piece = piece.strip()
                    if not piece:
                        continue
                    # 支持 `foo as bar`：真正要校验的是被导入的那个原名
                    original = piece.split(" as ")[0].strip()
                    if original and original not in exports:
                        offenders.append("%s:%d 从 %s 导入了不存在的 `%s`"
                                         % (name, lineno, target, original))
        self.assertEqual(offenders, [],
                         "具名导入指向了模块没有导出的名字：\n  " + "\n  ".join(offenders))

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

    def test_dom_lookup_maps_only_use_defined_keys(self):
        """
        ★ 窗口类里 `this.$ = { 名字: querySelector(...) }` 这张表，
        用到的每个键都必须在表里定义过。

        这条是补一个**真实出过的 bug**：音乐播放器里表定义的是 `listHead`，
        而 renderList() 写的是 `this.$.listTitle` —— 于是
        `this.$.listTitle` 是 **undefined**（注意不是 null），
        赋值 .textContent 当场抛
        「Cannot set properties of undefined (setting 'textContent')」，
        而且它在 renderList 的**第一行**，后面 `innerHTML` 那行根本执行不到，
        用户看到的只是「导入了歌但列表是空的」。

        为什么原来的闸门没拦住：node --check 只查语法；上面的三条只查
        `api.xxx` / `ui.xxx` / `wm.xxx` 这种跨模块成员访问，
        而 `this.$.xxx` 是**同一个文件内部**的名字，谁都没管。
        这类名字写错不会在加载时报错，只在跑到那一行时才炸 ——
        正是最该用静态检查挡住的那种。
        """
        offenders = []
        for name in _js_files():
            src = _read(name)
            match = re.search(r"this\.\$ = \{(.*?)\n\s*\};", src, re.S)
            if not match:
                continue
            defined = set(re.findall(r"(\w+)\s*:\s*this\.root\.querySelector", match.group(1)))
            used = set(re.findall(r"this\.\$\.(\w+)", src))
            for key in sorted(used - defined):
                offenders.append("%s 用了 this.$.%s，但它不在 this.$ 映射里"
                                 % (name, key))
        self.assertEqual(offenders, [],
                         "DOM 查找表里没有这个键（运行时会是 undefined）：\n  "
                         + "\n  ".join(offenders))

    def test_dollar_map_entries_are_all_used(self):
        """
        反向检查：表里定义了却没人用的键，通常是**改名字时漏改了用法**。

        （比如把 `listTitle` 改名成 `listHead` 却只改了一半 —— 上面那条会红，
        这条则会在「表里多出一个再也没人用的键」时提醒。）
        """
        offenders = []
        for name in _js_files():
            src = _read(name)
            match = re.search(r"this\.\$ = \{(.*?)\n\s*\};", src, re.S)
            if not match:
                continue
            defined = set(re.findall(r"(\w+)\s*:\s*this\.root\.querySelector", match.group(1)))
            used = set(re.findall(r"this\.\$\.(\w+)", src))
            for key in sorted(defined - used):
                offenders.append("%s 的 this.$ 里 `%s` 定义了却没用到" % (name, key))
        self.assertEqual(offenders, [],
                         "DOM 查找表里有没人用的键（可能改名漏改）：\n  "
                         + "\n  ".join(offenders))


if __name__ == "__main__":
    unittest.main(verbosity=2)

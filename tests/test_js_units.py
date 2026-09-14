# -*- coding: utf-8 -*-
"""
前端用例（用 node 直接跑）的统一入口
====================================

tests/js/*.test.mjs 是一批**不依赖浏览器、也不依赖 Electron** 的前端用例：

    nowplaying.test.mjs                 网页播放器的上报模块（歌词上报）
    desktop-lyrics-client.test.mjs      桌面歌词悬浮窗的连接层（重连/轮询/退避）
    desktop-lyrics-overlay.test.mjs     桌面歌词悬浮窗的渲染逻辑（时间与歌词行对应）

这里只负责把它们接进 unittest，让「跑一遍测试」就能覆盖前端逻辑。

为什么要用 node 跑，而不是用 Python 重写一遍逻辑
------------------------------------------------
★ 重写一遍就等于测了另一份实现：真正跑在浏览器/Electron 里的是那几份 JS，
  在 Python 里再写一遍只能验证「我以为的逻辑」，而且两边会各自演化。
  所以这里跑的是**同一份文件**。

这三个模块为什么值得单独测
--------------------------
它们的共同点是「出错也不吭声」：
  * 上报失败是**故意的静默**（绝不能影响听歌）；
  * 悬浮窗连不上就只是一片空白，没有弹窗也没有报错；
  * 歌词对不上轴、暂停了还在滚，用户只会说「歌词是坏的」。
这些都没有截图能看出来的症状，只能把行为一条条钉在用例里。

没有 node 怎么办
----------------
跳过（skip）而不是失败：部署机器上不一定装 node，而这套测试的主体（服务端）
在没有 node 的环境里应当照常跑完。跳过会明确显示原因，不会被误读成通过。
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import subprocess
import unittest

from tests._harness import BASE_DIR

NODE = shutil.which("node") or shutil.which("node.exe")

JS_DIR = os.path.join(BASE_DIR, "tests", "js")

# 已知的用例文件。少任何一个都说明有人把文件删了 —— 那种情况下
# 「一个文件都跑不到」和「跑过但全绿」在输出上是一样的，必须显式挡住。
EXPECTED_FILES = {
    "nowplaying.test.mjs",
    "desktop-lyrics-client.test.mjs",
    "desktop-lyrics-overlay.test.mjs",
}

# 每个文件至少要有这么多用例。设下限而不是精确数字：精确数字会让「加一个用例」
# 变成要改两处的事，而下限一样能挡住「把用例删空让测试变绿」。
MIN_CASES_PER_FILE = 5


def find_test_files():
    return sorted(glob.glob(os.path.join(JS_DIR, "*.test.mjs")))


@unittest.skipUnless(NODE, "未安装 node，跳过前端逻辑的用例")
class FrontendJsTests(unittest.TestCase):

    def run_js(self, path):
        """
        跑一个用例文件，返回 (是否通过, 输出, 用例数)。

        输出按 utf-8 显式解码：用例名与断言消息都是中文，而 Windows 控制台是
        GBK —— 让子进程继承控制台编码的话，失败信息会变成乱码，等于没有信息。
        """
        proc = subprocess.run([NODE, path], cwd=BASE_DIR,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=300)
        output = (proc.stdout or b"").decode("utf-8", "replace")
        matched = re.search(r"Ran (\d+) tests", output)
        count = int(matched.group(1)) if matched else -1
        passed = proc.returncode == 0 and "0 failed" in output and count > 0
        return passed, output, count

    def test_all_frontend_cases_pass(self):
        files = find_test_files()
        self.assertTrue(files, "一个前端用例文件都没找到：%s" % JS_DIR)

        problems = []
        for path in files:
            passed, output, count = self.run_js(path)
            if not passed:
                problems.append("★ %s\n%s" % (os.path.basename(path), output))
            elif count < MIN_CASES_PER_FILE:
                problems.append("★ %s 只有 %d 个用例（少于 %d 个），是不是被删了？\n%s"
                                % (os.path.basename(path), count,
                                   MIN_CASES_PER_FILE, output))
        self.assertEqual(problems, [], "\n\n".join(problems))

    def test_every_expected_file_is_still_there(self):
        present = {os.path.basename(path) for path in find_test_files()}
        missing = sorted(EXPECTED_FILES - present)
        self.assertEqual(missing, [],
                         "★ 这些前端用例文件不见了（不许靠删文件让测试变绿）：%s" % missing)


if __name__ == "__main__":
    unittest.main(verbosity=2)

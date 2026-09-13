# -*- coding: utf-8 -*-
"""
配置层的一条安全性质：**不要产生意外的文件系统副作用**。

背景（本项目真实发生过一次事故）：
    config.prepare() 里的「首次运行」分支会把明文口令写到
    FIRST_RUN_PASSWORD_PATH，而那个常量锚在 BASE_DIR（**代码所在目录**），
    与「当前用的是哪份配置」无关。

    于是任何「拿一份没有口令的临时配置跑一次 prepare()」的场景
    —— 单元测试、诊断脚本、临时起的测试服务器 —— 都会把真实部署旁边的
    FIRST_RUN_PASSWORD.txt 覆盖掉，用户看到的就是一个**根本不生效的密码**。
    更难查的是：config.json 没被动过，所以登录其实还是好的，
    只有那个文件在说谎。

    现在规则是：「没有配置文件上下文，就不写文件」。
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest

from fileweb import config as config_module


def _read(path: str) -> str:
    """用 with 读文件，避免测试自己制造 ResourceWarning 噪声。"""
    with io.open(path, encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str) -> None:
    with io.open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


class PrepareSideEffectTests(unittest.TestCase):
    """prepare() 的落盘行为必须被「有没有配置文件路径」严格约束。"""

    def setUp(self):
        self.path = config_module.FIRST_RUN_PASSWORD_PATH
        self.existed = os.path.exists(self.path)
        self.before = _read(self.path) if self.existed else None
        self.before_mtime = os.path.getmtime(self.path) if self.existed else None

    def tearDown(self):
        # 兜底：万一被测代码真的写了，立刻还原。
        # 绝不能让真实部署的口令文件停在「被测试改过」的状态。
        if self.existed:
            if _read(self.path) != self.before:
                _write(self.path, self.before)
        elif os.path.exists(self.path):
            os.unlink(self.path)

    def _assert_real_file_untouched(self, when: str) -> None:
        if self.existed:
            self.assertEqual(_read(self.path), self.before,
                             "%s：真实口令文件被改写了" % when)
            self.assertEqual(os.path.getmtime(self.path), self.before_mtime,
                             "%s：真实口令文件被重新写入（mtime 变了）" % when)
        else:
            self.assertFalse(os.path.exists(self.path),
                             "%s：竟然凭空创建了口令文件" % when)

    def test_bare_prepare_has_no_filesystem_side_effect(self):
        """
        没有配置文件上下文（测试/诊断脚本最常用的调法）：一律不落盘。

        这条就是那次事故的回归测试 —— 它以前会把真实口令文件覆盖掉。
        """
        config_module.prepare({})            # 完全没有 auth，会走首次运行分支
        self._assert_real_file_untouched("裸调 prepare({})")

    def test_prepare_with_config_path_writes_beside_that_config(self):
        """给了配置文件路径时，口令文件要落在那个配置旁边，而不是代码目录。"""
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = os.path.join(tmp, "config.json")
            _write(cfg_path, "{}")

            config_module.prepare({}, cfg_path)

            self.assertTrue(
                os.path.isfile(os.path.join(tmp, "FIRST_RUN_PASSWORD.txt")),
                "口令文件没有写到给定配置的旁边")
            self._assert_real_file_untouched("带 cfg_path 调 prepare()")

    def test_default_deployment_location_is_unchanged(self):
        """
        默认部署下路径必须和以前**完全一致**（就在 config.json 旁边）。

        这条是防「修 bug 时把正常行为也改了」：用户升级后不该发现
        口令文件换了地方。
        """
        self.assertEqual(
            config_module._password_file_beside(config_module.CONFIG_PATH),
            config_module.FIRST_RUN_PASSWORD_PATH,
        )

    def test_empty_config_path_yields_no_destination(self):
        """空路径要明确表示「没有落盘目标」，而不是回退到代码目录。"""
        self.assertEqual(config_module._password_file_beside(""), "")


class GenPasswordToolContractTests(unittest.TestCase):
    """
    tools/gen_password.py 依赖模块常量指向真实口令文件。

    上面顺手把写入改成了「由调用方给路径」，所以这里钉一下：
    那条常量必须仍然指向默认部署的口令文件，否则改密码工具会写错地方。
    """

    def test_module_constant_still_points_at_base_dir(self):
        self.assertEqual(
            os.path.dirname(config_module.FIRST_RUN_PASSWORD_PATH),
            config_module.BASE_DIR,
        )
        self.assertTrue(
            config_module.FIRST_RUN_PASSWORD_PATH.endswith("FIRST_RUN_PASSWORD.txt"))


class ConfigCreationGuardTests(unittest.TestCase):
    """
    ``load(create_if_missing=False)`` 必须真的什么都不创建。

    为什么单独钉这一条：测试脚手架 `tests/_harness.py` 用 ``load()`` 取一份
    默认配置当基线，而 ``load()`` 的默认参数是 ``create_if_missing=True``。
    它瞄准的是**真实的** config.json。平时那份文件在，走的是只读分支，
    一切正常；可一旦它被改名、删掉或换机器部署漏了，跑一次测试就会
    「重新生成一份真实配置」，并顺手把随机口令写进
    FIRST_RUN_PASSWORD.txt —— **一次测试就把线上部署的登录信息毁掉**。
    这是实测过的：``load(missing, create_if_missing=True)`` 会同时产出
    config.json 和 FIRST_RUN_PASSWORD.txt 两个文件。
    """

    HARNESS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_harness.py")

    def test_load_with_create_if_missing_false_creates_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "config.json")
            cfg = config_module.load(missing, create_if_missing=False)

            self.assertFalse(os.path.exists(missing),
                             "明确说了不要创建，却还是写出了配置文件")
            self.assertFalse(
                os.path.exists(os.path.join(tmp, "FIRST_RUN_PASSWORD.txt")),
                "不该产生口令文件")
            # 但仍然要返回一份可用的默认配置，否则脚手架拿不到基线
            self.assertIsInstance(cfg, dict)
            self.assertIn("auth", cfg)
            self.assertIn("roots", cfg)

    def test_harness_does_not_allow_config_creation(self):
        """
        源码级断言：脚手架取基线配置时必须关掉「找不到就创建」。

        这条刻意做得「笨」一点 —— 那个隐患只在灾难场景下才显形（要先把
        真实 config.json 挪走），用行为测试很难覆盖，所以直接盯住那行调用。
        """
        with io.open(self.HARNESS, encoding="utf-8") as fh:
            src = fh.read()

        self.assertNotIn(
            "config_module.load()", src,
            "脚手架又用回了不带参数的 load()：一旦真实 config.json 缺失，"
            "它会被重新创建并覆盖真实口令文件")
        self.assertIn("create_if_missing=False", src,
                      "脚手架没有显式关掉配置创建")


if __name__ == "__main__":
    unittest.main(verbosity=2)

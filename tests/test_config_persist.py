# -*- coding: utf-8 -*-
"""
「配置写回哪个文件」的回归测试。

★ 这组测试对应一次**真实事故**，不是假想的风险：

    测试里起的服务是用 `--config <临时目录>/config.json` 启动的，
    但 `AppState.persist()`（改壁纸、改密码时会调）走的是 `config.save(cfg)`
    —— 不带路径，于是落到模块默认的 `CONFIG_PATH`，也就是**项目根目录那份
    真实部署的 config.json**。结果跑一次测试就把线上配置覆盖成了测试配置：
    端口变成随机值、host 变成 127.0.0.1、标题变成 fileweb-test、用户名变成
    pwuser。服务一重启就换端口、局域网也访问不到，而且原来的口令全部失效。

    `load()` 本来就知道自己读的是哪个文件，只是这个信息在把 cfg 传给
    `create_app()` 之后就丢了。修法是让 `load()` 把来源路径记在 cfg 里
    （沿用项目已有的 `_` 前缀内部键约定，如 `_raw_auth_keys`），
    `save()` 在没有显式路径时优先用它。

所以这里钉两条：
    1. `save(cfg)` 不带路径时，必须写回 cfg 被读出来的那个文件；
    2. 读的是临时配置时，**真实 config.json 必须一个字节都不变**。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import unittest

from fileweb import config as config_module


def _digest(path: str) -> str:
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


class ConfigPersistPathTests(unittest.TestCase):

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-cfgpath-")
        self.target = os.path.join(self.work, "config.json")

    def _write_minimal(self, **server):
        payload = {"server": dict({"host": "127.0.0.1", "port": 1234,
                                   "title": "temp"}, **server)}
        with open(self.target, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)

    # -- 基本契约 -----------------------------------------------------------

    def test_load_records_where_it_read_from(self):
        self._write_minimal()
        cfg = config_module.load(self.target)
        self.assertEqual(cfg.get("_cfg_path"), self.target,
                         "load() 应当记住配置的来源路径")

    def test_save_without_a_path_writes_back_to_the_loaded_file(self):
        self._write_minimal()
        cfg = config_module.load(self.target)

        cfg["server"]["port"] = 4321
        config_module.save(cfg)          # 故意不传 path

        with open(self.target, encoding="utf-8") as fh:
            saved = json.load(fh)
        self.assertEqual(saved["server"]["port"], 4321,
                         "save() 没有写回它读出来的那个文件")

    def test_internal_keys_are_not_persisted(self):
        self._write_minimal()
        cfg = config_module.load(self.target)
        config_module.save(cfg)

        with open(self.target, encoding="utf-8") as fh:
            raw = fh.read()
        self.assertNotIn("_cfg_path", raw, "内部键不该落盘")
        self.assertNotIn("_raw_auth_keys", raw)

    def test_explicit_path_still_wins(self):
        """显式传路径时要覆盖来源路径 —— 这是给「另存一份」留的口子。"""
        self._write_minimal()
        cfg = config_module.load(self.target)

        other = os.path.join(self.work, "other.json")
        cfg["server"]["port"] = 9999
        config_module.save(cfg, other)

        with open(other, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["server"]["port"], 9999)
        with open(self.target, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["server"]["port"], 1234,
                             "显式指定了别的路径，原文件就不该被动")

    # -- ★ 真实事故的复现场景 ----------------------------------------------

    def test_touching_a_temp_config_leaves_the_real_one_untouched(self):
        """
        读临时配置、改一改、保存 —— 项目根目录的真实 config.json 必须纹丝不动。

        这条就是那次事故的最小复现：只要 save() 的目标路径退化成模块默认值，
        它立刻会红。
        """
        real = config_module.CONFIG_PATH
        if not os.path.isfile(real):
            self.skipTest("项目根目录没有 config.json（未部署），跳过")

        before = _digest(real)

        shutil.copyfile(real, self.target)
        cfg = config_module.load(self.target)
        # 模拟测试环境对配置的覆盖（端口/主机/标题/用户名都会变）
        cfg["server"] = {"host": "127.0.0.1", "port": 65001, "title": "fileweb-test"}
        cfg["auth"] = dict(cfg.get("auth") or {})
        cfg["auth"]["username"] = "pwuser"
        config_module.save(cfg)

        # 临时那份确实被更新了
        with open(self.target, encoding="utf-8") as fh:
            self.assertEqual(json.load(fh)["server"]["port"], 65001)

        # 而真实那份必须一个字节都没变
        self.assertEqual(before, _digest(real),
                         "真实部署的 config.json 被测试改写了！")


if __name__ == "__main__":
    unittest.main(verbosity=2)

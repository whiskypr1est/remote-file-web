# -*- coding: utf-8 -*-
"""
界面状态存储（fileweb/userstate.py）单元测试
==========================================

只用标准库 unittest。

覆盖的关键行为：
    * 文件缺失 / 内容损坏 / 根节点不是对象时，load() 必须返回 {} 而**不抛异常**
      （这份状态只影响「桌面看起来是否和上次一样」，不该把桌面加载搞崩）
    * save() 是原子的：不留临时文件、不产生半截内容
    * 非 dict 输入、无法 JSON 序列化的内容、超过体积上限的内容都要被拒绝
    * 上限按**序列化后的 UTF-8 字节数**计算（中文一个字 3 字节）
    * 并发读改写不会互相打断（模块级锁）
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import unittest

from fileweb import userstate


class UserStateLoadTests(unittest.TestCase):
    """load() 的容错行为。"""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-us-load-")
        self.path = os.path.join(self.work, "user_state.json")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_missing_file_returns_empty_dict(self):
        """文件不存在 = 还没存过状态，属于正常情况，返回空字典。"""
        self.assertEqual(userstate.load(self.path), {})

    def test_corrupt_json_returns_empty_dict_without_raising(self):
        """半截 JSON（比如写盘时断电）不能让整个桌面加载失败。"""
        with open(self.path, "w", encoding="utf-8") as fh:
            fh.write('{"windows": [1, 2, ')     # 故意截断

        self.assertEqual(userstate.load(self.path), {})

    def test_non_object_root_returns_empty_dict(self):
        """根节点是数组/字符串等都不是合法状态，一律退回空字典。"""
        for content in ("[1, 2, 3]", '"just a string"', "42", "null"):
            with open(self.path, "w", encoding="utf-8") as fh:
                fh.write(content)
            self.assertEqual(userstate.load(self.path), {}, "内容 %r 应当被忽略" % content)

    def test_roundtrip_preserves_arbitrary_structure(self):
        """服务端不解释状态内容，必须原样存取（含中文与嵌套结构）。"""
        payload = {
            "version": 1,
            "windows": [
                {"id": "win_1", "title": "项目资料", "x": 10, "y": 20, "w": 800, "h": 600},
                {"id": "win_2", "cwd": {"root": "share", "path": "Work/中文目录"}},
            ],
            "active": "win_2",
            "view": "icons",
        }
        userstate.save(payload, self.path)
        self.assertEqual(userstate.load(self.path), payload)

    def test_utf8_bom_file_is_readable(self):
        """用记事本编辑过的文件会带 BOM，读取时要能容错。"""
        with open(self.path, "w", encoding="utf-8-sig") as fh:
            json.dump({"view": "list"}, fh, ensure_ascii=False)

        self.assertEqual(userstate.load(self.path), {"view": "list"})


class UserStateSaveTests(unittest.TestCase):
    """save() 的校验与原子性。"""

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-us-save-")
        self.path = os.path.join(self.work, "user_state.json")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def test_non_dict_is_rejected(self):
        """状态必须是 JSON 对象：客户端不能拿它当任意文件的写入通道。"""
        for bad in ([], "text", 42, None, True):
            with self.assertRaises(TypeError):
                userstate.save(bad, self.path)  # type: ignore[arg-type]

    def test_not_json_serializable_is_rejected(self):
        """含无法序列化的值（比如 set/object）要明确报错，而不是写坏文件。"""
        with self.assertRaises(ValueError):
            userstate.save({"bad": {1, 2, 3}}, self.path)

    def test_oversized_state_is_rejected(self):
        """超过上限必须抛错（由路由层翻译成 HTTP 413），不能悄悄写爆磁盘。"""
        # 上限 256KB，这里塞 400KB 的纯 ASCII，确保稳定越界
        huge = {"blob": "x" * (400 * 1024)}
        with self.assertRaises(userstate.UserStateTooLargeError):
            userstate.save(huge, self.path)
        self.assertFalse(os.path.exists(self.path), "被拒绝的状态不该落盘")

    def test_limit_counts_utf8_bytes_not_characters(self):
        """
        上限按 UTF-8 字节数算：中文一个字 3 字节。

        若按字符数算，同样大小的中文状态实际会写出 3 倍体积，
        上限就形同虚设。
        """
        # 刚好超过上限的三分之一但按字符数远未超限的内容：
        # 100000 个汉字 = 300000 字节 > 256KB，而 100000 字符 < 262144
        text = "中" * 100_000
        self.assertGreater(len(text.encode("utf-8")), userstate.MAX_STATE_BYTES)
        self.assertLess(len(text), userstate.MAX_STATE_BYTES)

        with self.assertRaises(userstate.UserStateTooLargeError):
            userstate.save({"blob": text}, self.path)

    def test_exactly_within_limit_is_accepted(self):
        """边界之内要能正常写入，避免把合法状态误判为过大。"""
        # 留出 JSON 外框（{"blob": ""} 等）的余量
        text = "x" * (userstate.MAX_STATE_BYTES - 64)
        userstate.save({"blob": text}, self.path)
        self.assertEqual(userstate.load(self.path)["blob"], text)

    def test_save_leaves_no_temp_files(self):
        """原子写必须把临时文件替换掉，不能在数据目录里留垃圾。"""
        userstate.save({"view": "icons"}, self.path)

        leftovers = [
            name for name in os.listdir(self.work)
            if name != os.path.basename(self.path)
        ]
        self.assertEqual(leftovers, [], "原子写不应留下临时文件：%s" % leftovers)
        self.assertEqual(sorted(os.listdir(self.work)), ["user_state.json"])

    def test_save_overwrites_atomically(self):
        """覆盖保存后内容完整可读（整体替换语义，不做合并）。"""
        userstate.save({"a": 1}, self.path)
        userstate.save({"b": 2}, self.path)
        self.assertEqual(userstate.load(self.path), {"b": 2})

    def test_clear_removes_file(self):
        """clear() 用于「重置桌面布局」，删掉文件比写空对象语义更干净。"""
        userstate.save({"a": 1}, self.path)
        self.assertTrue(userstate.clear(self.path))
        self.assertFalse(os.path.exists(self.path))
        # 再删一次应返回 False 而不是报错
        self.assertFalse(userstate.clear(self.path))

    def test_concurrent_saves_never_produce_corrupt_file(self):
        """
        多线程并发保存后文件必须仍是**完整可解析的 JSON**。

        模块级锁保证读改写不会交错，原子替换保证不会出现半截文件；
        这里只断言最终文件可解析（最后写入者获胜），不假设顺序。
        """
        errors = []

        def worker(index: int) -> None:
            try:
                for _ in range(25):
                    userstate.save({"worker": index, "data": "x" * 200}, self.path)
                    userstate.load(self.path)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(errors, [], "并发保存不应报错：%s" % errors)

        with open(self.path, "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)          # 解析失败即说明文件被写坏
        self.assertIn("worker", data)


class UserStatePathTests(unittest.TestCase):
    """路径解析：默认值必须可被 config 覆盖。"""

    # 这里传 _AUTH_STUB 只是为了**少走一条无关的分支**，不是必需的防御：
    # 「口令文件被意外写到真实部署旁边」这个隐患已经在 config.py 根部修掉了
    # —— 裸调 prepare()（不给 cfg_path）现在一律不落盘，见
    # fileweb/config.py 的 _password_file_beside() 与 tests/test_config_safety.py
    # 的回归测试。这份 stub 作为第二层防御保留（万一将来 prepare() 又被改成
    # 有副作用，这里也不会踩到）。
    _AUTH_STUB = {"password_hash": "pbkdf2_sha256$1$stub$stub", "password": ""}

    def test_default_path_is_next_to_project_root(self):
        """默认落在项目根目录下，与 desktop_shortcuts.json 同级，便于备份。"""
        self.assertTrue(userstate.USER_STATE_PATH.endswith("user_state.json"))
        self.assertEqual(
            os.path.dirname(userstate.USER_STATE_PATH),
            userstate.BASE_DIR,
        )

    def test_config_prepare_publishes_absolute_path(self):
        """config.prepare() 要把配置里的相对路径解析成绝对路径并下发给本模块。"""
        from fileweb import config as config_module

        saved = userstate.ACTIVE_PATH
        try:
            cfg = config_module.prepare({
                "auth": dict(self._AUTH_STUB),
                "user_state_path": "./some_dir/state.json",
            })
            self.assertTrue(
                os.path.isabs(cfg["user_state_path"]),
                "prepare() 应把相对路径解析成绝对路径：%s" % cfg["user_state_path"],
            )
            self.assertTrue(cfg["user_state_path"].endswith("state.json"))
            # 并且 userstate 实际使用的路径要跟着变
            self.assertEqual(userstate.ACTIVE_PATH, cfg["user_state_path"])
        finally:
            userstate.ACTIVE_PATH = saved

    def test_config_prepare_falls_back_on_empty_value(self):
        """配置里留空时必须退回默认文件名，而不是解析成「一个目录」。"""
        from fileweb import config as config_module

        saved = userstate.ACTIVE_PATH
        try:
            cfg = config_module.prepare({
                "auth": dict(self._AUTH_STUB),
                "user_state_path": "",
            })
            self.assertTrue(cfg["user_state_path"].endswith("user_state.json"))
            self.assertEqual(
                os.path.dirname(cfg["user_state_path"]),
                config_module.BASE_DIR,
            )
        finally:
            userstate.ACTIVE_PATH = saved


    def test_relative_state_path_follows_the_config_file(self):
        """
        ★ 相对路径的基准是**当前配置文件所在目录**，不是代码目录。

        这是那次「测试把真实部署的 user_state.json 写坏」事故的根因回归测试：
        用临时配置跑起来的实例，状态必须落在临时配置旁边，
        绝不能去写项目根目录里那份**属于真实部署**的状态文件。
        """
        from fileweb import config as config_module

        saved = userstate.ACTIVE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfg_path = os.path.join(tmp, "config.json")
                cfg = config_module.prepare({
                    "auth": dict(self._AUTH_STUB),
                    "user_state_path": "user_state.json",     # 默认值（相对）
                }, cfg_path)

                self.assertEqual(
                    os.path.dirname(cfg["user_state_path"]),
                    os.path.normpath(tmp),
                    "相对路径必须解析到配置文件所在目录：%s" % cfg["user_state_path"],
                )
                self.assertNotEqual(
                    os.path.dirname(cfg["user_state_path"]),
                    config_module.BASE_DIR,
                    "临时配置的状态文件绝不能落到代码目录（真实部署那里）",
                )
        finally:
            userstate.ACTIVE_PATH = saved

    def test_absolute_state_path_is_honoured(self):
        """用户显式写了绝对路径时必须完全尊重，不能被「目录基准」改写。"""
        from fileweb import config as config_module

        saved = userstate.ACTIVE_PATH
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wanted = os.path.join(tmp, "custom", "desk.json")
                cfg = config_module.prepare({
                    "auth": dict(self._AUTH_STUB),
                    "user_state_path": wanted,
                }, os.path.join(tmp, "config.json"))
                self.assertEqual(cfg["user_state_path"], os.path.normpath(wanted))
        finally:
            userstate.ACTIVE_PATH = saved

    def test_default_deployment_path_is_unchanged(self):
        """
        默认部署下路径必须与修之前**完全一致**（就在 config.json 旁边）。

        防「修 bug 时把正常行为也改了」：用户升级后不该发现状态文件换了地方，
        否则他会以为自己的桌面布局丢了。
        """
        from fileweb import config as config_module

        saved = userstate.ACTIVE_PATH
        try:
            cfg = config_module.prepare({
                "auth": dict(self._AUTH_STUB),
                "user_state_path": config_module.DEFAULT_CONFIG["user_state_path"],
            }, config_module.CONFIG_PATH)
            self.assertEqual(
                os.path.dirname(cfg["user_state_path"]),
                config_module.BASE_DIR,
            )
        finally:
            userstate.ACTIVE_PATH = saved


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
新增功能的回归测试：文件复制/移动，以及命令行（CMD）窗口
=======================================================

只用标准库 + 已随 `uvicorn[standard]` 安装的 `websockets`
（不引入 pytest / httpx / requests）。

覆盖的关键行为：
    * 复制/移动遇重名必须**自动改名**，绝不覆盖已有文件
    * 不能把文件夹复制/移动到它自己或它的子孙目录里（否则无限递归）
    * 受保护目录禁止作为写入目标；被拒绝时源文件必须原封不动
    * 根目录本身不能作为复制/移动的源
    * 命令行：创建会话需要 CSRF；WebSocket **必须登录**且 Origin 同源；
      不存在的 sid / 别人的 sid 都要被拒绝
    * 命令行确实能执行命令，且工作目录在命令之间保持
    * terminal.max_sessions 上限生效；terminal.enabled=false 时整体关闭
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest

from tests._harness import ServerProcess


# ---------------------------------------------------------------------------
# WebSocket 小工具（客户端侧）
# ---------------------------------------------------------------------------

def _connect(url, headers):
    """按 websockets 的版本兼容地建立连接。"""
    import websockets

    try:
        return websockets.connect(url, additional_headers=headers,
                                 open_timeout=20, close_timeout=5)
    except TypeError:  # 旧版本参数名是 extra_headers
        return websockets.connect(url, extra_headers=headers,
                                  open_timeout=20, close_timeout=5)


def _ws_reject_probe(url, headers):
    """
    尝试建立连接，返回 (是否被拒绝, 说明)。

    被拒绝是**期望结果**：中间件在握手阶段就发 close(1008)，
    客户端表现为握手异常（InvalidStatus 之类）。
    """
    async def run():
        try:
            async with _connect(url, headers) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
            return False, "连接居然成功了（本应被拒绝）"
        except Exception as exc:  # noqa: BLE001
            return True, "%s: %s" % (type(exc).__name__, exc)

    return asyncio.run(run())


def _ws_run(url, headers, commands, marker, timeout=30):
    """连上去把命令依次发过去，收集输出直到出现 marker（或超时）。"""
    async def run():
        collected = ""
        async with _connect(url, headers) as ws:
            for command in commands:
                await ws.send(json.dumps({"type": "input", "data": command}))

            deadline = time.time() + timeout
            while time.time() < deadline and marker not in collected:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=8)
                except asyncio.TimeoutError:
                    break
                message = json.loads(raw)
                kind = message.get("type")
                if kind == "output":
                    collected += message.get("data", "")
                elif kind == "exit":
                    collected += "\n[进程已结束]"
                    break
                elif kind == "error":
                    collected += "\n[错误] %s" % message.get("message")
        return collected

    return asyncio.run(run())


def _ws_close_session(url, headers, timeout=15):
    """
    连上去并发送 {"type":"close"}，等确认消息回来后再断开。

    等确认（而不是发完就走）是必须的：服务端收到 close 后要杀整棵进程树，
    那是异步的；如果客户端立刻退出，测试进程可能在服务端清理完之前就
    开始下一步，从而看到「上一个会话还占着名额」的假象。
    """
    async def run():
        async with _connect(url, headers) as ws:
            await ws.send(json.dumps({"type": "close"}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except asyncio.TimeoutError:
                    break
                except Exception:  # noqa: BLE001 - 服务端关闭连接即视为已结束
                    break
        return True

    try:
        return asyncio.run(run())
    except Exception:  # noqa: BLE001 - 会话可能已经被回收，清理动作失败不算错
        return False


# ---------------------------------------------------------------------------
# 复制 / 移动
# ---------------------------------------------------------------------------

class CopyMoveTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-transfer-")
        cls.root = os.path.join(cls.work, "main")
        cls.other = os.path.join(cls.work, "other")
        cls.locked = os.path.join(cls.work, "locked")
        for path in (cls.root, cls.other, cls.locked):
            os.makedirs(path, exist_ok=True)

        protected = cls.locked

        def _extra(cfg):
            cfg["protected_paths"] = [protected]

        cls.server = ServerProcess(
            [
                {"id": "main", "name": "main", "path": cls.root, "readonly": False},
                {"id": "other", "name": "other", "path": cls.other, "readonly": False},
                {"id": "locked", "name": "locked", "path": cls.locked, "readonly": False},
            ],
            extra_config=_extra,
        ).start()
        cls.client = cls.server.login_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------------

    def _write(self, directory, name, content="x"):
        path = os.path.join(directory, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def _read(self, path):
        with open(path, "r", encoding="utf-8") as fh:
            return fh.read()

    def _transfer(self, endpoint, paths, target_root, target_path="", **kwargs):
        return self.client.json("POST", "/api/fs/" + endpoint, {
            "root": "main",
            "paths": paths,
            "target_root": target_root,
            "target_path": target_path,
        }, **kwargs)

    # -- 测试 ---------------------------------------------------------------

    def test_copy_in_same_dir_auto_renames(self):
        self._write(self.root, "copy-src.txt", "hello")

        status, data = self._transfer("copy", ["copy-src.txt"], "main")
        self.assertEqual(status, 200, data)

        self.assertTrue(
            os.path.isfile(os.path.join(self.root, "copy-src (1).txt")),
            "重名时必须自动改名（copy-src (1).txt），而不是覆盖原文件",
        )
        self.assertEqual(self._read(os.path.join(self.root, "copy-src.txt")), "hello",
                         "原文件必须原封不动")
        self.assertEqual(self._read(os.path.join(self.root, "copy-src (1).txt")), "hello",
                         "复制出来的内容必须与原文件一致")
        self.assertEqual(data.get("copied"), ["copy-src.txt"])
        self.assertEqual([item["to"] for item in data.get("renamed", [])],
                         ["copy-src (1).txt"])

    def test_copy_directory_into_own_subdir_is_rejected(self):
        os.makedirs(os.path.join(self.root, "tree", "inner"), exist_ok=True)

        status, data = self._transfer("copy", ["tree"], "main", "tree/inner")
        self.assertEqual(
            status, 400,
            "把文件夹复制进它自己的子目录必须被拒绝，否则会无限递归：%s" % data,
        )

    def test_move_across_roots(self):
        src = self._write(self.root, "move-src.txt", "M")

        status, data = self._transfer("move", ["move-src.txt"], "other")
        self.assertEqual(status, 200, data)

        self.assertFalse(os.path.exists(src), "移动之后源文件应当不存在了")
        self.assertTrue(os.path.isfile(os.path.join(self.other, "move-src.txt")))
        self.assertEqual(self._read(os.path.join(self.other, "move-src.txt")), "M")
        self.assertEqual(data.get("moved"), ["move-src.txt"])

    def test_move_auto_renames_and_never_overwrites(self):
        self._write(self.root, "dup.txt", "from-main")
        self._write(self.other, "dup.txt", "already-here")

        status, data = self._transfer("move", ["dup.txt"], "other")
        self.assertEqual(status, 200, data)

        self.assertEqual(self._read(os.path.join(self.other, "dup.txt")), "already-here",
                         "目标已存在的同名文件绝不能被覆盖")
        self.assertEqual(self._read(os.path.join(self.other, "dup (1).txt")), "from-main")

    def test_copy_into_protected_dir_is_rejected(self):
        self._write(self.root, "protected-copy.txt", "P")

        status, data = self._transfer("copy", ["protected-copy.txt"], "locked")
        self.assertEqual(status, 403, "受保护目录不能作为写入目标：%s" % data)

    def test_move_into_protected_dir_is_rejected(self):
        src = self._write(self.root, "protected-move.txt", "P")

        status, data = self._transfer("move", ["protected-move.txt"], "locked")
        self.assertEqual(status, 403, "受保护目录不能作为写入目标：%s" % data)
        self.assertTrue(os.path.exists(src), "被拒绝时源文件必须原封不动")

    def test_root_itself_cannot_be_transferred(self):
        status, data = self._transfer("copy", [""], "other")
        self.assertEqual(status, 403, "根目录本身不能作为复制/移动的源：%s" % data)

    def test_missing_source_is_404(self):
        status, data = self._transfer("copy", ["definitely-missing.txt"], "main")
        self.assertEqual(status, 404, data)

    def test_transfer_requires_csrf(self):
        status, _ = self._transfer("copy", ["whatever.txt"], "main", with_csrf=False)
        self.assertEqual(status, 403, "复制/移动属于改状态请求，必须带 CSRF 令牌")

    def test_terminal_is_disabled_by_default_in_this_server(self):
        """本类的服务没开 terminal，用来验证 enabled=false 时整体关闭。"""
        status, data = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(status, 403,
                         "terminal.enabled=false 时创建会话必须 403：%s" % data)


# ---------------------------------------------------------------------------
# 命令行窗口
# ---------------------------------------------------------------------------

class TerminalTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-terminal-")
        cls.root = os.path.join(cls.work, "root")
        os.makedirs(cls.root, exist_ok=True)

        def _extra(cfg):
            cfg["terminal"]["enabled"] = True
            cfg["terminal"]["shell"] = "cmd.exe"
            cfg["terminal"]["max_sessions"] = 4
            cfg["terminal"]["idle_timeout_seconds"] = 300

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            extra_config=_extra,
        ).start()
        cls.client = cls.server.login_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------------

    def _origin(self):
        return "http://127.0.0.1:%d" % self.server.port

    def _ws_url(self, sid):
        return "ws://127.0.0.1:%d/api/terminal/ws?sid=%s" % (self.server.port, sid)

    def _headers(self, **overrides):
        headers = {"Cookie": self.client.cookie, "Origin": self._origin()}
        headers.update(overrides)
        return headers

    def _create_session(self):
        status, data = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("id"), "创建会话必须返回非空 sid：%s" % data)
        # ★ 每个会话都登记清理：会话现在能跨断开存活（这正是本特性的目的），
        # 所以用例结束时必须**显式**结束它，否则它一直占着 terminal.max_sessions
        # 名额，同类的后续用例就会因为 429 而失败（而且失败位置会随执行顺序漂移）。
        self.addCleanup(self._release, data["id"])
        return data["id"]

    def _release(self, sid):
        """
        连上去，然后**显式**要求服务端结束这个会话。

        ★ 这里必须发 {"type":"close"}：会话可分离之后，仅仅断开 WebSocket
        只会把会话「挂起」（进程继续跑、输出继续缓存，等用户重连），
        不再等于结束会话。测试要清理掉自己创建的会话，就得明确要求关闭，
        否则它们会一直占着 max_sessions 名额，后面的用例会拿不到新会话。
        """
        _ws_close_session(self._ws_url(sid), self._headers())

    # -- 测试 ---------------------------------------------------------------

    def test_session_requires_csrf(self):
        status, _ = self.client.json("POST", "/api/terminal/session", {}, with_csrf=False)
        self.assertEqual(status, 403, "创建命令行会话必须带 CSRF 令牌")

    def test_ws_without_cookie_is_rejected(self):
        """最关键的一条：未登录绝不能连上终端。"""
        sid = self._create_session()
        try:
            rejected, why = _ws_reject_probe(self._ws_url(sid),
                                             {"Origin": self._origin()})  # 故意不带 Cookie
            self.assertTrue(rejected, "未登录的 WebSocket 必须被拒绝：" + why)
        finally:
            self._release(sid)

    def test_ws_with_foreign_origin_is_rejected(self):
        sid = self._create_session()
        try:
            rejected, why = _ws_reject_probe(
                self._ws_url(sid), self._headers(Origin="http://evil.example"),
            )
            self.assertTrue(rejected, "跨站 Origin 的 WebSocket 必须被拒绝：" + why)
        finally:
            self._release(sid)

    def test_ws_without_origin_is_rejected(self):
        sid = self._create_session()
        try:
            rejected, why = _ws_reject_probe(
                self._ws_url(sid), {"Cookie": self.client.cookie},   # 故意不带 Origin
            )
            self.assertTrue(rejected, "缺少 Origin 的 WebSocket 必须被拒绝：" + why)
        finally:
            self._release(sid)

    def test_ws_with_bogus_sid_is_rejected(self):
        rejected, why = _ws_reject_probe(
            self._ws_url("definitely-not-a-real-sid"), self._headers(),
        )
        self.assertTrue(rejected, "不存在的 sid 必须被拒绝：" + why)

    def test_command_executes_and_cwd_persists(self):
        sid = self._create_session()
        output = _ws_run(self._ws_url(sid), self._headers(),
                         ["echo TERMINAL_OK\r\n"], "TERMINAL_OK")
        self.assertIn("TERMINAL_OK", output,
                      "命令行必须真的能执行命令并回传输出：%r" % output)

        sid2 = self._create_session()
        output2 = _ws_run(
            self._ws_url(sid2), self._headers(),
            ["cd /d C:\\Windows\r\n", "dir /b *.ini\r\n"], "win.ini",
        )
        self.assertIn("win.ini", output2.lower(),
                      "工作目录必须跨命令保持，否则 dir 不会列出 C:\\Windows 的内容：%r"
                      % output2)


class TerminalLimitTests(unittest.TestCase):
    """单独起一个 max_sessions=1 的实例，验证会话数上限。"""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-termlimit-")
        cls.root = os.path.join(cls.work, "root")
        os.makedirs(cls.root, exist_ok=True)

        def _extra(cfg):
            cfg["terminal"]["enabled"] = True
            cfg["terminal"]["max_sessions"] = 1
            cfg["terminal"]["idle_timeout_seconds"] = 300

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            extra_config=_extra,
        ).start()
        cls.client = cls.server.login_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    def test_max_sessions_is_enforced(self):
        status, first = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(status, 200, first)

        status2, second = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(status2, 429,
                         "超过 terminal.max_sessions 必须被拒绝：%s" % second)

        # ★ 收尾必须**显式关闭**而非「连上再断开」：断开只是让会话挂起
        # （进程继续跑、继续占名额），只有发 {"type":"close"} 才真正结束它。
        # 而且这次尝试创建（并必然失败）的请求没有返回 sid，只能清理第一个。
        _ws_close_session(
            "ws://127.0.0.1:%d/api/terminal/ws?sid=%s" % (self.server.port, first["id"]),
            {"Cookie": self.client.cookie,
             "Origin": "http://127.0.0.1:%d" % self.server.port},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)

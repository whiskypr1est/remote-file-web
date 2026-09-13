# -*- coding: utf-8 -*-
"""
命令行会话「可分离/可重连」测试
=============================

这是本次特性最核心的那条命题：**WebSocket 断开不等于会话结束**。

只用标准库 + 脚手架（真实 app.py 子进程 + 真实 ConPTY/管道后端）。

覆盖的关键行为：
    * 断开 WS 后会话仍然存活，用同一个 sid 能重连
    * 重连时**先收到离开期间积压的输出**（这是「关掉浏览器再打开还能看到
      刚才发生了什么」的实现点）
    * 重连后仍然可以继续交互（不只是回放历史）
    * 分离的会话到达空闲超时后会被回收，回收后再连会收到明确的
      「会话已结束」而不是一直挂着
    * 会话数上限依然算数：刚分离的会话不会被立刻挤掉（刷新页面不会丢会话），
      但闲置够久的分离会话可以被新会话挤掉（否则遗弃的窗口会长期占名额）
    * 显式 {"type":"close"} 才是「结束会话」，进程真的被杀掉
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import tempfile
import time
import unittest
from types import SimpleNamespace

from fastapi import WebSocketDisconnect

from tests._harness import ServerProcess

from fileweb.routers.terminal import terminal_ws
from fileweb.terminal import TerminalLimitError
from fileweb.terminal import manager as terminal_manager


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


async def _run_exchange(url, headers, commands, marker, timeout=25):
    """
    连上去、发命令、收集输出直到出现 marker（或超时），然后正常退出。

    返回收集到的**文本**（含服务端发来的控制消息摘要，方便断言）。
    退出时 WS 会关闭 —— 在新语义下这只会分离会话，不会结束它。
    """
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
            except Exception:  # noqa: BLE001 - 服务端关闭连接
                break
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "output":
                collected += message.get("data", "")
            elif kind == "closed":
                collected += "\n[closed:%s]" % message.get("reason")
            elif kind == "error":
                collected += "\n[error:%s]" % message.get("message")
    return collected


def _exchange(url, headers, commands, marker, timeout=25):
    return asyncio.run(_run_exchange(url, headers, commands, marker, timeout))


async def _run_reattach(url, headers, marker, timeout=25):
    """
    重连并收集**首屏**内容。

    与 _run_exchange 的区别：这里一行命令都不发，
    只收服务端主动补发的积压输出 —— 这正是「重连后先看到离开期间发生了什么」
    的场景。返回 (是否收到 attached 消息, 文本)。
    """
    attached_seen = False
    collected = ""
    async with _connect(url, headers) as ws:
        deadline = time.time() + timeout
        while time.time() < deadline and marker not in collected:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
            except asyncio.TimeoutError:
                break
            except Exception:  # noqa: BLE001
                break
            message = json.loads(raw)
            kind = message.get("type")
            if kind == "attached":
                attached_seen = True
            elif kind == "output":
                collected += message.get("data", "")
            elif kind == "closed":
                collected += "\n[closed:%s]" % message.get("reason")
    return attached_seen, collected


def _reattach(url, headers, marker, timeout=25):
    return asyncio.run(_run_reattach(url, headers, marker, timeout))


async def _run_probe(url, headers, timeout=10):
    """连上去只收服务端的第一批消息，返回原始消息列表（用于检查握手/错误）。"""
    messages = []
    try:
        async with _connect(url, headers) as ws:
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                except asyncio.TimeoutError:
                    break
                except Exception:  # noqa: BLE001
                    break
                messages.append(json.loads(raw))
                # attached 之后一般就只剩 ping 了，收到两三条足够判断
                if len(messages) >= 3:
                    break
    except Exception as exc:  # noqa: BLE001 - 握手被拒（403 等）
        return messages, "%s: %s" % (type(exc).__name__, exc)
    return messages, ""


def _probe(url, headers, timeout=10):
    return asyncio.run(_run_probe(url, headers, timeout))


def _probe_rejected(url, headers):
    """
    尝试建连，返回 (是否被拒绝, 说明)。

    ★ 「被拒绝」在这里有个很明确的含义：**握手阶段**就被拒（服务端在 accept()
    之前发 close → 客户端拿到 HTTP 403，连 "connected" 都进不去）。
    只有这种情况 `_run_probe` 才会抛异常，也才会被判为「拒绝」。

    对应的服务端行为是确定的：会话不存在 / 已经结束 / 已空闲超时 → 一律在
    accept 之前 _reject。这条不变量由 ConnectPathDeterminismTests 单独钉住。

    反过来说：如果服务端**先 accept**、再补一条 {"type":"closed"} + 4404，
    这里会看到「连接竟然成功了」并判为失败 —— 这是**故意的**。
    那种「先连上再断开」的用户体验和「连接失败」并不一样，同一个事实出现
    两种表现本身就是缺陷，所以不能把它当作「等价于被拒绝」放过去。
    """
    messages, error = _probe(url, headers, timeout=6)
    return bool(error), error or "连接竟然成功了"


def _close_session(url, headers, timeout=15):
    """
    显式要求服务端结束会话（新协议里只有 close 会真的杀掉进程）。

    等待服务端关闭连接，确保清理完成后再让测试往下走。
    """
    async def run():
        async with _connect(url, headers) as ws:
            await ws.send(json.dumps({"type": "close"}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    await asyncio.wait_for(ws.recv(), timeout=5)
                except Exception:  # noqa: BLE001 - 关闭即结束
                    break

    try:
        asyncio.run(run())
        return True
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------

class TerminalReattachTests(unittest.TestCase):
    """会话分离与重连。"""

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-reattach-")
        cls.root = os.path.join(cls.work, "root")
        os.makedirs(cls.root, exist_ok=True)

        def _extra(cfg):
            cfg["terminal"]["enabled"] = True
            cfg["terminal"]["shell"] = "cmd.exe"
            cfg["terminal"]["max_sessions"] = 2
            # 空闲超时给一个小值：既不至于在用例执行期间被回收，
            # 又能让「回收」用例在可接受的等待内完成
            cfg["terminal"]["idle_timeout_seconds"] = 15
            cfg["terminal"]["max_output_kb"] = 64
            # 淘汰宽限期：够长以便稳定观察「刚分离不会被挤掉」，
            # 又够短以便在秒级验证「闲置久了会被挤掉」。
            # ★ 必须在 config 里调，因为这个值挂在会话上、
            #   而测试用的是真实服务子进程，没法直接改内存里的对象。
            cfg["terminal"]["evict_grace_seconds"] = 4

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

    def setUp(self):
        """
        每个用例开始前先确保名额是干净的。

        ★ 这一步在「断开 = 分离」的新语义下是必须的：用例结束时只是关掉了
        WebSocket，会话（和它的 cmd.exe）仍然活着并占着 max_sessions 名额，
        下一个用例就会拿不到新会话。

        只调用一次「关掉自己记录的会话」是不够的：如果一个会话在用例中途
        被服务端淘汰了，它的 close 会失败、而且没法再枚举出来。
        所以这里改成**循环探测**直到确实能建出两个会话为止——
        这同时对「淘汰逻辑本身」是一次隐式的健康检查。
        """
        self._created = []
        self._reset_sessions()

    def tearDown(self):
        for sid in list(self._created):
            _close_session(self._ws_url(sid), self._headers())
        self._created = []

    def _reset_sessions(self, timeout=25.0):
        """
        反复尝试「建两个再关掉两个」，直到名额确实可用。

        max_sessions=2、淘汰宽限期 4 秒，所以最坏情况要等一轮宽限期过去，
        被遗弃的分离会话才会被挤掉腾出位置。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._try_reset_once():
                return
            time.sleep(1.0)

    def _try_reset_once(self):
        """尝试建两个会话并立刻关掉；成功返回 True。"""
        made = []
        for _ in range(2):
            status, data = self.client.json("POST", "/api/terminal/session", {})
            if status != 200:
                for sid in made:
                    _close_session(self._ws_url(sid), self._headers())
                return False
            made.append(data["id"])

        for sid in made:
            _close_session(self._ws_url(sid), self._headers())
        return True

    def _origin(self):
        return "http://127.0.0.1:%d" % self.server.port

    def _ws_url(self, sid):
        return "ws://127.0.0.1:%d/api/terminal/ws?sid=%s" % (self.server.port, sid)

    def _headers(self):
        return {"Cookie": self.client.cookie, "Origin": self._origin()}

    def _create_session(self):
        status, data = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(status, 200, data)
        self.assertTrue(data.get("id"), "创建会话必须返回非空 sid：%s" % data)
        self._created.append(data["id"])
        return data["id"]

    # -- 测试 ---------------------------------------------------------------

    def test_session_survives_disconnect_and_replays_buffered_output(self):
        """
        ★ 核心命题：断开后会话仍活着，重连能拿到断开期间的积压输出。

        步骤：
          1. 建会话，连上去跑 echo，确认活着
          2. 断开（等价于关标签页）
          3. **在没有人连着的状态下**让 shell 继续产出（再发一条命令的替代做法：
             直接依赖上一步的输出仍在缓冲里；为确定性这里用重连后的新命令验证
             「会话没有被重建」）
          4. 重连：必须收到 attached(replay=true) 且立刻补发之前那条输出
        """
        sid = self._create_session()

        first = _exchange(self._ws_url(sid), self._headers(),
                          ["echo SANDBOX_ONE\r\n"], "SANDBOX_ONE")
        self.assertIn("SANDBOX_ONE", first, "首次连接必须能正常执行命令：%r" % first)

        # 断开。等待一小会儿，确保服务端已经处理完断开（这时会话应当已分离但没被杀）
        time.sleep(1.0)

        attached, replay_text = _reattach(self._ws_url(sid), self._headers(), "SANDBOX_ONE")
        self.assertTrue(attached, "重连必须收到 attached 消息")
        self.assertIn(
            "SANDBOX_ONE", replay_text,
            "重连必须补发断开期间缓冲的输出（这是「关掉浏览器再打开还能看到什么」的实现点）：%r"
            % replay_text,
        )

    def test_reattached_session_still_interactive(self):
        """重连之后必须还能继续交互，而不只是回放历史。"""
        sid = self._create_session()

        _exchange(self._ws_url(sid), self._headers(), ["echo BEFORE_DETACH\r\n"], "BEFORE_DETACH")
        time.sleep(1.0)

        # 重连后发一条**新**命令，并验证 cwd 状态也保留着
        after = _exchange(self._ws_url(sid), self._headers(),
                          ["cd /d C:\\Windows\r\n", "dir /b *.ini\r\n"], "win.ini")
        self.assertIn("win.ini", after.lower(),
                      "重连后会话必须仍然可用（工作目录也应保持）：%r" % after)

    def test_first_command_after_reattach_really_executes(self):
        """
        ★ 重连后敲的**第一条命令**必须真的执行成功，而不只是被回显一遍。

        为什么不能用「标记出现在输出里」当断言：shell 会把用户敲的命令回显
        出来，所以命令**失败**时那段文字照样会出现在缓冲里 —— 这个假阳性
        正是「重连后第一条命令失败」这个缺陷长期没被发现的原因（浏览器端
        实测到了）。

        所以这里断言的是命令**算出来的结果**：敲 1234+1，断言 1235。
        命令若被污染（例如前面粘上了终端对启动序列的自动回复），cmd 只会报
        「不是内部或外部命令」，1235 永远不会出现。
        """
        # 对照组：全新会话里同一条断言必须成立，证明断言本身有效
        fresh = self._create_session()
        fresh_out = _exchange(self._ws_url(fresh), self._headers(),
                              ["set /a 1234+1\r\n"], "1235")
        self.assertIn("1235", fresh_out,
                      "对照组（新会话）应当算出 1235：%r" % fresh_out)

        # 正题：断开之后重连，把这条命令作为重连后的**第一条**命令发出
        sid = self._create_session()
        _exchange(self._ws_url(sid), self._headers(), ["echo BEFORE\r\n"], "BEFORE")
        time.sleep(1.0)          # 给服务端时间处理断开（会话转为分离状态）

        out = _exchange(self._ws_url(sid), self._headers(),
                        ["set /a 1234+1\r\n"], "1235")
        self.assertIn(
            "1235", out,
            "重连后的第一条命令必须真的执行成功（只出现命令回显不算）：%r" % out,
        )

    def test_attached_message_marks_replay(self):
        """
        重连时服务端要标明 replay=true，前端据此提示「已恢复到之前的会话」。

        ★ replay 的语义是「这个会话在本次连接之前已经被连过」，
        不是「此刻还有人连着」。所以「先断开、再重连」也必须是 True ——
        这正是用户刷新页面回来时的情况，也是最需要提示「已恢复」的场景。
        """
        sid = self._create_session()

        messages, error = _probe(self._ws_url(sid), self._headers(), timeout=6)
        self.assertEqual(error, "", "首次连接不该被拒绝：%s" % error)
        self.assertTrue(messages and messages[0].get("type") == "attached",
                        "服务端应先发 attached：%s" % messages)
        self.assertFalse(messages[0].get("replay"), "首次连接 replay 应为 false：%s" % messages[0])

        # 断开之后重连：必须被识别为「回到原来的会话」
        time.sleep(1.5)

        messages2, error2 = _probe(self._ws_url(sid), self._headers(), timeout=6)
        self.assertEqual(error2, "", "重连不该被拒绝：%s" % error2)
        self.assertTrue(messages2 and messages2[0].get("type") == "attached",
                        "重连应先发 attached：%s" % messages2)
        self.assertTrue(
            messages2[0].get("replay"),
            "断开再重连必须被标记为 replay=true（前端靠它提示「已恢复到之前的会话」）：%s"
            % messages2[0],
        )

    def test_attach_message_can_connect_without_query_param(self):
        """前端也可以用首条 attach 消息指定会话（不带 ?sid= 的写法）。"""
        sid = self._create_session()
        # 先让会话产出一点内容
        _exchange(self._ws_url(sid), self._headers(), ["echo ATTACH_MSG\r\n"], "ATTACH_MSG")
        time.sleep(1.0)

        async def run():
            # 不带任何查询参数连上去
            url = "ws://127.0.0.1:%d/api/terminal/ws" % self.server.port
            first_type = None
            async with _connect(url, self._headers()) as ws:
                await ws.send(json.dumps({"type": "attach", "sid": sid}))
                collected = ""
                deadline = time.time() + 20
                while time.time() < deadline and "ATTACH_MSG" not in collected:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=8)
                    except Exception:  # noqa: BLE001
                        break
                    message = json.loads(raw)
                    if first_type is None:
                        first_type = message.get("type")
                    if message.get("type") == "output":
                        collected += message.get("data", "")
                return first_type, collected

        first_type, collected = asyncio.run(run())
        self.assertEqual(first_type, "attached", "用 attach 消息建连也应先收到 attached")
        self.assertIn("ATTACH_MSG", collected,
                      "用 attach 消息建连也必须能拿到积压输出：%r" % collected)

    def test_unknown_session_is_rejected_with_clear_reason(self):
        """不知道的 sid 要在握手阶段被拒（避免被用来探测有效 sid）。"""
        rejected, why = _probe_rejected(self._ws_url("definitely-not-a-real-sid"), self._headers())
        self.assertTrue(rejected, "不存在的 sid 必须被拒绝：%s" % why)

    def test_explicit_close_ends_session(self):
        """
        显式 {"type":"close"} 才是「结束会话」：之后重连必须被拒绝。

        这条把「分离」与「结束」两种语义区分开，是本次改动不误伤
        「用户真的要关掉命令行」的保证。
        """
        sid = self._create_session()
        _exchange(self._ws_url(sid), self._headers(), ["echo WILL_CLOSE\r\n"], "WILL_CLOSE")

        self.assertTrue(_close_session(self._ws_url(sid), self._headers()),
                        "显式关闭应当成功完成")

        time.sleep(0.5)
        rejected, why = _probe_rejected(self._ws_url(sid), self._headers())
        self.assertTrue(rejected, "会话被显式关闭后，重连必须被拒绝：%s" % why)

    def test_detached_session_still_alive_after_disconnect(self):
        """
        断开后子进程必须**还在跑**（这是「会话活着」的直接证据）。

        做法：断开后不去连它，而是等一会儿再重连，并验证 shell 的**进程内状态**
        仍然在（cd 过的目录没有丢）。如果断开时进程被杀了，重连时 cwd 会回到
        起始目录，win.ini 就不会出现。
        """
        sid = self._create_session()

        # 先把 cwd 切到 C:\Windows，然后断开
        _exchange(self._ws_url(sid), self._headers(),
                  ["cd /d C:\\Windows\r\n", "echo CWD_SET\r\n"], "CWD_SET")
        time.sleep(1.5)

        # 重连后不重新 cd，直接 dir：若进程还活着，cwd 应当仍是 C:\Windows
        after = _exchange(self._ws_url(sid), self._headers(),
                          ["dir /b *.ini\r\n"], "win.ini")
        self.assertIn(
            "win.ini", after.lower(),
            "断开期间进程必须继续存活（cwd 应当保留在 C:\\Windows）：%r" % after,
        )

    def test_freshly_detached_session_is_not_evicted(self):
        """
        刚分离的会话不能被新会话挤掉 —— 否则「刷新页面」这种瞬时重连会丢会话。

        本类 max_sessions=2、淘汰宽限期 4 秒：占满 2 个名额后立刻建第三个，
        必须因为「两个会话里最旧的那个也还很新」而拿到 429。
        """
        first = self._create_session()
        second = self._create_session()
        # 连上 first 再断开，使它成为「刚分离」的会话（_last_active 刚刷新）
        _exchange(self._ws_url(first), self._headers(), ["echo FRESH\r\n"], "FRESH")

        # 立刻建新会话：宽限期未到，两个会话都不该被牺牲
        status, data = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(
            status, 429,
            "刚分离的会话不应被立刻挤掉（否则刷新页面就会丢会话）：%s %s" % (status, data),
        )
        self.assertIsNone(data.get("id"), "被拒绝的创建请求不该返回 sid：%s" % data)

        # 而且它必须仍然可以重连（证明确实没被淘汰）
        attached, replay = _reattach(self._ws_url(first), self._headers(), "FRESH")
        self.assertTrue(attached, "刚分离的会话必须还能重连")
        self.assertIn("FRESH", replay, "重连应拿到缓冲内容：%r" % replay)

    def test_aged_detached_session_can_be_evicted(self):
        """
        闲置超过宽限期的分离会话可以被新会话挤掉，避免名额被遗弃的窗口长期占满。

        要点一：必须先把 max_sessions（本类为 2）**占满**，新的创建才会触发淘汰。
        只有一个会话时名额没用满，正常创建即可成功，根本走不到淘汰逻辑。
        要点二：淘汰挑的是**最久没有活动**的那个（LRU），所以这里让 first 保持
        最新（连一次）、让 second 一直没人碰（最旧），被挤掉的就一定是 second。
        """
        first = self._create_session()
        second = self._create_session()      # 两格占满

        # 连一下 first 再断开：让 first 的"最后活动时间"刷新为刚刚，
        # 于是 second 成为最久没活动的那个（淘汰次序就此确定）
        _exchange(self._ws_url(first), self._headers(), ["echo KEEP_ME\r\n"], "KEEP_ME")

        # 等过淘汰宽限期（4 秒）—— 这段时间里没有人碰 second
        time.sleep(5.0)

        # 此时名额已满且 second 已闲置够久：这次创建应当挤掉 second
        status, data = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(
            status, 200,
            "闲置超过宽限期的分离会话应当被挤掉，让出名额：%s %s" % (status, data),
        )
        third = data.get("id")
        self.assertTrue(third)
        self._created.append(third)

        # second 被挤掉后应当真的没了：重连必须被拒绝
        time.sleep(0.5)
        rejected, why = _probe_rejected(self._ws_url(second), self._headers())
        self.assertTrue(rejected, "被挤掉的会话重连应被拒绝：%s" % why)

        # first 刚被用过，不该被牺牲：它必须还能重连，且积压内容还在
        attached, replay = _reattach(self._ws_url(first), self._headers(), "KEEP_ME")
        self.assertTrue(attached, "最近使用过的会话不该被淘汰")
        self.assertIn("KEEP_ME", replay, "未被淘汰的会话应仍保留缓冲输出：%r" % replay)

    def test_reattach_is_never_subject_to_max_sessions(self):
        """
        ★ 重连**不受** max_sessions 限制：名额满了也必须能回到已有会话。

        「分离的会话继续占名额」与「重连永远可行」这两条必须同时成立，
        否则用户就是被自己的旧窗口锁死：关掉浏览器后想回来，却被告知
        「会话数已达上限」。所以限制只能拦「新建」，不能拦「回来」。
        """
        first = self._create_session()
        second = self._create_session()          # 两格占满（本类 max_sessions=2）

        # 名额已满 → 新建必须被拒
        status, data = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(
            status, 429,
            "名额已满时新建会话必须被拒绝：%s %s" % (status, data),
        )

        # 但重连已有会话必须成功（它不经过 max_sessions 检查）
        attached, _replay = _reattach(self._ws_url(first), self._headers(), "__none__", timeout=8)
        self.assertTrue(attached, "名额已满时仍必须能重连已有会话")
        attached2, _replay2 = _reattach(self._ws_url(second), self._headers(), "__none__", timeout=8)
        self.assertTrue(attached2, "名额已满时仍必须能重连第二个已有会话")


class TerminalIdleReapTests(unittest.TestCase):
    """
    分离会话必须会被空闲超时回收（单独起一个超时很短的实例）。

    为什么单独一个类：空闲超时是配置项，而「验证回收」必须等它真的到期。
    放在主类里就得把超时调得极小、从而干扰其它用例（比如正常重连还没做
    完就被回收了）。这里用一个 idle_timeout_seconds=2 的独立实例，
    把「回收」这条路径干净地隔离出来。
    """

    @classmethod
    def setUpClass(cls):
        cls.work = tempfile.mkdtemp(prefix="fw-reap-")
        cls.root = os.path.join(cls.work, "root")
        os.makedirs(cls.root, exist_ok=True)

        def _extra(cfg):
            cfg["terminal"]["enabled"] = True
            cfg["terminal"]["shell"] = "cmd.exe"
            cfg["terminal"]["max_sessions"] = 2
            cfg["terminal"]["idle_timeout_seconds"] = 2

        cls.server = ServerProcess(
            [{"id": "main", "name": "main", "path": cls.root, "readonly": False}],
            extra_config=_extra,
        ).start()
        cls.client = cls.server.login_client()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()
        shutil.rmtree(cls.work, ignore_errors=True)

    def _ws_url(self, sid):
        return "ws://127.0.0.1:%d/api/terminal/ws?sid=%s" % (self.server.port, sid)

    def setUp(self):
        """同样要在用例之间清空会话（断开只是分离，不会释放名额）。"""
        self._created = []

    def tearDown(self):
        for sid in list(self._created):
            _close_session(self._ws_url(sid), self._headers())
        self._created = []

    def _create_session(self):
        status, data = self.client.json("POST", "/api/terminal/session", {})
        self.assertEqual(status, 200, data)
        self._created.append(data["id"])
        return data["id"]

    def _headers(self):
        return {"Cookie": self.client.cookie,
                "Origin": "http://127.0.0.1:%d" % self.server.port}

    def test_detached_session_is_reaped_after_idle_timeout(self):
        """
        断开后不再连它，等空闲超时到期：会话必须被回收，重连要被明确拒绝。

        ★ 为什么是「隔一段时间探一次」而不是「探到为止地狂试」：
        一次成功的连接会把会话重新 attach 上去，而 attach 会**暂停空闲计时**
        （这正是「有人在用就别回收」的实现），所以密集重试等于不停把回收时间
        往后推，反而永远看不到回收。这里的做法是先等过超时（8 秒 > 2 秒超时 +
        5 秒看门狗下限 / 2 秒巡检），然后每 5 秒探一次，留足重新计时的余地。

        ★ 轮询不会掩盖真正的泄漏缺陷：如果会话因为 bug 永远保持「有客户端」
        状态，它就永远不会被回收，也就永远能连上 —— 无论探多少次都只会成功，
        最后照样超期失败。所以这条断言仍然能抓住「分离会话泄漏」这个真问题
        （2026-09-13 就抓到过一次：握手后第一次 send 抛异常导致 detach 没执行，
        见 AttachIsAlwaysPairedWithDetachTests）。
        """
        sid = self._create_session()

        output = _exchange(self._ws_url(sid), self._headers(),
                           ["echo WILL_BE_REAPED\r\n"], "WILL_BE_REAPED")
        # ★ 先把「这一步确实成功了」钉住。
        # 否则一旦这次连接本身出了问题（最容易出问题的就是握手后那一次 send），
        # 用例仍会继续往下跑，最后报「会话还能连上」—— 把读者的注意力引向
        # 回收逻辑，而真正的故障点在上面这一步。先断言前提成立，失败信息才指向
        # 正确的地方。
        self.assertIn(
            "WILL_BE_REAPED", output,
            "首次连接必须真的执行了命令，否则本用例的前提不成立：%r" % output,
        )

        # 超时 2 秒；看门狗检查间隔下限 5 秒、巡检 2 秒 —— 正常几秒内必然回收。
        # 这里先给 8 秒，再每 5 秒复查一次，最长等 60 秒（整包并发跑、机器忙时
        # 调度变慢也够用），避免「只在空闲机器上才过」的脆弱测试。
        time.sleep(8.0)

        deadline = time.time() + 60
        last = ""
        while True:
            rejected, why = _probe_rejected(self._ws_url(sid), self._headers())
            if rejected:
                return
            last = why
            if time.time() >= deadline:
                break
            time.sleep(5.0)

        self.fail("空闲超时后分离会话始终没有被回收（等待超过 60 秒仍能连上）：%s" % last)

    def test_attached_session_is_not_reaped(self):
        """
        有客户端连着的会话**不该**被空闲超时回收，即使超过超时时间也没输出。

        这是「用户看着屏幕不动手」的场景：只看 _last_active 会把正常使用中的
        会话误杀，所以看门狗必须给「有客户端连着」的会话续期。
        """
        sid = self._create_session()

        async def run():
            async with _connect(self._ws_url(sid), self._headers()) as ws:
                # 连上之后什么都不发，只是挂着（远超 idle_timeout=2 秒）
                await asyncio.sleep(10)
                # 会话应当还活着：发一条命令仍能得到回显
                await ws.send(json.dumps({"type": "input", "data": "echo STILL_ALIVE\r\n"}))
                collected = ""
                deadline = time.time() + 15
                while time.time() < deadline and "STILL_ALIVE" not in collected:
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=6)
                    except asyncio.TimeoutError:
                        break
                    except Exception:  # noqa: BLE001
                        break
                    message = json.loads(raw)
                    if message.get("type") == "output":
                        collected += message.get("data", "")
                    elif message.get("type") == "closed":
                        return False, "会话被错误地回收了：%s" % message.get("message")
                return "STILL_ALIVE" in collected, collected

        alive, detail = asyncio.run(run())
        self.assertTrue(alive, "有客户端连着的会话不该被空闲回收：%s" % detail)

        _close_session(self._ws_url(sid), self._headers())


class TerminalManagerUnitTests(unittest.TestCase):
    """
    直接驱动进程内的 TerminalManager（确定性验证两条硬要求）。

    为什么要有这个类：下面两条性质如果只靠 WebSocket + 真实时间来验证，
    就得「等空闲超时到期」，既慢又受机器负载影响。这里用真实 cmd.exe，
    但把「变成无人连接的时刻」直接拨到过去，于是回收逻辑可以立即触发，
    断言是确定的：

      1. 空闲回收必须**关闭进程**且**从注册表摘除**（否则它会一直占着
         max_sessions 名额，还会让重连看起来「连上了又断」）
      2. close_all() 必须连**分离中**的会话一起杀掉（服务退出不留孤儿 cmd.exe）
    """

    def setUp(self):
        # 每个用例都重置注册表与锁：锁是懒创建的，跨 asyncio.run（不同事件循环）
        # 复用同一把锁会让后续用例挂在不属于它的循环上。
        terminal_manager._sessions.clear()
        terminal_manager._lock = None

    def tearDown(self):
        # 兜底：无论用例怎么结束，都不在测试进程里留下 cmd.exe
        asyncio.run(terminal_manager.close_all())
        terminal_manager._lock = None
        terminal_manager._sessions.clear()

    def _create_one(self, idle_timeout=0):
        return terminal_manager.create(
            shell="cmd.exe",
            start_dir=os.getcwd(),
            idle_timeout=idle_timeout,
            max_sessions=4,
            session_token="unit-test-token",
            cols=80,
            rows=24,
        )

    def test_idle_reap_closes_process_and_deregisters(self):
        """
        空闲回收必须同时做到「杀进程」和「从注册表摘除」。

        只做前者会留下一个仍占名额的死条目；只做后者会留下孤儿进程。
        """
        async def run():
            session = await self._create_one(idle_timeout=30)
            token = session.attach()
            session.detach(token)                 # 模拟浏览器关闭

            self.assertTrue(session.is_alive(), "刚分离的会话进程应当还在跑")

            # 把「变成无人连接」的时刻拨到 1 小时前 —— 等价于「已闲置很久」，
            # 于是不必真等 30 秒
            session._detached_at = time.time() - 3600
            self.assertTrue(session.is_idle_expired(), "闲置了一小时应当判定为超时")

            reaped = await terminal_manager.reap_idle()
            return session, reaped

        session, reaped = asyncio.run(run())

        self.assertIn(session.sid, reaped, "该会话应当被回收：%s" % reaped)
        self.assertNotIn(session.sid, terminal_manager._sessions,
                         "回收后必须从注册表摘除（否则会一直占着 max_sessions 名额）")
        self.assertFalse(session.is_alive(), "回收必须真的杀掉进程，不能只摘除条目")
        self.assertEqual(terminal_manager.get(session.sid), None,
                         "回收后按 sid 必须取不到会话（重连会被明确拒绝）")

    def test_watchdog_is_wired_to_deregister_on_reap(self):
        """
        会话的看门狗必须被接上管理器的回收入口。

        房间里的坑：看门狗若直接调用 session.close()，会话虽然被杀掉，
        却仍留在管理器的注册表里 —— 它继续占名额，而且重连时握手会成功、
        随后才收到结束消息。所以必须由管理器先摘除再关闭。
        """
        async def run():
            session = await self._create_one(idle_timeout=30)
            return session

        session = asyncio.run(run())
        self.assertIsNotNone(
            session._reap_callback,
            "管理器必须给会话注入回收回调（见 session.set_reap_callback）",
        )

    def test_close_all_kills_detached_session(self):
        """
        close_all() 必须连**分离中**的会话一起杀掉。

        这是「关掉浏览器不等于结束会话」的代价：服务退出时如果不清理
        这些分离会话，就会留下常驻的 cmd.exe（以服务身份运行时还是 SYSTEM）。
        """
        async def run():
            session = await self._create_one()
            token = session.attach()
            session.detach(token)                 # 分离，但进程仍在跑
            alive_before = session.is_alive()
            await terminal_manager.close_all()
            return session, alive_before

        session, alive_before = asyncio.run(run())

        self.assertTrue(alive_before, "分离中的会话在 close_all 之前应当还活着")
        self.assertEqual(terminal_manager.count(), 0, "close_all 之后注册表必须清空")
        self.assertFalse(session.is_alive(), "close_all 必须杀掉分离中的会话进程")

    def test_dead_session_frees_quota_for_new_create(self):
        """
        已被回收/退出的会话不能继续占名额，否则用户会被自己的旧窗口锁死。
        """
        async def run():
            # 配额设为 1，先用满
            first = await terminal_manager.create(
                shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=0,
                max_sessions=1, session_token="unit-test-token", cols=80, rows=24,
            )
            # 名额已满：新建必须抛 Limit
            limited = False
            try:
                await terminal_manager.create(
                    shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=0,
                    max_sessions=1, session_token="unit-test-token", cols=80, rows=24,
                )
            except TerminalLimitError:
                limited = True

            # 让第一个会话「确定结束」后，名额应当被释放
            await terminal_manager.close_session(first.sid)
            second = await terminal_manager.create(
                shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=0,
                max_sessions=1, session_token="unit-test-token", cols=80, rows=24,
            )
            return limited, first, second

        limited, first, second = asyncio.run(run())
        self.assertTrue(limited, "名额占满时新建必须被拒绝")
        self.assertFalse(first.is_alive(), "被关闭的会话应当已经结束")
        self.assertTrue(second.is_alive(), "旧会话结束后必须能开出新会话")

    def test_sweeper_actually_runs_and_reaps(self):
        """
        ★ 兜底巡检必须**真的在跑**，而且真的会回收分离会话。

        为什么要专门钉这一条：_ensure_sweeper() 里有一条「拿不到运行中的事件
        循环就放弃」的分支。一旦走到那条路，巡检协程根本没启动，而
        「分离会话不会泄漏」的兜底保证就只剩会话自己的看门狗在撑 ——
        代码和文档却都还写着巡检在兜底。这种事必须能被测出来，
        而不是只能靠读代码相信。

        做法上刻意**把看门狗停掉**：否则回收可能是看门狗干的，
        就证明不了巡检这条线本身是好的。
        """
        async def run():
            session = await terminal_manager.create(
                shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=1,
                max_sessions=4, session_token="unit-test-token", cols=80, rows=24,
            )
            # 巡检协程必须真的被建起来，而且没有立刻退出
            self.assertIsNotNone(terminal_manager._sweep_task, "巡检协程没有启动")
            self.assertFalse(terminal_manager._sweep_task.done(),
                             "巡检协程启动后立刻就退出了")
            # 周期按最短的 idle_timeout 算：1/4 秒被下限抬到 2 秒
            self.assertEqual(terminal_manager._sweep_interval(), 2.0,
                             "巡检周期应当按最短空闲时限计算并夹到 2 秒")

            # ★ 只考验巡检：把该会话的看门狗停掉
            if session._watchdog_task is not None:
                session._watchdog_task.cancel()

            token = session.attach()
            session.detach(token)        # 变成分离会话，空闲时限 1 秒

            # 不再连它（连接会把空闲计时清零），直接盯注册表看它有没有被收走
            deadline = time.time() + 15
            while time.time() < deadline:
                if session.sid not in terminal_manager._sessions:
                    break
                await asyncio.sleep(0.5)

            self.assertNotIn(
                session.sid, terminal_manager._sessions,
                "巡检没有回收分离会话 —— 兜底防线实际没生效",
            )
            self.assertFalse(session.is_alive(), "回收必须真的杀掉进程")

        asyncio.run(run())


    def test_replayed_backlog_has_terminal_queries_stripped(self):
        """
        ★ 回归测试：补发的积压里不能再出现「会让终端自己开口说话」的序列。

        真实浏览器验证抓到的缺陷：会话启动时 ConPTY 发一段固定启动序列，
        实测原文是
            \\x1b[1t \\x1b[c \\x1b[?1004h \\x1b[?9001h \\x1b]0;cmd\\x07 \\x1b[2J \\x1b[H
        其中 \\x1b[c 是设备属性查询、\\x1b[?1004h 打开焦点上报 —— 两者都会让
        终端往回发数据（实测抓到的自动输入正是 ["\\x1b[?1;2c", "\\x1b[I"]，
        一一对应）。新建会话时 shell 还在启动，这两句被吃掉；**重连**时同一段
        序列被当积压补发，而 shell 已停在提示符上，于是它们变成「用户打的字」，
        和用户下一条命令拼在一起 —— 症状就是重连后**第一条命令必失败**：
            ^[[?1;2cecho BBB86K3
            '是内部或外部命令，也不是可运行的程序

        这里直接注入**实测抓到的那段启动序列**，再像重连那样 attach 并读取，
        断言：两个危险序列被剔掉、渲染相关内容照常补发、不相关的控制序列
        不被误剔、而**实时**输出里的同类序列必须原样保留。
        """
        # 实测抓到的 ConPTY 启动序列（末尾加上可断言的可见标记）
        startup = (
            "\x1b[1t\x1b[c\x1b[?1004h\x1b[?9001h\x1b]0;cmd\x07"
            "\x1b[2J\x1b[H\x1b[32mSTARTUP-BANNER\x1b[0m"
        )

        async def run():
            session = await terminal_manager.create(
                shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=30,
                max_sessions=4, session_token="unit-test-token", cols=80, rows=24,
            )
            terminal_manager.stop_sweeper()

            # 先把 ConPTY 自己真实的启动横幅读完，免得它混进后面的读数里
            warm = session.attach()
            while await session.next_chunk(warm, wait=0.3):
                pass
            session.detach(warm)

            session._emit(startup)

            # 像重连那样 attach：此刻缓冲里的内容全部属于「补发」
            token = session.attach()
            replayed = ""
            while True:
                chunk = await session.next_chunk(token, wait=0.3)
                if not chunk:
                    break
                replayed += chunk

            return session, token, replayed

        session, token, replayed = asyncio.run(run())

        # 1) 两个「会让终端回话」的序列都必须被剔掉
        self.assertNotIn(
            "\x1b[c", replayed,
            "补发的积压里不该再有设备属性查询（否则重连后第一条命令会被"
            "终端的自动回复污染）：%r" % replayed,
        )
        self.assertNotIn(
            "\x1b[?1004h", replayed,
            "补发的积压里不该再打开焦点上报（否则终端一获得焦点就会把 "
            "ESC[I 当成用户输入发过来）：%r" % replayed,
        )

        # 2) 可见内容与渲染序列必须照常补发，否则恢复出来的画面是错的
        self.assertIn("STARTUP-BANNER", replayed, "可见内容必须照常补发：%r" % replayed)
        self.assertIn("\x1b[2J", replayed, "清屏序列必须保留")
        self.assertIn("\x1b[32m", replayed, "颜色必须保留")
        self.assertIn("\x1b]0;cmd\x07", replayed, "窗口标题必须保留")

        # 3) 不相关的控制序列不能被误剔（?9001h 与 CSI 1t 都不会让终端回话）
        self.assertIn("\x1b[?9001h", replayed, "不该误剔无关的模式设置")
        self.assertIn("\x1b[1t", replayed, "不该误剔无关的窗口控制序列")

    def test_live_output_keeps_terminal_queries(self):
        """
        反面对照：**实时**输出里的同类序列必须原样保留。

        剔除只针对补发的那一段。会话中间真正需要问终端要光标位置的全屏程序
        （vim 之类）必须照常拿到回复，不能因为修这个 bug 把它们一起弄坏。
        """
        async def run():
            session = await terminal_manager.create(
                shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=30,
                max_sessions=4, session_token="unit-test-token", cols=80, rows=24,
            )
            terminal_manager.stop_sweeper()

            warm = session.attach()
            while await session.next_chunk(warm, wait=0.3):
                pass
            session.detach(warm)

            # attach 之后产生的输出 = 实时输出，必须原样送出
            token = session.attach()
            session._emit("\x1b[6n\x1b[?1004hLIVE")
            live = ""
            while "LIVE" not in live:
                chunk = await session.next_chunk(token, wait=0.5)
                if not chunk:
                    break
                live += chunk
            return session, live

        session, live = asyncio.run(run())
        self.assertIn("\x1b[6n", live,
                      "实时输出里的查询必须原样保留（剔除只针对补发）：%r" % live)
        self.assertIn("\x1b[?1004h", live,
                      "实时输出里的模式设置必须原样保留：%r" % live)
        self.assertTrue(session.is_alive(), "整个过程里会话应当一直活着")


class AttachIsAlwaysPairedWithDetachTests(unittest.TestCase):
    """
    ★ 回归测试：**只要 attach() 成功过，就一定要有对应的 detach()**。

    这条不变量一旦被破坏，后果不是「测试偶发失败」而是**线上功能被锁死**：
    客户端条目留在会话里 → has_clients() 永远为真 → idle_seconds() 恒为 0 →
    看门狗和巡检都永远不会回收它 → 那个 cmd.exe 和它的 max_sessions 名额
    就永久泄漏，用户最后只会看到「命令行会话数已达上限」却找不到原因。

    为什么用「假 WebSocket」而不是真连一个：真实的触发条件是
    「握手刚完成、连接就断了，导致服务端第一次 send 抛异常」——
    靠真实连接 + 负载去撞它，既不确定也慢。这里直接把那次 send 打回异常，
    于是这个竞态变成一条**确定性**的断言，不需要任何等时间。

    这个用例在修复前是**失败**的（捕获到泄漏），见报告中的前后对照。
    """

    def setUp(self):
        terminal_manager._sessions.clear()
        terminal_manager._lock = None
        terminal_manager.stop_sweeper()

    def tearDown(self):
        asyncio.run(terminal_manager.close_all())
        terminal_manager._sessions.clear()
        terminal_manager._lock = None

    def _fake_websocket(self, sid, sent, boom_after=None):
        """
        最小的 WebSocket 替身。

        boom_after：在第几次 send_json 时模拟「连接已断」并抛异常。
        None 表示不抛（正常行为）。
        """
        state_stub = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(
                cfg={"terminal": {"enabled": True, "idle_timeout_seconds": 30}}
            ))),
            query_params={"sid": sid},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session_token="unit-test-token"),
        )

        class Boom(Exception):
            pass

        async def accept():
            return None

        async def send_json(payload):
            sent.append(payload.get("type"))
            if boom_after is not None and len(sent) >= boom_after:
                raise Boom("客户端在握手的瞬间断了")

        async def close(code=1000, reason=""):
            return None

        async def receive_text():
            raise AssertionError("本例不应走到收消息那一步")

        state_stub.accept = accept
        state_stub.send_json = send_json
        state_stub.close = close
        state_stub.receive_text = receive_text
        return state_stub

    def test_client_is_detached_when_first_send_fails(self):
        """
        握手后立刻断连（第一次 send 就抛）：客户端登记必须被注销。

        这正是线上「刷新页面/关标签页/网络重置」时会撞上的那一次。
        """
        async def run():
            session = await terminal_manager.create(
                shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=30,
                max_sessions=4, session_token="unit-test-token", cols=80, rows=24,
            )
            sent = []
            ws = self._fake_websocket(session.sid, sent, boom_after=1)
            try:
                await terminal_ws(ws)
            except Exception:  # noqa: BLE001 - 真实部署里由 Starlette 记录，这里只看状态
                pass
            return session, sent

        session, sent = asyncio.run(run())

        self.assertIn("attached", sent, "服务端应当尝试发送过 attached")
        self.assertFalse(
            session.has_clients(),
            "第一次 send 失败后客户端登记必须被注销；否则 has_clients() 恒为真，"
            "该会话永远不会被空闲回收（泄漏 cmd.exe 与 max_sessions 名额）",
        )
        self.assertGreater(
            session.idle_seconds(), 0.0,
            "注销之后空闲计时必须开始走，否则照样回收不了",
        )

    def test_client_is_detached_when_later_send_fails(self):
        """第二次 send 才失败（收发过程中断线）同样必须注销。"""
        async def run():
            session = await terminal_manager.create(
                shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=30,
                max_sessions=4, session_token="unit-test-token", cols=80, rows=24,
            )
            sent = []
            ws = self._fake_websocket(session.sid, sent, boom_after=2)
            try:
                await terminal_ws(ws)
            except Exception:  # noqa: BLE001
                pass
            return session

        session = asyncio.run(run())
        self.assertFalse(session.has_clients(), "断线后客户端登记必须被注销")


class ConnectPathDeterminismTests(unittest.TestCase):
    """
    ★ 连接路径必须**确定性地**在握手阶段拒绝已经不在的会话。

    为什么专门钉这条：对一个「已经不在了」的会话，服务端有两种可能的反应 ——
      * accept **之前**就 close（拒绝握手）→ 客户端看到连接失败，提示明确；
      * 先 accept、再补一条 {"type":"closed"} + 4404 → 客户端先「连上」再断开。
    哪一种会发生，如果取决于「后台回收/巡检恰好在什么时候跑过」，那同一件事
    就有两种用户可见行为。这是不该有的不确定性，所以连接路径必须自己判定，
    不能依赖「回收任务已经把注册表清干净了」。

    这里用替身 WebSocket 直接调路由，因此不受时间与负载影响：
    断言的是「accept 有没有被调用」——被拒绝时它**绝不**该被调用。

    所有操作都在**同一个** asyncio.run 里完成：会话、事件循环对象（队列/Event）
    与路由必须属于同一个循环，跨循环复用是个坑。
    """

    def setUp(self):
        terminal_manager._sessions.clear()
        terminal_manager._lock = None
        terminal_manager.stop_sweeper()

    def tearDown(self):
        asyncio.run(terminal_manager.close_all())
        terminal_manager._sessions.clear()
        terminal_manager._lock = None

    async def _create(self):
        session = await terminal_manager.create(
            shell="cmd.exe", start_dir=os.getcwd(), idle_timeout=30,
            max_sessions=4, session_token="unit-test-token", cols=80, rows=24,
        )
        # 关掉兜底巡检：下面要自己控制「什么时候回收」，否则它会替我们回收掉，
        # 就测不到「注册表里还留着已该消失的会话」这个窗口了
        terminal_manager.stop_sweeper()
        return session

    @staticmethod
    def _expire_idle(session):
        """
        把会话伪造成「早已没人连、并且已过空闲时限」，但不惊动任何回收任务。

        这正是竞态窗口：会话已经该消失了，但注册表里还留着它。
        """
        token = session.attach()
        session.detach(token)
        session._detached_at = time.time() - 3600

    @staticmethod
    def _stub(sid, calls, receive="never"):
        """
        记录 accept / send / close 调用的最小替身。

        receive="never" ：一旦有人去收消息就报错（被拒绝的路径不该收消息）
        receive="close" ：模拟客户端读完就走（正常路径用）
        """
        async def accept():
            calls.append(("accept", None))

        async def send_json(payload):
            calls.append(("send", payload.get("type")))

        async def close(code=1000, reason=""):
            calls.append(("close", code))

        async def receive_text():
            if receive == "never":
                raise AssertionError("这条路径不该去收客户端消息")
            raise WebSocketDisconnect(1000)

        return SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(
                cfg={"terminal": {"enabled": True, "idle_timeout_seconds": 30}}
            ))),
            query_params={"sid": sid},
            client=SimpleNamespace(host="127.0.0.1"),
            state=SimpleNamespace(session_token="unit-test-token"),
            accept=accept,
            send_json=send_json,
            close=close,
            receive_text=receive_text,
        )

    def test_idle_expired_session_lookup_returns_none(self):
        """空闲超时的会话在查询层就该被当作「不存在」，而不是还能取到。"""
        async def run():
            session = await self._create()
            self._expire_idle(session)

            self.assertTrue(session.is_idle_expired(), "前提：该会话应判定为已空闲超时")
            self.assertIsNone(
                terminal_manager.get(session.sid),
                "空闲超时的会话不应还能被查到（否则连接路径的行为会随巡检时机漂移）",
            )
            # 查询层只是「拒绝连接」，不该把进程杀掉 —— 关进程是看门狗/巡检的职责
            self.assertTrue(session.is_alive(), "查询不该顺手杀进程")

            reaped = await terminal_manager.reap_idle()
            self.assertIn(session.sid, reaped, "巡检应当把它回收掉")
            self.assertFalse(session.is_alive(), "回收必须真的杀掉进程")
            self.assertIsNone(terminal_manager.get(session.sid), "回收后注册表里不该还有它")

        asyncio.run(run())

    def test_idle_expired_session_is_rejected_before_accept(self):
        """空闲超时的会话：必须在 accept 之前被拒（不出现「先连上再断开」）。"""
        async def run():
            session = await self._create()
            self._expire_idle(session)

            calls = []
            await terminal_ws(self._stub(session.sid, calls))
            return calls

        calls = asyncio.run(run())
        names = [c[0] for c in calls]
        self.assertNotIn("accept", names,
                         "已空闲超时的会话不该走到 accept（那就是「先连上再断开」）")
        self.assertIn(("close", 1008), calls, "应当用 1008 拒绝握手：%s" % calls)
        self.assertNotIn("send", names, "被拒绝的握手不该给客户端发数据：%s" % calls)

    def test_dead_but_still_registered_session_is_rejected_before_accept(self):
        """
        已经被关掉、但**还留在注册表里**的会话，同样必须在 accept 之前被拒。

        这是另一个真实的残留窗口：会话被显式关闭（或进程退出）之后，
        条目要等下一次 _prune_locked 才会被摘掉。这段时间里它照样能被查到，
        所以连接路径必须自己判死，否则用户会经历「连上了，然后立刻被断开」。
        """
        async def run():
            session = await self._create()
            await session.close()                # 直接关，故意不经过 manager.close_session
            # close() 不会自己摘除注册表条目，这里直接查注册表确认它仍然留着
            # （不能再用 get() 作前提：get() 现在会把已结束的会话判为不存在）
            self.assertIn(session.sid, terminal_manager._sessions,
                          "前提：该会话应当仍留在注册表里")
            self.assertTrue(session.is_dead(), "前提：它应当被判为已结束")

            calls = []
            await terminal_ws(self._stub(session.sid, calls))
            return calls

        calls = asyncio.run(run())
        names = [c[0] for c in calls]
        self.assertNotIn("accept", names,
                         "已结束的会话不该走到 accept（否则用户会「连上又被断开」）")
        self.assertIn(("close", 1008), calls, "应当用 1008 拒绝握手：%s" % calls)

    def test_dead_session_is_evicted_by_lookup(self):
        """
        已结束的会话被查询拒掉时，要顺手摘除，立刻腾出 max_sessions 名额。

        这类会话的进程已经不在（或 close() 已经跑过），摘掉不会留下孤儿进程，
        所以「摘除」是安全且划算的；留在注册表里只会白占名额。
        """
        async def run():
            session = await self._create()
            await session.close()
            self.assertIn(session.sid, terminal_manager._sessions)

            self.assertIsNone(terminal_manager.get(session.sid), "已结束的会话应当查不到")
            self.assertNotIn(session.sid, terminal_manager._sessions,
                             "拒掉的同时应当把它摘除，否则它继续占着名额")
            return session

        session = asyncio.run(run())
        self.assertFalse(session.is_alive())

    def test_recently_detached_session_still_resolves(self):
        """
        ★ 反向的关键性质：**只是断开、还在空闲时限内**的会话必须照常能查到。

        这条要是被判成「不存在」，重连功能就整体失效了 —— 而那正是本特性的
        全部意义。所以「让连接路径确定性拒绝已消失的会话」不能做过头。
        """
        async def run():
            session = await self._create()          # idle_timeout=30
            token = session.attach()
            session.detach(token)                   # 刚断开，远未到空闲时限

            self.assertFalse(session.is_idle_expired(), "前提：它还没到空闲时限")
            self.assertIs(
                terminal_manager.get(session.sid), session,
                "刚断开、仍在时限内的会话必须仍然可以查到（否则重连就废了）",
            )

            # 而且它必须真的能连上：走一遍真实路由，确认是被 accept 的
            calls = []
            await terminal_ws(self._stub(session.sid, calls, receive="close"))
            self.assertIn("accept", [c[0] for c in calls],
                          "仍在时限内的分离会话必须能重连：%s" % calls)

        asyncio.run(run())

    def test_healthy_session_is_accepted(self):
        """反面对照：健康会话必须正常接受，别把「确定性」做成「一律拒绝」。"""
        async def run():
            session = await self._create()
            calls = []
            await terminal_ws(self._stub(session.sid, calls, receive="close"))
            return calls

        calls = asyncio.run(run())
        kinds = [c[0] for c in calls]
        sent_types = [c[1] for c in calls if c[0] == "send"]
        self.assertIn("accept", kinds, "健康会话必须被接受：%s" % calls)
        self.assertIn("attached", sent_types, "接受之后应当发送 attached：%s" % calls)


if __name__ == "__main__":
    unittest.main(verbosity=2)

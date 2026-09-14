# -*- coding: utf-8 -*-
"""
桌面歌词转发（服务端）
======================

分三层测，因为这三层的失败模式完全不同：

  1. HubUnitTests    —— 中枢本身（内存表，不启服务）：清洗、排序、needSong、
                        过期、以及「订阅全部时该显示谁」的挑选规则。
                        这一层用秒级等待测过期，快且不依赖网络。
  2. LyricsHttpTests —— 真实服务上的上报与查询：包括**不需要登录**这条
                        架构性决定（悬浮窗没有会话 Cookie）。
  3. LyricsWsTests   —— 真实服务上的 WebSocket 广播：连上先补当前状态、
                        上报后收到新状态、stop 后收到 idle、按 user 过滤、
                        「播放器关掉后歌词自己消失」（过期推送）、token 扩展点。

★ 有几个用例是**为将来改坏它的人写的**（断言「不该发生的事」）：
  * test_endpoints_need_no_login —— 中间件若哪天把非 /api 路径也纳入认证，
    悬浮窗会全线 403，而这只会在真机上表现为「歌词窗一直黑着」；
  * test_ws_only_gets_own_source —— 分组订阅若退化成「广播给所有人」，
    表现是「别人放歌时我的歌词被顶掉」，很难复现；
  * test_ws_pushes_idle_after_stale —— 关掉浏览器后歌词不消失，
    是所有症状里最容易被忽略、又最确定会被用户抱怨的一个。
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fileweb.lyrics import (  # noqa: E402
    MAX_LINE_CHARS,
    MAX_LYRICS_LINES,
    MAX_TEXT_CHARS,
    LyricsHub,
)
from tests._harness import Client, ServerProcess  # noqa: E402

import websockets  # noqa: E402


# ---------------------------------------------------------------------------
# WebSocket 小工具（与 test_features.py 同一套写法，只保留这里需要的）
# ---------------------------------------------------------------------------

def _connect(url, headers=None):
    try:
        return websockets.connect(url, additional_headers=headers or {},
                                 open_timeout=20, close_timeout=5)
    except TypeError:  # 旧版本参数名是 extra_headers
        return websockets.connect(url, extra_headers=headers or {},
                                  open_timeout=20, close_timeout=5)


def _ws_first(url, headers=None, timeout=10):
    """连上去读第一条消息（服务端保证先补一条当前状态）。"""
    async def run():
        async with _connect(url, headers) as ws:
            return json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout))

    return asyncio.run(run())


def _ws_then(url, action, count=1, headers=None, timeout=10):
    """
    连上 -> 读初始状态 -> 在**线程里**执行 action -> 继续读 count 条。

    action 放到线程里是因为它通常是一次同步的 HTTP 上报（urllib），
    而这里正跑在事件循环里 —— 直接调用会把循环连同 WS 一起卡住。
    """
    async def run():
        received = []
        async with _connect(url, headers) as ws:
            received.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout)))
            if action is not None:
                await asyncio.to_thread(action)
            for _ in range(count):
                received.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=timeout)))
        return received

    return asyncio.run(run())


def _ws_drain(url, action, headers=None, timeout=2.5):
    """
    连上 -> 读初始状态 -> 执行 action -> 把 timeout 内收到的**所有**消息收下来。

    用于「分组订阅不该被别人顶掉」这类断言：不要求一条都没有（服务端可能推
    一条内容相同的 idle），而是要求**内容不变** —— 这才是用户看到的性质。
    """
    async def run():
        received = []
        async with _connect(url, headers) as ws:
            received.append(json.loads(await asyncio.wait_for(ws.recv(), timeout=10)))
            if action is not None:
                await asyncio.to_thread(action)
            deadline = time.time() + timeout
            while True:
                remain = deadline - time.time()
                if remain <= 0:
                    break
                try:
                    received.append(json.loads(
                        await asyncio.wait_for(ws.recv(), timeout=remain)))
                except asyncio.TimeoutError:
                    break
            return received

    return asyncio.run(run())


def _ws_rejected(url, headers=None):
    """握手是否被拒（服务端在 accept 之前 close -> 客户端表现为握手失败）。"""
    async def run():
        try:
            async with _connect(url, headers) as ws:
                await asyncio.wait_for(ws.recv(), timeout=5)
            return False
        except Exception:  # noqa: BLE001 - 拒绝就是期望结果
            return True

    return asyncio.run(run())


def _song(**overrides):
    payload = {
        "type": "song",
        "source": "wangqi",
        "title": "夜曲",
        "artist": "周杰伦",
        "album": "十一月的萧邦",
        "duration": 227.0,
        "currentTime": 0.0,
        "playing": True,
        "lyrics": [
            {"time": 0.0, "text": "一群嗜血的蚂蚁 被腐肉所吸引"},
            {"time": 43.2, "text": "为你弹奏萧邦的夜曲"},
            {"time": 78.5, "text": "纪念我死去的爱情"},
        ],
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# 1) 中枢本身
# ---------------------------------------------------------------------------

class HubUnitTests(unittest.TestCase):

    def setUp(self):
        self.hub = LyricsHub(stale_seconds=60)

    def tearDown(self):
        self.hub.shutdown()

    # -- 清洗 ---------------------------------------------------------------

    def test_lyrics_are_sorted_and_blank_lines_dropped(self):
        """
        LRC 里空行很常见（用来分段），单行悬浮窗上只会表现为「当前行是空白」。
        另外，时间戳坏掉的行要**整行丢掉**（归到 0 秒只会在开头闪一下）。
        """
        self.hub.report(_song(lyrics=[
            {"time": 10, "text": "第二行"},
            {"time": 5, "text": "第一行"},
            {"time": 7, "text": "   "},
            {"time": 8, "text": ""},
            {"time": "abc", "text": "时间坏的"},
            {"time": -3, "text": "负数时间"},
            {"text": "没有时间"},
            {"time": 12, "text": "第三行"},
        ]))
        lyrics = self.hub.pick("")["lyrics"]
        self.assertEqual([item["text"] for item in lyrics], ["第一行", "第二行", "第三行"])
        self.assertEqual([item["time"] for item in lyrics], [5.0, 10.0, 12.0])

    def test_newlines_inside_a_line_are_collapsed(self):
        """悬浮窗是**单行**显示的；歌词里混进换行会把布局顶乱，所以上报时就抹平。"""
        self.hub.report(_song(title="标题\n带换行", artist="歌手\r\n带换行",
                              lyrics=[{"time": 1, "text": "上句\n下句"}]))
        state = self.hub.pick("")
        self.assertNotIn("\n", state["title"])
        self.assertNotIn("\n", state["artist"])
        self.assertNotIn("\n", state["lyrics"][0]["text"])

    def test_length_limits_are_enforced(self):
        """
        这是局域网内**任何人都能写**的接口，长度必须由服务端说了算：
        否则一条超大 JSON 就能把服务端内存吃光。
        """
        self.hub.report(_song(
            title="標" * 5000,
            source="s" * 500,
            lyrics=[{"time": index, "text": "字" * 2000}
                    for index in range(MAX_LYRICS_LINES + 500)],
        ))
        state = self.hub.pick("")
        self.assertLessEqual(len(state["title"]), MAX_TEXT_CHARS)
        self.assertLessEqual(len(state["source"]), 64)
        self.assertEqual(len(state["lyrics"]), MAX_LYRICS_LINES)
        self.assertLessEqual(len(state["lyrics"][0]["text"]), MAX_LINE_CHARS)

    def test_bad_numbers_do_not_leak_into_state(self):
        """NaN / inf / 负数 / 字符串全都要归到安全值，不能让它们流到客户端去算滚动。"""
        self.hub.report(_song(duration="不是数字", currentTime=float("nan")))
        state = self.hub.pick("")
        self.assertEqual(state["duration"], 0.0)
        self.assertEqual(state["currentTime"], 0.0)

        self.hub.report(_song(duration=100, currentTime=float("inf")))
        self.assertEqual(self.hub.pick("")["currentTime"], 0.0)

    # -- 上报语义 -----------------------------------------------------------

    def test_progress_is_clamped_to_duration(self):
        """
        夹到 duration 是有意义的：悬浮窗会用「进度 + 本地流逝时间」外推滚动，
        进度一旦大于时长，外推会把歌词停在最后一行之后，看起来像卡死。
        """
        self.hub.report(_song(duration=100.0))
        self.hub.report({"type": "progress", "source": "wangqi",
                         "currentTime": 9999, "playing": True})
        self.assertEqual(self.hub.pick("")["currentTime"], 100.0)

    def test_progress_without_a_song_asks_for_the_song(self):
        """
        ★ 这条是「服务端重启后歌词自己回来」的实现：进度上报里没有歌名与歌词，
        所以服务端查不到状态时只能请播放器把整首歌重报一次。
        """
        result, error = self.hub.report({"type": "progress", "source": "wangqi",
                                         "currentTime": 12.0, "playing": True})
        self.assertIsNone(error)
        self.assertTrue(result["needSong"], "缺少状态时必须回 needSong=true")
        self.assertIsNone(self.hub.pick(""), "只有进度不能凭空造出一首歌")

    def test_song_report_resets_progress_and_playing(self):
        self.hub.report(_song(currentTime=100.0, playing=False))
        self.hub.report(_song(title="下一首", currentTime=0.0))
        state = self.hub.pick("")
        self.assertEqual(state["title"], "下一首")
        self.assertEqual(state["currentTime"], 0.0)
        self.assertTrue(state["playing"])

    def test_stop_removes_only_that_source(self):
        self.hub.report(_song(source="wangqi", title="甲"))
        self.hub.report(_song(source="lisi", title="乙"))
        self.hub.report({"type": "stop", "source": "wangqi"})
        self.assertEqual(self.hub.pick("")["title"], "乙", "另一个源不该被牵连")
        self.assertIsNone(self.hub.pick("wangqi"))

    def test_rejects_malformed_reports(self):
        for payload, why in (
            ("不是对象", "字符串"),
            ([1, 2], "数组"),
            ({}, "空对象"),
            ({"currentTime": 1, "playing": True}, "缺 type（只有进度字段）"),
            ({"type": "nothing"}, "未知 type"),
            ({"type": "song", "source": "x"}, "song 缺 title"),
            ({"type": "song", "title": "   "}, "title 只有空白"),
        ):
            result, error = self.hub.report(payload)
            self.assertFalse(result["ok"], "%s 应该被拒" % why)
            self.assertTrue(error, "%s 应该给出原因" % why)

    # -- 挑选规则 -----------------------------------------------------------

    def test_pick_prefers_the_playing_source(self):
        """
        ★ 暂停期间播放器仍在发心跳，所以「最近上报的」不能作为唯一依据：
        否则一个暂停着的同学会把正在放歌的同学顶掉，表现为歌词莫名其妙地跳。
        """
        self.hub.report(_song(source="wangqi", title="甲的", playing=True))
        self.hub.report(_song(source="lisi", title="乙的", playing=False))
        self.hub.report({"type": "progress", "source": "lisi",
                         "currentTime": 30, "playing": False})
        self.assertEqual(self.hub.pick("")["title"], "甲的",
                         "暂停的那个即使上报得更近，也不该顶掉正在播放的")

        # 全部暂停时退回「最近上报的」：暂停中的歌仍然要显示（悬浮窗会半透明），
        # 否则一按暂停歌词就整条消失，用户会以为坏了。
        self.hub.report({"type": "progress", "source": "wangqi",
                         "currentTime": 30, "playing": False})
        self.assertEqual(self.hub.pick("")["title"], "甲的", "都暂停时取最近上报的（甲）")
        self.hub.report({"type": "progress", "source": "lisi",
                         "currentTime": 31, "playing": False})
        self.assertEqual(self.hub.pick("")["title"], "乙的", "都暂停时取最近上报的（乙）")

    def test_pick_filters_by_source(self):
        self.hub.report(_song(source="wangqi", title="甲的"))
        self.hub.report(_song(source="lisi", title="乙的"))
        self.assertEqual(self.hub.pick("wangqi")["title"], "甲的")
        self.assertEqual(self.hub.pick("lisi")["title"], "乙的")
        self.assertIsNone(self.hub.pick("zhaoliu"))

    def test_snapshot_is_idle_when_there_is_nothing(self):
        payload = self.hub.snapshot()
        self.assertTrue(payload["idle"])
        self.assertEqual(payload["type"], "state")
        self.assertLessEqual({"type", "idle", "revision", "serverTime", "staleSeconds"},
                             set(payload))

    # -- 过期 ---------------------------------------------------------------

    def test_purge_stale_drops_expired_sources(self):
        hub = LyricsHub(stale_seconds=2)
        try:
            hub.report(_song(source="wangqi"))
            self.assertIsNotNone(hub.pick(""))
            hub._states["wangqi"]["updatedAt"] = time.time() - 5
            self.assertEqual(hub.purge_stale(), ["wangqi"])
            self.assertIsNone(hub.pick(""), "过期的源不能再被选中")
            self.assertTrue(hub.snapshot()["idle"])
        finally:
            hub.shutdown()

    def test_stale_seconds_is_bounded(self):
        """太小会让正常的网络抖动被误判成「已关闭」（歌词一闪一闪），
        太大则关掉浏览器后歌词久久不消失。"""
        hub = LyricsHub(stale_seconds=0)
        try:
            self.assertGreaterEqual(hub.stale_seconds, 2.0)
            hub.configure(99999)
            self.assertLessEqual(hub.stale_seconds, 600.0)
            hub.configure("不是数字")
            self.assertGreaterEqual(hub.stale_seconds, 2.0)
        finally:
            hub.shutdown()

    def test_source_table_is_bounded(self):
        """防止有人用随机 source 名把内存撑大（每个源最多 2000 行歌词）。"""
        hub = LyricsHub(stale_seconds=60)
        try:
            for index in range(260):
                hub.report(_song(source="u%d" % index, title="歌 %d" % index))
            self.assertLessEqual(len(hub._states), 200)
            self.assertIsNotNone(hub.pick("u259"), "刚上报的源必须还在")
        finally:
            hub.shutdown()

    # -- 订阅投递 -----------------------------------------------------------

    def test_filtered_subscriber_is_not_woken_by_other_sources(self):
        subscriber = self.hub.subscribe("wangqi")
        self.hub.report(_song(source="lisi"))
        self.assertTrue(subscriber.queue.empty(), "别人的上报不该惊动分组订阅者")
        self.hub.report(_song(source="wangqi"))
        self.assertFalse(subscriber.queue.empty())

    def test_subscriber_receives_idle_when_its_source_stops(self):
        subscriber = self.hub.subscribe("wangqi")
        self.hub.report(_song(source="wangqi"))
        self.hub.report({"type": "stop", "source": "wangqi"})
        payloads = []
        while not subscriber.queue.empty():
            payloads.append(subscriber.queue.get_nowait())
        self.assertTrue(payloads[-1]["idle"], "停止后最后一条必须是 idle")

    def test_full_queue_keeps_the_newest(self):
        """慢客户端只该影响自己：队列满时丢掉最旧的，保证最新状态一定送到。"""
        subscriber = self.hub.subscribe("")
        for index in range(subscriber.QUEUE_SIZE + 10):
            self.hub.report(_song(currentTime=float(index)))
        self.assertEqual(subscriber.queue.qsize(), subscriber.QUEUE_SIZE)
        newest = None
        while not subscriber.queue.empty():
            newest = subscriber.queue.get_nowait()
        self.assertAlmostEqual(newest["currentTime"],
                               float(subscriber.QUEUE_SIZE + 9), places=3)

    def test_unsubscribe_stops_delivery(self):
        subscriber = self.hub.subscribe("")
        self.hub.unsubscribe(subscriber)
        self.hub.report(_song())
        self.assertTrue(subscriber.queue.empty())
        self.assertEqual(self.hub.subscriber_count, 0)

    def test_purging_another_source_does_not_wake_a_filtered_subscriber(self):
        """
        ★ 过期回收会 bump 全局 revision，但被回收的是**别人**的源时，
        这个订阅者该看到的画面一点没变，就不该被推一条消息。

        用全局 revision 当「变了吗」的依据就会踩这个坑；所以这里钉的是
        「按内容签名」这条实现选择。
        """
        subscriber = self.hub.subscribe("wangqi")
        self.hub.report(_song(source="wangqi", title="甲的"))
        self.hub.report(_song(source="lisi", title="乙的"))
        subscriber.queue.get_nowait()          # 清掉自己的那条，只看之后

        self.hub._states["lisi"]["updatedAt"] = time.time() - 3600
        self.hub.purge_stale()
        self.assertTrue(subscriber.queue.empty(), "别人的源过期不该惊动我")

        self.hub.report(_song(source="wangqi", title="又一首"))
        self.assertFalse(subscriber.queue.empty(), "自己的源变了必须立刻推")

    def test_identical_state_is_not_pushed_twice(self):
        """重复推同一个画面只会让悬浮窗白重绘一次（还可能在网差时抖动）。"""
        subscriber = self.hub.subscribe("")
        self.hub.report(_song(source="wangqi"))
        self.assertFalse(subscriber.queue.empty())
        while not subscriber.queue.empty():
            subscriber.queue.get_nowait()
        self.hub.reflect_all()
        self.assertTrue(subscriber.queue.empty())

    def test_welcome_sets_the_signature_so_nothing_is_resent(self):
        subscriber = self.hub.subscribe("")
        self.hub.report(_song(source="wangqi"))
        payload = self.hub.welcome(subscriber)
        self.assertFalse(payload["idle"])
        while not subscriber.queue.empty():      # 清掉 welcome 之前积压的那条
            subscriber.queue.get_nowait()
        self.hub.reflect_all()
        self.assertTrue(subscriber.queue.empty(), "welcome 之后不该再来一条重复的")


# ---------------------------------------------------------------------------
# 2) 真实服务：上报与查询
# ---------------------------------------------------------------------------

class LyricsHttpTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        def enable_lyrics(cfg):
            cfg["lyrics"] = {"enabled": True, "stale_seconds": 60, "token": ""}

        cls.server = ServerProcess([tempfile.mkdtemp(prefix="fw-lyr-")],
                                   extra_config=enable_lyrics)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def setUp(self):
        # ★ 故意**不登录**：这正是这个功能的架构性前提（悬浮窗没有会话 Cookie）。
        #   端点不在 /api 下，所以中间件既不校验登录也不校验 CSRF。
        self.client = Client(self.server.port)
        self.addCleanup(self._cleanup_state)
        self.client.json("POST", "/now-playing", {"type": "stop", "source": "wangqi"})
        self.client.json("POST", "/now-playing", {"type": "stop", "source": "lisi"})

    def _cleanup_state(self):
        for source in ("wangqi", "lisi"):
            self.client.json("POST", "/now-playing", {"type": "stop", "source": source})

    def test_no_login_and_no_session_cookie_is_a_deliberate_choice(self):
        """
        ★ 架构性决定：这几个端点必须在 /api 之外。

        如果哪天中间件把非 /api 路径也纳入认证，悬浮窗会全线 403 ——
        而这只会在真机上表现为「歌词窗一直黑着」，本地测试全绿也发现不了。
        """
        self.assertEqual(self.client.cookie, "")
        status, data = self.client.json("GET", "/now-playing")
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])
        self.assertTrue(data["enabled"])

    def test_system_info_reports_the_flag(self):
        """前端靠 features.lyrics 决定要不要开始上报。"""
        client = self.server.login_client()
        status, data = client.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertIs(data["features"]["lyrics"], True)

    def test_song_report_is_visible_through_get(self):
        status, data = self.client.json("POST", "/now-playing", _song())
        self.assertEqual(status, 200, data)
        self.assertTrue(data["ok"])

        status, data = self.client.json("GET", "/now-playing")
        self.assertEqual(status, 200, data)
        display = data["display"]
        self.assertFalse(display["idle"])
        self.assertEqual(display["source"], "wangqi")
        self.assertEqual(display["title"], "夜曲")
        self.assertEqual(display["artist"], "周杰伦")
        self.assertEqual(len(display["lyrics"]), 3)
        self.assertIn("serverTime", display)

    def test_progress_without_song_reports_need_song(self):
        status, data = self.client.json("POST", "/now-playing", {
            "type": "progress", "source": "wangqi", "currentTime": 5, "playing": True})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["needSong"])

    def test_progress_updates_the_existing_song(self):
        self.client.json("POST", "/now-playing", _song())
        status, data = self.client.json("POST", "/now-playing", {
            "type": "progress", "source": "wangqi", "currentTime": 42.5, "playing": True})
        self.assertEqual(status, 200, data)
        self.assertFalse(data["needSong"])
        self.assertAlmostEqual(data["display"]["currentTime"], 42.5, places=3)
        self.assertTrue(data["display"]["playing"])

    def test_stop_clears_the_source(self):
        self.client.json("POST", "/now-playing", _song())
        status, data = self.client.json("POST", "/now-playing",
                                        {"type": "stop", "source": "wangqi"})
        self.assertEqual(status, 200, data)
        self.assertTrue(data["display"]["idle"])

    def test_sources_listing_for_diagnostics(self):
        self.client.json("POST", "/now-playing", _song())
        status, data = self.client.json("GET", "/now-playing?sources=1")
        self.assertEqual(status, 200, data)
        sources = {item["source"]: item for item in data["sources"]}
        self.assertIn("wangqi", sources)
        self.assertEqual(sources["wangqi"]["title"], "夜曲")
        self.assertEqual(sources["wangqi"]["lines"], 3)
        self.assertNotIn("lyrics", sources["wangqi"], "列表接口不该带上歌词正文")

    def test_unknown_type_is_400(self):
        status, data = self.client.json("POST", "/now-playing", {"type": "乱写"})
        self.assertEqual(status, 400, data)
        self.assertFalse(data["ok"])

    def test_song_without_title_is_400(self):
        status, data = self.client.json("POST", "/now-playing", {"type": "song"})
        self.assertEqual(status, 400, data)
        self.assertIn("title", data.get("error") or "")

    def test_broken_json_is_400(self):
        status, data = self.client.raw_post("/now-playing", b"{not json",
                                           content_type="application/json")
        self.assertEqual(status, 400, data)
        self.assertEqual(data.get("code"), "bad_json")

    def test_empty_body_is_rejected_not_crashed(self):
        status, data = self.client.raw_post("/now-playing", b"",
                                           content_type="application/json")
        self.assertEqual(status, 400, data)

    def test_oversized_report_is_413(self):
        """
        一个超大 body 不该把服务端内存吃掉（上限见路由里的 MAX_BODY_BYTES）。

        先上报一首正常的歌、再发一条**标题不同**的超大上报，然后确认库里
        还是原来那首歌 —— 否则「413 了但内容照样生效」也会让这条测试变绿。

        ★ 这里可能拿不到响应而是连接被重置：服务端一看 Content-Length 就拒了，
        并不会把 9MB 读完（省带宽的**有意**取舍，与音乐上传同一套做法）。
        所以硬断言是「没有被收下」，响应码是能拿到才核对。
        """
        self.client.json("POST", "/now-playing", _song(title="原来的歌"))
        # 故意做成**合法 JSON**：如果只是往体后面补空白，服务端会因为
        # 「不是合法 JSON」而拒掉，测的就不是体积上限了。
        blob = json.dumps(_song(title="超大的歌",
                                lyrics=[{"time": 0, "text": "字" * 3_000_000}])).encode("utf-8")
        self.assertGreater(len(blob), 2 * 1024 * 1024, "用例本身得先超过上限")

        try:
            status, data = self.client.raw_post("/now-playing", blob,
                                               content_type="application/json")
        except Exception:  # noqa: BLE001 - 服务端提前拒绝导致连接重置
            status, data = 0, {"_reset": True}

        self.assertIn(status, (0, 413), "太大时应当拒绝：%s %s" % (status, data))
        state = self._state_of(self.server.port, "wangqi")
        self.assertIsNotNone(state)
        self.assertEqual(state["title"], "原来的歌", "超大上报不该生效")

    @staticmethod
    def _state_of(port, source):
        _status, data = Client(port).json("GET", "/now-playing?sources=1")
        for item in (data or {}).get("sources") or []:
            if item["source"] == source:
                return item
        return None


class LyricsDisabledTests(unittest.TestCase):
    """lyrics.enabled = false：要给出**明确**的关闭原因，而不是静默失败。"""

    @classmethod
    def setUpClass(cls):
        def disable_lyrics(cfg):
            cfg["lyrics"] = {"enabled": False, "stale_seconds": 10, "token": ""}

        cls.server = ServerProcess([tempfile.mkdtemp(prefix="fw-lyr-off-")],
                                   extra_config=disable_lyrics)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def test_get_is_403_with_a_reason(self):
        status, data = Client(self.server.port).json("GET", "/now-playing")
        self.assertEqual(status, 403, data)
        self.assertEqual(data["code"], "lyrics_disabled")
        self.assertIn("lyrics.enabled", data["message"])

    def test_post_is_403(self):
        status, data = Client(self.server.port).json("POST", "/now-playing", _song())
        self.assertEqual(status, 403, data)

    def test_websocket_is_rejected_at_handshake(self):
        url = "ws://127.0.0.1:%d/ws/lyrics" % self.server.port
        self.assertTrue(_ws_rejected(url), "功能关闭时必须拒绝连接")

    def test_system_info_reports_the_flag(self):
        """
        前端靠 features.lyrics 决定要不要开始上报，所以这个开关必须真的出现在
        系统信息里：关掉之后应当**一条上报都不产生**，而不是上报后被 403。
        """
        client = self.server.login_client()
        status, data = client.json("GET", "/api/system/info")
        self.assertEqual(status, 200, data)
        self.assertIs(data["features"]["lyrics"], False)


class LyricsTokenTests(unittest.TestCase):
    """token 是**预留**的扩展点：留空 = 不校验（默认），配了就必须带上。"""

    TOKEN = "lab-lyrics-token"

    @classmethod
    def setUpClass(cls):
        def set_token(cfg):
            cfg["lyrics"] = {"enabled": True, "stale_seconds": 60, "token": cls.TOKEN}

        cls.server = ServerProcess([tempfile.mkdtemp(prefix="fw-lyr-tok-")],
                                   extra_config=set_token)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def test_get_without_token_is_403(self):
        status, data = Client(self.server.port).json("GET", "/now-playing")
        self.assertEqual(status, 403, data)
        self.assertEqual(data["code"], "bad_token")

    def test_get_with_token_works(self):
        status, data = Client(self.server.port).json(
            "GET", "/now-playing?token=%s" % self.TOKEN)
        self.assertEqual(status, 200, data)

    def test_post_without_token_is_403(self):
        status, data = Client(self.server.port).json("POST", "/now-playing", _song())
        self.assertEqual(status, 403, data)
        self.assertEqual(data["code"], "bad_token")

    def test_post_token_in_body_works(self):
        payload = _song()
        payload["token"] = self.TOKEN
        status, data = Client(self.server.port).json("POST", "/now-playing", payload)
        self.assertEqual(status, 200, data)

    def test_websocket_needs_the_token(self):
        base = "ws://127.0.0.1:%d/ws/lyrics" % self.server.port
        self.assertTrue(_ws_rejected(base), "没带 token 必须拒绝")
        self.assertTrue(_ws_rejected(base + "?token=wrong"), "token 不对必须拒绝")
        payload = _ws_first(base + "?token=" + self.TOKEN)
        self.assertEqual(payload["type"], "state")


# ---------------------------------------------------------------------------
# 3) 真实服务：WebSocket 广播
# ---------------------------------------------------------------------------

class LyricsWsTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        def configure(cfg):
            cfg["lyrics"] = {"enabled": True, "stale_seconds": 2, "token": ""}

        cls.server = ServerProcess([tempfile.mkdtemp(prefix="fw-lyr-ws-")],
                                   extra_config=configure)
        cls.server.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.cleanup()

    def setUp(self):
        self.client = Client(self.server.port)
        for source in ("wangqi", "lisi"):
            self.client.json("POST", "/now-playing", {"type": "stop", "source": source})

    def _url(self, query=""):
        url = "ws://127.0.0.1:%d/ws/lyrics" % self.server.port
        return url + ("?" + query if query else "")

    def test_connecting_pushes_the_current_state_immediately(self):
        """连上就补一条状态，客户端不必再发一次 HTTP 查询（也不会有空白期）。"""
        self.client.json("POST", "/now-playing", _song())
        payload = _ws_first(self._url())
        self.assertEqual(payload["type"], "state")
        self.assertFalse(payload["idle"])
        self.assertEqual(payload["title"], "夜曲")
        self.assertEqual(len(payload["lyrics"]), 3)

    def test_idle_state_when_nothing_is_playing(self):
        payload = _ws_first(self._url())
        self.assertTrue(payload["idle"])

    def test_http_report_reaches_subscribers(self):
        """★ 主链路：浏览器 POST 上报 -> 悬浮窗收到广播。"""
        received = _ws_then(
            self._url(),
            lambda: self.client.json("POST", "/now-playing", _song()),
            count=1)
        first, second = received
        self.assertTrue(first["idle"])
        self.assertFalse(second["idle"])
        self.assertEqual(second["source"], "wangqi")
        self.assertEqual(second["title"], "夜曲")
        self.assertGreater(second["revision"], first["revision"], "revision 必须递增")

    def test_progress_reports_keep_coming(self):
        """
        一次连接里连报两条（歌 + 进度）——浏览器实际就是这样：切歌后每 0.5 秒
        跟一条进度。放在同一个 action 里是为了不受用例服务端 stale 窗口的影响。
        """
        def report_both():
            self.client.json("POST", "/now-playing", _song())
            self.client.json("POST", "/now-playing",
                             {"type": "progress", "source": "wangqi",
                              "currentTime": 61.5, "playing": False})

        received = _ws_then(self._url(), report_both, count=2)
        self.assertEqual(received[1]["title"], "夜曲")
        self.assertAlmostEqual(received[2]["currentTime"], 61.5, places=3)
        self.assertFalse(received[2]["playing"], "暂停状态必须原样传下去")

    def test_stop_is_broadcast_as_idle(self):
        self.client.json("POST", "/now-playing", _song())
        received = _ws_then(self._url(),
                            lambda: self.client.json("POST", "/now-playing",
                                                     {"type": "stop", "source": "wangqi"}),
                            count=1)
        self.assertFalse(received[0]["idle"], "连上时应该还有歌")
        self.assertTrue(received[1]["idle"], "停止后必须推 idle（悬浮窗据此淡出）")

    def test_both_subscribers_receive_the_same_report(self):
        """广播不是「发给第一个」：两个悬浮窗（例如两台机器）都要收到。"""
        async def run():
            async with _connect(self._url()) as one, _connect(self._url()) as two:
                await asyncio.wait_for(one.recv(), timeout=10)
                await asyncio.wait_for(two.recv(), timeout=10)
                await asyncio.to_thread(
                    lambda: self.client.json("POST", "/now-playing", _song()))
                return (json.loads(await asyncio.wait_for(one.recv(), timeout=10)),
                        json.loads(await asyncio.wait_for(two.recv(), timeout=10)))

        first, second = asyncio.run(run())
        self.assertEqual(first["title"], second["title"], "两个订阅者应收到同一条广播")
        self.assertFalse(first["idle"])

    def test_ws_only_gets_own_source(self):
        """
        ★ 分组订阅：?user=wangqi 的悬浮窗不该被别人放歌顶掉。

        退化（改成一律广播）时的症状是「别人放歌，我的歌词被换成他的」——
        在实验室里很难复现，所以在这里钉死。

        断言的是**内容不变**而不是「一条消息都没有」：服务端可能推一条内容
        相同的 idle（例如别人的状态过期被回收），那对用户是零影响。
        """
        received = _ws_drain(
            self._url("user=wangqi"),
            lambda: self.client.json("POST", "/now-playing",
                                     _song(source="lisi", title="别人的歌")))
        self.assertTrue(received[0]["idle"], "一开始什么都没在放")
        for message in received[1:]:
            self.assertNotEqual(message.get("source"), "lisi",
                                "订阅 wangqi 却收到了 lisi 的内容")
            self.assertTrue(message.get("idle"),
                            "订阅 wangqi 时只该收到「空闲」，不该冒出别人的歌：%s" % message)

        received = _ws_then(
            self._url("user=wangqi"),
            lambda: self.client.json("POST", "/now-playing",
                                     _song(source="wangqi", title="我的歌")),
            count=1)
        self.assertEqual(received[1]["title"], "我的歌")

    def test_unfiltered_subscriber_switches_to_a_new_source(self):
        """订阅全部时，谁开始放就显示谁（见 lyrics.pick 的挑选规则）。"""
        self.client.json("POST", "/now-playing", _song(source="wangqi", title="甲的"))
        received = _ws_then(
            self._url(),
            lambda: self.client.json("POST", "/now-playing",
                                     _song(source="lisi", title="乙的")),
            count=1)
        self.assertEqual(received[1]["title"], "乙的")

    def test_ws_pushes_idle_after_stale(self):
        """
        ★★ 「关掉浏览器后歌词自己消失」——整个功能里最容易被漏掉、
        又最确定会被用户抱怨的一条。

        配置里 stale_seconds=2：上报一首歌之后什么都不做，
        服务端应当主动推一条 idle（而不是等客户端来问）。
        """
        received = _ws_then(self._url(),
                            lambda: self.client.json("POST", "/now-playing", _song()),
                            count=2, timeout=12)
        self.assertFalse(received[1]["idle"])
        self.assertTrue(received[2]["idle"], "过期后必须主动推 idle")

    def test_get_now_playing_is_idle_after_stale(self):
        self.client.json("POST", "/now-playing", _song())
        status, data = self.client.json("GET", "/now-playing")
        self.assertFalse(data["display"]["idle"])
        time.sleep(2.6)
        status, data = self.client.json("GET", "/now-playing")
        self.assertTrue(data["display"]["idle"], "过期后查询必须返回 idle")

    def test_report_can_be_sent_over_the_websocket(self):
        """WS 通道同样接受上报（浏览器走 POST，但两条路等价 —— 见路由头部）。"""
        async def run():
            async with _connect(self._url()) as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)          # 初始状态
                await ws.send(json.dumps(_song(source="wangqi", title="走 WS 的")))
                while True:
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "state" and not message.get("idle"):
                        return message

        message = asyncio.run(run())
        self.assertEqual(message["title"], "走 WS 的")

    def test_ping_gets_a_pong(self):
        """悬浮窗用它测延迟 / 保活（不依赖底层的协议层 ping）。"""
        async def run():
            async with _connect(self._url()) as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send(json.dumps({"type": "ping", "t": 12345}))
                while True:
                    message = json.loads(await asyncio.wait_for(ws.recv(), timeout=10))
                    if message.get("type") == "pong":
                        return message

        message = asyncio.run(run())
        self.assertEqual(message["t"], 12345)
        self.assertIn("serverTime", message)

    def test_unknown_message_gets_an_error_not_a_disconnect(self):
        """排障时手输错了不该把连接搞断（断了还得重连等 1 秒）。"""
        async def run():
            async with _connect(self._url()) as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                await ws.send("这不是 JSON")
                return json.loads(await asyncio.wait_for(ws.recv(), timeout=10))

        message = asyncio.run(run())
        self.assertEqual(message["type"], "error")
        self.assertEqual(message["code"], "bad_json")

    def test_subscriber_is_released_on_disconnect(self):
        """
        断开必须释放订阅者：否则每次刷新页面都会在服务端留一份队列，
        跑一天下来就是内存泄漏 —— 而且症状（越来越慢）很难归因到这个功能。
        """
        async def run():
            async with _connect(self._url()) as ws:
                await asyncio.wait_for(ws.recv(), timeout=10)
                _status, data = await asyncio.to_thread(
                    lambda: self.client.json("GET", "/now-playing?sources=1"))
                return data["subscribers"]

        count = asyncio.run(run())
        self.assertEqual(count, 1, "连接期间应当正好有一个订阅者")
        # 断开后要等一小会儿让服务端处理收尾
        deadline = time.time() + 5
        while time.time() < deadline:
            _status, data = self.client.json("GET", "/now-playing?sources=1")
            if data["subscribers"] == 0:
                break
            time.sleep(0.2)
        self.assertEqual(data["subscribers"], 0, "断开后订阅者必须被回收")


if __name__ == "__main__":
    unittest.main(verbosity=2)

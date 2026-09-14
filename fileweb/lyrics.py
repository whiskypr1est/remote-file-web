# -*- coding: utf-8 -*-
"""
桌面歌词转发中枢（内存表）
==========================

虚拟桌面里有一个内置音乐播放器（static/js/music.js）。用户希望在**关掉浏览器
之后**仍然能在屏幕角上看到歌词 —— 那是一个独立进程（desktop-lyrics/ 里的
Electron 悬浮窗），所以中间需要一个中转：

    网页播放器 --上报--> 服务端（本模块）--广播--> 悬浮窗

为什么要中转，而不是让悬浮窗直接连浏览器
----------------------------------------
浏览器不能当服务端：它没有固定端口、没有证书，而且关掉标签页连接就断了。
更重要的是**悬浮窗本来就要在浏览器关掉之后继续工作**，所以状态必须离开浏览器。

为什么是内存
------------
和 presence.py 一样，这是**实时状态**，不是数据。进程重启后「现在在放什么」
本来就无从得知，客户端重连时会自己来问一次（GET /now-playing），
所以没必要落盘 —— 也就没有清理、迁移、并发写这些麻烦事。
（对比：曲库/歌单是**数据**，所以它们老老实实落在磁盘上。）

为什么浏览器用 HTTP 上报，而不是复用那条 WebSocket
--------------------------------------------------
上报是**单向、幂等、每 500ms 一次**的：用 POST 时每次都是独立的成功/失败，
一次失败不影响下一次，浏览器侧不必维护长连接和重连状态机；
WS 就专做「服务端 -> 悬浮窗」这一个方向的推送，职责单一。
若你更希望浏览器也走 WS，本模块的 WS 通道**同样接受** report 消息
（见 routers/lyrics.py），两条路完全等价。

★ 一个源（source）一份状态
--------------------------
上报里带 `source`（用户名）。同一台机器上不同用户各播各的，所以状态要按源
分别记；悬浮窗可以只订阅某个用户（?user=wangqi），默认订阅全部。
订阅全部时该显示谁？见 pick()：**正在播放的优先**，然后才比谁上报得更近。
不这么定的话，某个同学暂停着（暂停期间仍在发心跳）会把正在放歌的同学顶掉，
表现为歌词莫名其妙地来回跳。

★ 过期（stale）
--------------
悬浮窗必须在「浏览器被关掉」之后自己消失。能依据的只有一件事：**还在上报吗**。
播放器播放时每 500ms 报一次、暂停时每 5s 报一次心跳，所以
超过 stale_seconds（默认 10 秒）没有任何消息 = 那个源已经不在了。

★ 安全边界
----------
本模块的数据来自 /now-playing 与 /ws/lyrics，它们**不在 /api 下**，因此不要求
登录（见 app.py 的中间件注释）：悬浮窗是独立客户端，没有会话 Cookie。
按用户的要求，这是局域网内互相信任的部署，不做鉴权；`lyrics.token` 是预留的
扩展点（留空 = 不校验）。由此带来两个必须写明的后果：
  1. 局域网内任何人都能伪造上报，往别人的悬浮窗上推任意文字；
  2. 因此所有文本长度都必须截断（见下面的 MAX_* 常量），不能由上报方决定内存占用。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 上限：这些是**防御性**的，不是产品参数
# ---------------------------------------------------------------------------
# 歌词行数与单行长度：一首歌正常不过几百行，LRC 文件本身也被
# music.MAX_LYRICS_BYTES 限制在 256KB。这里留足余量的同时给出硬上限，
# 避免一条超大 JSON 就把服务端内存吃掉。
MAX_LYRICS_LINES = 2000
MAX_LINE_CHARS = 500
MAX_TEXT_CHARS = 200            # 标题 / 歌手 / 专辑
MAX_SOURCE_CHARS = 64

# 状态表里最多保留多少个源。正常就是用户数；上限是为了防止有人用随机
# source 名把内存撑大（每个源最多 2000 行歌词）。
MAX_SOURCES = 200

DEFAULT_STALE_SECONDS = 10.0
# 扫描间隔：过期判定不需要精确到毫秒，1 秒足够（表现为「关掉浏览器后
# 歌词最多多留 1 秒左右」）。再小只是白烧 CPU。
SWEEP_INTERVAL_SECONDS = 1.0

# 上报类型的合法取值
REPORT_TYPES = ("song", "progress", "stop")


# ---------------------------------------------------------------------------
# 取值清洗
# ---------------------------------------------------------------------------

def clamp_text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    """把任意输入变成「去掉首尾空白、且不超过 limit 个字符」的字符串。"""
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def clamp_number(value: Any, default: float = 0.0) -> float:
    """把任意输入变成非负有限浮点数（NaN / inf / 字符串 / None 都归到 default）。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return number if number >= 0 else default


def parse_time(value: Any) -> Optional[float]:
    """
    解析歌词行的时间戳，**不合法就返回 None**。

    与 clamp_number 的区别在于「不合法」要丢掉整行，而不是归到 0 秒：
    一行没有时间戳的歌词是没法同步的，归到 0 秒只会让它挤在开头一闪而过，
    倒不如不显示。（parseLrc 产出的每一行都带合法时间，所以正常不会被丢。）
    """
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds != seconds or seconds in (float("inf"), float("-inf")) or seconds < 0:
        return None
    return seconds


def clamp_lyrics(raw: Any) -> List[Dict[str, Any]]:
    """
    把上报的歌词规整成 [{time, text}, ...]（按时间升序）。

    与 static/js/music.js 的 parseLrc() 返回的 {time, text} 完全对齐 —— 两边
    是同一份结构，上报时不需要转换。这里只做三件事：丢坏行、截断、排序。
    """
    if not isinstance(raw, list):
        return []

    lines: List[Dict[str, Any]] = []
    for item in raw[:MAX_LYRICS_LINES]:
        if not isinstance(item, dict):
            continue
        seconds = parse_time(item.get("time"))
        if seconds is None:
            continue
        text = clamp_text(item.get("text"), MAX_LINE_CHARS)
        if not text:
            # 空行（LRC 里很常见，用来分段）对单行悬浮窗没有意义，直接丢掉，
            # 免得悬浮窗上出现「当前行是空白」的闪烁。
            continue
        lines.append({"time": seconds, "text": text})

    lines.sort(key=lambda item: item["time"])
    return lines


def normalize_source(value: Any) -> str:
    return clamp_text(value, MAX_SOURCE_CHARS)


# ---------------------------------------------------------------------------
# 订阅者
# ---------------------------------------------------------------------------

class Subscriber:
    """
    一个已连接的悬浮窗。

    每个订阅者自带一条**有界队列**，而不是让广播方直接 await ws.send()：
    后者只要有一个客户端网络卡住，就会把上报接口一起拖住（上报是每 500ms
    一次的，一卡就会雪崩）。有界队列 + 丢最旧，保证慢客户端只影响自己。
    """

    __slots__ = ("queue", "user", "last_signature", "closed")

    # 队列长度：状态是「最新覆盖旧的」，积压几十条已经毫无意义
    QUEUE_SIZE = 32

    def __init__(self, user: str = ""):
        self.queue: "asyncio.Queue[Dict[str, Any]]" = asyncio.Queue(maxsize=self.QUEUE_SIZE)
        self.user = user            # "" = 订阅全部
        # 上一次推给它的内容签名（见 LyricsHub._signature）。存「内容」而不是
        # 「全局 revision」是必须的：过期回收会 bump 全局 revision，但被回收的
        # 是**别人的**源时，这个订阅者的画面其实一点没变，不该再推一条。
        self.last_signature: Optional[str] = None
        self.closed = False

    def matches(self, source: str) -> bool:
        return not self.user or self.user == source

    def offer(self, payload: Dict[str, Any]) -> None:
        """投递一条消息；队列满时丢掉**最旧**的一条，保证最新的状态一定送到。"""
        if self.closed:
            return
        try:
            self.queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:  # pragma: no cover - 并发下的兜底
                pass
            try:
                self.queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - 上面刚腾出位置
                pass


# ---------------------------------------------------------------------------
# 中枢
# ---------------------------------------------------------------------------

class LyricsHub:
    """
    「现在在放什么」的内存表 + 广播。

    线程模型：**只在事件循环里用**。所有方法都不含 await（除了扫描任务本身），
    因此不存在「读到一半被改写」的情况，也就不需要锁 —— 这是 asyncio 的
    常规做法，不是偷懒。跨线程调用（例如线程池里）是不允许的。
    """

    def __init__(self, stale_seconds: float = DEFAULT_STALE_SECONDS):
        self._states: Dict[str, Dict[str, Any]] = {}   # source -> state
        self._subscribers: List[Subscriber] = []
        self._revision = 0
        self._stale_seconds = DEFAULT_STALE_SECONDS
        self._sweeper: Optional[asyncio.Task] = None
        self.configure(stale_seconds)
        # 统计量（只增不减，供 /now-playing 与排障用）
        self.stats: Dict[str, int] = {"reports": 0, "rejected": 0, "subscribers_seen": 0}

    # -- 配置 ---------------------------------------------------------------

    def configure(self, stale_seconds: Any = None) -> None:
        try:
            value = float(stale_seconds)
        except (TypeError, ValueError):
            value = DEFAULT_STALE_SECONDS
        if value != value or value <= 0:      # NaN / 0 / 负数
            value = DEFAULT_STALE_SECONDS
        # 下限 2 秒：太短的话播放器正常的网络抖动就会被判成「已关闭」，
        # 悬浮窗会一闪一闪的。
        self._stale_seconds = max(2.0, min(600.0, value))

    @property
    def stale_seconds(self) -> float:
        return self._stale_seconds

    # -- 上报 ---------------------------------------------------------------

    def report(self, payload: Any) -> Tuple[Dict[str, Any], Optional[str]]:
        """
        处理一条上报。

        返回 (响应, 错误)。错误非空表示这条上报被拒（响应里带有原因），
        但**调用方不需要把它当异常**：上报是尽力而为的，播放器不会因为
        服务端拒了一条就停止播放。
        """
        if not isinstance(payload, dict):
            self.stats["rejected"] += 1
            return {"ok": False, "error": "上报必须是 JSON 对象"}, "上报必须是 JSON 对象"

        # ★ 不认 type 一律拒绝（**不给默认值**）。上报只有三个合法形状，
        #   给默认值的话，一个字段名写错的上报会被当成 progress 悄悄吞掉 ——
        #   那正是「明明在放歌，悬浮窗却一直空着」这种最难查的故障。
        kind = clamp_text(payload.get("type"), 32)
        if kind not in REPORT_TYPES:
            self.stats["rejected"] += 1
            message = ("type 必须是 %s 之一（收到的是 %r）"
                       % ("、".join(REPORT_TYPES), kind))
            return {"ok": False, "error": message}, message

        source = normalize_source(payload.get("source"))
        self.stats["reports"] += 1

        if kind == "stop":
            self._states.pop(source, None)
            self._revision += 1
            self.reflect_all(source)
            return self._result("stop", source), None

        if kind == "progress":
            state = self._states.get(source)
            if state is None:
                # 服务端重启过、或者状态已过期被清掉了：只靠进度无法知道
                # 歌名与歌词，所以请播放器把整首歌再报一次（它手里有全部信息）。
                # 这条路径就是「服务端重启后歌词能自己回来」的实现。
                return {"ok": True, "type": "progress", "needSong": True,
                        "display": self.snapshot()}, None
            state["currentTime"] = self._clamp_time(payload.get("currentTime"),
                                                    state.get("duration") or 0.0)
            state["playing"] = bool(payload.get("playing", state.get("playing", False)))
            if payload.get("album") is not None:
                state["album"] = clamp_text(payload.get("album"))
            state["version"] = self._next_version(source)
            state["updatedAt"] = time.time()
            self._revision += 1
            self.reflect_all(source)
            return self._result("progress", source), None

        # ---- song：整首歌的信息（含歌词）----
        title = clamp_text(payload.get("title"))
        if not title:
            self.stats["rejected"] += 1
            message = "song 上报缺少 title"
            return {"ok": False, "error": message}, message

        duration = clamp_number(payload.get("duration"))
        state = {
            "source": source,
            "version": self._next_version(source),
            "title": title,
            "artist": clamp_text(payload.get("artist")),
            "album": clamp_text(payload.get("album")),
            "duration": duration,
            "lyrics": clamp_lyrics(payload.get("lyrics")),
            "currentTime": self._clamp_time(payload.get("currentTime"), duration),
            "playing": bool(payload.get("playing", True)),
            "updatedAt": time.time(),
        }

        if source not in self._states and len(self._states) >= MAX_SOURCES:
            # 表满了：丢掉最旧的源。正常永远走不到这里（源 = 用户名）。
            oldest = min(self._states.items(), key=lambda kv: kv[1].get("updatedAt", 0.0))
            self._states.pop(oldest[0], None)

        self._states[source] = state
        self._revision += 1
        self.reflect_all(source)
        return self._result("song", source), None

    def _result(self, kind: str, source: str) -> Dict[str, Any]:
        return {"ok": True, "type": kind, "needSong": False,
                "source": source, "display": self.snapshot()}

    def _next_version(self, source: str) -> int:
        """
        某个源自己的版本号，每次它上报就 +1。

        为什么要一个**按源**的版本号，而不是只看全局 revision：
        revision 是所有源共用的，过期回收也会让它 +1。若拿它当「画面变了吗」
        的依据，一个同学的状态过期就会给所有人（包括正订阅别人的悬浮窗）
        各推一条无用消息 —— 平时只是白跑一趟，网差的时候就是多余的唤醒。
        按源的版本号配合 pick() 算出的「该显示谁」，才真正等于「画面变了」。
        """
        previous = self._states.get(source) or {}
        return int(previous.get("version") or 0) + 1

    @staticmethod
    def _signature(payload: Dict[str, Any]) -> str:
        """「这个订阅者现在该看到的画面」的指纹（内容层面的，不是计数器的）。"""
        if payload.get("idle"):
            return "idle"
        return "%s#%s" % (payload.get("source") or "", payload.get("version") or 0)

    @staticmethod
    def _clamp_time(value: Any, duration: float) -> float:
        """
        播放进度归一到 [0, duration]。

        夹到 duration 是有意义的：悬浮窗会用「进度 + 本地流逝时间」外推滚动，
        一旦进度比时长还大（播放器在末尾的抖动），外推就会把歌词停在最后一行
        之后，看起来像卡死。夹住之后最多就是停在最后一行。
        """
        position = clamp_number(value)
        if duration and duration > 0:
            return min(position, duration)
        return position

    # -- 查询 ---------------------------------------------------------------

    def _fresh(self, now: float) -> List[Dict[str, Any]]:
        return [state for state in self._states.values()
                if now - float(state.get("updatedAt") or 0.0) <= self._stale_seconds]

    def pick(self, user: str = "") -> Optional[Dict[str, Any]]:
        """
        选出该订阅者应该显示的那一份状态。

        规则（顺序有意义）：
          1. 只在**没过期**的状态里挑；
          2. 按 user 过滤（空 = 不筛）；
          3. 正在播放的优先 —— 见模块头部「一个源一份状态」；
          4. 同一档里取最近上报的。
        """
        now = time.time()
        candidates = [state for state in self._fresh(now)
                      if not user or state.get("source") == user]
        if not candidates:
            return None
        playing = [state for state in candidates if state.get("playing")]
        pool = playing or candidates
        return max(pool, key=lambda state: float(state.get("updatedAt") or 0.0))

    def snapshot(self, user: str = "") -> Dict[str, Any]:
        """
        给某个订阅者/查询者的当前状态。

        空闲时同样返回一个 **type=state** 的完整对象（idle=true），
        这样客户端只需要处理一种消息形状：连上就是 state，空闲就是 idle，
        不必再区分「事件」和「快照」。
        """
        payload: Dict[str, Any] = {
            "type": "state",
            "revision": self._revision,
            "serverTime": time.time(),
            "staleSeconds": self._stale_seconds,
            "idle": True,
        }
        state = self.pick(user)
        if state is not None:
            payload.update(state)
            payload["idle"] = False
        if user:
            payload["subscribe"] = user
        return payload

    def sources(self) -> List[Dict[str, Any]]:
        """当前有状态的所有源（管理/排障用，不含歌词正文）。"""
        now = time.time()
        return sorted(
            (
                {
                    "source": state.get("source") or "",
                    "title": state.get("title") or "",
                    "artist": state.get("artist") or "",
                    "playing": bool(state.get("playing")),
                    "currentTime": state.get("currentTime") or 0.0,
                    "duration": state.get("duration") or 0.0,
                    "lines": len(state.get("lyrics") or []),
                    "fresh": now - float(state.get("updatedAt") or 0.0) <= self._stale_seconds,
                    "updatedAt": state.get("updatedAt") or 0.0,
                }
                for state in self._states.values()
            ),
            key=lambda item: item["updatedAt"],
            reverse=True,
        )

    # -- 订阅 ---------------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def subscribe(self, user: str = "") -> Subscriber:
        subscriber = Subscriber(user)
        self._subscribers.append(subscriber)
        self.stats["subscribers_seen"] += 1
        self._ensure_sweeper()
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        subscriber.closed = True
        try:
            self._subscribers.remove(subscriber)
        except ValueError:
            pass
        if not self._subscribers:
            self._stop_sweeper()

    def reflect_all(self, changed_source: Optional[str] = None) -> None:
        """
        把「每个订阅者现在该看到什么」推给它。

        注意这里推的是**该订阅者的显示状态**，而不是「谁上报了什么」：
        这样一个源停止播放时，订阅它的悬浮窗收到的是 idle；而订阅全部的
        悬浮窗收到的是「另一个还在放的源」—— 三种情况（停止、切换源、过期）
        共用同一条代码路径，客户端不必自己维护「谁在放」的模型。

        只有**画面真的变了**才推（比对 _signature）；按源过滤的订阅者与其它源
        的变化无关，直接跳过（省掉一次快照构造）。
        """
        for subscriber in list(self._subscribers):
            if subscriber.closed:
                continue
            if subscriber.user and changed_source is not None \
                    and subscriber.user != changed_source:
                continue
            payload = self.snapshot(subscriber.user)
            signature = self._signature(payload)
            if signature == subscriber.last_signature:
                continue
            subscriber.last_signature = signature
            subscriber.offer(payload)

    def welcome(self, subscriber: Subscriber) -> Dict[str, Any]:
        """
        连上时的第一条消息。

        顺手记下签名，免得紧接着的一次 reflect 把同样的内容再推一遍。
        （若在这两步之间恰好来了一条上报，客户端会多收一条**内容相同或更新**
        的状态 —— 重复渲染同一个状态是无害的，而漏掉一条会一直错到下一拍。）
        """
        payload = self.snapshot(subscriber.user)
        subscriber.last_signature = self._signature(payload)
        return payload

    # -- 过期回收 -----------------------------------------------------------

    def purge_stale(self) -> List[str]:
        """
        丢掉过期状态，返回被丢掉的源。

        ★ 这里**必须**配合「暂停时仍然发心跳」：如果一个源只是暂停了很久，
        它的状态过期后被清掉，等用户按播放时只剩一条 progress 上报 ——
        那时服务端已经不知道歌名和歌词了。所以进度上报在查不到状态时会回
        needSong=true，请播放器把整首歌重报一次（见 report()）。
        两边合起来，无论暂停多久、甚至服务端重启，歌词都能自己恢复。
        """
        now = time.time()
        gone = [source for source, state in self._states.items()
                if now - float(state.get("updatedAt") or 0.0) > self._stale_seconds]
        for source in gone:
            self._states.pop(source, None)
        if gone:
            self._revision += 1
            self.reflect_all()
        return gone

    def _ensure_sweeper(self) -> None:
        """
        懒启动扫描任务。

        只在**有人订阅**时才需要它：没有订阅者就没有人要通知，而过期判定在
        pick()/purge_stale() 里是按时刻实时算的，所以 GET /now-playing 就算
        没有扫描任务也永远返回正确结果。
        """
        if self._sweeper is not None and not self._sweeper.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # 没有事件循环（单测里直接调）：不启动
            return
        self._sweeper = loop.create_task(self._sweep_forever())

    def _stop_sweeper(self) -> None:
        task = self._sweeper
        self._sweeper = None
        if task is not None and not task.done():
            task.cancel()

    async def _sweep_forever(self) -> None:
        try:
            while True:
                await asyncio.sleep(SWEEP_INTERVAL_SECONDS)
                self.purge_stale()
        except asyncio.CancelledError:  # pragma: no cover - 正常关闭路径
            raise

    def shutdown(self) -> None:
        """服务关停时清干净（供测试与 lifespan 使用）。"""
        self._stop_sweeper()
        for subscriber in list(self._subscribers):
            subscriber.closed = True
        self._subscribers.clear()
        self._states.clear()
        self._revision += 1


# ---------------------------------------------------------------------------
# 进程内单例
# ---------------------------------------------------------------------------
# 一个进程只服务一个应用实例（uvicorn 单进程、测试也是一个服务一个子进程），
# 所以用模块级单例就够了 —— 与 presence.py / userstate.py 的做法一致，
# 不必再往 AppState 上挂一份。
_HUB = LyricsHub()


def get_hub() -> LyricsHub:
    return _HUB


def reset_hub() -> LyricsHub:
    """清空单例（只给测试用：同一个进程里连跑多个用例时互不干扰）。"""
    _HUB.shutdown()
    return _HUB

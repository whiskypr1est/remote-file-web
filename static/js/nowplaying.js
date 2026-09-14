/* ==========================================================================
   桌面歌词上报
   --------------------------------------------------------------------------
   把「现在在放什么」上报给服务端（fileweb/routers/lyrics.py），服务端再广播给
   桌面悬浮窗客户端（desktop-lyrics/）。这样**关掉浏览器之后**歌词还在。

   为什么走 HTTP 而不是 WebSocket
   ------------------------------
   上报是单向、幂等、每 0.5 秒一次的：每次 POST 都是独立的成功/失败，一次失败
   不影响下一次，这里也就不需要维护长连接与重连状态机。服务端 -> 悬浮窗那一
   方向才用 WebSocket（那边需要长连接推送）。

   ★ 三条硬性约束（都是「不能因为歌词影响听歌」的具体化）
   ------------------------------------------------------
   1. **绝不影响播放**：所有网络动作都是「发出去就不管」，任何异常都被吞掉，
      连日志都不往控制台刷（每 0.5 秒一条报错会把控制台淹没）。
   2. **绝不堆积请求**：同一时刻只允许一个在途请求，超时 3 秒就中断。
      否则服务端一旦无响应，2 次/秒的请求会越积越多，最后把浏览器拖死。
   3. **失败就退避**：连续失败几次后把节奏放慢到 5 秒一次（依然静默重试），
      服务端恢复后会自动回到 0.5 秒 —— 不需要用户做任何事。

   暂停时为什么要发心跳
   --------------------
   服务端靠「还在上报吗」判断播放器是不是已经关掉了（见 lyrics.py 的 stale）。
   暂停时若完全静默，10 秒后服务端就认为人走了，悬浮窗会淡出 —— 但用户只是
   按了暂停，歌词该留着（半透明）。所以暂停期间每 5 秒补一条心跳。

   为什么上报里带 source（用户名）
   -------------------------------
   一台机器上不同用户各播各的，服务端按 source 分别记状态；悬浮窗可以只订阅
   某个用户（?user=xxx）。不带的话所有人的悬浮窗会互相顶掉。
   ========================================================================== */

/** 上报地址。★ 故意在 /api 之外：悬浮窗与上报都不需要登录（见服务端注释）。 */
const ENDPOINT = '/now-playing';

/** 播放中的最小上报间隔（服务端据此判断「还活着」）。 */
const PROGRESS_INTERVAL_MS = 500;
/** 暂停时的心跳间隔。必须明显小于服务端的 stale 窗口（默认 10 秒）。 */
const PAUSED_HEARTBEAT_MS = 5000;
/** 单次请求超时。超过就当失败，避免请求堆积。 */
const REQUEST_TIMEOUT_MS = 3000;
/** 连续失败到这个次数之后，把节奏放慢（静默重试，不打扰用户）。 */
const FAILURES_BEFORE_SLOW = 2;
/** 退避后的上报间隔。 */
const SLOW_RETRY_MS = 5000;
/** 歌词行数上限，与服务端的 MAX_LYRICS_LINES 保持一致。 */
const MAX_LYRICS_LINES = 2000;

const config = { enabled: false, source: '' };

/** 最近一次上报的整首歌（没有它就不发进度：服务端也无从知道是哪首歌）。 */
let song = null;
let lastTime = 0;
let lastPlaying = false;
let lastProgressAt = 0;
let inFlight = false;
/** 在途请求结束后要补发的那一条（见 send 的说明）。只保留最新的。 */
let pending = null;
let failures = 0;
let heartbeatTimer = null;
let unloadInstalled = false;

/* -- 内部：发送 ------------------------------------------------------------ */

/**
 * 发一条上报。
 *
 * ★ 同一时刻只允许一个在途请求，但**不丢后续的状态**：在途期间来的上报会
 *   存进 pending，等这次结束后补发（只保留最新的那条）。
 *
 *   这条「合并待发」不是优化，是必需的：切歌时会先报一次「歌名（歌词还没
 *   到）」，歌词加载完（几十毫秒后）再报一次带歌词的。如果后一条被丢掉，
 *   悬浮窗就永远只有歌名没有歌词 —— 而且看起来像「这歌没歌词」。
 *
 * @param {object} payload
 * @param {boolean} [beacon] true 时用 sendBeacon（页面正在卸载，不能再 fetch）
 */
function send(payload, beacon) {
  if (beacon && typeof navigator !== 'undefined' && navigator.sendBeacon) {
    try {
      // sendBeacon 的 Content-Type 由 Blob 的类型决定 —— 服务端按 JSON 解析，
      // 所以必须显式带上 application/json。
      navigator.sendBeacon(ENDPOINT,
        new Blob([JSON.stringify(payload)], { type: 'application/json' }));
    } catch (err) { /* 卸载路径上什么都不做 */ }
    return;
  }

  if (inFlight) {
    pending = payload;
    return;
  }
  inFlight = true;

  let timer = null;
  let controller = null;
  let resend = false;
  try {
    controller = typeof AbortController === 'function' ? new AbortController() : null;
  } catch (err) {
    controller = null;
  }
  if (controller) {
    timer = setTimeout(function () {
      try {
        controller.abort();
      } catch (err) { /* 忽略 */ }
    }, REQUEST_TIMEOUT_MS);
  }

  const options = {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  };
  if (controller) {
    options.signal = controller.signal;
  }

  fetch(ENDPOINT, options)
    .then(function (resp) {
      if (!resp.ok) {
        // 被明确拒绝（功能已关闭 / token 不符 / 上报不合法）：重试也是同样结果，
        // 所以按失败计（放慢节奏），但同样静默 —— 不打扰听歌的人。
        failures += 1;
        return null;
      }
      failures = 0;
      return resp.json().catch(function () { return null; });
    })
    .then(function (data) {
      // ★ 服务端说「我不知道现在放的是哪首歌」= 它重启过，或者这个源的状态
      //   已经过期被回收了。此时光报进度没有意义，必须把整首歌重报一次 ——
      //   这就是「服务端重启后歌词自己回来」的实现。
      resend = !!(data && data.needSong);
    })
    .catch(function () {
      failures += 1;         // ★ 静默：不 toast、不 console.error、不影响播放
    })
    .then(function () {
      if (timer) {
        clearTimeout(timer);
      }
      inFlight = false;
      if (resend) {
        sendSong();
      }
      if (pending) {
        const next = pending;
        pending = null;
        send(next);
      }
    });
}

/** 组装并发送整首歌的上报（歌词只保留能对轴的行）。 */
function sendSong() {
  if (!song) {
    return;
  }
  send({
    type: 'song',
    source: config.source,
    title: song.title,
    artist: song.artist,
    album: song.album,
    duration: song.duration,
    currentTime: lastTime,
    playing: lastPlaying,
    lyrics: song.lyrics
  });
}

/** 把 [{time,text}] 规整成可对轴的行（丢掉无时间戳的，并限制条数）。 */
function normalizeLyrics(lines) {
  if (!Array.isArray(lines)) {
    return [];
  }
  const out = [];
  for (let i = 0; i < lines.length && out.length < MAX_LYRICS_LINES; i += 1) {
    const line = lines[i] || {};
    const raw = line.time;
    const text = String(line.text == null ? '' : line.text).trim();
    // ★ 必须先排除 null / undefined / 空串，再交给 Number()：
    //   JS 里 Number(null) === 0、Number('') === 0，直接转换会把「没有时间轴
    //   的纯文本歌词」当成「第 0 秒的歌词」，结果是整首歌的歌词全挤在开头，
    //   悬浮窗会一直卡在第一句。parseLrc 对纯文本歌词给的就是 time: null。
    if (raw === null || raw === undefined || raw === '') {
      continue;
    }
    const seconds = Number(raw);
    // 纯文本歌词（没有时间轴）对单行滚动悬浮窗没有意义，直接不发 ——
    // 悬浮窗会退化成只显示「歌名 - 歌手」，比显示一行不会动的字要好。
    if (!text || !isFinite(seconds) || seconds < 0) {
      continue;
    }
    out.push({ time: seconds, text: text });
  }
  out.sort(function (a, b) { return a.time - b.time; });
  return out;
}

/** 暂停期间的心跳定时器（只有上报过歌之后才需要）。 */
function ensureHeartbeat() {
  if (heartbeatTimer !== null) {
    return;
  }
  heartbeatTimer = setInterval(function () {
    if (!config.enabled || !song || lastPlaying) {
      return;                // 播放中由 timeupdate 驱动，这里不重复发
    }
    reportProgress(lastTime, false, true);
  }, PAUSED_HEARTBEAT_MS);
}

function stopHeartbeat() {
  if (heartbeatTimer !== null) {
    clearInterval(heartbeatTimer);
    heartbeatTimer = null;
  }
}

/**
 * 页面被关掉/刷新时补一条 stop。
 *
 * 服务端本来也能靠超时把歌词收掉（10 秒），但那意味着「关掉浏览器后歌词还
 * 挂着十秒」；补一条 stop 就能立刻消失。用 sendBeacon 是因为卸载阶段 fetch
 * 会被浏览器直接掐掉。
 */
function installUnloadHandler() {
  if (unloadInstalled || typeof window === 'undefined') {
    return;
  }
  unloadInstalled = true;
  window.addEventListener('pagehide', function () {
    if (config.enabled && song) {
      send({ type: 'stop', source: config.source }, true);
    }
  });
}

/* -- 对外接口 -------------------------------------------------------------- */

/**
 * 配置上报（由 music.js 在拿到 /api/system/info 的 features 之后调用）。
 *
 * @param {{enabled?: boolean, source?: string}} options
 *        enabled 来自 features.lyrics；source 是当前用户名。
 */
export function configureNowPlaying(options) {
  const opts = options || {};
  const wasEnabled = config.enabled;
  config.enabled = opts.enabled === true;
  if (Object.prototype.hasOwnProperty.call(opts, 'source')) {
    config.source = String(opts.source || '');
  }
  if (!config.enabled && wasEnabled && song) {
    // 服务端把功能关掉了：通知一次 stop，别让悬浮窗一直挂着上一首
    send({ type: 'stop', source: config.source });
    song = null;
    stopHeartbeat();
  }
  if (config.enabled) {
    installUnloadHandler();
  }
}

/**
 * 上报一首歌（切歌、或歌词刚加载/刚改完）。
 *
 * @param {{title?: string, artist?: string, album?: string, duration?: number,
 *          currentTime?: number, playing?: boolean, lyrics?: Array}} info
 */
export function reportSong(info) {
  if (!config.enabled) {
    return;
  }
  const data = info || {};
  const title = String(data.title || '').trim();
  if (!title) {
    return;                 // 没歌名服务端也会拒（400），不如本地就拦住
  }
  song = {
    title: title,
    artist: String(data.artist || '').trim(),
    album: String(data.album || '').trim(),
    duration: isFinite(Number(data.duration)) ? Number(data.duration) : 0,
    lyrics: normalizeLyrics(data.lyrics)
  };
  lastTime = isFinite(Number(data.currentTime)) ? Number(data.currentTime) : 0;
  lastPlaying = data.playing !== false;
  lastProgressAt = Date.now();
  failures = 0;             // 换歌是新信息，值得立刻重试一次
  sendSong();
  ensureHeartbeat();
}

/**
 * 上报播放进度。由 music.js 在 timeupdate / play / pause / seeked 时调用。
 *
 * @param {number} currentTime 秒
 * @param {boolean} playing
 * @param {boolean} [force] 状态变化（暂停/继续/拖动）时传 true，立刻发一条
 */
export function reportProgress(currentTime, playing, force) {
  if (!config.enabled || !song) {
    return;
  }
  const seconds = isFinite(Number(currentTime)) ? Number(currentTime) : 0;
  const isPlaying = playing !== false;
  const now = Date.now();

  // 状态本身变了（播 -> 暂停、拖动进度）必须立刻上报：这是用户按了按钮的
  // 直接反馈，晚 0.5 秒看起来就像没反应。
  const changed = !!force || isPlaying !== lastPlaying;

  lastTime = seconds;
  lastPlaying = isPlaying;

  if (!changed) {
    const interval = isPlaying ? PROGRESS_INTERVAL_MS : PAUSED_HEARTBEAT_MS;
    if (now - lastProgressAt < interval) {
      return;
    }
    // 连续失败后退避：服务端没起来时不要以 2 次/秒的节奏一直敲
    if (failures >= FAILURES_BEFORE_SLOW && now - lastProgressAt < SLOW_RETRY_MS) {
      return;
    }
  }

  lastProgressAt = now;
  send({
    type: 'progress',
    source: config.source,
    currentTime: seconds,
    playing: isPlaying
  });
}

/** 停止上报（关掉播放器窗口时调用）：让悬浮窗立刻淡出。 */
export function reportStop() {
  if (!config.enabled || !song) {
    return;
  }
  song = null;
  stopHeartbeat();
  send({ type: 'stop', source: config.source });
}

/** 供排障/测试查看当前上报状态（只在控制台里看看，界面不依赖它）。 */
export function nowPlayingState() {
  return {
    enabled: config.enabled,
    source: config.source,
    song: song ? song.title : '',
    lyrics: song ? song.lyrics.length : 0,
    currentTime: lastTime,
    playing: lastPlaying,
    failures: failures,
    inFlight: inFlight
  };
}

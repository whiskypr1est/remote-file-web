'use strict';
/* ==========================================================================
   与服务端的连接
   --------------------------------------------------------------------------
   两条路，一起用：

     1. **WebSocket 订阅**（主路）：连上就收到一条当前状态，之后每次切歌/进度
        变化都会推过来。这是「实时歌词」的来源。
     2. **HTTP 轮询兜底**（副路）：只在 WebSocket 断开期间启用，每 3 秒问一次
        GET /now-playing。为什么要有它：有些网络设备/安全软件会拦掉 WebSocket
        的 Upgrade 请求，此时只靠 WS 的客户端会永远停在「连不上」；
        而轮询虽然不是实时的（歌词会慢一拍），但至少能用。

   ★ 重连用指数退避（1/2/4/8/15 秒封顶，带抖动）。
     固定 1 秒重试在服务端没开的时候会一直敲；退避到 15 秒又会让「服务端刚
     起来」等太久 —— 加抖动是为了避免一个教室里几十台机器同时重连。
   ========================================================================== */

const WebSocket = require('ws');

/** 重连间隔（毫秒），最后一项封顶后一直用它。 */
const RECONNECT_DELAYS = [1000, 2000, 4000, 8000, 15000];

/** 断线期间轮询 /now-playing 的间隔。 */
const POLL_INTERVAL_MS = 3000;

/** HTTP 请求超时。局域网内超过这个时间基本就是连不上了。 */
const REQUEST_TIMEOUT_MS = 8000;

/** 协议层心跳间隔；连续两个周期没有 pong 就认为这条连接已经死了。 */
const PING_INTERVAL_MS = 15000;

/*
 ★ 两条路（WS 连不上时的探测 explain()，以及轮询兜底 startPolling()）都可能发现
   「服务端关了功能 / 要 token」。这两句话说给用户听，必须**完全一致** ——
   否则谁先到就先显示，而 Wi-Fi 抖一下就会换个说法，用户以为换了个毛病。
   所以文案只在这一处定义。
*/
const MESSAGE_DISABLED =
  '服务端已关闭桌面歌词功能（config.json 里 lyrics.enabled = false）';
const MESSAGE_BAD_TOKEN =
  '服务端要求 token，请在「连接设置」里填写（见服务端 config.json 的 lyrics.token）';

/**
 * 把用户填的地址规整成 base url。
 *
 * 老师多半会直接填「192.168.1.10:8000」（不带协议），或者从浏览器地址栏
 * 复制一个带路径的完整地址 —— 两种都得能用，否则第一步就卡住了。
 */
function normalizeBaseUrl(raw) {
  let text = String(raw || '').trim();
  if (!text) {
    return '';
  }
  // ★ 非 ASCII（中文、全角字符等）一律先挡掉。原因：new URL('http://这不是地址')
  //   不会报错 —— 它把主机名做 punycode 转换后返回一个「合法」的 xn--… 地址，
  //   于是明显填错的地址会被当成有效地址，用户看到的是「连不上服务端」，
  //   而不是「地址填得不对」。地址本来就只可能是 ASCII。
  if (/[^\x20-\x7E]/.test(text)) {
    return '';
  }
  if (!/^https?:\/\//i.test(text)) {
    text = 'http://' + text;
  }
  let url = null;
  try {
    url = new URL(text);
  } catch (err) {
    return '';
  }
  // 局域网里合法的主机名只有：IP、机器名、域名（字母/数字/点/横线/下划线）。
  // 再加一道，挡住「全是符号」这类 new URL 也可能接受的输入。
  const host = url.hostname;
  if (!/^[A-Za-z0-9._-]+$/.test(host) && !/^\[[0-9A-Fa-f:.]+\]$/.test(host)) {
    return '';
  }
  // 只保留协议 + 主机 + 端口：用户复制来的地址可能带 /desktop 之类的路径
  return url.protocol + '//' + url.host;
}

function query(params) {
  const parts = [];
  for (const key of Object.keys(params)) {
    const value = params[key];
    if (value) {
      parts.push(encodeURIComponent(key) + '=' + encodeURIComponent(value));
    }
  }
  return parts.length ? '?' + parts.join('&') : '';
}

function wsUrl(base, options) {
  const url = new URL(base);
  url.protocol = url.protocol === 'https:' ? 'wss:' : 'ws:';
  url.pathname = '/ws/lyrics';
  url.search = query({ user: options.user, token: options.token });
  return url.toString();
}

function stateUrl(base, options) {
  return base + '/now-playing' + query({ user: options.user, token: options.token });
}

function sourcesUrl(base, options) {
  return base + '/now-playing' + query({ token: options.token, sources: 1 });
}

class LyricsClient {
  /**
   * @param {object} options
   * @param {function} options.getConfig 返回 {serverUrl, user, token}
   * @param {function} options.onState   收到一份状态（与 WS 的 state 消息同形）
   * @param {function} options.onStatus  连接状态变化 {state, message}
   */
  constructor(options) {
    this.getConfig = options.getConfig;
    this.onState = options.onState;
    this.onStatus = options.onStatus;

    this.socket = null;
    this.attempt = 0;
    this.reconnectTimer = null;
    this.pollTimer = null;
    this.pingTimer = null;
    this.missedPongs = 0;
    this.stopped = true;
    this.lastStatus = '';
  }

  /* -- 生命周期 ----------------------------------------------------------- */

  start() {
    this.stopped = false;
    this.connect();
  }

  stop() {
    this.stopped = true;
    this.clearTimers();
    this.closeSocket();
  }

  /** 配置变了（地址/用户/token）：拆掉重来。 */
  reload() {
    if (this.stopped) {
      return;
    }
    this.attempt = 0;
    this.clearTimers();
    this.closeSocket();
    this.connect();
  }

  /* -- 内部 --------------------------------------------------------------- */

  clearTimers() {
    for (const key of ['reconnectTimer', 'pollTimer', 'pingTimer']) {
      if (this[key]) {
        clearTimeout(this[key]);
        clearInterval(this[key]);
        this[key] = null;
      }
    }
  }

  closeSocket() {
    const socket = this.socket;
    this.socket = null;
    if (socket) {
      try {
        socket.removeAllListeners();
        // ★ 这一行不是可有可无的：如果连接**还没建立**（正在 CONNECTING）就被
        //   terminate，ws 会在下一个 tick 异步发出 'error'。上面刚把监听器全摘了，
        //   于是这个 error 没有任何人接 —— Node 对无主的 'error' 事件是**直接
        //   抛异常终止进程**。表现在用户那里就是：在设置里改服务器地址（会走
        //   reload -> closeSocket）时，整个程序无声无息地消失了。
        //   留一个空监听器即为此。
        socket.on('error', () => {});
        socket.terminate();
      } catch (err) { /* 已经断了 */ }
    }
  }

  status(state, message) {
    // 只在真的变了的时候回调：否则托盘提示会被反复刷新
    const key = state + '|' + (message || '');
    if (key === this.lastStatus) {
      return;
    }
    this.lastStatus = key;
    this.onStatus({ state: state, message: message || '' });
  }

  connect() {
    if (this.stopped) {
      return;
    }
    const cfg = this.getConfig() || {};
    const base = normalizeBaseUrl(cfg.serverUrl);
    if (!base) {
      // 还没配置服务器：不算错误，等用户填完地址会自动重连
      this.status('unconfigured', '还没有设置服务器地址');
      return;
    }

    this.status(this.attempt === 0 ? 'connecting' : 'reconnecting',
      this.attempt === 0 ? '正在连接…' : '正在重连（第 ' + this.attempt + ' 次）');

    let socket = null;
    try {
      socket = new WebSocket(wsUrl(base, cfg), { handshakeTimeout: REQUEST_TIMEOUT_MS });
    } catch (err) {
      this.scheduleReconnect();
      return;
    }
    this.socket = socket;

    socket.on('open', () => {
      this.attempt = 0;
      this.missedPongs = 0;
      this.stopPolling();
      this.status('online', '已连接');
      this.startPing();
    });

    socket.on('message', (data) => {
      let message = null;
      try {
        message = JSON.parse(String(data));
      } catch (err) {
        return;                    // 不是 JSON 就忽略，绝不让它把连接搞断
      }
      if (message && message.type === 'state') {
        this.onState(message);
      }
    });

    socket.on('pong', () => { this.missedPongs = 0; });

    socket.on('error', () => {
      // 具体原因靠 close/下面的 explain() 给：这里只记状态，不弹窗打扰用户。
      // （WebSocket 的 error 事件几乎总是跟着一次 close。）
    });

    socket.on('close', () => {
      if (this.socket !== socket) {
        return;                    // 已经换成新连接了，这次关闭是旧的
      }
      this.socket = null;
      this.stopPing();
      if (this.stopped) {
        return;
      }
      this.startPolling();
      this.scheduleReconnect();
    });
  }

  startPing() {
    this.stopPing();
    this.pingTimer = setInterval(() => {
      const socket = this.socket;
      if (!socket || socket.readyState !== WebSocket.OPEN) {
        return;
      }
      if (this.missedPongs >= 2) {
        // 两个周期没有回应：连接看起来是通的，其实早断了（拔网线、
        // 交换机重启都会这样）。主动拆掉走重连，否则歌词会一直停着。
        this.status('reconnecting', '连接没有响应，正在重连…');
        try {
          socket.terminate();
        } catch (err) { /* 忽略 */ }
        return;
      }
      this.missedPongs += 1;
      try {
        socket.ping();
      } catch (err) { /* 忽略 */ }
    }, PING_INTERVAL_MS);
  }

  stopPing() {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
  }

  scheduleReconnect() {
    if (this.stopped || this.reconnectTimer) {
      return;
    }
    const base = RECONNECT_DELAYS[Math.min(this.attempt, RECONNECT_DELAYS.length - 1)];
    this.attempt += 1;
    // 抖动 ±25%：避免整个教室的机器在同一毫秒一起重连
    const delay = Math.round(base * (0.75 + Math.random() * 0.5));
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
    this.explain();
  }

  /**
   * 连不上时问一次 HTTP：是为了把「服务端把功能关了」和「服务端根本连不上」
   * 分开告诉用户 —— 前者去改 config.json 就行，后者要去查网络/服务有没有起。
   * 只在第一次失败时问，之后不再重复（避免一边退避一边还敲 HTTP）。
   */
  explain() {
    if (this.attempt !== 1) {
      return;
    }
    const cfg = this.getConfig() || {};
    const base = normalizeBaseUrl(cfg.serverUrl);
    if (!base) {
      return;
    }
    this.probe(base, cfg).then((result) => {
      if (this.stopped || this.socket) {
        return;
      }
      if (result.disabled) {
        this.status('disabled', MESSAGE_DISABLED);
      } else if (result.badToken) {
        this.status('badToken', MESSAGE_BAD_TOKEN);
      } else if (result.ok) {
        this.status('polling', 'WebSocket 连不上，已改用轮询（歌词会慢一拍）');
      } else {
        this.status('offline', result.message || '未知原因');
      }
    });
  }

  async probe(base, cfg) {
    try {
      const response = await fetchWithTimeout(sourcesUrl(base, cfg), REQUEST_TIMEOUT_MS);
      if (response.status === 403) {
        let body = null;
        try {
          body = await response.json();
        } catch (err) { /* 无响应体 */ }
        const code = (body && body.code) || '';
        if (code === 'lyrics_disabled') {
          return { ok: false, disabled: true };
        }
        if (code === 'bad_token') {
          return { ok: false, badToken: true };
        }
        return { ok: false, message: '服务端拒绝了这个客户端（403）' };
      }
      if (!response.ok) {
        return { ok: false, message: 'HTTP ' + response.status };
      }
      await response.json();
      return { ok: true };
    } catch (err) {
      return { ok: false, message: (err && err.message) || '网络不可达' };
    }
  }

  /* -- 轮询兜底 ----------------------------------------------------------- */

  startPolling() {
    if (this.pollTimer || this.stopped) {
      return;
    }
    const tick = async () => {
      const cfg = this.getConfig() || {};
      const base = normalizeBaseUrl(cfg.serverUrl);
      if (!base || this.socket) {
        return;
      }
      try {
        const response = await fetchWithTimeout(stateUrl(base, cfg), REQUEST_TIMEOUT_MS);
        if (response.status === 403) {
          const body = await response.json().catch(() => null);
          const code = (body && body.code) || '';
          if (code === 'lyrics_disabled') {
            this.status('disabled', MESSAGE_DISABLED);
          } else if (code === 'bad_token') {
            this.status('badToken', MESSAGE_BAD_TOKEN);
          }
          return;
        }
        if (!response.ok) {
          return;
        }
        const data = await response.json();
        if (!this.socket && data && data.display) {
          this.status('polling', 'WebSocket 连不上，已改用轮询（歌词会慢一拍）');
          this.onState(data.display);
        }
      } catch (err) {
        // 轮询失败就等着，重连逻辑会继续尝试 WebSocket
      }
    };
    tick();
    this.pollTimer = setInterval(tick, POLL_INTERVAL_MS);
  }

  stopPolling() {
    if (this.pollTimer) {
      clearInterval(this.pollTimer);
      this.pollTimer = null;
    }
  }
}

/** 带超时的 fetch（Electron 的主进程有全局 fetch，见 Node 18+）。 */
function fetchWithTimeout(url, timeout) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeout);
  return fetch(url, { signal: controller.signal })
    .finally(() => clearTimeout(timer));
}

module.exports = {
  LyricsClient,
  normalizeBaseUrl,
  wsUrl,
  stateUrl,
  sourcesUrl
};

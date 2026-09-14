'use strict';
/* ==========================================================================
   桌面歌词 · 渲染进程
   --------------------------------------------------------------------------
   只做一件事：把主进程推过来的状态画成**一行大字**。

   三个关键实现细节
   ----------------
   1. **本地外推时间**：服务端每 0.5 秒才给一次进度，直接用它会看到歌词
      半秒一跳。这里用「收到的进度 + 本地流逝的时间」算出当前时刻，
      于是滚动是连续的。用本地收到时刻而不是服务端的 serverTime，
      是因为两台机器的时钟可能差好几分钟（局域网里很常见），
      而「收到的那一刻」是不会有偏差的。
   2. **只在换行时碰 DOM**：进度每 0.5 秒来一次，如果每次都重写文本，
      淡入动画会不停重放，看起来一直在闪。所以比对文本、相同就什么都不做。
   3. **歌词一律用 textContent 写入**：它是外部数据（局域网内谁都能推），
      绝不能走 innerHTML —— 配合 index.html 里的 CSP 双重兜底。

   暂停时：停止外推（那一行就停在原地），整块降为半透明。
   ========================================================================== */

const els = {
  stage: document.getElementById('stage'),
  lyric: document.getElementById('lyric'),
  sub: document.getElementById('sub'),
  progress: document.getElementById('progress'),
  progressFill: document.getElementById('progressFill')
};

/* 主进程通过 preload 暴露的接口。给出空实现是为了让这个页面在**浏览器里**
   也能直接打开（用它来核对排版与描边效果，见 README 的说明）。 */
const api = window.lyrics || {
  ready: function () {},
  onState: function () {},
  onStatus: function () {},
  onConfig: function () {}
};

let state = null;
let receivedAt = 0;
let config = {
  fontSize: 40,
  showProgress: false,
  clickThrough: true,
  locked: false,
  editing: false
};
let status = { state: 'connecting', message: '' };

let drawnText = null;        // 上一次真正写进 DOM 的那行字
let frames = 0;              // 用帧数节流进度条，不必每帧都改样式

/** 当前应显示的时刻（秒）：播放中把本地流逝的时间算进去，暂停就停在原地。 */
function currentTime() {
  if (!state || state.idle) {
    return 0;
  }
  let seconds = Number(state.currentTime);
  if (!isFinite(seconds) || seconds < 0) {
    seconds = 0;
  }
  if (state.playing) {
    seconds += (Date.now() - receivedAt) / 1000;
  }
  const duration = Number(state.duration);
  if (isFinite(duration) && duration > 0) {
    seconds = Math.min(seconds, duration);   // 别让它在末尾继续往前跑
  }
  return seconds;
}

/** 二分找出「最后一个 time <= t」的行号；-1 表示还没到第一句（前奏）。 */
function findLine(lyrics, seconds) {
  let low = 0;
  let high = lyrics.length - 1;
  let found = -1;
  while (low <= high) {
    const middle = (low + high) >> 1;
    if (lyrics[middle].time <= seconds) {
      found = middle;
      low = middle + 1;
    } else {
      high = middle - 1;
    }
  }
  return found;
}

function titleLine() {
  if (!state) {
    return '';
  }
  const title = String(state.title || '').trim();
  const artist = String(state.artist || '').trim();
  if (!title) {
    return artist;
  }
  return artist ? title + ' - ' + artist : title;
}

function statusLabel() {
  const map = {
    online: '已连接',
    polling: '轮询模式（WebSocket 连不上，歌词会慢一拍）',
    connecting: '正在连接…',
    reconnecting: '正在重连…',
    offline: '连不上服务端：' + (status.message || '请检查网络与服务器地址'),
    disabled: status.message || '服务端已关闭桌面歌词功能',
    badToken: status.message || '服务端要求 token',
    unconfigured: status.message || '还没有设置服务器地址'
  };
  return map[status.state] || status.message || '';
}

/** 当前该显示的那行字。 */
function activeText() {
  if (!state || state.idle) {
    // 窗口在正常模式下会被主进程隐藏，这里的内容只有「调整模式」下才看得到
    return statusLabel() || '等待播放…';
  }
  const lyrics = state.lyrics || [];
  if (!lyrics.length) {
    return titleLine();
  }
  const index = findLine(lyrics, currentTime());
  if (index < 0) {
    // 前奏：还没唱，先显示歌名，比空着好
    return titleLine();
  }
  return lyrics[index].text;
}

/** 副标题：正常播放时**必须为空**（需求是单行大字）。 */
function subText() {
  if (config.editing) {
    const parts = [];
    const title = titleLine();
    if (title) {
      parts.push(title);
    }
    if (state && state.playing === false && !state.idle) {
      parts.push('已暂停');
    }
    const label = statusLabel();
    if (label) {
      parts.push(label);
    }
    parts.push(config.locked ? '位置已锁定' : '拖动可移动窗口');
    return parts.join('   ·   ');
  }

  // 正常模式：只在「没在放歌 + 出了状况」时提示。
  // 放歌时屏幕上不该出现任何多余的字。
  const idle = !state || state.idle === true;
  if (idle && ['offline', 'disabled', 'badToken', 'unconfigured'].indexOf(status.state) >= 0) {
    return statusLabel();
  }
  return '';
}

function setText(element, text) {
  if (element.textContent === text) {
    return;
  }
  element.textContent = text;
}

function setLine(text) {
  if (text === drawnText) {
    return;
  }
  drawnText = text;
  els.lyric.textContent = text;
  // 重新触发淡入：先把 class 拿掉并强制重排，否则同名动画不会重放
  els.lyric.classList.remove('in');
  void els.lyric.offsetWidth;
  els.lyric.classList.add('in');
}

function paintProgress() {
  if (!config.showProgress || !state || state.idle) {
    return;
  }
  const duration = Number(state.duration);
  const total = isFinite(duration) && duration > 0 ? duration : 0;
  const ratio = total > 0 ? Math.min(1, Math.max(0, currentTime() / total)) : 0;
  els.progressFill.style.width = (ratio * 100).toFixed(2) + '%';
}

function draw(force) {
  const idle = !state || state.idle === true;
  const paused = !idle && state.playing === false;

  els.stage.classList.toggle('idle', idle);
  els.stage.classList.toggle('paused', paused);

  setLine(activeText());
  setText(els.sub, subText());

  // 进度条每 4 帧（约 60ms）更新一次就足够顺滑，不必每帧都动样式
  frames += 1;
  if (force || frames % 4 === 0) {
    paintProgress();
  }
}

function applyConfig() {
  document.documentElement.style.setProperty('--lyric-size', config.fontSize + 'px');
  // 只有「调整模式 + 未锁定」时整块区域才是拖拽把手
  document.body.classList.toggle('draggable', !!(config.editing && !config.locked));
  els.progress.hidden = !config.showProgress;
  draw(true);
}

/* ---------------------------------------------------------------------------
   与主进程的对接
   --------------------------------------------------------------------------- */

let rafId = null;

function schedule() {
  if (rafId === null) {
    rafId = requestAnimationFrame(tick);
  }
}

function tick() {
  rafId = null;
  draw(false);
  // 暂停时不再排下一帧 —— 需求里说的「暂停时停止滚动」就是这里
  if (state && !state.idle && state.playing) {
    schedule();
  }
}

api.onState(function (envelope) {
  state = (envelope && envelope.state) || null;
  receivedAt = (envelope && envelope.receivedAt) || Date.now();
  // ★ 这里**不能**把 drawnText 清成 null 去「强制重画」：进度每 0.5 秒来一次，
  //   清掉就等于每 0.5 秒重写一次文本，而重写会重放淡入动画 ——
  //   表现是歌词一直在闪。文本没变就不用动 DOM，setLine 自己会判断。
  draw(true);
  if (state && !state.idle && state.playing) {
    schedule();
  }
});

api.onStatus(function (payload) {
  status = payload || { state: 'offline', message: '' };
  draw(true);
});

api.onConfig(function (payload) {
  config = Object.assign({}, config, payload || {});
  applyConfig();
});

applyConfig();
api.ready();

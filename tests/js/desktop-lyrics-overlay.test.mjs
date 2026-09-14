/* ==========================================================================
   桌面歌词悬浮窗 · 渲染逻辑测试（node 直接跑，不需要 Electron）
   --------------------------------------------------------------------------
   跑法：node tests/js/desktop-lyrics-overlay.test.mjs

   为什么要测：渲染层里最容易错的不是排版，而是**时间与歌词行的对应关系**：
     * 服务端每 0.5 秒才给一次进度，直接用它歌词会半秒一跳 ——
       所以要「进度 + 本地流逝时间」外推；
     * 暂停时必须停止外推，否则暂停后歌词还会自己往前滚；
     * 前奏（还没到第一句）该显示什么；
     * 拖动进度条之后要立刻跳到正确的那一行。
   这些在真机上只表现为「歌词对不上」或者「暂停了还在动」，很难当场定位。

   做法：用一个极小的假 DOM + 假的 requestAnimationFrame 跑**真实的**
   overlay.js（不是重写一遍逻辑），帧由测试自己驱动，于是时序完全可控。
   ========================================================================== */

import assert from 'node:assert/strict';

/* ---------------------------------------------------------------------------
   假 DOM / 假浏览器环境
   --------------------------------------------------------------------------- */

/** 与 index.html 一致的静态类名。 */
const STATIC_CLASSES = {
  stage: ['stage'],
  lyric: ['lyric'],
  sub: ['sub'],
  progress: ['progress'],
  progressFill: ['progress-fill']
};

const elements = {};
const cssVars = {};

function makeElement(id, initialClasses) {
  // 静态类名要照抄 index.html 上的写法（stage / lyric / sub / …）：
  // 渲染层只会 toggle 状态类（paused / idle / in），静态类不会出现在调用里，
  // 假的 DOM 如果不预置，className 就会少一段，断言会误判。
  const classes = new Set(initialClasses || []);
  let text = '';
  const element = {
    id: id,
    writes: 0,                 // textContent 被写入的次数（用于验证「不重复写」）
    hidden: false,
    style: { width: '', setProperty: function () {} },
    offsetWidth: 100,          // setLine 里的强制重排会读它
    classList: {
      add: function (name) { classes.add(name); },
      remove: function (name) { classes.delete(name); },
      contains: function (name) { return classes.has(name); },
      toggle: function (name, force) {
        const want = force === undefined ? !classes.has(name) : !!force;
        if (want) {
          classes.add(name);
        } else {
          classes.delete(name);
        }
        return want;
      },
      toString: function () { return Array.from(classes).join(' '); }
    }
  };
  Object.defineProperty(element, 'textContent', {
    get: function () { return text; },
    set: function (value) { text = value; element.writes += 1; }
  });
  Object.defineProperty(element, 'className', {
    get: function () { return Array.from(classes).join(' '); }
  });
  return element;
}

globalThis.document = {
  getElementById: function (id) {
    if (!elements[id]) {
      elements[id] = makeElement(id, STATIC_CLASSES[id] || []);
    }
    return elements[id];
  },
  documentElement: {
    style: {
      setProperty: function (name, value) { cssVars[name] = value; }
    }
  },
  body: makeElement('body', [])
};

/** 帧由测试驱动：不真的排程，于是「有没有排下一帧」本身就能断言。 */
let pendingFrame = null;
let rafRequests = 0;
globalThis.requestAnimationFrame = function (callback) {
  rafRequests += 1;
  pendingFrame = callback;
  return rafRequests;
};

/** 一次可控的时钟。 */
let clock = 1_000_000;
const realNow = Date.now;
Date.now = function () { return clock; };

/** 假的 preload 接口：把回调收下来，测试通过它推状态。 */
const hooks = { state: [], status: [], config: [], ready: 0 };
globalThis.window = {
  lyrics: {
    ready: function () { hooks.ready += 1; },
    onState: function (cb) { hooks.state.push(cb); },
    onStatus: function (cb) { hooks.status.push(cb); },
    onConfig: function (cb) { hooks.config.push(cb); }
  }
};

// ★ 只导入一次：模块内部是有状态的（当前状态、上一次画过的文本），
//   而 CJS 模块的缓存无法用 query 绕过。所以测试之间靠「推一份完整的新状态」
//   复位，而不是靠重新加载文件。
await import(new URL('../../desktop-lyrics/src/renderer/overlay.js', import.meta.url));

const els = {
  stage: elements.stage,
  lyric: elements.lyric,
  sub: elements.sub,
  progress: elements.progress,
  progressFill: elements.progressFill
};

const LYRICS = [
  { time: 0, text: '一群嗜血的蚂蚁 被腐肉所吸引' },
  { time: 12.4, text: '我面无表情 看孤独的风景' },
  { time: 26.8, text: '失去你 爱恨开始分明' },
  { time: 41.2, text: '为你弹奏萧邦的夜曲' },
  { time: 55.6, text: '纪念我死去的爱情' }
];

/** 推一份状态（并顺带把 config/status 推下去）。 */
function push(state, options) {
  const opts = options || {};
  // ★ 刻意**不**清 pendingFrame：渲染层内部用 rafId 记着自己排过一帧，
  //   在它背后把回调丢掉，rafId 就永远不会归零，之后再也排不出新帧
  //   （表现为「外推不生效」的假故障）。帧要么由 frames() 跑掉，要么留着。
  hooks.config.forEach(function (cb) {
    cb(Object.assign({
      fontSize: 40, showProgress: false, clickThrough: true, locked: false, editing: false
    }, opts.config || {}));
  });
  hooks.status.forEach(function (cb) { cb(opts.status || { state: 'online', message: '' }); });
  hooks.state.forEach(function (cb) {
    cb({ state: state, receivedAt: opts.receivedAt === undefined ? clock : opts.receivedAt });
  });
}

/** 推进若干帧（默认一帧）。 */
function frames(count) {
  const total = count || 1;
  for (let i = 0; i < total; i += 1) {
    const callback = pendingFrame;
    pendingFrame = null;
    if (callback) {
      callback();
    }
  }
}

const song = (overrides) => Object.assign({
  idle: false, playing: true, source: 'wangqi', title: '夜曲', artist: '周杰伦',
  duration: 227, currentTime: 44.5, lyrics: LYRICS
}, overrides || {});

/* ---------------------------------------------------------------------------
   用例
   --------------------------------------------------------------------------- */

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

test('播放中显示当前该唱的那一句', async () => {
  push(song());
  assert.equal(els.lyric.textContent, '为你弹奏萧邦的夜曲');
  assert.equal(els.stage.className, 'stage');
});

test('★ 用本地时间外推：两条进度之间歌词也会自己往前走', async () => {
  // 服务端给的进度是 41.3 秒（唱到第 4 句），但 15 秒之后本地应该已经到
  // 第 5 句（55.6 秒）—— 这正是「不能只按上报的进度画」的意义。
  push(song({ currentTime: 41.3 }));
  assert.equal(els.lyric.textContent, '为你弹奏萧邦的夜曲');

  clock += 15000;
  frames(1);
  assert.equal(els.lyric.textContent, '纪念我死去的爱情', '15 秒后应当推进到下一句');
});

test('★ 暂停时停止外推：时间过去多久都停在那一句', async () => {
  const before = rafRequests;
  push(song({ currentTime: 44.5, playing: false }));
  assert.equal(els.lyric.textContent, '为你弹奏萧邦的夜曲');
  assert.equal(els.stage.className, 'stage paused', '暂停时整块要半透明');

  clock += 60000;
  frames(1);
  assert.equal(els.lyric.textContent, '为你弹奏萧邦的夜曲', '暂停后不该继续滚');

  // 暂停时不该再排帧（不排 = 不烧 CPU、也不再重画）
  assert.equal(rafRequests, before, '暂停时不应请求新的动画帧');
});

test('播放中会持续排帧（滚动是连续的）', async () => {
  const before = rafRequests;
  push(song({ currentTime: 1 }));
  assert.ok(rafRequests > before, '播放中应当排帧');
  frames(1);
  assert.ok(pendingFrame, '跑完一帧还要继续排');
});

test('前奏（还没到第一句）显示歌名', async () => {
  push(song({ currentTime: 0, lyrics: [{ time: 9.5, text: '第一句' }] }));
  assert.equal(els.lyric.textContent, '夜曲 - 周杰伦');
});

test('没有歌词时显示歌名（纯文本歌词的情形）', async () => {
  push(song({ lyrics: [] }));
  assert.equal(els.lyric.textContent, '夜曲 - 周杰伦');
});

test('空闲且连不上时，把原因显示出来', async () => {
  const before = rafRequests;
  push({ idle: true }, { status: { state: 'offline', message: '网络不可达' } });
  assert.equal(els.lyric.textContent, '连不上服务端：网络不可达');
  assert.equal(els.stage.className, 'stage idle');
  assert.equal(rafRequests, before, '空闲时不该排帧');
});

test('正常播放时副标题必须是空的（需求是单行大字）', async () => {
  push(song());
  assert.equal(els.sub.textContent, '', '播放时不该有多余的一行小字');
});

test('调整模式：显示歌名与拖动提示，并允许拖动', async () => {
  push(song(), { config: { clickThrough: false, editing: true } });
  assert.match(els.sub.textContent, /夜曲/);
  assert.match(els.sub.textContent, /拖动可移动窗口/);
  assert.match(document.body.className, /draggable/);
});

test('锁定位置后不可拖动', async () => {
  push(song(), { config: { clickThrough: false, editing: true, locked: true } });
  assert.doesNotMatch(document.body.className, /draggable/);
  assert.match(els.sub.textContent, /位置已锁定/);
});

test('字号会写进 CSS 变量（由样式表决定实际大小）', async () => {
  push(song(), { config: { fontSize: 52 } });
  assert.equal(cssVars['--lyric-size'], '52px');
});

test('进度条按当前时间/总时长计算', async () => {
  push(song({ currentTime: 25, duration: 100, playing: false }),
    { config: { showProgress: true } });
  assert.equal(els.progress.hidden, false);
  assert.equal(els.progressFill.style.width, '25.00%');
});

test('进度条：进度超出总长时夹到 100%', async () => {
  push(song({ currentTime: 999, duration: 100, playing: false }),
    { config: { showProgress: true } });
  assert.equal(els.progressFill.style.width, '100.00%');
});

test('没开进度条时保持隐藏', async () => {
  push(song(), { config: { showProgress: false } });
  assert.equal(els.progress.hidden, true);
});

test('★ 同一句歌词不重复写 DOM（否则淡入动画会一直重放、看起来在闪）', async () => {
  push(song());
  const after1 = els.lyric.writes;
  // 模拟每 0.5 秒来一次的进度上报：歌词没换行
  push(song({ currentTime: 45.0 }));
  push(song({ currentTime: 45.5 }));
  push(song({ currentTime: 46.0 }));
  assert.equal(els.lyric.writes, after1, '同一句不该反复写 textContent');

  // 换到下一句时必须写
  push(song({ currentTime: 56.0 }));
  assert.equal(els.lyric.writes, after1 + 1, '换行时必须更新文本');
});

test('歌词是外部数据：一律按纯文本写入（不会被当成 HTML）', async () => {
  push(song({ lyrics: [{ time: 0, text: '<img src=x onerror=alert(1)>' }] }));
  assert.equal(els.lyric.textContent, '<img src=x onerror=alert(1)>');
});

test('ready 只发一次，且在启动时就发（主进程据此补发状态）', async () => {
  assert.equal(hooks.ready, 1);
});

/* ---------------------------------------------------------------------------
   运行
   --------------------------------------------------------------------------- */

let failed = 0;
for (const item of tests) {
  try {
    await item.fn();
    console.log('ok   ' + item.name);
  } catch (err) {
    failed += 1;
    console.log('FAIL ' + item.name);
    console.log('     ' + (err && err.message ? err.message : String(err)));
  }
}

Date.now = realNow;
console.log('');
console.log('Ran ' + tests.length + ' tests, ' + failed + ' failed');
process.exitCode = failed ? 1 : 0;

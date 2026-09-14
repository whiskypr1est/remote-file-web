/* ==========================================================================
   桌面歌词上报模块的测试（用 node 直接跑，无第三方依赖）
   --------------------------------------------------------------------------
   为什么要专门测这个文件
   ----------------------
   它的**设计目标就是「出错也不吭声」**（绝不影响听歌），代价是所有 bug 都
   没有症状：上报丢了、节流算错、歌词被覆盖 —— 用户只会看到「悬浮窗不动」，
   而在浏览器里既没有报错也没有提示。所以这里把它的行为一条条钉住。

   为什么能直接在 node 里跑
   ------------------------
   模块只用 fetch / Blob / setInterval / Date.now，没有 DOM 依赖
   （window 与 navigator.sendBeacon 都有 typeof 保护）。这里把 fetch、定时器、
   时钟全部换成假的，于是异步时序可以被精确控制。

   跑法：node tests/js/nowplaying.test.mjs
   ========================================================================== */

import assert from 'node:assert/strict';

/* ---------------------------------------------------------------------------
   假环境
   --------------------------------------------------------------------------- */

/** 真的 setTimeout：下面的假定时器会把它换掉，所以先存下来。 */
const realSetTimeout = globalThis.setTimeout.bind(globalThis);

let clock = 1_000_000;

/** 计划好的响应：每次 fetch 取一个。 */
function createFetch() {
  const calls = [];
  const plan = [];
  const impl = (url, options) => {
    const payload = JSON.parse(options.body || '{}');
    calls.push({ url, payload });
    const step = plan.shift() || { ok: true, data: { ok: true } };
    if (step.reject) {
      return Promise.reject(new Error('network down'));
    }
    const response = {
      ok: step.ok !== false,
      json: async () => (step.data || {})
    };
    if (step.manual) {
      // 手动放行：用于「在途期间又来了新状态」这种时序
      return new Promise((resolve) => { step.release = () => resolve(response); });
    }
    return Promise.resolve(response);
  };
  return {
    calls,
    install() { globalThis.fetch = impl; },
    plan(step) { plan.push(step); return step; }
  };
}

/** 记录 setInterval / setTimeout 的假定时器（不真的排程，避免拖住进程）。 */
function createTimers() {
  const intervals = [];
  const timeouts = [];
  globalThis.setInterval = (fn, ms) => {
    intervals.push({ fn, ms, cleared: false });
    return intervals.length;
  };
  globalThis.clearInterval = (id) => {
    if (intervals[id - 1]) {
      intervals[id - 1].cleared = true;
    }
  };
  globalThis.setTimeout = (fn, ms) => {
    timeouts.push({ fn, ms, cleared: false });
    return timeouts.length;
  };
  globalThis.clearTimeout = (id) => {
    if (timeouts[id - 1]) {
      timeouts[id - 1].cleared = true;
    }
  };
  return { intervals, timeouts };
}

/**
 * 把微任务与已就绪的 promise 全部走完。
 *
 * ★ 必须用**真实的** setTimeout 制造一次宏任务边界：模块的一条上报要经过
 *   fetch -> resp.json() -> then -> then 好几个微任务，只 await 一次走不完，
 *   inFlight 会一直为真，后面的断言全部看到「请求没发出去」。
 *   顺带一个更隐蔽的后果：上一个用例没走完的 promise 链会在下一个用例里
 *   继续跑，用**下一个用例的** fetch 发出一条请求 —— 表现就是「凭空多了一条」。
 */
const flush = () => new Promise((resolve) => realSetTimeout(resolve, 0));

let moduleCounter = 0;

/** 每次都 import 一份**全新的**模块：模块内部是单例状态，测试之间必须隔离。 */
async function freshModule() {
  moduleCounter += 1;
  const url = new URL('../../static/js/nowplaying.js?t=' + moduleCounter, import.meta.url);
  return import(url.href);
}

/** 起一个干净的测试现场：假网络 + 假定时器 + 可控时钟。 */
async function scene() {
  const net = createFetch();
  net.install();
  const timers = createTimers();
  Date.now = () => clock;
  const np = await freshModule();
  return { net, timers, np };
}

const SONG = {
  title: '夜曲',
  artist: '周杰伦',
  duration: 227,
  lyrics: [
    { time: 43.2, text: '为你弹奏萧邦的夜曲' },
    { time: 0, text: '一群嗜血的蚂蚁' },
    { time: null, text: '没有时间轴的纯文本行' },
    { time: 78.5, text: '纪念我死去的爱情' }
  ]
};

/* ---------------------------------------------------------------------------
   用例
   --------------------------------------------------------------------------- */

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

test('没配置时一条请求都不发（features.lyrics 关闭 = 零流量）', async () => {
  const { net, np } = await scene();

  np.reportSong(SONG);
  np.reportProgress(1, true);
  np.reportStop();
  await flush();

  assert.equal(net.calls.length, 0, '未启用时不该产生任何上报');
  assert.equal(np.nowPlayingState().enabled, false);
});

test('上报歌曲：带上规整后的歌词（丢无时间轴的行、按时间排序）', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong(SONG);
  await flush();

  assert.equal(net.calls.length, 1);
  const body = net.calls[0].payload;
  assert.equal(body.type, 'song');
  assert.equal(body.source, 'wangqi', 'source 是分组订阅的依据，必须带上');
  assert.equal(body.title, '夜曲');
  assert.equal(body.duration, 227);
  assert.deepEqual(body.lyrics.map((line) => line.text),
    ['一群嗜血的蚂蚁', '为你弹奏萧邦的夜曲', '纪念我死去的爱情']);
  assert.deepEqual(body.lyrics.map((line) => line.time), [0, 43.2, 78.5]);
});

test('纯文本歌词（time 为 null）不会被当成第 0 秒', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong({ title: '无轴歌词的歌', lyrics: [{ time: null, text: '甲' }, { time: null, text: '乙' }] });
  await flush();

  assert.equal(net.calls[0].payload.title, '无轴歌词的歌', '歌名照报，悬浮窗退化成显示歌名');
  assert.deepEqual(net.calls[0].payload.lyrics, [],
    '★ JS 里 Number(null) === 0，不先排除 null 的话整首歌的歌词会全挤在第 0 秒');
});

test('没有歌名时不发（服务端也会拒，本地就拦住）', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong({ title: '   ', artist: 'x' });
  await flush();

  assert.equal(net.calls.length, 0);
});

test('进度按 500ms 节流（timeupdate 一秒来四次，不能每次都发）', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong(SONG);
  await flush();
  assert.equal(net.calls.length, 1);

  // 现实里 timeupdate 大约 250ms 一次，而切歌那一拍刚报过整首歌，
  // 所以要先走够 500ms 才会看到第二条。
  clock += 500;
  np.reportProgress(1.0, true);
  await flush();
  assert.equal(net.calls.length, 2, '第一条进度要发出去');

  clock += 200;
  np.reportProgress(1.2, true);
  await flush();
  assert.equal(net.calls.length, 2, '200ms 之后的那条要被节流掉');

  clock += 400;                     // 距上一条 600ms
  np.reportProgress(1.6, true);
  await flush();
  assert.equal(net.calls.length, 3, '超过 500ms 要发');
});

test('播放/暂停切换立刻上报（不受节流限制）', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong(SONG);
  await flush();
  clock += 500;
  np.reportProgress(5, true);
  await flush();
  const before = net.calls.length;

  clock += 50;                       // 远不到 500ms
  np.reportProgress(5, false);       // 用户按了暂停
  await flush();

  assert.equal(net.calls.length, before + 1, '暂停必须立刻通知服务端');
  assert.equal(net.calls[net.calls.length - 1].payload.playing, false);
});

test('暂停期间发心跳（否则服务端 10 秒后判定人已离开）', async () => {
  const { net, timers, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong({ ...SONG, playing: false });
  await flush();

  const beat = timers.intervals.filter((item) => !item.cleared && item.ms === 5000).pop();
  assert.ok(beat, '上报歌曲后应当挂上暂停心跳定时器');

  const before = net.calls.length;
  clock += 5000;
  beat.fn();
  await flush();
  assert.equal(net.calls.length, before + 1, '暂停中也要定期上报');
  assert.equal(net.calls[net.calls.length - 1].payload.type, 'progress');
  assert.equal(net.calls[net.calls.length - 1].payload.playing, false);

  // 恢复播放后心跳不该再发（播放中由 timeupdate 驱动）
  np.reportProgress(10, true, true);
  await flush();
  const after = net.calls.length;
  clock += 5000;
  beat.fn();
  await flush();
  assert.equal(net.calls.length, after, '播放中不该再发心跳');
});

test('★ 在途期间的后续上报不会丢：歌词补报必须发出去', async () => {
  const { net, np } = await scene();

  // 切歌时先报歌名（这条会挂在网络上不放行），几十毫秒后歌词到位再报一次
  const first = net.plan({ manual: true });
  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong({ title: '夜曲', artist: '周杰伦', duration: 0, lyrics: [] });
  await flush();
  assert.equal(net.calls.length, 1);

  np.reportSong(SONG);               // 歌词到了
  await flush();
  assert.equal(net.calls.length, 1, '在途期间只允许一个请求');

  first.release();                   // 第一条回来了
  await flush();

  assert.equal(net.calls.length, 2, '挂起的那条必须补发，否则悬浮窗永远没有歌词');
  assert.deepEqual(net.calls[1].payload.lyrics.map((line) => line.text),
    ['一群嗜血的蚂蚁', '为你弹奏萧邦的夜曲', '纪念我死去的爱情']);

  // 合并只保留最新：连来三条，最终只需要补一条
  const second = net.plan({ manual: true });
  np.reportSong({ title: '第一版', lyrics: [] });
  await flush();
  np.reportSong({ title: '第二版', lyrics: [] });
  np.reportSong({ title: '第三版', lyrics: [] });
  await flush();
  assert.equal(net.calls.length, 3, '在途期间仍然只发一条');
  second.release();
  await flush();
  assert.equal(net.calls[net.calls.length - 1].payload.title, '第三版');
  assert.equal(net.calls.length, 4, '合并成一条，而不是把三条都补发出去');
});

test('★ 服务端说 needSong 时自动重报整首歌（服务端重启后歌词能自己回来）', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong(SONG);
  await flush();

  // 模拟服务端重启后的一问三不知
  net.plan({ ok: true, data: { ok: true, needSong: true } });
  clock += 500;
  np.reportProgress(60, true);
  await flush();

  assert.equal(net.calls.length, 3, '进度之后应当自动补一条整首歌');
  assert.equal(net.calls[2].payload.type, 'song');
  assert.equal(net.calls[2].payload.title, '夜曲');
  assert.equal(net.calls[2].payload.lyrics.length, 3, '必须带上歌词，否则悬浮窗只有歌名');
});

test('失败时静默退避，恢复后回到正常节奏（全程不抛错、不影响播放）', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  net.plan({ reject: true });
  np.reportSong(SONG);
  await flush();
  assert.equal(np.nowPlayingState().failures, 1);

  net.plan({ reject: true });
  clock += 1000;
  np.reportProgress(1, true);
  await flush();
  assert.equal(np.nowPlayingState().failures, 2);

  // 连续失败之后：500ms 的节拍要放慢到 5 秒，不能让服务端一挂就被刷屏
  const before = net.calls.length;
  clock += 600;
  np.reportProgress(2, true);
  await flush();
  assert.equal(net.calls.length, before, '退避期间不该按 500ms 节奏继续发');

  clock += 5000;
  np.reportProgress(3, true);
  await flush();
  assert.equal(net.calls.length, before + 1, '退避后仍要静默重试');

  clock += 600;
  np.reportProgress(4, true);
  await flush();
  assert.equal(np.nowPlayingState().failures, 0, '成功之后失败计数必须清零');
});

test('服务端明确拒绝（4xx）也按失败计，不会以每秒两次的节奏空转', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  net.plan({ ok: false, data: { ok: false, code: 'bad_token' } });
  np.reportSong(SONG);
  await flush();

  assert.equal(np.nowPlayingState().failures, 1, '被拒绝也算失败');
  assert.equal(np.nowPlayingState().song, '夜曲', '但本地状态要留着，好等下一次重报');
});

test('reportStop 立刻上报停止并停掉心跳', async () => {
  const { net, timers, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong(SONG);
  await flush();

  np.reportStop();
  await flush();
  assert.equal(net.calls[net.calls.length - 1].payload.type, 'stop');
  assert.equal(np.nowPlayingState().song, '', '停止后不该再记着上一首');
  assert.ok(timers.intervals.every((item) => item.cleared), '心跳定时器要清掉');

  const before = net.calls.length;
  np.reportProgress(10, false, true);
  await flush();
  assert.equal(net.calls.length, before, '停止后不该再上报进度');
});

test('服务端关掉功能后：configureNowPlaying 会收尾并停止上报', async () => {
  const { net, np } = await scene();

  np.configureNowPlaying({ enabled: true, source: 'wangqi' });
  np.reportSong(SONG);
  await flush();

  np.configureNowPlaying({ enabled: false, source: 'wangqi' });
  await flush();
  assert.equal(net.calls[net.calls.length - 1].payload.type, 'stop', '要通知一次停止');

  const before = net.calls.length;
  np.reportProgress(10, true, true);
  np.reportSong(SONG);
  await flush();
  assert.equal(net.calls.length, before, '关闭后一条都不该再发');
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

console.log('');
console.log('Ran ' + tests.length + ' tests, ' + failed + ' failed');
process.exitCode = failed ? 1 : 0;

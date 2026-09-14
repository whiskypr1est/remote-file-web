/* ==========================================================================
   桌面歌词悬浮窗 · 连接层测试（node 直接跑，不需要 Electron）
   --------------------------------------------------------------------------
   跑法：node tests/js/desktop-lyrics-client.test.mjs

   为什么能脱离 Electron 测：desktop-lyrics/src/client.js **只依赖 ws 和 fetch**，
   没有 require('electron')。而它恰恰是整个客户端里逻辑最重的一块：
   指数退避重连、心跳判死、WebSocket 连不上时退化成轮询、以及把
   「服务端关了功能 / 要 token / 连不上」分辨清楚。这些 bug 在真机上
   只表现为「歌词不出来」，所以必须在这儿钉住。

   is 的边界：Electron 的窗口本身（透明、置顶、鼠标穿透）在这里测不了 ——
   那需要一台有交互桌面的机器，见 README 的说明。
   ========================================================================== */

import assert from 'node:assert/strict';
import http from 'node:http';
import { createRequire } from 'node:module';

// ★ 用 createRequire 从 desktop-lyrics 里解析依赖：ws 装在
//   desktop-lyrics/node_modules 下，从本文件（tests/js/）直接 import 是找不到的。
const requireFromClient = createRequire(
  new URL('../../desktop-lyrics/src/client.js', import.meta.url));
const { WebSocketServer } = requireFromClient('ws');

// client.js 是 CommonJS：动态 import 后真正的导出在 default 上
const ns = await import(new URL('../../desktop-lyrics/src/client.js', import.meta.url));
const clientApi = ns.default || ns;
const { LyricsClient, normalizeBaseUrl, wsUrl, stateUrl, sourcesUrl } = clientApi;

const SONG_STATE = {
  type: 'state', idle: false, revision: 7, source: 'wangqi', title: '夜曲',
  artist: '周杰伦', duration: 227, currentTime: 44.5, playing: true,
  lyrics: [{ time: 0, text: '一群嗜血的蚂蚁' }, { time: 41.2, text: '为你弹奏萧邦的夜曲' }]
};

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

/** 等到条件成立，或超时抛错（比 sleep 固定时长稳定得多）。 */
async function until(check, label, timeout = 8000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    if (check()) {
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 40));
  }
  throw new Error('等待超时：' + label);
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

/**
 * 起一个「像本项目服务端」的小服务器：同一个端口上既跑 WebSocket，也跑 HTTP。
 *
 * @param {object} options
 * @param {boolean} [options.rejectUpgrade] 握手直接断开（模拟防火墙/安全软件拦掉 WS）
 * @param {number}  [options.httpStatus]     /now-playing 的状态码
 * @param {string}  [options.httpCode]       403 时的错误码（lyrics_disabled / bad_token）
 * @param {boolean} [options.sendOnConnect]  连上就推一条状态
 */
function startServer(options) {
  const opts = options || {};
  const connections = [];
  const server = http.createServer((request, response) => {
    const status = opts.httpStatus || 200;
    if (status !== 200) {
      const body = JSON.stringify({
        ok: false, code: opts.httpCode || '', message: '测试用错误'
      });
      response.writeHead(status, { 'Content-Type': 'application/json' });
      response.end(body);
      return;
    }
    const body = JSON.stringify({
      ok: true,
      enabled: true,
      display: SONG_STATE,
      sources: [{ source: 'wangqi', title: '夜曲', playing: true, fresh: true }]
    });
    response.writeHead(200, { 'Content-Type': 'application/json' });
    response.end(body);
  });

  const wss = new WebSocketServer({ noServer: true });
  server.on('upgrade', (request, socket, head) => {
    if (opts.rejectUpgrade) {
      socket.destroy();            // 模拟「Upgrade 被拦掉」
      return;
    }
    wss.handleUpgrade(request, socket, head, (ws) => {
      connections.push(ws);
      if (opts.sendOnConnect !== false) {
        ws.send(JSON.stringify(SONG_STATE));
      }
    });
  });

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => {
      resolve({
        port: server.address().port,
        base: 'http://127.0.0.1:' + server.address().port,
        connections,
        /** 主动踢掉所有连接（模拟服务端重启 / 网络中断） */
        drop() {
          for (const ws of connections.splice(0)) {
            try {
              ws.terminate();
            } catch (err) { /* 忽略 */ }
          }
        },
        close() {
          for (const ws of connections.splice(0)) {
            try {
              ws.terminate();
            } catch (err) { /* 忽略 */ }
          }
          wss.close();
          server.close();
        }
      });
    });
  });
}

/** 造一个被测试的客户端，并把状态/状态码记录下来。 */
function makeClient(config) {
  const record = { states: [], statuses: [] };
  const instance = new LyricsClient({
    getConfig: () => config,
    onState: (state) => record.states.push(state),
    onStatus: (status) => record.statuses.push(status)
  });
  record.client = instance;
  record.lastStatus = () => (record.statuses[record.statuses.length - 1] || {});
  record.hasStatus = (state) => record.statuses.some((item) => item.state === state);
  return record;
}

/* ---------------------------------------------------------------------------
   地址处理（纯函数，先把最容易出错的一块钉住）
   --------------------------------------------------------------------------- */

test('地址规范化：不带协议、带路径、带斜杠都能用', async () => {
  assert.equal(normalizeBaseUrl('192.168.1.10:8000'), 'http://192.168.1.10:8000');
  assert.equal(normalizeBaseUrl('http://192.168.1.10:8000/'), 'http://192.168.1.10:8000');
  assert.equal(normalizeBaseUrl('http://192.168.1.10:8000/desktop'), 'http://192.168.1.10:8000',
    '从浏览器地址栏复制来的路径要去掉');
  assert.equal(normalizeBaseUrl('https://files.lan/'), 'https://files.lan');
  assert.equal(normalizeBaseUrl('   '), '');
  assert.equal(normalizeBaseUrl('这不是地址'), '');
});

test('ws/wss 与 http/https 对应，user 与 token 会带上', async () => {
  assert.equal(wsUrl('http://a.lan:8000', { user: '', token: '' }),
    'ws://a.lan:8000/ws/lyrics');
  assert.equal(wsUrl('https://a.lan', { user: 'wangqi', token: 't k' }),
    'wss://a.lan/ws/lyrics?user=wangqi&token=t%20k');
  assert.equal(stateUrl('http://a.lan:8000', { user: 'wangqi' }),
    'http://a.lan:8000/now-playing?user=wangqi');
  assert.equal(sourcesUrl('http://a.lan:8000', { token: '' }),
    'http://a.lan:8000/now-playing?sources=1');
});

/* ---------------------------------------------------------------------------
   连接与重连
   --------------------------------------------------------------------------- */

test('连上就收到状态，状态码是 online', async () => {
  const server = await startServer();
  const record = makeClient({ serverUrl: server.base, user: '', token: '' });
  try {
    record.client.start();
    await until(() => record.states.length > 0, '收到第一条状态');
    await until(() => record.hasStatus('online'), '状态变成 online');
    assert.equal(record.states[0].title, '夜曲');
    assert.equal(record.states[0].source, 'wangqi');
  } finally {
    record.client.stop();
    server.close();
  }
});

test('★ 服务端把连接踢掉后，客户端会自己重连并重新拿到状态', async () => {
  const server = await startServer();
  const record = makeClient({ serverUrl: server.base, user: '', token: '' });
  try {
    record.client.start();
    await until(() => record.states.length >= 1, '第一条状态');

    server.drop();                                  // 模拟服务端重启
    await until(() => record.hasStatus('reconnecting'), '进入重连状态');
    await until(() => record.states.length >= 2, '重连后重新收到状态', 12000);

    assert.equal(record.states[1].title, '夜曲');
  } finally {
    record.client.stop();
    server.close();
  }
});

test('★ WebSocket 被拦掉时退化成轮询，歌词仍然能出来', async () => {
  // 有些网络设备/安全软件会拦掉 Upgrade 请求。此时如果只会重连，
  // 用户看到的就是「永远连不上」；轮询兜底让它至少能慢一拍地显示。
  const server = await startServer({ rejectUpgrade: true });
  const record = makeClient({ serverUrl: server.base, user: '', token: '' });
  try {
    record.client.start();
    await until(() => record.hasStatus('polling'), '进入轮询模式', 12000);
    await until(() => record.states.length > 0, '轮询拿到了状态', 12000);
    assert.equal(record.states[0].title, '夜曲');
  } finally {
    record.client.stop();
    server.close();
  }
});

test('服务端关了功能：明确告诉用户是「功能关了」，不是网络问题', async () => {
  const server = await startServer({ rejectUpgrade: true, httpStatus: 403, httpCode: 'lyrics_disabled' });
  const record = makeClient({ serverUrl: server.base, user: '', token: '' });
  try {
    record.client.start();
    await until(() => record.hasStatus('disabled'), '状态变成 disabled', 12000);
    const status = record.lastStatus();
    assert.match(status.message, /lyrics\.enabled/, '要把改哪里说清楚');
  } finally {
    record.client.stop();
    server.close();
  }
});

test('服务端要求 token：提示去填 token', async () => {
  const server = await startServer({ rejectUpgrade: true, httpStatus: 403, httpCode: 'bad_token' });
  const record = makeClient({ serverUrl: server.base, user: '', token: '' });
  try {
    record.client.start();
    await until(() => record.hasStatus('badToken'), '状态变成 badToken', 12000);
  } finally {
    record.client.stop();
    server.close();
  }
});

test('服务器根本没起：进入重连而不是崩溃', async () => {
  // 用一个已经关闭的端口（先起再关，确保没人监听）
  const server = await startServer();
  const base = server.base;
  server.close();
  await sleep(150);

  const record = makeClient({ serverUrl: base, user: '', token: '' });
  try {
    record.client.start();
    await until(() => record.hasStatus('reconnecting') || record.hasStatus('offline'),
      '进入重连/离线状态', 12000);
    assert.equal(record.states.length, 0, '连不上时不该有状态');
  } finally {
    record.client.stop();
  }
});

test('还没配置服务器：不算错误，也不该反复重连', async () => {
  const record = makeClient({ serverUrl: '', user: '', token: '' });
  try {
    record.client.start();
    await until(() => record.hasStatus('unconfigured'), '状态是 unconfigured');
    await sleep(300);
    assert.equal(record.states.length, 0);
    // 只在配置变化时 reload，所以这里不应该出现重连提示
    assert.equal(record.hasStatus('reconnecting'), false);
  } finally {
    record.client.stop();
  }
});

test('配置改了（换服务器）会重连到新地址', async () => {
  const first = await startServer();
  const second = await startServer();
  const config = { serverUrl: first.base, user: '', token: '' };
  const record = makeClient(config);
  try {
    record.client.start();
    await until(() => record.states.length >= 1, '连上第一台');

    config.serverUrl = second.base;
    record.client.reload();
    await until(() => second.connections.length > 0, '连上第二台', 12000);
  } finally {
    record.client.stop();
    first.close();
    second.close();
  }
});

test('stop() 之后不再重连、不再收状态', async () => {
  const server = await startServer();
  const record = makeClient({ serverUrl: server.base, user: '', token: '' });
  try {
    record.client.start();
    await until(() => record.states.length >= 1, '连上');
    record.client.stop();

    const statesBefore = record.states.length;
    server.drop();
    await sleep(1500);
    assert.equal(record.states.length, statesBefore, 'stop 之后不该再有状态');
    assert.equal(server.connections.length, 0, '不该再建立新连接');
  } finally {
    server.close();
  }
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

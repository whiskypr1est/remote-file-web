'use strict';
/* ==========================================================================
   桌面歌词 · 悬浮窗冒烟测试
   --------------------------------------------------------------------------
   跑法（在 desktop-lyrics 目录下）：
       npx electron tools/smoke.js [输出目录]

   它用**真实的** Electron、真实的窗口参数、真实的 preload 与渲染层跑一遍，
   然后：
     1. 等渲染层通过 preload 发出 "lyrics:ready"（这一步就验证了 IPC 通道是通的）；
     2. 推一份示例状态进去；
     3. 用 executeJavaScript 读回 DOM，断言「那行字真的画出来了」；
     4. 用 capturePage 把窗口截成 PNG，供人工核对描边/字号。

   为什么要专门做这个：悬浮窗的毛病几乎都发生在「窗口参数 + IPC + 渲染」的接缝上
   （比如频道名写错、preload 没挂上、透明窗口没生效），而这些在浏览器里预览
   HTML 是**测不出来**的 —— 只有真的起一次 Electron 才知道。

   退出码 0 = 全部通过；非 0 = 有断言失败（输出里会写清楚是哪一条）。
   ========================================================================== */

const path = require('path');
const fs = require('fs');
const { app, BrowserWindow, ipcMain } = require('electron');

// 这台机器（服务器会话）里未必有可用的 GPU；用软件渲染让结果可预期。
// 真正的悬浮窗不开这个开关，所以它只影响本测试。
app.disableHardwareAcceleration();

const OUT_DIR = outputDir(process.argv);
const INDEX = path.join(__dirname, '..', 'src', 'renderer', 'index.html');
const PRELOAD = path.join(__dirname, '..', 'src', 'preload.js');

/**
 * 输出目录：只认**不以 `-` 开头**的参数。
 *
 * ★ 别把 process.argv[2] 直接当目录名：Chromium 自己的开关（--no-sandbox、
 *   --disable-gpu…）也会出现在 argv 里，于是会建出一个叫 `--disable-gpu`
 *   的目录 —— 这正是本项目里真实发生过的事。
 */
function outputDir(argv) {
  for (const arg of argv.slice(2)) {
    if (arg && !arg.startsWith('-') && !arg.endsWith('.js')) {
      return path.resolve(arg);
    }
  }
  return path.join(__dirname, '..', 'dist-smoke');
}

const LYRICS = [
  { time: 0, text: '一群嗜血的蚂蚁 被腐肉所吸引' },
  { time: 12.4, text: '我面无表情 看孤独的风景' },
  { time: 26.8, text: '失去你 爱恨开始分明' },
  { time: 41.2, text: '为你弹奏萧邦的夜曲' },
  { time: 55.6, text: '纪念我死去的爱情' }
];

const SCENES = [
  {
    name: 'playing',
    // 正唱到 41.2 秒那一行
    state: {
      idle: false, playing: true, source: 'wangqi', title: '夜曲', artist: '周杰伦',
      duration: 227, currentTime: 44.5, lyrics: LYRICS
    },
    config: { fontSize: 40, showProgress: false, clickThrough: true, locked: false, editing: false },
    expect: { text: '为你弹奏萧邦的夜曲', classes: ['stage'] }
  },
  {
    name: 'paused',
    state: {
      idle: false, playing: false, source: 'wangqi', title: '夜曲', artist: '周杰伦',
      duration: 227, currentTime: 44.5, lyrics: LYRICS
    },
    config: { fontSize: 40, showProgress: false, clickThrough: true, locked: false, editing: false },
    // 暂停时整块降为半透明（需求：暂停时半透明并停止滚动）
    expect: { text: '为你弹奏萧邦的夜曲', classes: ['stage', 'paused'] }
  },
  {
    name: 'intro',
    // 前奏：还没到第一句，显示歌名
    state: {
      idle: false, playing: true, source: 'wangqi', title: '夜曲', artist: '周杰伦',
      duration: 227, currentTime: 1.0,
      lyrics: [{ time: 9.5, text: '一群嗜血的蚂蚁 被腐肉所吸引' }]
    },
    config: { fontSize: 40, showProgress: false, clickThrough: true, locked: false, editing: false },
    expect: { text: '夜曲 - 周杰伦', classes: ['stage'] }
  },
  {
    name: 'editing',
    // 调整模式：关掉鼠标穿透，此时窗口一直显示，副标题给出歌名与连接状态
    state: {
      idle: false, playing: true, source: 'wangqi', title: '夜曲', artist: '周杰伦',
      duration: 227, currentTime: 44.5, lyrics: LYRICS
    },
    config: { fontSize: 52, showProgress: true, clickThrough: false, locked: false, editing: true },
    status: { state: 'online', message: '' },
    expect: { text: '为你弹奏萧邦的夜曲', classes: ['stage'], subContains: '拖动可移动窗口' }
  },
  {
    name: 'offline',
    // 没在放歌 + 连不上：正常模式下也要把原因显示出来（否则用户只会看到一片空白）
    state: { idle: true },
    config: { fontSize: 40, showProgress: false, clickThrough: true, locked: false, editing: false },
    // ★ message 是**不带前缀**的裸原因：完整那句由渲染层的 statusLabel() 拼，
    //   两处都带前缀就会显示成「连不上服务端：连不上服务端：网络不可达」
    status: { state: 'offline', message: '网络不可达' },
    expect: { text: '连不上服务端：网络不可达', classes: ['stage', 'idle'] }
  }
];

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function createWindow() {
  return new BrowserWindow({
    width: 1100,
    height: 150,
    transparent: true,
    frame: false,
    resizable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    focusable: false,
    hasShadow: false,
    show: true,
    backgroundColor: '#00000000',
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      backgroundThrottling: false
    }
  });
}

async function runScene(win, ready, scene, index) {
  const failures = [];

  win.webContents.send('lyrics:config', Object.assign({
    fontSize: 40, showProgress: false, clickThrough: true, locked: false, editing: false
  }, scene.config || {}));
  win.webContents.send('lyrics:status', scene.status || { state: 'online', message: '' });
  win.webContents.send('lyrics:state', { state: scene.state, receivedAt: Date.now() });

  // 等一帧动画走完
  await wait(700);
  void ready;

  const actual = await win.webContents.executeJavaScript(`(() => {
    const stage = document.getElementById('stage');
    const lyric = document.getElementById('lyric');
    const sub = document.getElementById('sub');
    const style = getComputedStyle(lyric);
    return {
      text: lyric.textContent,
      sub: sub.textContent,
      stageClasses: stage.className,
      fontSize: style.fontSize,
      textShadow: style.textShadow.split('rgba')[0].trim().slice(0, 40),
      progressHidden: document.getElementById('progress').hidden,
      draggable: document.body.className,
      transparentBody: getComputedStyle(document.body).backgroundColor
    };
  })()`);

  if (actual.text !== scene.expect.text) {
    failures.push('当前行不对：期望「' + scene.expect.text + '」，实际「' + actual.text + '」');
  }
  for (const name of scene.expect.classes || []) {
    if (actual.stageClasses.split(/\s+/).indexOf(name) < 0) {
      failures.push('缺少样式类 ' + name + '（实际：' + actual.stageClasses + '）');
    }
  }
  if (scene.expect.subContains && actual.sub.indexOf(scene.expect.subContains) < 0) {
    failures.push('副标题里没有「' + scene.expect.subContains + '」：' + actual.sub);
  }
  if (actual.transparentBody !== 'rgba(0, 0, 0, 0)') {
    failures.push('body 背景不是透明的，会挡住桌面：' + actual.transparentBody);
  }
  if (scene.name === 'editing') {
    if (actual.progressHidden !== false) {
      failures.push('开了「显示进度条」但进度条还是隐藏的');
    }
    if (actual.draggable.indexOf('draggable') < 0) {
      failures.push('调整模式下 body 没有 draggable 类，拖不动窗口');
    }
  } else if (actual.draggable.indexOf('draggable') >= 0) {
    failures.push('正常模式下不该可拖动（body 上有 draggable）');
  }

  fs.mkdirSync(OUT_DIR, { recursive: true });
  const image = await win.webContents.capturePage();
  const png = path.join(OUT_DIR, String(index + 1).padStart(2, '0') + '-' + scene.name + '.png');
  fs.writeFileSync(png, image.toPNG());

  return { scene: scene.name, failures, actual, png, size: image.getSize() };
}

app.whenReady().then(async () => {
  const results = [];

  /*
   ★ 全程只用一个窗口，五个场景共用它。

   早先的写法是每个场景新建窗口、跑完 destroy —— 在没有交互桌面的服务器会话上
   会崩：销毁一个 transparent 窗口之后再建下一个，渲染子进程直接挂掉
   （日志里是 crashpad_client_win.cc: not connected），表现是「第一条场景过了、
   后面全没了」。复用同一个窗口既绕开了它，也更快（少四次窗口初始化）。
  */
  const win = createWindow();
  const ready = new Promise((resolve) => {
    ipcMain.once('lyrics:ready', () => resolve(true));
    setTimeout(() => resolve(false), 8000);
  });
  await win.loadFile(INDEX);
  const gotReady = await ready;
  if (!gotReady) {
    results.push({
      scene: '(启动)',
      failures: ['渲染层没有发出 lyrics:ready（preload 或 overlay.js 没跑起来）'],
      actual: {}
    });
  }

  for (let i = 0; i < SCENES.length && gotReady; i += 1) {
    try {
      results.push(await runScene(win, gotReady, SCENES[i], i));
    } catch (err) {
      results.push({
        scene: SCENES[i].name,
        failures: ['场景执行出错：' + ((err && err.message) || err)],
        actual: {}
      });
    }
  }

  let failed = 0;
  for (const result of results) {
    if (result.failures.length) {
      failed += 1;
      console.log('FAIL ' + result.scene);
      for (const item of result.failures) {
        console.log('     ' + item);
      }
    } else {
      console.log('ok   ' + result.scene + '  ->  ' + result.actual.text);
    }
  }
  console.log('');
  console.log('截图输出目录：' + OUT_DIR);
  console.log('Ran ' + results.length + ' scenes, ' + failed + ' failed');

  app.exit(failed ? 1 : 0);
});

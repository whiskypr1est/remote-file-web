'use strict';
/* ==========================================================================
   桌面歌词 · 主进程
   --------------------------------------------------------------------------
   职责划分（与需求一致）：
     * 主进程：悬浮窗本身、托盘、本地配置、与服务端的连接（WebSocket + 轮询）。
     * 渲染进程：**只负责画歌词**（一行大字 + 描边），不做任何网络与文件操作。
   这样分的好处是连接状态、重连、位置保存这些「有状态」的事都在主进程里，
   渲染进程随便刷新（甚至崩了重建）都不会影响连接。

   悬浮窗的几个关键设置，以及为什么：
     transparent + frame:false   透明无边框，才能只显示一行字
     alwaysOnTop('screen-saver') 最高层级。★ 但**独占全屏的游戏仍然盖不住** ——
                                 那是 Windows 合成器的限制（Discord / Steam
                                 的浮层同样盖不住），请让游戏跑「无边框窗口化」。
     skipTaskbar                 不占任务栏（它不是「一个程序窗口」，是一层浮字）
     focusable:false             永远不抢焦点：否则玩游戏时鼠标一点浮层，
                                 游戏就跑到后台去了
     setIgnoreMouseEvents(true)  鼠标穿透：点的还是游戏，不是这行字

   托盘是唯一的操作入口（因为窗口本身故意不接受键盘/鼠标）：显示与隐藏、
   鼠标穿透、锁定位置、字号、订阅哪个用户、连接设置、开机自启、退出。
   ========================================================================== */

const path = require('path');
const fs = require('fs');
const {
  app, BrowserWindow, Menu, Tray, ipcMain, nativeImage, screen, shell
} = require('electron');

const store = require('./store');
const { LyricsClient, normalizeBaseUrl, sourcesUrl } = require('./client');

const RENDERER = path.join(__dirname, 'renderer');
const PRELOAD = path.join(__dirname, 'preload.js');
const TRAY_ICON = path.join(__dirname, 'assets', 'tray.png');

const FONT_SIZES = [
  { label: '小', value: 28 },
  { label: '中', value: 40 },
  { label: '大', value: 52 },
  { label: '特大', value: 68 }
];

let overlay = null;
let settingsWin = null;
let tray = null;
let client = null;

/** 最近一次收到的状态。渲染进程可能在它之后才加载完，所以必须留一份。 */
let lastState = null;
let lastStatus = { state: 'connecting', message: '' };
/** 托盘里的用户列表（从服务端 ?sources=1 拿到），异步刷新。 */
let knownSources = [];

/* ---------------------------------------------------------------------------
   窗口
   --------------------------------------------------------------------------- */

/** 把窗口放在屏幕内；位置无效（拔了显示器/改了分辨率）时回到底部居中。 */
function resolveBounds(cfg) {
  const displays = screen.getAllDisplays();
  const primary = screen.getPrimaryDisplay();
  const work = primary.workArea;

  const width = cfg.width;
  const height = cfg.height;
  const fallbackX = Math.round(work.x + (work.width - width) / 2);
  // 默认放在底部往上 12% 的位置：既不挡字幕，也不贴边
  const fallbackY = Math.round(work.y + work.height - height - Math.round(work.height * 0.12));

  let x = cfg.x === null ? fallbackX : cfg.x;
  let y = cfg.y === null ? fallbackY : cfg.y;

  // ★ 位置必须落在某块屏幕的可见区域里。否则「上次把歌词拖到副屏、这次副屏
  //   没插」就会得到一个永远看不见、又拖不动的窗口 —— 用户只能去删配置文件。
  const visible = displays.some((display) => {
    const area = display.workArea;
    const overlapX = Math.min(x + width, area.x + area.width) - Math.max(x, area.x);
    const overlapY = Math.min(y + height, area.y + area.height) - Math.max(y, area.y);
    return overlapX > 40 && overlapY > 20;      // 至少露出一小块才算「看得见」
  });
  if (!visible) {
    x = fallbackX;
    y = fallbackY;
  }
  return { x, y, width, height };
}

function createOverlay() {
  const cfg = store.load();
  const bounds = resolveBounds(cfg);

  overlay = new BrowserWindow({
    x: bounds.x,
    y: bounds.y,
    width: bounds.width,
    height: bounds.height,
    transparent: true,
    frame: false,
    resizable: false,
    movable: !cfg.locked,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    skipTaskbar: true,
    alwaysOnTop: true,
    focusable: false,
    hasShadow: false,
    show: false,
    backgroundColor: '#00000000',
    title: '桌面歌词',
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false,
      // 悬浮窗长时间只显示一行字，后台节流会让动画卡顿，这里明确关掉
      backgroundThrottling: false
    }
  });

  // screen-saver 是最高的常规层级；普通窗口、任务栏、多数游戏浮层都在它之下
  overlay.setAlwaysOnTop(true, 'screen-saver');
  // 虚拟桌面切换后仍然可见（Windows 上同样生效）
  overlay.setVisibleOnAllWorkspaces(true, { visibleOnFullScreen: true });
  applyClickThrough(cfg.clickThrough);
  applyConfigToRenderer();

  overlay.loadFile(path.join(RENDERER, 'index.html'));

  overlay.webContents.on('did-finish-load', () => {
    applyConfigToRenderer();
    if (lastState) {
      send('lyrics:state', stateEnvelope(lastState));
    }
    send('lyrics:status', lastStatus);
    updateVisibility();
  });

  // 位置只存不还原：还原由 resolveBounds 在启动时做一次
  let saveTimer = null;
  overlay.on('moved', () => {
    if (saveTimer) {
      clearTimeout(saveTimer);
    }
    saveTimer = setTimeout(() => {
      saveTimer = null;
      if (!overlay || overlay.isDestroyed()) {
        return;
      }
      const rect = overlay.getBounds();
      store.save({ x: rect.x, y: rect.y });
    }, 400);
  });

  overlay.on('closed', () => {
    overlay = null;
  });

  return overlay;
}

function send(channel, payload) {
  if (overlay && !overlay.isDestroyed()) {
    overlay.webContents.send(channel, payload);
  }
}

/** 给状态加上「本机收到它的时刻」：渲染进程用它做平滑外推，避免依赖时钟同步。 */
function stateEnvelope(state) {
  return { state: state, receivedAt: Date.now() };
}

function applyClickThrough(enabled) {
  if (overlay && !overlay.isDestroyed()) {
    // forward:true 让鼠标移动事件仍然送到渲染进程（悬停效果可用），
    // 但点击会穿透到下面的窗口 —— 这才是「不挡鼠标」的正确行为。
    overlay.setIgnoreMouseEvents(!!enabled, { forward: true });
  }
}

function applyMovable(cfg) {
  if (overlay && !overlay.isDestroyed()) {
    overlay.setMovable(!cfg.locked);
  }
}

function currentRendererConfig() {
  const cfg = store.load();
  return {
    fontSize: cfg.fontSize,
    showProgress: cfg.showProgress,
    clickThrough: cfg.clickThrough,
    locked: cfg.locked,
    // 「调整模式」= 关掉鼠标穿透。此时窗口要一直显示（哪怕没在放歌），
    // 否则用户根本没有东西可拖。
    editing: !cfg.clickThrough
  };
}

function applyConfigToRenderer() {
  send('lyrics:config', currentRendererConfig());
}

/**
 * 该不该显示。
 *
 * 正常使用（鼠标穿透开着）时：**没在放歌就完全隐藏** —— 屏幕上不该有一层
 * 看不见的透明窗口挡着（哪怕它穿透，也占着视觉位置）。
 * 调整模式（穿透关掉）时：一直显示，没歌就显示提示文字，好让用户拖动定位。
 */
function updateVisibility() {
  if (!overlay || overlay.isDestroyed()) {
    return;
  }
  const cfg = store.load();
  const idle = !lastState || lastState.idle === true;
  const shouldShow = !cfg.hidden && (!idle || !cfg.clickThrough);
  if (shouldShow) {
    if (!overlay.isVisible()) {
      // ★ showInactive 而不是 show：绝不能把焦点从游戏里抢走
      overlay.showInactive();
    }
  } else if (overlay.isVisible()) {
    overlay.hide();
  }
}

/* ---------------------------------------------------------------------------
   连接
   --------------------------------------------------------------------------- */

function createClient() {
  client = new LyricsClient({
    getConfig: () => {
      const cfg = store.load();
      return { serverUrl: cfg.serverUrl, user: cfg.user, token: cfg.token };
    },
    onState: (state) => {
      lastState = state;
      send('lyrics:state', stateEnvelope(state));
      updateVisibility();
    },
    onStatus: (status) => {
      lastStatus = status;
      send('lyrics:status', status);
      updateTray();
    }
  });
  return client;
}

/** 拉一次服务端的源列表（谁在放歌），用于托盘里的「订阅用户」。 */
async function refreshSources() {
  const cfg = store.load();
  const base = normalizeBaseUrl(cfg.serverUrl);
  if (!base) {
    knownSources = [];
    updateTray();
    return { ok: false, message: '还没有设置服务器地址', sources: [] };
  }
  try {
    const response = await fetch(sourcesUrl(base, cfg), { method: 'GET' });
    if (response.status === 403) {
      const body = await response.json().catch(() => null);
      const code = (body && body.code) || '';
      const message = code === 'lyrics_disabled'
        ? '服务端已关闭桌面歌词功能'
        : (code === 'bad_token' ? '服务端要求 token' : '服务端拒绝了这次请求');
      return { ok: false, message: message, sources: [] };
    }
    if (!response.ok) {
      return { ok: false, message: 'HTTP ' + response.status, sources: [] };
    }
    const data = await response.json();
    knownSources = (data.sources || []).map((item) => item.source).filter(Boolean);
    updateTray();
    return { ok: true, message: '连接正常', sources: data.sources || [] };
  } catch (err) {
    return { ok: false, message: (err && err.message) || '网络不可达', sources: [] };
  }
}

/* ---------------------------------------------------------------------------
   托盘
   --------------------------------------------------------------------------- */

function trayImage() {
  try {
    if (fs.existsSync(TRAY_ICON)) {
      const image = nativeImage.createFromPath(TRAY_ICON);
      if (!image.isEmpty()) {
        return image;
      }
    }
  } catch (err) { /* 落到空图标 */ }
  return nativeImage.createEmpty();
}

function statusText() {
  const map = {
    online: '已连接',
    polling: '轮询模式（WebSocket 连不上）',
    connecting: '正在连接…',
    reconnecting: '正在重连…',
    offline: '连不上服务端',
    disabled: '服务端已关闭该功能',
    badToken: '需要 token',
    unconfigured: '还没有设置服务器地址'
  };
  return map[lastStatus.state] || lastStatus.message || '未知状态';
}

function userMenu() {
  const cfg = store.load();
  const items = [{
    label: '全部（谁在放就显示谁）',
    type: 'radio',
    checked: !cfg.user,
    click: () => setConfig({ user: '' })
  }];
  for (const name of knownSources) {
    items.push({
      label: name,
      type: 'radio',
      checked: cfg.user === name,
      click: () => setConfig({ user: name })
    });
  }
  if (cfg.user && !knownSources.includes(cfg.user)) {
    // 手填的用户名（也可能只是他此刻没在放歌）
    items.push({ label: cfg.user, type: 'radio', checked: true, click: () => {} });
  }
  items.push({ type: 'separator' });
  items.push({ label: '刷新用户列表', click: () => { refreshSources(); } });
  return items;
}

function buildMenu() {
  const cfg = store.load();
  return Menu.buildFromTemplate([
    { label: '桌面歌词 · ' + statusText(), enabled: false },
    { type: 'separator' },
    {
      label: '显示歌词',
      type: 'checkbox',
      checked: !cfg.hidden,
      click: (item) => setConfig({ hidden: !item.checked })
    },
    {
      label: '鼠标穿透（推荐开启）',
      type: 'checkbox',
      checked: cfg.clickThrough,
      click: (item) => setConfig({ clickThrough: !item.checked })
    },
    {
      label: '锁定位置',
      type: 'checkbox',
      checked: cfg.locked,
      click: (item) => setConfig({ locked: !item.checked })
    },
    {
      label: '字号',
      submenu: FONT_SIZES.map((item) => ({
        label: item.label + '（' + item.value + 'px）',
        type: 'radio',
        checked: cfg.fontSize === item.value,
        click: () => setConfig({ fontSize: item.value })
      }))
    },
    {
      label: '显示进度条',
      type: 'checkbox',
      checked: cfg.showProgress,
      click: (item) => setConfig({ showProgress: !item.checked })
    },
    { label: '订阅用户', submenu: userMenu() },
    { type: 'separator' },
    { label: '连接设置…', click: () => openSettings() },
    {
      label: '开机自启',
      type: 'checkbox',
      checked: cfg.autoStart,
      click: (item) => setConfig({ autoStart: !item.checked })
    },
    { label: '打开说明文档', click: () => openReadme() },
    { type: 'separator' },
    { label: '退出', click: () => quitApp() }
  ]);
}

function updateTray() {
  if (!tray || tray.isDestroyed()) {
    return;
  }
  tray.setToolTip('桌面歌词 · ' + statusText());
  tray.setContextMenu(buildMenu());
}

function openReadme() {
  const file = path.join(__dirname, '..', 'README.md');
  if (fs.existsSync(file)) {
    shell.openPath(file);
  }
}

function quitApp() {
  store.save({});          // 顺手落一次盘，别丢掉刚改的设置
  app.isQuitting = true;
  app.quit();
}

function setConfig(patch) {
  const before = store.load();
  const next = store.save(patch);

  if (next.clickThrough !== before.clickThrough) {
    applyClickThrough(next.clickThrough);
  }
  if (next.locked !== before.locked) {
    applyMovable(next);
  }
  if (next.autoStart !== before.autoStart) {
    applyAutoStart(next.autoStart);
  }
  if (next.serverUrl !== before.serverUrl || next.user !== before.user
      || next.token !== before.token) {
    lastState = null;
    if (client) {
      client.reload();
    }
  }
  applyConfigToRenderer();
  updateVisibility();
  updateTray();
  if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.webContents.send('settings:changed', publicConfig());
  }
  return next;
}

function publicConfig() {
  const cfg = store.load();
  return {
    serverUrl: cfg.serverUrl,
    user: cfg.user,
    token: cfg.token,
    fontSize: cfg.fontSize,
    showProgress: cfg.showProgress,
    clickThrough: cfg.clickThrough,
    locked: cfg.locked,
    autoStart: cfg.autoStart,
    hidden: cfg.hidden,
    status: lastStatus,
    sources: knownSources
  };
}

/* ---------------------------------------------------------------------------
   开机自启
   --------------------------------------------------------------------------- */

function applyAutoStart(enabled) {
  // 开发模式（npm start）下 process.execPath 是 electron.exe，必须补上应用
  // 目录，否则开机启动的会是一个空的 Electron。
  const args = app.isPackaged ? [] : [app.getAppPath()];
  try {
    app.setLoginItemSettings({
      openAtLogin: !!enabled,
      path: process.execPath,
      args: args
    });
  } catch (err) {
    console.error('[自启] 设置失败：', err && err.message);
  }
}

/* ---------------------------------------------------------------------------
   设置窗口
   --------------------------------------------------------------------------- */

function openSettings() {
  if (settingsWin && !settingsWin.isDestroyed()) {
    settingsWin.show();
    settingsWin.focus();
    return;
  }
  settingsWin = new BrowserWindow({
    width: 560,
    height: 660,
    resizable: true,
    minimizable: false,
    maximizable: false,
    title: '桌面歌词 · 连接设置',
    backgroundColor: '#f5f6f8',
    webPreferences: {
      preload: PRELOAD,
      contextIsolation: true,
      nodeIntegration: false
    }
  });
  settingsWin.setMenuBarVisibility(false);
  settingsWin.loadFile(path.join(RENDERER, 'settings.html'));
  settingsWin.on('closed', () => { settingsWin = null; });
}

/* ---------------------------------------------------------------------------
   IPC
   --------------------------------------------------------------------------- */

function registerIpc() {
  // ---- 悬浮窗侧 ----
  ipcMain.on('lyrics:ready', () => {
    applyConfigToRenderer();
    send('lyrics:status', lastStatus);
    if (lastState) {
      send('lyrics:state', stateEnvelope(lastState));
    }
  });

  // ---- 设置窗口侧 ----
  ipcMain.handle('settings:load', () => publicConfig());

  ipcMain.handle('settings:save', (event, payload) => {
    const patch = {};
    const data = payload || {};
    for (const key of ['serverUrl', 'user', 'token']) {
      if (typeof data[key] === 'string') {
        patch[key] = data[key].trim();
      }
    }
    for (const key of ['fontSize', 'showProgress', 'clickThrough', 'locked',
      'autoStart', 'hidden']) {
      if (data[key] !== undefined) {
        patch[key] = data[key];
      }
    }
    setConfig(patch);
    return { ok: true, config: publicConfig() };
  });

  ipcMain.handle('settings:test', async (event, payload) => {
    const data = payload || {};
    const base = normalizeBaseUrl(data.serverUrl);
    if (!base) {
      return { ok: false, message: '地址填得不对，例如 http://192.168.1.10:8000' };
    }
    try {
      const response = await fetch(sourcesUrl(base, {
        token: typeof data.token === 'string' ? data.token.trim() : ''
      }), { method: 'GET' });
      if (response.status === 403) {
        const body = await response.json().catch(() => null);
        const code = (body && body.code) || '';
        if (code === 'lyrics_disabled') {
          return { ok: false, message: '服务端连上了，但桌面歌词功能被关闭了'
            + '（config.json 里 lyrics.enabled = false）' };
        }
        if (code === 'bad_token') {
          return { ok: false, message: '服务端连上了，但需要 token（见 lyrics.token）' };
        }
        return { ok: false, message: '服务端拒绝了请求（403）' };
      }
      if (!response.ok) {
        return { ok: false, message: '服务端返回 HTTP ' + response.status };
      }
      const result = await response.json();
      const sources = result.sources || [];
      const playing = sources.filter((item) => item.fresh && item.playing);
      let message = '连接正常：' + base;
      if (playing.length) {
        message += '（正在播放：' + playing.map((item) => item.title).join('、') + '）';
      } else if (sources.length) {
        message += '（暂时没有人在放歌）';
      }
      return { ok: true, message: message, sources: sources };
    } catch (err) {
      return { ok: false, message: '连不上：' + ((err && err.message) || '网络不可达') };
    }
  });

  ipcMain.on('settings:close', () => {
    if (settingsWin && !settingsWin.isDestroyed()) {
      settingsWin.close();
    }
  });

  ipcMain.on('settings:open-readme', () => openReadme());
}

/* ---------------------------------------------------------------------------
   启动
   --------------------------------------------------------------------------- */

// 只允许一个实例：两个悬浮窗会显示同一行字，用户还得猜该点哪个
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    openSettings();
  });

  app.whenReady().then(() => {
    app.setAppUserModelId('com.remotedesktop.desktoplyrics');

    createOverlay();
    createClient().start();
    registerIpc();

    tray = new Tray(trayImage());
    tray.setToolTip('桌面歌词');
    tray.on('click', () => openSettings());        // 左键点托盘 = 打开设置
    updateTray();

    // 让托盘里的「开机自启」反映注册表里的真实状态（可能是别处设的）
    try {
      const settings = app.getLoginItemSettings();
      if (settings.openAtLogin !== store.load().autoStart) {
        store.save({ autoStart: settings.openAtLogin });
        updateTray();
      }
    } catch (err) { /* 读不到就按配置里的值显示 */ }

    refreshSources();

    // 首次运行（还没有服务器地址）：直接把设置窗口弹出来，别让用户对着
    // 一个永远不动的托盘图标猜。
    if (!store.load().serverUrl) {
      openSettings();
    }
  });

  // 关掉所有窗口不等于退出：这个程序是「托盘常驻」的
  app.on('window-all-closed', () => {});
}

'use strict';
/* ==========================================================================
   悬浮窗的本地配置
   --------------------------------------------------------------------------
   存在 Electron 的 userData 目录里（Windows 上大致是
   %APPDATA%\桌面歌词\config.json），**不写进项目目录** —— 安装到 Program Files
   之后那里是只读的，而且每台机器的服务器地址本来就不一样。

   写入用「先写临时文件再改名」：改名在同一分区上是原子的，这样即使写到一半
   断电（实验室机器直接拔电是常态），也不会留下一个半截的 JSON 把配置弄丢。
   ========================================================================== */

const fs = require('fs');
const path = require('path');
const { app } = require('electron');

const FILE_NAME = 'config.json';

const DEFAULTS = {
  // 服务端地址，例如 http://192.168.1.10:8000（留空 = 首次运行，会弹出设置）
  serverUrl: '',
  // 只订阅这个用户（空 = 订阅所有人；多用户时建议填自己的用户名）
  user: '',
  // 预留的鉴权扩展点：服务端 config.json 里 lyrics.token 配了值才需要填
  token: '',
  // 悬浮窗位置。null = 还没定过，首次运行时自动放到底部居中
  x: null,
  y: null,
  width: 1100,
  height: 150,
  // 鼠标穿透：默认开 —— 平时它只是一行字，不该挡住鼠标
  clickThrough: true,
  // 锁定位置：锁定后拖不动（防止玩游戏时误拖）
  locked: false,
  fontSize: 40,
  // 底部那条细进度条（默认关：用户要的是「单行大字」）
  showProgress: false,
  // 用户手动隐藏（托盘里的「显示歌词」）
  hidden: false,
  autoStart: false
};

/** 数值类字段的取值范围：防止手改配置文件写出一个看不见的窗口。 */
const RANGES = {
  width: [320, 4000],
  height: [60, 1200],
  fontSize: [16, 120]
};

let cache = null;

function filePath() {
  return path.join(app.getPath('userData'), FILE_NAME);
}

function clampNumber(value, fallback, range) {
  const number = Number(value);
  if (!isFinite(number)) {
    return fallback;
  }
  const [low, high] = range;
  return Math.min(high, Math.max(low, Math.round(number)));
}

/** 把任意输入规整成一份可用的配置（坏值一律退回默认，绝不让它抛错）。 */
function normalize(raw) {
  const source = raw && typeof raw === 'object' ? raw : {};
  const out = {};
  for (const key of Object.keys(DEFAULTS)) {
    out[key] = Object.prototype.hasOwnProperty.call(source, key)
      ? source[key]
      : DEFAULTS[key];
  }

  out.serverUrl = String(out.serverUrl || '').trim();
  out.user = String(out.user || '').trim();
  out.token = String(out.token || '').trim();
  out.clickThrough = out.clickThrough !== false;
  out.locked = out.locked === true;
  out.hidden = out.hidden === true;
  out.autoStart = out.autoStart === true;
  out.showProgress = out.showProgress === true;
  out.width = clampNumber(out.width, DEFAULTS.width, RANGES.width);
  out.height = clampNumber(out.height, DEFAULTS.height, RANGES.height);
  out.fontSize = clampNumber(out.fontSize, DEFAULTS.fontSize, RANGES.fontSize);

  for (const key of ['x', 'y']) {
    const number = Number(out[key]);
    // 位置允许为 null（表示「还没定过」）；有值时必须是个像样的整数
    out[key] = isFinite(number) && out[key] !== null && out[key] !== ''
      ? Math.round(number)
      : null;
  }
  return out;
}

function load() {
  if (cache) {
    return cache;
  }
  let raw = null;
  try {
    raw = JSON.parse(fs.readFileSync(filePath(), 'utf8'));
  } catch (err) {
    // 文件不存在（首次运行）或内容坏了：都用默认值，不打扰用户
    raw = null;
  }
  cache = normalize(raw);
  return cache;
}

function save(patch) {
  const next = normalize(Object.assign({}, load(), patch || {}));
  cache = next;
  const target = filePath();
  const temp = target + '.tmp';
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
    fs.writeFileSync(temp, JSON.stringify(next, null, 2), 'utf8');
    fs.renameSync(temp, target);
  } catch (err) {
    // 存不下来不该让程序崩掉：内存里的配置仍然生效，只是下次启动会丢
    console.error('[配置] 保存失败：', err && err.message);
  }
  return next;
}

module.exports = { DEFAULTS, load, save, normalize, filePath };

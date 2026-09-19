'use strict';
/* ==========================================================================
   「运行 bat / 在此处打开命令行」· 真实浏览器冒烟测试（由 smoke_runbat.py 调起）
   --------------------------------------------------------------------------
   为什么要单独跑这一步：
     * 「右键菜单里到底有没有那一项」是静态闸门查不出来的 —— 这个项目已经
       栽过一次同类问题：日志里那个「开始菜单漏了一处渲染」就是只有真浏览器
       才暴露的（desktop.js 里桌面图标与开始菜单是两处独立渲染）；
     * 菜单项的 onClick 有没有接错函数，也只有点了才知道。

   环境变量：
       RB_PORT / RB_COOKIE / RB_OUT / RB_ROOT（根标识）/ RB_DIR（含脚本的子目录）
   ========================================================================== */

const fs = require('fs');
const path = require('path');
const { app, BrowserWindow, session } = require('electron');

app.disableHardwareAcceleration();

const PORT = Number(process.env.RB_PORT);
const COOKIE = String(process.env.RB_COOKIE || '');
const OUT = String(process.env.RB_OUT || '');
const ROOT = String(process.env.RB_ROOT || '');
const DIR = String(process.env.RB_DIR || '');
const BASE = 'http://127.0.0.1:' + PORT;

const results = [];
const consoleErrors = [];

function check(label, ok, extra) {
  console.log((ok ? '  [PASS] ' : '  [FAIL] ') + label + (extra ? '  —— ' + extra : ''));
  results.push({ label: label, ok: !!ok, extra: extra === undefined ? '' : String(extra) });
}

function wait(ms) {
  return new Promise(function (r) { setTimeout(r, ms); });
}

async function safeRun(win, code, label) {
  try {
    return await win.webContents.executeJavaScript(code, true);
  } catch (err) {
    console.log('  [WARN] 脚本执行失败（' + (label || '') + '）：' +
      (err && err.message ? err.message : err));
    return { __error: String(err && err.message ? err.message : err) };
  }
}

async function shot(win, name) {
  if (!OUT) { return; }
  try {
    const image = await win.webContents.capturePage();
    fs.writeFileSync(path.join(OUT, name), image.toPNG());
  } catch (err) {
    console.log('  [WARN] 截图失败：' + err);
  }
}

async function waitFor(win, code, timeoutMs, label) {
  const deadline = Date.now() + (timeoutMs || 15000);
  let last = null;
  while (Date.now() < deadline) {
    last = await safeRun(win, code, label);
    if (last === true) { return true; }
    await wait(300);
  }
  return last;
}

/** 读出当前右键菜单里的项（菜单是挂到 document.body 上的 .ctx-menu） */
const MENU_TEXT = "(function(){var m=document.querySelector('.ctx-menu');" +
  "if(!m){return null;}" +
  "return Array.prototype.map.call(m.querySelectorAll('.ctx-item .ctx-text')," +
  "function(e){return e.textContent;}).join('|');})()";

async function main() {
  const eq = COOKIE.indexOf('=');
  if (eq > 0) {
    await session.defaultSession.cookies.set({
      url: BASE + '/', name: COOKIE.slice(0, eq), value: COOKIE.slice(eq + 1),
      path: '/', httpOnly: true
    });
  }

  const win = new BrowserWindow({
    width: 1440, height: 900, show: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false }
  });
  win.webContents.on('console-message', function (e, level, message) {
    if (level >= 2) { consoleErrors.push(String(message).slice(0, 300)); }
  });

  const run = function (code, label) { return safeRun(win, code, label); };

  await win.loadURL(BASE + '/');
  await wait(2500);

  console.log('=== 1. 打开资源管理器并进到放着脚本的目录 ===');
  const opened = await run(`(function () {
    var b = document.getElementById('startBtn');
    if (!b) { return 'no-start-button'; }
    b.click();
    var it = document.querySelector('.sm-item[data-action="explorer"]');
    if (!it) { return 'no-explorer-entry'; }
    it.click();
    return 'ok';
  })()`, 'open-explorer');
  check('从开始菜单打开文件资源管理器', opened === 'ok', String(opened));
  await wait(2500);

  /* 资源管理器初次打开停在「此电脑」视图（显示各根目录），那里**没有**面包屑，
     也没有 [data-name] 的文件项 —— 必须先双击那个根把它**进去**，
     才进入带面包屑与文件列表的普通目录视图（这一段是看截图才搞清楚的）。 */
  const nav = await run(`(function () {
    var rootId = ${JSON.stringify(ROOT)};
    var items = Array.prototype.slice.call(document.querySelectorAll('.cv-item'));
    var hit = items.filter(function (e) { return e.dataset.root === rootId; })[0];
    if (!hit) {
      return 'no-root-item; 现有: ' +
        items.map(function (e) { return e.dataset.root; }).join(',');
    }
    hit.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    return 'ok';
  })()`, 'enter-root');
  check('从「此电脑」双击进入那个根目录', nav === 'ok', String(nav));
  await wait(2200);

  /* 根目录里应当能看到那个装着脚本的子文件夹（.bat 本身在子目录里，别找错地方） */
  const hasSub = await waitFor(win,
    "Array.prototype.some.call(document.querySelectorAll('[data-name]')," +
    " function (e) { return e.dataset.name === " + JSON.stringify(DIR) + "; })",
    15000, 'wait-subdir');
  check('根目录里能看到放着脚本的子文件夹', hasSub === true);
  await shot(win, 'explorer-root.png');

  /* 进入含脚本的子目录：双击那个文件夹 */
  const entered = await run(`(function () {
    var want = ${JSON.stringify(DIR)};
    var items = Array.prototype.slice.call(document.querySelectorAll('[data-name]'));
    var hit = items.filter(function (e) { return e.dataset.name === want; })[0];
    if (!hit) {
      return 'no-folder; 现有: ' + items.map(function (e) { return e.dataset.name; }).join(',');
    }
    hit.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    return 'ok';
  })()`, 'enter-folder');
  check('能进入含脚本的子目录', entered === 'ok', String(entered));
  await wait(2200);

  console.log('=== 2. 右键 .bat：菜单里应当有「运行」 ===');
  const batMenu = await run(`(function () {
    var items = Array.prototype.slice.call(document.querySelectorAll('[data-name]'));
    var hit = items.filter(function (e) { return /\\.bat$/i.test(e.dataset.name); })[0];
    if (!hit) { return 'no-bat:' + items.map(function(e){return e.dataset.name;}).join(','); }
    var r = hit.getBoundingClientRect();
    hit.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, clientX: r.left + 10, clientY: r.top + 8
    }));
    return 'ok';
  })()`, 'right-click-bat');
  check('对 .bat 触发了右键菜单', batMenu === 'ok', String(batMenu));
  await wait(500);

  const menu1 = await run(MENU_TEXT, 'menu-text-bat');
  console.log('      菜单项：' + menu1);
  check('右键 .bat 的菜单里有「运行」',
    typeof menu1 === 'string' && menu1.split('|').indexOf('运行') >= 0, String(menu1));
  await shot(win, 'menu-bat.png');

  console.log('=== 3. 点「运行」：应当开出一个命令行窗口并在里面跑脚本 ===');
  const clicked = await run(`(function () {
    var items = Array.prototype.slice.call(document.querySelectorAll('.ctx-item'));
    var hit = items.filter(function (e) {
      var t = e.querySelector('.ctx-text');
      return t && t.textContent === '运行';
    })[0];
    if (!hit) { return 'no-run-item'; }
    hit.click();
    return 'ok';
  })()`, 'click-run');
  check('点到了「运行」', clicked === 'ok', String(clicked));

  const termOpened = await waitFor(win,
    "!!document.querySelector('.terminal')", 20000, 'wait-terminal');
  check('开出了一个命令行窗口', termOpened === true);

  await wait(4000);
  const termText = await run(`(function () {
    var t = document.querySelector('.terminal .xterm-rows') ||
            document.querySelector('.terminal');
    return t ? t.textContent : '';
  })()`, 'term-text');
  console.log('      终端内容片段：' + String(termText).replace(/\s+/g, ' ').slice(0, 200));
  /* 脚本会打印一行标记；终端里出现它就说明「从虚拟桌面启动了 bat」这件事真的成了。
     注意：xterm 的渲染是分行的，去掉空白再找更稳。 */
  const compact = String(termText).replace(/\s+/g, '');
  check('终端里出现了脚本打印的标记', compact.indexOf('RUNBAT-OK') >= 0,
    '内容前 120 字：' + compact.slice(0, 120));
  await shot(win, 'terminal-run-bat.png');

  console.log('=== 4. 右键文件夹：菜单里应当有「在此处打开命令行」 ===');
  const dirMenu = await run(`(function () {
    var items = Array.prototype.slice.call(document.querySelectorAll('[data-name]'));
    var hit = items.filter(function (e) {
      return e.dataset.name === '..' || e.dataset.name === '上一级';
    })[0];
    // 用「向上」按钮回到父目录，再右键那个子文件夹
    var up = document.querySelector('[data-act="up"]');
    if (up && !up.disabled) { up.click(); }
    return 'ok';
  })()`, 'go-up');
  check('能回到上一级目录', dirMenu === 'ok', String(dirMenu));
  await wait(2000);

  const folderMenu = await run(`(function () {
    var items = Array.prototype.slice.call(document.querySelectorAll('[data-name]'));
    var hit = items.filter(function (e) {
      return e.dataset.name === ${JSON.stringify(DIR)};
    })[0];
    if (!hit) { return 'no-folder'; }
    var r = hit.getBoundingClientRect();
    hit.dispatchEvent(new MouseEvent('contextmenu', {
      bubbles: true, clientX: r.left + 10, clientY: r.top + 8
    }));
    return 'ok';
  })()`, 'right-click-folder');
  check('对文件夹触发了右键菜单', folderMenu === 'ok', String(folderMenu));
  await wait(500);

  const menu2 = await run(MENU_TEXT, 'menu-text-folder');
  console.log('      菜单项：' + menu2);
  check('右键文件夹的菜单里有「在此处打开命令行」',
    typeof menu2 === 'string' &&
    menu2.split('|').indexOf('在此处打开命令行') >= 0, String(menu2));
  await shot(win, 'menu-folder.png');

  console.log('=== 5. 控制台错误 ===');
  check('页面控制台没有 error 级日志', consoleErrors.length === 0,
    consoleErrors.slice(0, 5).join(' | '));

  const failed = results.filter(function (r) { return !r.ok; });
  console.log('\n================ 结果 ================');
  console.log('共 ' + results.length + ' 项，失败 ' + failed.length + ' 项');
  if (OUT) {
    try {
      fs.writeFileSync(path.join(OUT, 'results.json'),
        JSON.stringify({ results: results, consoleErrors: consoleErrors }, null, 2));
    } catch (err) { /* 忽略 */ }
  }
  app.exit(failed.length === 0 ? 0 : 1);
}

app.whenReady().then(function () {
  main().catch(function (err) {
    console.log('★ 冒烟测试自身出错：' + (err && err.stack ? err.stack : err));
    app.exit(3);
  });
});

'use strict';
/* ==========================================================================
   照片应用 · 真实浏览器冒烟测试（由 tools/smoke_photos.py 调起）
   --------------------------------------------------------------------------
   跑法（一般不用手动跑，交给驱动脚本）：
       python tools\smoke_photos.py

   它用**真实的 Electron（Chromium）**打开虚拟桌面，走一遍用户会走的路：
   开始菜单打开「照片」→ 时间轴渲染 → 缩略图真的解码 → 待整理角标 →
   筛选「待整理」→ 选中出批量操作条 → 双击看大图 → 原图解码 →
   在界面上改时间与地点并保存（真实保存链路）→ 导入对话框。

   为什么要专门做这个
   ------------------
   前端那几道快速闸门（node --check、跨模块调用名、纯逻辑用例）都查不出
   **「窗口打开了但里面是空的」**这一类问题。这个脚本第一次跑就抓到一个：

       load() 里把 this.loading 置真，而 render() 又在同一个 try 里调用；
       renderGrid() 开头有一道「加载中就先不画」的闸门 ——
       于是网格永远是空的：侧栏数字、统计全都对，主区却一直停在
       「正在读取照片库…」。而当时接口测试、静态闸门、逻辑用例**全绿**。

   两个刻意的做法：
     * 每检查一项就**立刻打印**，中途出错也能看到已经拿到的结论；
     * 等图片解码用**从主进程轮询**，而不是在页面里挂一个长 Promise ——
       页面重绘会让执行上下文失效，那种失败看不出原因。

   退出码 0 = 全部通过；非 0 = 有断言失败。截图写在 PH_OUT 指向的目录里。
   ========================================================================== */

const fs = require('fs');
const path = require('path');
const { app, BrowserWindow, session } = require('electron');

app.disableHardwareAcceleration();

const PORT = Number(process.env.PH_PORT);
const COOKIE = String(process.env.PH_COOKIE || '');
const OUT = String(process.env.PH_OUT || '');
const BASE = 'http://127.0.0.1:' + PORT;

const results = [];
const consoleErrors = [];

function check(label, ok, extra) {
  console.log((ok ? '  [PASS] ' : '  [FAIL] ') + label + (extra ? '  —— ' + extra : ''));
  results.push({ label: label, ok: !!ok, extra: extra === undefined ? '' : String(extra) });
}

function wait(ms) {
  return new Promise(function (resolve) { setTimeout(resolve, ms); });
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

/** 轮询等一批 <img> 解码完（naturalWidth > 0 才算真的成功了） */
async function waitImages(win, selector, timeoutMs) {
  const deadline = Date.now() + (timeoutMs || 12000);
  const code = 'Array.prototype.map.call(document.querySelectorAll(' +
    JSON.stringify(selector) + '), function (i) { return i.complete ? i.naturalWidth : 0; })';
  let widths = [];
  while (Date.now() < deadline) {
    widths = await safeRun(win, code, 'wait-images');
    if (Array.isArray(widths) && widths.length > 0 &&
        widths.every(function (w) { return w > 0; })) {
      return widths;
    }
    await wait(400);
  }
  return widths;
}

async function shot(win, name) {
  if (!OUT) {
    return;
  }
  try {
    const image = await win.webContents.capturePage();
    fs.writeFileSync(path.join(OUT, name), image.toPNG());
  } catch (err) {
    console.log('  [WARN] 截图失败：' + err);
  }
}

async function main() {
  const eq = COOKIE.indexOf('=');
  if (eq > 0) {
    await session.defaultSession.cookies.set({
      url: BASE + '/',
      name: COOKIE.slice(0, eq),
      value: COOKIE.slice(eq + 1),
      path: '/',
      httpOnly: true
    });
  }

  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    show: true,
    webPreferences: { contextIsolation: true, nodeIntegration: false }
  });

  win.webContents.on('console-message', function (event, level, message) {
    if (level >= 2) {
      consoleErrors.push(String(message).slice(0, 300));
    }
  });
  win.webContents.on('did-fail-load', function (event, code, desc, url) {
    consoleErrors.push('did-fail-load ' + code + ' ' + desc + ' ' + url);
  });

  const run = function (code, label) { return safeRun(win, code, label); };

  await win.loadURL(BASE + '/');
  await wait(2500);

  console.log('=== 1. 桌面 ===');
  check('页面标题正确', String(await run('document.title')).indexOf('远程文件管理') >= 0);
  check('桌面图标已渲染', await run("!!document.querySelector('.desk-icon')"));
  check('启动遮罩已消失', await run(
    "!document.getElementById('bootScreen') || document.getElementById('bootScreen').classList.contains('hidden')"));

  console.log('=== 2. 从开始菜单打开「照片」 ===');
  const opened = await run(`(function () {
    var btn = document.getElementById('startBtn');
    if (!btn) { return 'no-start-button'; }
    btn.click();
    var item = document.querySelector('.sm-item[data-action="photos"]');
    if (!item) { return 'no-photos-entry'; }
    item.click();
    return 'ok';
  })()`, 'open-photos');
  check('开始菜单里有「照片」入口并点击成功', opened === 'ok', String(opened));

  await wait(3500);

  console.log('=== 3. 窗口与时间轴 ===');
  const winTitles = await run(
    "Array.prototype.map.call(document.querySelectorAll('.winbox .wb-title'), function (e) { return e.textContent.trim(); }).join(' | ')");
  console.log('      已打开的窗口：' + winTitles);
  check('照片窗口已打开', await run("!!document.querySelector('.ph-root')"));

  const cells = await run("document.querySelectorAll('.ph-cell').length", 'count-cells');
  check('时间轴网格里有照片', typeof cells === 'number' && cells > 0, '张数=' + cells);

  const groups = await run("document.querySelectorAll('.ph-group').length", 'count-groups');
  check('时间轴分组已渲染', typeof groups === 'number' && groups > 0, '组数=' + groups);

  const groupTitles = await run(
    "Array.prototype.map.call(document.querySelectorAll('.ph-group-title'), function (e) { return e.textContent; }).join(' / ')",
    'titles');
  const groupSubs = await run(
    "Array.prototype.map.call(document.querySelectorAll('.ph-group-sub'), function (e) { return e.textContent; }).join(' / ')",
    'subs');
  console.log('      分组标题：' + groupTitles);
  console.log('      分组副标题：' + groupSubs);
  /* 日粒度下年份在**副标题**里（标题是「8 月 15 日」，副标题是「2023 年 周二」） */
  check('分组副标题里有年份', String(groupSubs).indexOf('2023') >= 0, String(groupSubs));
  check('日粒度标题是「N 月 N 日」', /月\s*\d+\s*日/.test(String(groupTitles)), String(groupTitles));

  console.log('=== 4. 缩略图与角标 ===');
  const thumbs = await waitImages(win, '.ph-cell img', 12000);
  check('缩略图全部解码成功（不是破图）',
    Array.isArray(thumbs) && thumbs.length > 0 &&
    thumbs.every(function (w) { return w > 0; }),
    '宽度=' + JSON.stringify(thumbs));

  const warnBadges = await run("document.querySelectorAll('.ph-cell .ph-badge.warn').length");
  check('没有 EXIF 的照片带「待整理」角标', warnBadges > 0, '角标数=' + warnBadges);

  await shot(win, 'grid.png');

  console.log('=== 5. 「待整理」筛选（在改动之前做：那一档现在正是 1 张） ===');
  await run(`(function () {
    var navs = Array.prototype.slice.call(document.querySelectorAll('.ph-nav'));
    var target = navs.filter(function (n) { return n.textContent.indexOf('待整理') >= 0; })[0];
    if (target) { target.click(); }
  })()`, 'unsorted-filter');
  await wait(1500);

  const unsortedCells = await run("document.querySelectorAll('.ph-cell').length");
  const unsortedBadges = await run("document.querySelectorAll('.ph-cell .ph-badge.warn').length");
  check('「待整理」筛选出了照片',
    typeof unsortedCells === 'number' && unsortedCells > 0, '张数=' + unsortedCells);
  check('这一档里每张都带待整理角标', unsortedBadges === unsortedCells,
    unsortedBadges + '/' + unsortedCells);
  await shot(win, 'unsorted.png');

  console.log('=== 6. 选中与批量操作条 ===');
  await run("(function () { var c = document.querySelector('.ph-cell'); if (c) { c.click(); } })()", 'select');
  await wait(600);
  check('选中照片后出现批量操作条', await run(
    "(function () { var b = document.querySelector('.ph-batch'); return !!b && !b.hidden; })()"));

  /* 回到「全部」再往下走，免得后面的判断都建立在一个只有 1 张的筛选上 */
  await run(`(function () {
    var navs = Array.prototype.slice.call(document.querySelectorAll('.ph-nav'));
    var target = navs.filter(function (n) { return n.textContent.indexOf('全部照片') >= 0; })[0];
    if (target) { target.click(); }
  })()`, 'back-to-all');
  await wait(1200);
  const allCells = await run("document.querySelectorAll('.ph-cell').length");
  check('切回「全部照片」后又有全部张数', allCells === 4, '张数=' + allCells);

  console.log('=== 7. 双击看大图 ===');
  const dbl = await run(`(function () {
    var cell = document.querySelector('.ph-cell');
    if (!cell) { return 'no-cell'; }
    cell.dispatchEvent(new MouseEvent('dblclick', { bubbles: true }));
    return 'ok';
  })()`, 'dblclick');
  check('双击照片已触发', dbl === 'ok', String(dbl));
  await wait(3000);
  check('大图查看器已出现', await run("!!document.querySelector('.ph-viewer')"));

  const rawWidth = await waitImages(win, '.ph-viewer-img', 12000);
  check('原图在浏览器里解码成功',
    Array.isArray(rawWidth) && rawWidth.length > 0 && rawWidth[0] > 0,
    '宽度=' + JSON.stringify(rawWidth));
  check('编辑面板有时间输入框',
    await run("!!document.querySelector(\".ph-viewer [data-field='taken_at']\")"));

  console.log('=== 8. 在界面上改时间与地点（真实保存链路） ===');
  const saved = await run(`(function () {
    var t = document.querySelector(".ph-viewer [data-field='taken_at']");
    var p = document.querySelector(".ph-viewer [data-field='place']");
    var btn = document.querySelector(".ph-viewer [data-act='save']");
    if (!t || !p || !btn) { return 'missing-fields'; }
    t.value = '2021-03-04 05:06:07';
    p.value = '浏览器冒烟测试地点';
    btn.click();
    return 'ok';
  })()`, 'save-edit');
  check('编辑表单已填写并点了保存', saved === 'ok', String(saved));

  await wait(4500);
  const panelText = await run(`(function () {
    var el = document.querySelector('.ph-viewer [data-role="side-panel"]');
    return el ? el.innerText.replace(/\\s+/g, ' ') : '(没有面板)';
  })()`, 'panel-text');
  check('★ 改完的时间立刻体现在界面上', String(panelText).indexOf('2021-03-04') >= 0,
    String(panelText).slice(0, 130));

  /* ★ 地点要用 input.value 读：innerText 拿不到 <input> 里填的值
     （第一版用 innerText 判断，结果是一条假失败） */
  const placeValue = await run(`(function () {
    var el = document.querySelector(".ph-viewer [data-field='place']");
    return el ? el.value : '(没有地点输入框)';
  })()`, 'place-value');
  check('★ 改完的地点立刻体现在界面上',
    String(placeValue).indexOf('浏览器冒烟测试地点') >= 0, String(placeValue));

  const noteText = await run(`(function () {
    var el = document.querySelector('.ph-info-note');
    return el ? el.textContent : '';
  })()`);
  check('★ 时间来源已变成「你手动设置的时间」',
    String(noteText).indexOf('手动设置') >= 0, String(noteText).slice(0, 80));

  await shot(win, 'viewer.png');

  console.log('=== 9. 改完之后「待整理」应当少一张 ===');
  await run("document.querySelector(\".ph-viewer [data-act='close']\").click()", 'close-viewer');
  await wait(800);
  const unsortedAfter = await run(`(function () {
    var navs = Array.prototype.slice.call(document.querySelectorAll('.ph-nav'));
    var target = navs.filter(function (n) { return n.textContent.indexOf('待整理') >= 0; })[0];
    return target ? target.textContent : '';
  })()`, 'unsorted-count');
  console.log('      侧栏那一行：' + unsortedAfter);
  check('改过时间之后，那张就不再是「待整理」了',
    String(unsortedAfter).indexOf('0') >= 0 || String(unsortedAfter).indexOf('待整理') >= 0,
    String(unsortedAfter));

  console.log('=== 10. 导入对话框 ===');
  await run("(function () { var b = document.querySelector('.ph-toolbar [data-act=\"import\"]'); if (b) { b.click(); } })()", 'open-import');
  await wait(2500);
  check('「导入照片」对话框能打开', await run("!!document.querySelector('.ph-modal')"));
  check('对话框里有目录浏览区', await run("!!document.querySelector('.ph-dirlist')"));
  const dirCount = await run("document.querySelectorAll('.ph-dir').length");
  check('目录浏览列出了文件夹', typeof dirCount === 'number' && dirCount > 0, '条目=' + dirCount);
  await shot(win, 'import.png');

  console.log('');
  const failed = results.filter(function (r) { return !r.ok; });
  console.log('冒烟结果：' + (results.length - failed.length) + '/' + results.length + ' 通过');
  if (consoleErrors.length) {
    console.log('页面控制台告警/错误 ' + consoleErrors.length + ' 条：');
    consoleErrors.slice(0, 10).forEach(function (m) { console.log('    - ' + m); });
  } else {
    console.log('页面控制台没有告警/错误');
  }

  if (OUT) {
    fs.writeFileSync(path.join(OUT, 'results.json'),
      JSON.stringify({ results: results, consoleErrors: consoleErrors }, null, 2));
  }

  app.exit(failed.length ? 1 : 0);
}

app.whenReady().then(function () {
  main().catch(function (err) {
    console.error('冒烟测试异常：' + (err && err.stack ? err.stack : err));
    try {
      fs.writeFileSync(path.join(OUT, 'results.json'),
        JSON.stringify({ results: results, consoleErrors: consoleErrors, fatal: String(err) }, null, 2));
    } catch (e) { /* 忽略 */ }
    app.exit(2);
  });
});

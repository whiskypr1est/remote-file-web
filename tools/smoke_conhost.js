'use strict';
/* ==========================================================================
   控制台镜像 · 真实浏览器冒烟测试（由 tools/smoke_conhost.py 调起）
   --------------------------------------------------------------------------
   跑法（一般不用手动跑，交给驱动脚本）：
       python tools\smoke_conhost.py

   它用**真实的 Electron（Chromium）**打开虚拟桌面，走一遍用户会走的路：
   开始菜单打开「控制台镜像」→ 列表渲染出真实控制台 → 点一个 →
   内容区真的画出字符栅格（含中文、不重复）→ 只读模式下没有输入区。

   为什么必须做这一步
   ------------------
   前端那几道快速闸门（node --check、跨模块调用名、纯逻辑用例）都查不出
   **「窗口打开了但里面是空的」**。这个项目已经栽过一次：照片网格因为一个
   loading 闸门永远画不出来，而当时接口测试、静态闸门、逻辑用例全绿 ——
   那次正是这个冒烟脚本第一次跑就抓到的。

   环境变量：
       CH_PORT    服务端口
       CH_COOKIE  管理员的会话 Cookie（"fw_session=…"）
       CH_OUT     截图输出目录
       CH_PID     期望在列表里找到的那个控制台进程 pid（确定性目标）
       CH_MARK    期望在内容里读到的标记文本
   ========================================================================== */

const fs = require('fs');
const path = require('path');
const { app, BrowserWindow, session } = require('electron');

app.disableHardwareAcceleration();

const PORT = Number(process.env.CH_PORT);
const COOKIE = String(process.env.CH_COOKIE || '');
const OUT = String(process.env.CH_OUT || '');
const TARGET_PID = String(process.env.CH_PID || '');
const MARK = String(process.env.CH_MARK || '');
const INPUT_MODE = String(process.env.CH_INPUT || '') === '1';
const BASE = 'http://127.0.0.1:' + PORT;

const results = [];
const consoleErrors = [];

function check(label, ok, extra) {
  console.log((ok ? '  [PASS] ' : '  [FAIL] ') + label +
    (extra ? '  —— ' + extra : ''));
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

/** 轮询直到条件为真（从主进程轮询，而不是在页面里挂长 Promise） */
async function waitFor(win, code, timeoutMs, label) {
  const deadline = Date.now() + (timeoutMs || 15000);
  let last = null;
  while (Date.now() < deadline) {
    last = await safeRun(win, code, label);
    if (last === true) {
      return true;
    }
    await wait(350);
  }
  return last;
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

  console.log('=== 2. 开始菜单里应当有「控制台镜像」（管理员才看得到） ===');
  const entry = await run(`(function () {
    var btn = document.getElementById('startBtn');
    if (!btn) { return 'no-start-button'; }
    btn.click();
    var item = document.querySelector('.sm-item[data-action="conhost"]');
    if (!item) {
      var all = Array.prototype.map.call(document.querySelectorAll('.sm-item'),
        function (e) { return e.getAttribute('data-action'); }).join(',');
      return 'no-conhost-entry; 现有: ' + all;
    }
    item.click();
    return 'ok';
  })()`, 'open-conhost');
  check('开始菜单里有「控制台镜像」并点击成功', entry === 'ok', String(entry));

  console.log('=== 3. 窗口打开且**不是空白** ===');
  check('镜像窗口已打开',
    await run("!!document.querySelector('.conhost')", 'has-root'));

  const listed = await waitFor(win,
    "document.querySelectorAll('.conhost-item').length > 0", 20000, 'wait-list');
  const itemCount = await run("document.querySelectorAll('.conhost-item').length");
  check('控制台列表渲染出了条目', listed === true, '条目数=' + itemCount);

  const listInfo = await run(
    "(document.querySelector('.conhost .pv-label') || {}).textContent || ''");
  console.log('      列表摘要：' + listInfo);
  check('列表摘要里报告了控制台数量',
    /共\s*\d+\s*个控制台/.test(String(listInfo)), String(listInfo));
  await shot(win, 'conhost-list.png');

  console.log('=== 4. 选中那个确定性目标（pid=' + TARGET_PID + '） ===');
  /* 用 JSON.stringify 拼进页面脚本：pid 是外部传入的，直接拼字符串容易出
     引号/转义问题（而且模板字符串里再嵌模板字符串会直接把脚本撕开）。 */
  const pidNeedle = JSON.stringify('pid ' + TARGET_PID);
  const picked = await run(`(function () {
    var items = Array.prototype.slice.call(document.querySelectorAll('.conhost-item'));
    var hit = items.filter(function (n) {
      return n.textContent.indexOf(${pidNeedle}) >= 0;
    })[0];
    if (!hit) {
      var all = items.map(function (n) {
        var m = n.querySelector('.conhost-item-meta');
        return m ? m.textContent : '?';
      }).join(' / ');
      return 'not-found; 现有: ' + all;
    }
    hit.click();
    return 'ok';
  })()`, 'select-target');
  check('在列表里找到了目标控制台并点击', picked === 'ok', String(picked));

  console.log('=== 5. 内容区真的画出了字符栅格 ===');
  /* ★ 必须等到**目标那个控制台的标记**出现，不能只等「有行」——
     列表默认会先选中第一个控制台并渲染出内容，所以「有行」在点击之前
     就已经为真了；只等这个条件会立刻通过，然后读到的是**上一个**控制台
     的内容（第一次跑就是这么误判的）。 */
  const markNeedle = JSON.stringify(MARK);
  const gotMark = await waitFor(win,
    "(document.querySelector('.conhost-view') || {}).textContent.indexOf(" +
    markNeedle + ") >= 0", 30000, 'wait-marker');
  const lineCount = await run("document.querySelectorAll('.conhost-line').length");
  check('内容区有渲染出来的行', typeof lineCount === 'number' && lineCount > 0,
    '行数=' + lineCount);

  const text = await run(
    "(document.querySelector('.conhost-view') || {}).textContent || ''", 'view-text');
  console.log('      内容片段：' + String(text).slice(0, 240).replace(/\s+/g, ' '));
  check('内容里包含目标控制台打印的标记「' + MARK + '」', gotMark === true,
    '前 120 字：' + String(text).slice(0, 120).replace(/\s+/g, ' '));
  /* 中文重复是已知坑：全角字符尾格不跳过的话会变成「本本机机」。
     服务端已经处理，这里用「连续两个相同汉字」做粗筛。 */
  check('中文没有出现「每个字重复两遍」',
    !/([\u4e00-\u9fa5])\1/.test(String(text)));

  console.log('=== 6. 输入区的出现与否必须与 allow_input 一致 ===');
  const inputVisible = await run(`(function () {
    var box = document.querySelector('.conhost-input');
    if (!box) { return false; }
    return box.style.display !== 'none' &&
      box.querySelectorAll('input,button').length > 0;
  })()`, 'input-area');
  /* 这一条是**双向**的，比只测一边有价值：
       只读配置下必须没有输入控件（否则界面在骗人 —— 让人以为能打字）；
       可写配置下必须有（否则功能被藏起来了，用户找不到入口）。 */
  check(INPUT_MODE
    ? '开启 allow_input 时应当出现输入区'
    : '未开启 allow_input 时没有输入区（纯只读）',
    inputVisible === INPUT_MODE, 'inputVisible=' + inputVisible);

  await shot(win, 'conhost-content.png');

  console.log('=== 7. 切到「屏幕」视图仍然有内容（且状态栏确认已切换） ===');
  const toggled = await run(`(function () {
    var btns = Array.prototype.slice.call(document.querySelectorAll('.conhost .pv-btn'));
    var b = btns.filter(function (n) { return n.textContent.indexOf('视图') >= 0; })[0];
    if (!b) { return 'no-mode-button'; }
    b.click();
    return b.textContent;
  })()`, 'switch-mode');
  check('找到了视图切换按钮并点击', String(toggled).indexOf('屏幕') >= 0,
    String(toggled));

  /* 等状态栏**自己**说切到屏幕视图了 —— 这比固定 sleep 稳：
     它确认的是「服务端按新模式返回并渲染完成」，而不是「大概过了两秒」。 */
  const screenOk = await waitFor(win,
    "(function () { var s = document.querySelector('.conhost-status');" +
    " return !!s && s.textContent.indexOf('屏幕视图') >= 0; })()",
    25000, 'wait-screen-mode');
  const screenLines = await run("document.querySelectorAll('.conhost-line').length");
  check('屏幕视图已生效且渲染出了行',
    screenOk === true && typeof screenLines === 'number' && screenLines > 0,
    '状态栏确认=' + screenOk + ' 行数=' + screenLines);
  await shot(win, 'conhost-screen.png');

  console.log('=== 8. 控制台错误 ===');
  check('页面控制台没有 error 级日志', consoleErrors.length === 0,
    consoleErrors.slice(0, 6).join(' | '));

  /* ------------------------------------------------------------------
     9. 输入注入（只在 CH_INPUT=1 时跑，因为服务端配置得跟着打开）

     ★ 用 `set /a 111*3` 做验证的技巧：**输入里没有 333，输出里才有**。
       所以「内容里出现 333」这件事只能来自「命令真的被目标 shell 执行了」，
       而不是「把我们自己发出去的字符串又读回来了」—— 后者是自欺欺人的
       假通过（命令回显里带着原始命令行，用普通 echo 就会误判）。
     ------------------------------------------------------------------ */
  if (INPUT_MODE) {
    console.log('=== 9. 输入注入（allow_input = true）===');
    const hasInput = await run(`(function () {
      var box = document.querySelector('.conhost-input');
      if (!box || box.style.display === 'none') { return false; }
      return !!box.querySelector('input') &&
        box.querySelectorAll('button').length > 0;
    })()`, 'input-area-on');
    check('开启 allow_input 后出现输入区与功能键', hasInput === true);

    await shot(win, 'conhost-input-area.png');

    const typed = 'set /a 111*3';
    const sent = await run(`(function () {
      var box = document.querySelector('.conhost-input input');
      if (!box) { return 'no-box'; }
      box.value = ${JSON.stringify(typed)};
      box.dispatchEvent(new KeyboardEvent('keydown',
        { key: 'Enter', bubbles: true }));
      return 'ok';
    })()`, 'send-input');
    check('在输入框里输入并回车发送', sent === 'ok', String(sent));

    const got = await waitFor(win,
      "(document.querySelector('.conhost-view') || {}).textContent.indexOf('333') >= 0",
      25000, 'wait-333');
    const after = await run(
      "(document.querySelector('.conhost-view') || {}).textContent || ''", 'after');
    console.log('      内容片段：' + String(after).slice(-200).replace(/\s+/g, ' '));
    check('注入的命令真的在目标控制台里执行了（输出出现 333）', got === true);

    /* 具名功能键也走一遍：发一个 Ctrl+C，只要求不报错（它打在提示符上，
       本来就不会有可见输出）。 */
    const ctrlc = await run(`(function () {
      var btns = Array.prototype.slice.call(
        document.querySelectorAll('.conhost-keys button'));
      var b = btns.filter(function (n) { return n.textContent.indexOf('Ctrl+C') >= 0; })[0];
      if (!b) { return 'no-ctrlc-button'; }
      b.click();
      return 'ok';
    })()`, 'ctrl-c');
    check('功能键（Ctrl+C）按钮可用', ctrlc === 'ok', String(ctrlc));

    const status = await run(
      "(document.querySelector('.conhost-status') || {}).textContent || ''");
    check('发送后状态栏给出了反馈',
      String(status).indexOf('发送') >= 0 || String(status).indexOf('已发送') >= 0,
      String(status));
    await shot(win, 'conhost-after-input.png');
  } else {
    console.log('=== 9. 输入注入：本次跳过（CH_INPUT 未开启，服务端是只读配置）===');
  }

  const failed = results.filter(function (r) { return !r.ok; });
  console.log('\n================ 结果 ================');
  console.log('共 ' + results.length + ' 项，失败 ' + failed.length + ' 项');
  if (OUT) {
    try {
      fs.writeFileSync(path.join(OUT, 'results.json'),
        JSON.stringify({ results: results, consoleErrors: consoleErrors }, null, 2));
    } catch (err) {
      console.log('写 results.json 失败：' + err);
    }
  }

  app.exit(failed.length === 0 ? 0 : 1);
}

app.whenReady().then(function () {
  main().catch(function (err) {
    console.log('★ 冒烟测试自身出错：' + (err && err.stack ? err.stack : err));
    app.exit(3);
  });
});

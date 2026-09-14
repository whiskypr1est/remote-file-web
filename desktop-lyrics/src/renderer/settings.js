'use strict';
/* ==========================================================================
   桌面歌词 · 连接设置窗口
   --------------------------------------------------------------------------
   「测试连接」是这里最重要的按钮：老师最常遇到的两种情况（地址写错、
   服务端把功能关了）在界面上长得一模一样 —— 都是「歌词不出来」。
   所以测试会把**具体原因**翻译成人话，并且顺手把服务端在放什么歌显示出来。
   ========================================================================== */

const api = window.settings || {
  load: async () => ({}),
  save: async () => ({ ok: false }),
  test: async () => ({ ok: false, message: '当前环境不支持' }),
  close: function () {},
  openReadme: function () {},
  onChanged: function () {}
};

const els = {
  serverUrl: document.getElementById('serverUrl'),
  user: document.getElementById('user'),
  token: document.getElementById('token'),
  userList: document.getElementById('userList'),
  fontSize: document.getElementById('fontSize'),
  showProgress: document.getElementById('showProgress'),
  clickThrough: document.getElementById('clickThrough'),
  locked: document.getElementById('locked'),
  autoStart: document.getElementById('autoStart'),
  test: document.getElementById('test'),
  testResult: document.getElementById('testResult'),
  save: document.getElementById('save'),
  close: document.getElementById('close'),
  readme: document.getElementById('readme'),
  status: document.getElementById('status')
};

/** 用户改过表单之后就别再用外部变化覆盖他的输入。 */
let dirty = false;

for (const element of [els.serverUrl, els.user, els.token]) {
  element.addEventListener('input', () => { dirty = true; });
}
for (const element of [els.fontSize, els.showProgress, els.clickThrough,
  els.locked, els.autoStart]) {
  element.addEventListener('change', () => { dirty = true; });
}

function statusLabel(status) {
  const map = {
    online: '已连接服务端',
    polling: '轮询模式（WebSocket 连不上）',
    connecting: '正在连接…',
    reconnecting: '正在重连…',
    offline: '连不上服务端',
    disabled: '服务端已关闭桌面歌词功能',
    badToken: '服务端要求 token',
    unconfigured: '还没有设置服务器地址'
  };
  return map[(status && status.state) || ''] || (status && status.message) || '—';
}

function setStatus(status) {
  els.status.textContent = statusLabel(status);
  els.status.className = 'status' + (status && (status.state === 'online' || status.state === 'polling')
    ? ' ok'
    : (status && status.state && status.state !== 'connecting'
      && status.state !== 'reconnecting' ? ' err' : ''));
}

function setResult(message, kind) {
  els.testResult.textContent = message || '';
  els.testResult.className = 'result' + (kind ? ' ' + kind : '');
}

function fillUserList(sources) {
  const names = (sources || []).map((item) => item.source).filter(Boolean);
  els.userList.innerHTML = '';
  for (const name of names) {
    const option = document.createElement('option');
    option.value = name;
    els.userList.appendChild(option);
  }
}

function fillForm(config) {
  els.serverUrl.value = config.serverUrl || '';
  els.user.value = config.user || '';
  els.token.value = config.token || '';
  els.fontSize.value = String(config.fontSize || 40);
  els.showProgress.checked = !!config.showProgress;
  els.clickThrough.checked = config.clickThrough !== false;
  els.locked.checked = !!config.locked;
  els.autoStart.checked = !!config.autoStart;
  fillUserList(config.sources);
  setStatus(config.status);
  if (!config.serverUrl) {
    els.serverUrl.focus();
  }
}

function payload() {
  return {
    serverUrl: els.serverUrl.value,
    user: els.user.value,
    token: els.token.value,
    fontSize: Number(els.fontSize.value) || 40,
    showProgress: els.showProgress.checked,
    clickThrough: els.clickThrough.checked,
    locked: els.locked.checked,
    autoStart: els.autoStart.checked
  };
}

async function refresh() {
  try {
    const config = await api.load();
    dirty = false;
    fillForm(config || {});
  } catch (err) {
    setResult('读取配置失败：' + ((err && err.message) || err), 'err');
  }
}

els.test.addEventListener('click', async () => {
  els.test.disabled = true;
  setResult('正在测试…', '');
  try {
    const data = payload();
    const result = await api.test({ serverUrl: data.serverUrl, token: data.token });
    if (result && result.ok) {
      setResult(result.message, 'ok');
      fillUserList(result.sources);
    } else {
      setResult((result && result.message) || '测试失败', 'err');
    }
  } catch (err) {
    setResult('测试失败：' + ((err && err.message) || err), 'err');
  } finally {
    els.test.disabled = false;
  }
});

els.save.addEventListener('click', async () => {
  els.save.disabled = true;
  try {
    const result = await api.save(payload());
    dirty = false;
    if (result && result.config) {
      fillForm(result.config);
    }
    setResult('已保存。', 'ok');
  } catch (err) {
    setResult('保存失败：' + ((err && err.message) || err), 'err');
  } finally {
    els.save.disabled = false;
  }
});

els.close.addEventListener('click', () => api.close());
els.readme.addEventListener('click', () => api.openReadme());

// 托盘里改了设置（例如切了字号）：用户没有正在编辑时才同步过来
api.onChanged((config) => {
  if (!config) {
    return;
  }
  setStatus(config.status);
  if (!dirty) {
    fillForm(config);
  }
});

// 回车即保存，省得每次都去点按钮
document.addEventListener('keydown', (event) => {
  if (event.key === 'Enter' && !els.save.disabled) {
    els.save.click();
  }
});

refresh();

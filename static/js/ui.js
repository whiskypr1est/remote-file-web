/* ==========================================================================
   UI 基础组件
   --------------------------------------------------------------------------
   提供：HTML 转义、格式化、Toast 提示、对话框（确认/输入/提示）、
         加载遮罩、右键菜单。全部用原生 DOM 实现，不依赖任何库。
   ========================================================================== */

import { icon } from './icons.js';

/* ---------------------------------------------------------------------------
   工具函数
   --------------------------------------------------------------------------- */

/** HTML 转义，所有拼接进 innerHTML 的用户数据都必须过这一层 */
export function escapeHtml(value) {
  if (value === null || value === undefined) {
    return '';
  }
  return String(value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/** 字节数 -> 人类可读；目录传 isDir=true 返回空串（与资源管理器一致） */
export function formatSize(bytes, isDir) {
  if (isDir) {
    return '';
  }
  const n = Number(bytes);
  if (!isFinite(n) || n < 0) {
    return '';
  }
  if (n < 1024) {
    return n + ' B';
  }
  const units = ['KB', 'MB', 'GB', 'TB', 'PB'];
  let value = n;
  for (let i = 0; i < units.length; i++) {
    value /= 1024;
    if (value < 1024 || i === units.length - 1) {
      return (value < 100 ? value.toFixed(1) : Math.round(value)) + ' ' + units[i];
    }
  }
  return n + ' B';
}

function pad2(n) {
  return (n < 10 ? '0' : '') + n;
}

/** 时间戳（秒）-> "YYYY-MM-DD HH:mm" */
export function formatTime(seconds) {
  const d = new Date((Number(seconds) || 0) * 1000);
  if (isNaN(d.getTime())) {
    return '';
  }
  return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) +
    ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

/** 只要日期部分 */
export function formatDate(seconds) {
  const text = formatTime(seconds);
  return text ? text.slice(0, 10) : '';
}

/** 复制文本到剪贴板（带旧浏览器兜底） */
export function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) {
    return navigator.clipboard.writeText(text);
  }
  // HTTP 环境下 clipboard API 不可用，用临时 textarea 兜底
  return new Promise(function (resolve, reject) {
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.left = '-9999px';
      ta.style.top = '0';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      ok ? resolve() : reject(new Error('复制失败'));
    } catch (err) {
      reject(err);
    }
  });
}

/* ---------------------------------------------------------------------------
   Toast 提示
   --------------------------------------------------------------------------- */

let toastWrap = null;

function ensureToastWrap() {
  if (!toastWrap) {
    toastWrap = document.createElement('div');
    toastWrap.className = 'toast-wrap';
    document.body.appendChild(toastWrap);
  }
  return toastWrap;
}

/**
 * 右下角消息提示。
 * @param {string} message 内容
 * @param {string} type    success | error | warn | info
 * @param {string} title   可选标题
 * @param {number} duration 毫秒，默认按类型自动决定
 */
export function toast(message, type, title, duration) {
  const wrap = ensureToastWrap();
  const kind = type || 'info';

  const box = document.createElement('div');
  box.className = 'toast ' + kind;

  let html = '';
  if (title) {
    html += '<div class="toast-title">' + escapeHtml(title) + '</div>';
  }
  html += '<div>' + escapeHtml(message) + '</div>';
  box.innerHTML = html;

  wrap.appendChild(box);

  requestAnimationFrame(function () {
    box.classList.add('show');
  });

  const life = duration || (kind === 'error' ? 6500 : 3200);

  const timer = setTimeout(function () {
    box.classList.remove('show');
    setTimeout(function () {
      box.remove();
    }, 260);
  }, life);

  // 点击可提前关闭
  box.addEventListener('click', function () {
    clearTimeout(timer);
    box.classList.remove('show');
    setTimeout(function () {
      box.remove();
    }, 260);
  });

  return box;
}

/* ---------------------------------------------------------------------------
   对话框
   --------------------------------------------------------------------------- */

let activeDialog = null;

/**
 * 通用对话框。
 *
 * @param {object} opts
 *   title      标题
 *   bodyHtml   正文 HTML（调用方负责转义）
 *   iconName   标题图标
 *   buttons    [{text, value, className}]，value 为 null 表示取消
 *   input      {value, placeholder, type} 传入则显示输入框
 *   focusSelector 打开后聚焦的元素选择器
 * @returns {Promise<any>} 点击按钮的 value；输入框存在时返回输入值或 null
 */
function openDialog(opts) {
  const options = opts || {};

  // 同一时间只允许一个对话框，避免层级混乱
  if (activeDialog) {
    activeDialog.close(null);
  }

  const mask = document.createElement('div');
  mask.className = 'dialog-mask';

  const dialog = document.createElement('div');
  dialog.className = 'dialog';

  let headHtml = '<div class="dialog-title">';
  if (options.iconName) {
    headHtml += '<span class="d-ico">' + icon(options.iconName) + '</span>';
  }
  headHtml += '<span>' + escapeHtml(options.title || '提示') + '</span></div>';

  let bodyHtml = '<div class="dialog-body">' + (options.bodyHtml || '');
  if (options.input) {
    bodyHtml += '<input type="' + (options.input.type || 'text') + '" id="dlgInput" ' +
      'value="' + escapeHtml(options.input.value || '') + '" ' +
      'placeholder="' + escapeHtml(options.input.placeholder || '') + '" spellcheck="false">';
  }
  if (options.hint) {
    bodyHtml += '<div class="dialog-hint">' + escapeHtml(options.hint) + '</div>';
  }
  bodyHtml += '</div>';

  let footHtml = '<div class="dialog-foot">';
  (options.buttons || [{ text: '确定', value: true, className: 'primary' }]).forEach(function (btn, index) {
    footHtml += '<button type="button" class="btn ' + (btn.className || '') + '" data-index="' + index + '">' +
      escapeHtml(btn.text) + '</button>';
  });
  footHtml += '</div>';

  dialog.innerHTML = headHtml + bodyHtml + footHtml;
  mask.appendChild(dialog);
  document.body.appendChild(mask);

  requestAnimationFrame(function () {
    mask.classList.add('open');
  });

  const inputEl = dialog.querySelector('#dlgInput');

  let settled = false;

  function close(value) {
    if (settled) {
      return;
    }
    settled = true;
    activeDialog = null;
    document.removeEventListener('keydown', onKey, true);
    mask.classList.remove('open');
    setTimeout(function () {
      mask.remove();
    }, 160);
    resolve(value);
  }

  let resolve;
  const promise = new Promise(function (res) {
    resolve = res;
  });

  /**
   * 取「确认时要交回去的值」。
   *
   * 默认是 #dlgInput 的 value。但有些对话框不止一个输入控件
   * （例如「修改密码」要同时取 当前/新/确认 三个框），
   * 这时调用方可以传 options.getValue(dialog) 自己决定返回什么。
   */
  function currentInputValue() {
    if (typeof options.getValue === 'function') {
      return options.getValue(dialog);
    }
    return inputEl ? inputEl.value : undefined;
  }

  /** 是否存在「能取值的输入控件」：决定确认时返回输入值还是按钮的静态值 */
  function hasValueSource() {
    return !!inputEl || typeof options.getValue === 'function';
  }

  // 按钮点击
  dialog.querySelectorAll('.dialog-foot .btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const cfg = (options.buttons || [])[Number(btn.dataset.index)] || {};
      if (cfg.value === null) {
        close(null);
      } else if (hasValueSource()) {
        close(currentInputValue());
      } else {
        close(cfg.value);
      }
    });
  });

  // 键盘：Enter 确认，Esc 取消
  function onKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close(null);
    } else if (e.key === 'Enter' && hasValueSource()) {
      e.preventDefault();
      e.stopPropagation();
      close(currentInputValue());
    } else if (e.key === 'Enter' && !hasValueSource()) {
      const primary = dialog.querySelector('.dialog-foot .btn.primary');
      if (primary) {
        e.preventDefault();
        e.stopPropagation();
        primary.click();
      }
    }
  }
  document.addEventListener('keydown', onKey, true);

  // 点击遮罩关闭（等价于取消）
  mask.addEventListener('mousedown', function (e) {
    if (e.target === mask) {
      close(null);
    }
  });

  activeDialog = { close: close };

  // 聚焦
  setTimeout(function () {
    if (inputEl) {
      inputEl.focus();
      inputEl.select();
    } else {
      const primary = dialog.querySelector('.dialog-foot .btn.primary') ||
        dialog.querySelector('.dialog-foot .btn');
      primary && primary.focus();
    }
  }, 60);

  return promise;
}

/** 提示框（只有一个「确定」按钮） */
export function showAlert(title, message, iconName) {
  return openDialog({
    title: title,
    bodyHtml: escapeHtml(message),
    iconName: iconName || 'info-circle',
    buttons: [{ text: '确定', value: true, className: 'primary' }]
  });
}

/**
 * 确认框。
 * @returns {Promise<boolean>}
 */
export function showConfirm(title, message, opts) {
  const options = opts || {};
  return openDialog({
    title: title,
    bodyHtml: escapeHtml(message),
    iconName: options.iconName || 'question',
    buttons: [
      { text: options.cancelText || '取消', value: false },
      {
        text: options.okText || '确定',
        value: true,
        className: options.danger ? 'danger' : 'primary'
      }
    ]
  }).then(function (v) {
    return v === true;
  });
}

/**
 * 输入框。
 * @returns {Promise<string|null>} 取消返回 null
 */
export function showPrompt(title, label, defaultValue, opts) {
  const options = opts || {};
  let body = '';
  if (label) {
    body += '<div style="margin-bottom:8px">' + escapeHtml(label) + '</div>';
  }
  return openDialog({
    title: title,
    bodyHtml: body,
    iconName: options.iconName || 'pencil',
    input: {
      value: defaultValue || '',
      placeholder: options.placeholder || ''
    },
    hint: options.hint || '',
    buttons: [
      { text: '取消', value: null },
      { text: options.okText || '确定', value: true, className: 'primary' }
    ]
  });
}

/* ---------------------------------------------------------------------------
   新建文件的命名对话框（可自选扩展名）
   --------------------------------------------------------------------------- */

/**
 * 常用扩展名快捷项。
 * value === '' 是「不改动输入框」的占位项；'__none__' 表示真的不要扩展名。
 */
const FILE_EXT_PRESETS = [
  ['', '常用类型（可选，选中后自动补到文件名）'],
  ['.txt', '.txt — 文本文件'],
  ['.md', '.md — Markdown'],
  ['.py', '.py — Python'],
  ['.js', '.js — JavaScript'],
  ['.json', '.json — JSON'],
  ['.html', '.html — 网页'],
  ['.css', '.css — 样式表'],
  ['.csv', '.csv — 表格'],
  ['.xml', '.xml — XML'],
  ['.yml', '.yml — YAML'],
  ['.ini', '.ini — 配置'],
  ['.log', '.log — 日志'],
  ['.sql', '.sql — SQL'],
  ['.sh', '.sh — Shell 脚本'],
  ['__none__', '（无扩展名）']
];

/**
 * 去掉文件名末尾的扩展名，返回「主文件名」。
 * 以点开头的名字（.gitignore、.bashrc）不当作有扩展名，原样返回。
 */
function stripExtension(name) {
  const text = String(name == null ? '' : name);
  const cut = Math.max(text.lastIndexOf('/'), text.lastIndexOf('\\'));
  const base = cut >= 0 ? text.slice(cut + 1) : text;
  const dot = base.lastIndexOf('.');
  if (dot <= 0) {
    return text;   // 没有点；或点就在开头（.gitignore 这类）
  }
  return text.slice(0, cut + 1 + dot);
}

/**
 * 「新建文件」对话框：文件名输入框 + 常用扩展名下拉。
 *
 * 设计要点：**文件名输入框是唯一的数据来源**，下拉框只做「快捷补全」——
 * 选中某个类型时就地替换输入框里文件名的扩展名部分。
 * 不做成「主文件名 + 扩展名」两个独立字段，是为了避免出现
 * 「下拉框显示 .txt、而输入框里其实是 .md」这种两个真相打架的情况。
 * 现在这套既能点选，也保留直接敲任意后缀（含列表里没有的，如 .tsv）的自由。
 *
 * @returns {Promise<string|null>} 完整文件名；取消返回 null
 */
export function showFileNameDialog(opts) {
  const options = opts || {};

  let selectHtml = '<select id="dlgFileExt">';
  FILE_EXT_PRESETS.forEach(function (pair) {
    selectHtml += '<option value="' + escapeHtml(pair[0]) + '">' + escapeHtml(pair[1]) + '</option>';
  });
  selectHtml += '</select>';

  const body = '<div style="margin-bottom:8px">' +
    escapeHtml(options.label || '请输入文件名（含扩展名）：') + '</div>' + selectHtml;

  const promise = openDialog({
    title: options.title || '新建文件',
    bodyHtml: body,
    iconName: options.iconName || 'file-text',
    input: {
      value: options.defaultName || '新建文本文档.txt',
      placeholder: options.placeholder || '例如：notes.md'
    },
    hint: options.hint || '扩展名可以点上面的下拉框，也可以直接敲。',
    buttons: [
      { text: '取消', value: null },
      { text: options.okText || '创建', value: true, className: 'primary' }
    ]
  });

  // openDialog 是把 DOM **同步**建好之后才返回 promise 的（见该函数内
  // dialog.innerHTML / appendChild 都在 return 之前），
  // 所以这里立刻就能拿到刚渲染出来的下拉框并挂上事件，不需要额外的回调。
  const select = document.getElementById('dlgFileExt');
  const nameInput = document.getElementById('dlgInput');
  if (select && nameInput) {
    select.addEventListener('change', function () {
      const ext = select.value;
      if (!ext) {
        return;                       // 占位项：不动输入框
      }
      const base = stripExtension(nameInput.value);
      nameInput.value = (ext === '__none__') ? base : base + ext;
      nameInput.focus();
      // 只选中「主文件名」那一段方便直接改写；扩展名留在选区之外不动它
      const keep = (ext === '__none__') ? 0 : ext.length;
      nameInput.setSelectionRange(0, Math.max(0, nameInput.value.length - keep));
    });
  }

  return promise;
}

/* ---------------------------------------------------------------------------
   修改密码对话框
   --------------------------------------------------------------------------- */

/** 与服务端 auth.py 的 MIN_PASSWORD_LENGTH 保持一致 */
const PASSWORD_MIN_LENGTH = 8;

/**
 * 「修改密码」对话框：当前密码 / 新密码 / 确认新密码 三个框。
 *
 * 为什么单独写一个、而不是连开三次 showPrompt：
 *   三次提示要用户分三步输、中间还不能回头改，体验很差；而且只有三个框
 *   摆在一起，才能**当场**校验「两次新密码是否一致」。
 *
 * 校验是双保险：
 *   * 输入时实时校验，不通过就把「确定」按钮置灰并给出提示 ——
 *     正常操作提交不出坏数据；
 *   * 调用方仍会再校验一遍，用于兜住按 Enter 提交这条路径
 *     （Enter 走的是 openDialog 内部的键盘处理，绕过了按钮的 disabled）。
 *
 * @returns {Promise<{current: string, next: string}|null>} 取消返回 null
 */
export function showPasswordDialog(opts) {
  const options = opts || {};

  function field(id, label, placeholder, autocomplete) {
    return '<div class="dlg-field">' +
      '<label class="dlg-label" for="' + id + '">' + escapeHtml(label) + '</label>' +
      '<input type="password" id="' + id + '" autocomplete="' + autocomplete + '" ' +
      'placeholder="' + escapeHtml(placeholder) + '" spellcheck="false">' +
      '</div>';
  }

  const body =
    field('pwCurrent', '当前密码', '请输入现在使用的密码', 'current-password') +
    field('pwNew', '新密码', '至少 ' + PASSWORD_MIN_LENGTH + ' 位', 'new-password') +
    field('pwConfirm', '确认新密码', '再输入一次新密码', 'new-password') +
    '<div class="dlg-inline-hint" id="pwHint"></div>';

  const promise = openDialog({
    title: options.title || '修改密码',
    bodyHtml: body,
    iconName: options.iconName || 'user',
    hint: options.hint || '',
    buttons: [
      { text: '取消', value: null },
      { text: '确定修改', value: true, className: 'primary' }
    ],
    // 三个框一起取值，交给调用方
    getValue: function (dialog) {
      function read(id) {
        const el = dialog.querySelector('#' + id);
        return el ? el.value : '';
      }
      return { current: read('pwCurrent'), next: read('pwNew') };
    }
  });

  // openDialog 是「同步建好 DOM 之后」才返回 promise 的，所以这里能立刻挂上事件
  const currentEl = document.getElementById('pwCurrent');
  const newEl = document.getElementById('pwNew');
  const confirmEl = document.getElementById('pwConfirm');
  const hintEl = document.getElementById('pwHint');
  const okBtn = document.querySelector('.dialog-foot .btn.primary');

  function validate() {
    const current = currentEl ? currentEl.value : '';
    const next = newEl ? newEl.value : '';
    const confirm = confirmEl ? confirmEl.value : '';

    let message = '';
    if (next && next.length < PASSWORD_MIN_LENGTH) {
      message = '新密码至少需要 ' + PASSWORD_MIN_LENGTH + ' 位';
    } else if (confirm && next !== confirm) {
      message = '两次输入的新密码不一致';
    } else if (next && current && next === current) {
      message = '新密码不能与当前密码相同';
    }

    const ready = !!current && !!next && !!confirm && !message;
    if (hintEl) {
      hintEl.textContent = message;
      hintEl.classList.toggle('is-error', !!message);
    }
    if (okBtn) {
      okBtn.disabled = !ready;
    }
    return ready;
  }

  [currentEl, newEl, confirmEl].forEach(function (el) {
    if (el) {
      el.addEventListener('input', validate);
    }
  });
  validate();

  if (currentEl) {
    currentEl.focus();
  }

  return promise;
}

/* ---------------------------------------------------------------------------
   加载遮罩
   --------------------------------------------------------------------------- */

let busyMask = null;

/**
 * 显示 / 隐藏全屏加载遮罩。
 * 支持嵌套调用（计数），避免内层提前关掉外层。
 */
let busyCount = 0;

export function setBusy(show, text) {
  if (show) {
    busyCount++;
    if (!busyMask) {
      busyMask = document.createElement('div');
      busyMask.className = 'busy-mask';
      busyMask.innerHTML = '<div class="spinner"></div><div class="busy-text">' +
        escapeHtml(text || '正在处理…') + '</div>';
      document.body.appendChild(busyMask);
    } else {
      const t = busyMask.querySelector('.busy-text');
      if (t && text) {
        t.textContent = text;
      }
    }
  } else {
    busyCount = Math.max(0, busyCount - 1);
    if (busyCount === 0 && busyMask) {
      busyMask.remove();
      busyMask = null;
    }
  }
}

/* ---------------------------------------------------------------------------
   右键菜单
   --------------------------------------------------------------------------- */

let ctxMenu = null;

/** 关掉当前右键菜单，并摘掉为它注册的全局监听 */
export function hideContextMenu() {
  if (ctxMenu) {
    ctxMenu.remove();
    ctxMenu = null;
  }
  detachCtxListeners();
}

/**
 * 弹出右键菜单。
 *
 * @param {number} x 屏幕坐标
 * @param {number} y
 * @param {Array}  items 每项：{label, iconName, danger, disabled, shortcut, onClick}
 *                       或字符串 'separator'
 */
export function showContextMenu(x, y, items) {
  hideContextMenu();

  const menu = document.createElement('div');
  menu.className = 'ctx-menu';

  items.forEach(function (item) {
    if (item === 'separator' || item.type === 'separator') {
      const sep = document.createElement('div');
      sep.className = 'ctx-sep';
      menu.appendChild(sep);
      return;
    }

    const row = document.createElement('div');
    row.className = 'ctx-item' + (item.danger ? ' danger' : '') + (item.disabled ? ' disabled' : '');

    let html = '<span class="ctx-ico">' + (item.iconName ? icon(item.iconName) : '') + '</span>';
    html += '<span class="ctx-text">' + escapeHtml(item.label) + '</span>';
    if (item.shortcut) {
      html += '<span class="ctx-key">' + escapeHtml(item.shortcut) + '</span>';
    }
    row.innerHTML = html;

    if (!item.disabled && typeof item.onClick === 'function') {
      row.addEventListener('click', function (e) {
        e.stopPropagation();
        hideContextMenu();
        item.onClick();
      });
    }

    menu.appendChild(row);
  });

  // 先放到 -9999 的位置测量尺寸，再决定最终坐标，避免超出屏幕
  menu.style.left = '-9999px';
  menu.style.top = '-9999px';
  document.body.appendChild(menu);

  const rect = menu.getBoundingClientRect();
  const maxX = window.innerWidth - rect.width - 6;
  const maxY = window.innerHeight - rect.height - 6;
  menu.style.left = Math.max(4, Math.min(x, maxX)) + 'px';
  menu.style.top = Math.max(4, Math.min(y, maxY)) + 'px';

  requestAnimationFrame(function () {
    menu.classList.add('open');
  });

  ctxMenu = menu;

  // 关闭时机：点击别处、按 Esc、滚动、右键别处
  setTimeout(function () {
    document.addEventListener('mousedown', onDocMouseDown, true);
    document.addEventListener('contextmenu', onDocContextMenu, true);
    window.addEventListener('resize', hideContextMenu, true);
    window.addEventListener('wheel', hideContextMenu, true);
    document.addEventListener('keydown', onDocKeyDown, true);
  }, 0);
}

function detachCtxListeners() {
  document.removeEventListener('mousedown', onDocMouseDown, true);
  document.removeEventListener('contextmenu', onDocContextMenu, true);
  window.removeEventListener('resize', hideContextMenu, true);
  window.removeEventListener('wheel', hideContextMenu, true);
  document.removeEventListener('keydown', onDocKeyDown, true);
}

function onDocMouseDown(e) {
  if (ctxMenu && !ctxMenu.contains(e.target)) {
    hideContextMenu();
    detachCtxListeners();
  }
}

function onDocContextMenu() {
  hideContextMenu();
  detachCtxListeners();
}

function onDocKeyDown(e) {
  if (e.key === 'Escape') {
    hideContextMenu();
    detachCtxListeners();
  }
}

export default {
  escapeHtml: escapeHtml,
  formatSize: formatSize,
  formatTime: formatTime,
  copyText: copyText,
  toast: toast,
  showAlert: showAlert,
  showConfirm: showConfirm,
  showPrompt: showPrompt,
  setBusy: setBusy,
  showContextMenu: showContextMenu,
  hideContextMenu: hideContextMenu
};

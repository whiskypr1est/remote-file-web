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

  function currentInputValue() {
    return inputEl ? inputEl.value : undefined;
  }

  // 按钮点击
  dialog.querySelectorAll('.dialog-foot .btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const cfg = (options.buttons || [])[Number(btn.dataset.index)] || {};
      if (cfg.value === null) {
        close(null);
      } else if (inputEl) {
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
    } else if (e.key === 'Enter' && inputEl) {
      e.preventDefault();
      e.stopPropagation();
      close(currentInputValue());
    } else if (e.key === 'Enter' && !inputEl) {
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

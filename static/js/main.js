/* ==========================================================================
   启动入口
   --------------------------------------------------------------------------
   流程：
     1. 询问后端当前是否已登录，未登录直接跳到登录页
     2. 拿到会话对应的 CSRF 令牌（后续所有写操作都要带）
     3. 拉取系统信息（根目录、功能开关、版本号）
     4. 构建桌面并隐藏启动遮罩
   ========================================================================== */

import * as api from './api.js';
import * as ui from './ui.js';
import { Desktop } from './desktop.js';
import { openExplorer } from './explorer.js';
import { openPreview } from './preview.js';

const bootScreen = document.getElementById('bootScreen');
const bootText = document.getElementById('bootText');

function setBootText(text) {
  if (bootText) {
    bootText.textContent = text;
  }
}

function hideBootScreen() {
  if (bootScreen) {
    bootScreen.classList.add('hidden');
    setTimeout(function () {
      bootScreen.remove();
    }, 400);
  }
}

function fatal(message) {
  setBootText('启动失败');
  hideBootScreen();
  ui.showAlert('无法启动', message, 'error');
}

async function boot() {
  setBootText('正在检查登录状态…');

  let status;
  try {
    status = await api.authStatus();
  } catch (err) {
    fatal('无法连接到服务器：' + ((err && err.message) || err));
    return;
  }

  if (!status || !status.authenticated) {
    location.replace('/login.html');
    return;
  }

  api.setCsrfToken(status.csrf_token);

  setBootText('正在加载系统信息…');

  let info;
  try {
    info = await api.systemInfo();
  } catch (err) {
    if (err && err.status === 401) {
      return; // api 层已经跳转登录页
    }
    fatal('读取系统信息失败：' + ((err && err.message) || err));
    return;
  }

  setBootText('正在准备桌面…');

  const desktop = new Desktop(info);
  desktop.init();

  // 暴露到全局，方便在浏览器控制台排查问题
  window.__desktop = desktop;

  // 支持用地址栏 hash 直达某个位置（方便收藏 / 分享给同事）：
  //   #explorer                    打开「此电脑」
  //   #explorer/share/图片          直接打开某个根目录下的文件夹
  //   #preview/share/说明.txt       直接打开某个文件的预览窗口
  applyDeepLink(desktop);
  window.addEventListener('hashchange', function () {
    applyDeepLink(desktop);
  });

  hideBootScreen();

  // 首次进入给个小提示：没装 LibreOffice 时说明一下
  if (info.features && info.features.libreoffice === false) {
    setTimeout(function () {
      ui.toast('未检测到 LibreOffice，doc/xls/ppt 等老格式文档将无法转换预览（docx/xlsx/pptx 仍可查看内容）。',
        'warn', '提示', 9000);
    }, 1200);
  }
}

boot().catch(function (err) {
  fatal((err && err.message) || String(err));
});

/**
 * 解析地址栏 hash 并打开对应的窗口。
 * 位置格式：#explorer/<根标识>/<相对路径>  或  #preview/<根标识>/<相对路径>
 */
function applyDeepLink(desktop) {
  const raw = location.hash.replace(/^#\/?/, '');
  if (!raw) {
    return;
  }

  let parts;
  try {
    parts = decodeURIComponent(raw).split('/');
  } catch (err) {
    parts = raw.split('/');
  }

  const kind = parts.shift();

  if (kind === 'explorer') {
    openExplorer(desktop, parts[0] || '', parts.slice(1).join('/'));
    return;
  }

  if (kind === 'preview' && parts.length >= 2) {
    const rootId = parts.shift();
    const rel = parts.join('/');
    openPreview({
      desktop: desktop,
      rootId: rootId,
      rel: rel,
      name: rel.split('/').pop() || rel,
      sizeText: ''
    });
  }
}

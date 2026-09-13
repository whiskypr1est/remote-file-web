/* ==========================================================================
   桌面外壳
   --------------------------------------------------------------------------
   负责壁纸、桌面图标、任务栏（开始按钮 / 窗口按钮 / 系统托盘）、开始菜单、
   时钟、桌面右键菜单，以及注销 / 换壁纸 / 关于 等入口。
   窗口本身由 wins.js 管理，本模块只负责"桌面"这一层。
   ========================================================================== */

import { icon } from './icons.js';
import * as api from './api.js';
import * as ui from './ui.js';
import { wm } from './wins.js';
import { openExplorer } from './explorer.js';
import { openPreview } from './preview.js';
import { openTerminal } from './terminal.js';
import { initSessionState, restoreState } from './sessionstate.js';

const WEEKDAYS = ['星期日', '星期一', '星期二', '星期三', '星期四', '星期五', '星期六'];

function pad2(n) {
  return (n < 10 ? '0' : '') + n;
}

function formatUptime(seconds) {
  const total = Math.max(0, Math.floor(seconds || 0));
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) {
    return days + ' 天 ' + hours + ' 小时';
  }
  if (hours > 0) {
    return hours + ' 小时 ' + minutes + ' 分钟';
  }
  return minutes + ' 分钟';
}

export class Desktop {
  constructor(info) {
    this.info = info;

    this.wallpaperEl = document.getElementById('wallpaper');
    this.iconsEl = document.getElementById('desktopIcons');
    this.startMenu = document.getElementById('startMenu');
    this.startBtn = document.getElementById('startBtn');
    this.trayPopup = document.getElementById('trayPopup');
    this.clockTimeEl = document.getElementById('clockTime');
    this.clockDateEl = document.getElementById('clockDate');
    this.trayIpEl = document.getElementById('trayIp');

    this.wallpaperInput = null;
    this.clockTimer = null;
    // 用户创建的桌面快捷方式（由服务端持久化，重启服务不会丢）
    this.shortcuts = [];
  }

  /* =========================================================================
     启动
     ========================================================================= */

  init() {
    this.applyWallpaper(this.info.ui.wallpaper);
    this.renderDesktopIcons();
    this.renderStartMenu();
    this.startClock();
    this.bindTaskbar();
    this.bindDesktop();
    this.wm = wm;
    // 异步拉取快捷方式，拿到后再重绘一次桌面图标
    this.loadShortcuts();
    // 还原上次的窗口布局（异步，不阻塞桌面先显示出来）
    this.restoreLayout();
  }

  /**
   * 还原上次关掉浏览器时的窗口布局。
   *
   * 刻意不 await：桌面外壳（壁纸、任务栏、图标）应该立刻可用，
   * 窗口慢一点出来完全可以接受。整条链路里 sessionstate 已经把所有异常
   * 都吞掉了，这里再兜一层，保证「还原失败」绝不会变成「桌面打不开」。
   */
  restoreLayout() {
    return initSessionState(this).then(function (state) {
      return restoreState(state);
    }).then(function (count) {
      if (count > 0) {
        console.info('[desktop] 已还原 ' + count + ' 个窗口');
      }
    }).catch(function (err) {
      console.warn('[desktop] 还原上次布局失败（不影响使用）:', (err && err.message) || err);
    });
  }

  /**
   * 命令行功能是否可用。
   *
   * 由服务端 /api/system/info 的 features.terminal 决定（config.json 里
   * terminal.enabled）。取明确的 true 才算开启：判定口径与 terminal.js 一致，
   * 避免出现「桌面显示入口、点开却说功能已关闭」这种不一致。
   */
  terminalEnabled() {
    return ((this.info.features || {}).terminal === true);
  }

  /* =========================================================================
     壁纸
     ========================================================================= */

  applyWallpaper(url) {
    if (url) {
      this.wallpaperEl.classList.add('custom');
      // 用 CSS 变量传图片地址，避免直接写在 background-image 上难以清理
      this.wallpaperEl.style.setProperty('--wallpaper-image', 'url("' + url + '")');
    } else {
      this.wallpaperEl.classList.remove('custom');
      this.wallpaperEl.style.removeProperty('--wallpaper-image');
    }
  }

  /** 选择本地图片作为壁纸并上传到服务器 */
  changeWallpaper() {
    const self = this;

    if (!this.wallpaperInput) {
      this.wallpaperInput = document.createElement('input');
      this.wallpaperInput.type = 'file';
      this.wallpaperInput.accept = 'image/png,image/jpeg,image/webp,image/gif,image/bmp';
      this.wallpaperInput.style.display = 'none';
      document.body.appendChild(this.wallpaperInput);

      this.wallpaperInput.addEventListener('change', function () {
        const file = self.wallpaperInput.files && self.wallpaperInput.files[0];
        self.wallpaperInput.value = '';
        if (!file) {
          return;
        }
        if (file.size > 20 * 1024 * 1024) {
          ui.showAlert('图片太大', '壁纸文件不能超过 20MB，请先压缩后再试。', 'warning');
          return;
        }
        self.uploadWallpaper(file);
      });
    }

    this.wallpaperInput.click();
  }

  uploadWallpaper(file) {
    const self = this;
    ui.setBusy(true, '正在上传壁纸…');

    const xhr = new XMLHttpRequest();
    xhr.open('POST', api.buildUrl('/api/system/wallpaper', { filename: file.name }), true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    const csrf = api.getCsrfToken();
    if (csrf) {
      xhr.setRequestHeader('X-CSRF-Token', csrf);
    }

    xhr.onload = function () {
      ui.setBusy(false);
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch (err) {
        data = null;
      }
      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) {
        self.applyWallpaper(data.wallpaper);
        self.info.ui.wallpaper = data.wallpaper;
        ui.toast('壁纸已更新', 'success');
      } else if (xhr.status === 401) {
        api.redirectToLogin();
      } else {
        ui.showAlert('设置壁纸失败', (data && (data.message || data.detail)) || ('HTTP ' + xhr.status), 'error');
      }
    };

    xhr.onerror = function () {
      ui.setBusy(false);
      ui.showAlert('设置壁纸失败', '网络错误，请稍后重试。', 'error');
    };

    xhr.send(file);
  }

  /** 恢复内置蓝色渐变壁纸 */
  resetWallpaper() {
    const self = this;
    api.request('POST', '/api/system/wallpaper/reset', { json: {} }).then(function (res) {
      self.applyWallpaper('');
      self.info.ui.wallpaper = '';
      ui.toast(res.message || '已恢复默认壁纸', 'success');
    }).catch(function (err) {
      ui.showAlert('操作失败', (err && err.message) || '未知错误', 'error');
    });
  }

  /* =========================================================================
     桌面图标
     ========================================================================= */

  renderDesktopIcons() {
    const self = this;
    const items = [];

    // ---- 内置图标 ----
    items.push({
      key: 'explorer',
      label: '文件资源管理器',
      iconName: 'app-explorer',
      kind: 'builtin',
      onOpen: function () { openExplorer(self, '', ''); }
    });

    // 命令提示符：仅在服务端开启该功能时显示入口
    if (this.terminalEnabled()) {
      items.push({
        key: 'terminal',
        label: '命令提示符',
        iconName: 'code',        // icons.js 没有专用 terminal 图标，用最接近的 code
        kind: 'builtin',
        onOpen: function () { openTerminal(self); }
      });
    }

    (this.info.roots || []).forEach(function (root) {
      items.push({
        key: 'root-' + root.id,
        label: root.name,
        // 自动挂载的磁盘画成盘符图标；配置里手写的目录画成文件夹图标
        iconName: root.kind === 'drive' ? 'drive' : 'folder',
        kind: 'builtin',
        onOpen: function () { openExplorer(self, root.id, ''); }
      });
    });

    // ---- 用户创建的快捷方式（只存在于本虚拟桌面）----
    this.shortcuts.forEach(function (shortcut) {
      items.push({
        key: 'sc-' + shortcut.id,
        label: shortcut.name,
        iconName: shortcut.is_dir ? 'folder' : 'file',
        kind: 'shortcut',
        shortcut: shortcut,
        onOpen: function () { self.openShortcut(shortcut); }
      });
    });

    items.push({
      key: 'about',
      label: '关于',
      iconName: 'app-about',
      kind: 'builtin',
      onOpen: function () { self.showAbout(); }
    });

    this.iconsEl.innerHTML = items.map(function (item) {
      const isShortcut = item.kind === 'shortcut';
      const missing = isShortcut && item.shortcut.exists === false ? ' missing' : '';
      const badge = isShortcut
        ? '<span class="shortcut-badge">' + icon('shortcut') + '</span>'
        : '';

      let title = item.label;
      if (isShortcut) {
        title = item.shortcut.abs || (item.shortcut.root + '/' + item.shortcut.path);
        if (item.shortcut.exists === false) {
          title += '\n（目标已不存在）';
        }
      }

      return '<div class="desk-icon' + (isShortcut ? ' shortcut' : '') + missing +
        '" data-key="' + ui.escapeHtml(item.key) + '" title="' + ui.escapeHtml(title) + '">' +
        '<div class="icon-img">' + icon(item.iconName) + badge + '</div>' +
        '<div class="icon-label">' + ui.escapeHtml(item.label) + '</div>' +
        '</div>';
    }).join('');

    // ---- 绑定点击 / 双击 / 右键 ----
    this.iconsEl.querySelectorAll('.desk-icon').forEach(function (el) {
      const item = items.find(function (i) { return i.key === el.dataset.key; });
      if (!item) {
        return;
      }

      el.addEventListener('mousedown', function (e) {
        e.stopPropagation();
        self.iconsEl.querySelectorAll('.desk-icon').forEach(function (other) {
          other.classList.toggle('selected', other === el);
        });
      });

      el.addEventListener('dblclick', function () {
        item.onOpen();
      });

      el.addEventListener('contextmenu', function (e) {
        e.preventDefault();
        e.stopPropagation();
        self.iconsEl.querySelectorAll('.desk-icon').forEach(function (other) {
          other.classList.toggle('selected', other === el);
        });

        if (item.kind === 'shortcut') {
          self.showShortcutMenu(e.clientX, e.clientY, item.shortcut);
        } else {
          ui.showContextMenu(e.clientX, e.clientY, [
            { label: '打开', iconName: 'open', onClick: item.onOpen }
          ]);
        }
      });
    });
  }

  /* =========================================================================
     虚拟桌面快捷方式
     ========================================================================= */

  /** 从服务端加载快捷方式并重绘桌面图标 */
  loadShortcuts() {
    const self = this;
    return api.listShortcuts().then(function (res) {
      self.shortcuts = (res && res.shortcuts) || [];
      self.renderDesktopIcons();
    }).catch(function (err) {
      if (err && err.status === 401) {
        return; // api 层已经跳转登录页
      }
      // 读不到就当作没有快捷方式，不影响桌面其它功能
      self.shortcuts = [];
      self.renderDesktopIcons();
    });
  }

  /** 供资源管理器调用：刚创建完快捷方式时刷新桌面 */
  reloadShortcuts() {
    return this.loadShortcuts();
  }

  /** 打开快捷方式 */
  openShortcut(shortcut) {
    if (!shortcut) {
      return;
    }

    if (shortcut.exists === false) {
      ui.showAlert(
        '快捷方式已失效',
        '目标已不存在：\n' + (shortcut.abs || (shortcut.root + '/' + shortcut.path)) +
        '\n\n可能是文件被移动/删除了，或者对应的磁盘（U 盘、移动硬盘）已拔出。',
        'warning'
      );
      return;
    }

    if (shortcut.is_dir) {
      openExplorer(this, shortcut.root, shortcut.path);
      return;
    }

    openPreview({
      desktop: this,
      rootId: shortcut.root,
      rel: shortcut.path,
      name: shortcut.path.split('/').pop() || shortcut.name,
      sizeText: ''
    });
  }

  /** 快捷方式的右键菜单 */
  showShortcutMenu(x, y, shortcut) {
    const self = this;

    ui.showContextMenu(x, y, [
      {
        label: '打开',
        iconName: 'open',
        onClick: function () { self.openShortcut(shortcut); }
      },
      {
        label: '打开所在位置',
        iconName: 'app-explorer',
        onClick: function () {
          const parent = shortcut.path.split('/').slice(0, -1).join('/');
          openExplorer(self, shortcut.root, parent);
        }
      },
      'separator',
      {
        label: '重命名',
        iconName: 'pencil',
        onClick: function () { self.renameShortcut(shortcut); }
      },
      {
        label: '删除快捷方式',
        iconName: 'trash',
        danger: true,
        onClick: function () { self.removeShortcut(shortcut); }
      }
    ]);
  }

  renameShortcut(shortcut) {
    const self = this;

    ui.showPrompt('重命名快捷方式', '请输入新的显示名称：', shortcut.name).then(function (name) {
      if (name === null) {
        return;
      }
      const trimmed = String(name).trim();
      if (!trimmed || trimmed === shortcut.name) {
        return;
      }

      api.renameShortcut(shortcut.id, trimmed).then(function (res) {
        ui.toast(res.message || '已重命名', 'success');
        return self.loadShortcuts();
      }).catch(function (err) {
        if (err && err.status !== 401) {
          ui.showAlert('重命名失败', err.message, 'error');
        }
      });
    });
  }

  removeShortcut(shortcut) {
    const self = this;

    ui.showConfirm(
      '删除快捷方式',
      '确定要把「' + shortcut.name + '」从桌面移除吗？\n\n' +
      '只会删除这个快捷方式图标，对应的真实文件不会被删除。',
      { okText: '删除', danger: true }
    ).then(function (ok) {
      if (!ok) {
        return;
      }
      api.deleteShortcut(shortcut.id).then(function (res) {
        ui.toast(res.message || '快捷方式已移除', 'success');
        return self.loadShortcuts();
      }).catch(function (err) {
        if (err && err.status !== 401) {
          ui.showAlert('删除失败', err.message, 'error');
        }
      });
    });
  }

  /* =========================================================================
     开始菜单
     ========================================================================= */

  renderStartMenu() {
    const self = this;
    const user = (this.info.user && this.info.user.username) || 'admin';

    const roots = this.info.roots || [];

    let html = '';
    html += '<div class="sm-user">' +
      '<div class="sm-avatar">' + icon('user') + '</div>' +
      '<div><div class="sm-user-name">' + ui.escapeHtml(user) + '</div>' +
      '<div class="sm-user-sub">' + ui.escapeHtml(this.info.server.hostname) + ' · 已登录</div></div>' +
      '</div>';

    html += '<div class="sm-list">';

    html += '<div class="sm-item" data-action="explorer">' +
      '<span class="sm-ico">' + icon('app-explorer') + '</span>' +
      '<span class="sm-text">文件资源管理器</span>' +
      '<span class="sm-hint">此电脑</span></div>';

    // 命令提示符：同样只在功能开启时出现
    if (this.terminalEnabled()) {
      html += '<div class="sm-item" data-action="terminal">' +
        '<span class="sm-ico">' + icon('code') + '</span>' +
        '<span class="sm-text">命令提示符</span>' +
        '<span class="sm-hint">CMD</span></div>';
    }

    roots.forEach(function (root) {
      html += '<div class="sm-item" data-action="root" data-root="' + ui.escapeHtml(root.id) + '">' +
        '<span class="sm-ico">' + icon('drive') + '</span>' +
        '<span class="sm-text">' + ui.escapeHtml(root.name) + '</span>' +
        '<span class="sm-hint">' + ui.escapeHtml(root.path) + '</span></div>';
    });

    html += '<div class="sm-sep"></div>';

    html += '<div class="sm-item" data-action="about">' +
      '<span class="sm-ico">' + icon('app-about') + '</span>' +
      '<span class="sm-text">关于</span></div>';

    html += '<div class="sm-item" data-action="wallpaper">' +
      '<span class="sm-ico">' + icon('image') + '</span>' +
      '<span class="sm-text">更换桌面壁纸</span></div>';

    html += '</div>';

    html += '<div class="sm-foot">' +
      '<div class="sm-foot-btn danger" data-action="logout">' + icon('logout') + '<span>注销</span></div>' +
      '<div class="sm-version">v' + ui.escapeHtml(this.info.app.version) + '</div>' +
      '</div>';

    this.startMenu.innerHTML = html;

    this.startMenu.querySelectorAll('.sm-item, .sm-foot-btn').forEach(function (el) {
      el.addEventListener('click', function () {
        const action = el.dataset.action;
        self.closeStartMenu();

        if (action === 'explorer') {
          openExplorer(self, '', '');
        } else if (action === 'terminal') {
          openTerminal(self);
        } else if (action === 'root') {
          openExplorer(self, el.dataset.root, '');
        } else if (action === 'about') {
          self.showAbout();
        } else if (action === 'wallpaper') {
          self.changeWallpaper();
        } else if (action === 'logout') {
          self.logout();
        }
      });
    });
  }

  openStartMenu() {
    this.startMenu.classList.add('open');
    this.startBtn.classList.add('open');
    this.closeTrayPopup();
  }

  closeStartMenu() {
    this.startMenu.classList.remove('open');
    this.startBtn.classList.remove('open');
  }

  toggleStartMenu() {
    if (this.startMenu.classList.contains('open')) {
      this.closeStartMenu();
    } else {
      this.openStartMenu();
    }
  }

  /* =========================================================================
     系统托盘
     ========================================================================= */

  startClock() {
    const self = this;

    function tick() {
      const now = new Date();
      if (self.clockTimeEl) {
        self.clockTimeEl.textContent = pad2(now.getHours()) + ':' + pad2(now.getMinutes());
      }
      if (self.clockDateEl) {
        self.clockDateEl.textContent =
          now.getFullYear() + '/' + pad2(now.getMonth() + 1) + '/' + pad2(now.getDate());
      }
      if (self.trayPopup.classList.contains('open')) {
        self.renderTrayPopup(now);
      }
    }

    tick();
    this.clockTimer = setInterval(tick, 1000);

    // 托盘显示服务器 IP（局域网用户看到的就是自己的访问地址）
    if (this.trayIpEl) {
      this.trayIpEl.textContent = location.host;
    }
  }

  renderTrayPopup(now) {
    const info = this.info;
    const dateText = now.getFullYear() + ' 年 ' + (now.getMonth() + 1) + ' 月 ' + now.getDate() + ' 日 ' +
      WEEKDAYS[now.getDay()];

    const ips = (info.server.ips || []).join('、') || '未知';

    this.trayPopup.innerHTML =
      '<div class="tp-time">' + pad2(now.getHours()) + ':' + pad2(now.getMinutes()) + ':' + pad2(now.getSeconds()) + '</div>' +
      '<div class="tp-date">' + ui.escapeHtml(dateText) + '</div>' +
      '<div class="tp-rows">' +
      '<div class="tp-row"><span class="k">服务器</span><span class="v">' + ui.escapeHtml(info.server.hostname) + '</span></div>' +
      '<div class="tp-row"><span class="k">本机 IP</span><span class="v">' + ui.escapeHtml(ips) + '</span></div>' +
      '<div class="tp-row"><span class="k">访问地址</span><span class="v">' + ui.escapeHtml(location.origin) + '</span></div>' +
      '<div class="tp-row"><span class="k">系统</span><span class="v">' + ui.escapeHtml(info.server.platform) + '</span></div>' +
      '<div class="tp-row"><span class="k">运行时长</span><span class="v">' + ui.escapeHtml(formatUptime(info.server.uptime_seconds)) + '</span></div>' +
      '<div class="tp-row"><span class="k">当前用户</span><span class="v">' + ui.escapeHtml(info.user.username) + '</span></div>' +
      '</div>';
  }

  openTrayPopup() {
    this.renderTrayPopup(new Date());
    this.trayPopup.classList.add('open');
    this.closeStartMenu();
  }

  closeTrayPopup() {
    this.trayPopup.classList.remove('open');
  }

  toggleTrayPopup() {
    if (this.trayPopup.classList.contains('open')) {
      this.closeTrayPopup();
    } else {
      this.openTrayPopup();
    }
  }

  /* =========================================================================
     事件绑定
     ========================================================================= */

  bindTaskbar() {
    const self = this;

    this.startBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      self.toggleStartMenu();
    });

    const clock = document.getElementById('trayClock');
    if (clock) {
      clock.addEventListener('click', function (e) {
        e.stopPropagation();
        self.toggleTrayPopup();
      });
    }

    const showDesktop = document.getElementById('showDesktop');
    if (showDesktop) {
      showDesktop.addEventListener('click', function () {
        wm.minimizeAll();
      });
    }

    // 点击任务栏空白处关闭弹出层
    document.getElementById('taskbar').addEventListener('click', function (e) {
      if (e.target.id === 'taskItems' || e.target.classList.contains('taskbar')) {
        self.closeStartMenu();
        self.closeTrayPopup();
      }
    });
  }

  bindDesktop() {
    const self = this;

    // 点击桌面空白处：取消图标选择 + 关闭弹出层
    document.getElementById('desktop').addEventListener('mousedown', function (e) {
      if (e.target.id === 'desktop' || e.target.id === 'wallpaper' || e.target.id === 'desktopIcons') {
        self.iconsEl.querySelectorAll('.desk-icon').forEach(function (el) {
          el.classList.remove('selected');
        });
      }
      if (!self.startMenu.contains(e.target) && e.target !== self.startBtn) {
        self.closeStartMenu();
      }
      if (!self.trayPopup.contains(e.target) && !e.target.closest('#trayClock')) {
        self.closeTrayPopup();
      }
    });

    // 桌面右键菜单
    const desktopEl = document.getElementById('desktop');
    desktopEl.addEventListener('contextmenu', function (e) {
      // 图标自己处理了右键，这里只处理空白区域
      if (e.target.closest('.desk-icon') || e.target.closest('.winbox') || e.target.closest('.taskbar')) {
        return;
      }
      e.preventDefault();

      const hasCustom = !!self.info.ui.wallpaper;

      const menuItems = [
        { label: '刷新', iconName: 'refresh', onClick: function () { location.reload(); } },
        'separator',
        { label: '打开文件资源管理器', iconName: 'app-explorer', onClick: function () { openExplorer(self, '', ''); } }
      ];

      // 命令提示符入口（仅在服务端开启时出现）
      if (self.terminalEnabled()) {
        menuItems.push({
          label: '在此打开命令提示符',
          iconName: 'code',
          onClick: function () { openTerminal(self); }
        });
      }

      menuItems.push(
        { label: '更换桌面壁纸…', iconName: 'image', onClick: function () { self.changeWallpaper(); } },
        {
          label: '恢复默认壁纸', iconName: 'rotate-left',
          disabled: !hasCustom,
          onClick: function () { self.resetWallpaper(); }
        },
        'separator',
        { label: '关于', iconName: 'app-about', onClick: function () { self.showAbout(); } }
      );

      ui.showContextMenu(e.clientX, e.clientY, menuItems);
    });

    // 全局快捷键
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') {
        self.closeStartMenu();
        self.closeTrayPopup();
      }
    });
  }

  /* =========================================================================
     关于 / 注销
     ========================================================================= */

  showAbout() {
    const info = this.info;
    const features = info.features || {};
    const versions = info.versions || {};
    const roots = info.roots || [];

    let text = '';
    text += (info.app.title || info.app.name) + '  v' + info.app.version + '\n\n';

    text += '【服务器】\n';
    text += '  主机名：' + info.server.hostname + '\n';
    text += '  系统：' + info.server.platform + '\n';
    text += '  Python：' + info.server.python + '\n';
    text += '  本机 IP：' + ((info.server.ips || []).join('、') || '未知') + '\n';
    text += '  当前访问：' + location.origin + '\n';
    text += '  运行时长：' + formatUptime(info.server.uptime_seconds) + '\n\n';

    text += '【允许访问的根目录】\n';
    if (roots.length) {
      roots.forEach(function (r) {
        text += '  · ' + r.name + '  ->  ' + r.path + (r.readonly ? '（只读）' : '') + '\n';
      });
    } else {
      text += '  （未配置）\n';
    }
    text += '\n';

    text += '【功能状态】\n';
    text += '  · Office 转换（LibreOffice）：' + (features.libreoffice ? '已安装' : '未安装，doc/xls/ppt 老格式无法预览') + '\n';
    text += '  · 删除到回收站：' + (features.recycle_bin ? '可用' : '不可用（将永久删除）') + '\n';
    text += '  · 缩略图：' + (features.thumbnails ? '已启用' : '已关闭') + '\n';
    text += '  · 命令提示符：' + (features.terminal ? '已启用' : '已关闭') + '\n';
    text += '  · 单文件上传上限：' + (info.limits.max_upload_mb || 2048) + ' MB\n';
    text += '  · 会话有效期：' + (info.limits.session_hours || 12) + ' 小时\n\n';

    text += '【前端组件】\n';
    text += '  · winbox.js ' + (versions.winbox || '未知') + '\n';
    text += '  · pdf.js ' + (versions.pdfjs || '未知') + '\n';

    ui.showAlert('关于 ' + (info.app.title || info.app.name), text, 'app-about');
  }

  logout() {
    ui.showConfirm('注销', '确定要注销当前登录吗？', { okText: '注销' }).then(function (ok) {
      if (!ok) {
        return;
      }
      api.logout().then(function () {
        location.replace('/login.html');
      }).catch(function () {
        // 即使请求失败也跳回登录页
        location.replace('/login.html');
      });
    });
  }
}

export default Desktop;

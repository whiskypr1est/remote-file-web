/* ==========================================================================
   文件资源管理器窗口
   --------------------------------------------------------------------------
   功能：
     * 地址栏（面包屑 + 可编辑输入框，支持粘贴绝对路径回车跳转）
     * 工具栏：后退 / 前进 / 向上 / 刷新 / 视图切换
     * 图标视图 与 详细列表视图（名称、修改日期、类型、大小，可点表头排序）
     * 多选：单击、Ctrl+单击、Shift+范围、Ctrl+A、Esc 取消
     * 文件操作：新建文件夹、重命名、删除（进回收站）、上传（拖拽 / 选择）、下载、批量打包
 * 复制 / 剪切 / 粘贴：剪贴板由所有窗口共享，重名由服务端自动改名（Ctrl+C / Ctrl+X / Ctrl+V）
 * 拖拽：选中项可拖到文件夹或空白处（按住 Ctrl 为复制），外部拖入的文件仍然走上传
     * 「此电脑」视图：展示所有允许访问的根目录及其容量
     * 右键菜单、键盘快捷键（F2 / Delete / F5 / Backspace / Enter）
   ========================================================================== */

import { icon, fileIcon, typeLabel } from './icons.js';
import * as api from './api.js';
import * as ui from './ui.js';
import { wm, registerWindowOwner } from './wins.js';
import { openPreview } from './preview.js';
import { openEditor, canEditText } from './editor.js';
import { openTerminal } from './terminal.js';
import { trackJob } from './jobs.js';

/**
 * 可以「双击直接运行」的脚本扩展名。
 *
 * ★ 与后端 routers/terminal.py 的 RUNNABLE_EXTS **必须保持一致**：
 *   前端这份只决定「菜单里显不显示」，真正的闸门在服务端（那边会 400）。
 *   两边跑偏的表现是「菜单里有、点了报错」或者反过来的那种不一致。
 */
const RUNNABLE_EXTS = /\.(bat|cmd|ps1)$/i;

/* 已经打开的浏览器窗口：windowId -> ExplorerWindow */
const openExplorers = new Map();

/* ---------------------------------------------------------------------------
   复制 / 剪切剪贴板
   ---------------------------------------------------------------------------
   和 Windows 一样是「全体窗口共用的一份」：在一个窗口里复制，切到另一个
   窗口（哪怕是另一个根目录）粘贴也能用，所以放在模块级而不是窗口实例上。
   --------------------------------------------------------------------------- */

const clipboard = {
  mode: '',   // '' | 'copy' | 'cut'
  items: []   // [{root, rel, name}] —— rel 直接拿去调后端接口
};

/** 内部拖拽用的自定义 MIME；外部从资源管理器拖进来的文件只有 "Files" */
const INTERNAL_DRAG_TYPE = 'application/x-fileweb-items';

/** 剪切待粘贴条目的半透明标记类 */
const CUT_CLASS = 'cut-pending';

/** 内部拖拽落点高亮类；不要复用 .dragover，那是外部上传用的遮罩 */
const DROP_CLASS = 'drop-target';

/** 内部拖拽过程中暂存的条目：少数浏览器在 drop 里拿不到 dataTransfer 内容，用它兜底 */
let internalDrag = null;

/**
 * 这次拖拽是不是「内部拖拽」（拖的是列表里的条目）。
 *
 * 必须把两种拖拽严格分开：外部从 Windows 资源管理器拖进来的文件，
 * dataTransfer.types 里只会出现 "Files"；我们自己的拖拽一定带上自定义 MIME。
 * 只有靠它区分，才能保证原有的「拖文件进来上传」不受影响。
 */
function isInternalDrag(e) {
  const dt = e.dataTransfer;
  if (!dt || !dt.types) {
    return false;
  }
  for (let i = 0; i < dt.types.length; i++) {
    if (dt.types[i] === INTERNAL_DRAG_TYPE) {
      return true;
    }
  }
  return false;
}

/** 读出本次内部拖拽携带的条目（root + paths）；取不到返回 null */
function readDragPayload(e) {
  let payload = null;

  const dt = e.dataTransfer;
  if (dt) {
    let raw = '';
    try {
      raw = dt.getData(INTERNAL_DRAG_TYPE);
    } catch (err) {
      raw = '';
    }
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch (err) {
        payload = null;
      }
    }
  }

  if ((!payload || !payload.paths || !payload.paths.length) && internalDrag) {
    payload = internalDrag;
  }
  if (!payload || !payload.paths || !payload.paths.length) {
    return null;
  }
  return payload;
}

/** 当前处于「剪切待粘贴」状态的条目键（root|rel） */
function cutKeys() {
  if (clipboard.mode !== 'cut') {
    return new Set();
  }
  return new Set(clipboard.items.map(function (it) { return it.root + '|' + it.rel; }));
}

/** 剪贴板变化后，把剪切标记同步到所有已打开的窗口 */
function syncCutMarkers() {
  const keys = cutKeys();
  openExplorers.forEach(function (explorer) {
    if (explorer && typeof explorer.syncCutClasses === 'function') {
      explorer.syncCutClasses(keys);
    }
  });
}

/** 清空剪贴板（粘贴完成、按 Esc 取消剪切时调用） */
function clearClipboard() {
  clipboard.mode = '';
  clipboard.items = [];
  syncCutMarkers();
}

/**
 * 内部拖拽的落点：
 *   文件夹行 -> 该文件夹；空白处 -> 当前目录；文件行 -> 无效（返回 null）。
 * 与资源管理器一致：文件本身不是有效的放置目标。
 */
function internalDropTarget(e, explorer) {
  const row = e.target && e.target.closest ? e.target.closest('[data-name]') : null;
  if (!row) {
    return { el: explorer.$body, rel: explorer.relPath };
  }
  const entry = explorer.entriesByName()[row.dataset.name];
  if (!entry || !entry.is_dir) {
    return null;
  }
  return {
    el: row,
    rel: entry.rel || (explorer.relPath ? explorer.relPath + '/' + entry.name : entry.name)
  };
}

/* ---------------------------------------------------------------------------
   压缩包（压缩 / 解压共用）
   ---------------------------------------------------------------------------
   可压缩的格式就这五种，取值和后端 fileweb/routers/fs.py 的 _FORMAT_EXT 对齐；
   解压不必告诉后端格式，它自己按扩展名认，这张表在这里只用来判断
   「选中的文件算不算压缩包、该不该出现解压菜单」。

   entry.ext 是 os.path.splitext 的结果，backup.tar.gz 只给出 ".gz"，
   所以一律拿完整文件名比后缀，并且把更长的后缀排在前面。
   --------------------------------------------------------------------------- */

/** 压缩对话框的格式选项；ext 用来同步压缩包名的后缀 */
const COMPRESS_FORMATS = [
  { value: 'zip', label: 'ZIP 压缩包（.zip）', ext: '.zip' },
  { value: 'tar', label: 'TAR 归档（.tar）', ext: '.tar' },
  { value: 'tar.gz', label: 'TAR.GZ 压缩包（.tar.gz）', ext: '.tar.gz' },
  { value: '7z', label: '7Z 压缩包（.7z）', ext: '.7z' },
  { value: 'rar', label: 'RAR 压缩包（.rar，需装 WinRAR）', ext: '.rar' }
];

/** 后端 detect_format 认得的后缀，长的在前，免得 .tar.gz 被 .gz 抢走 */
const ARCHIVE_EXTS = [
  '.tar.gz', '.tar.bz2', '.tar.xz',
  '.tgz', '.tbz2', '.txz',
  '.zip', '.tar', '.gz', '.bz2', '.xz', '.7z', '.rar'
];

/** 取文件名上的压缩包后缀；取不到返回 ''（即不是压缩包） */
function archiveExtOf(name) {
  const lower = String(name || '').toLowerCase();
  for (let i = 0; i < ARCHIVE_EXTS.length; i++) {
    if (lower.endsWith(ARCHIVE_EXTS[i])) {
      return ARCHIVE_EXTS[i];
    }
  }
  return '';
}

/** 去掉压缩包后缀得到「包名」：默认压缩包名和「解压到 XX 文件夹」都要用它 */
function archiveBaseName(name) {
  const text = String(name || '');
  const ext = archiveExtOf(text);
  const base = ext ? text.slice(0, text.length - ext.length) : text;
  return base || text;
}

/** 把压缩包名的后缀换成 ext；没有已知后缀就直接追加 */
function withArchiveExt(name, ext) {
  const text = String(name || '').trim();
  if (!text) {
    return '';
  }
  const matched = archiveExtOf(text);
  const base = matched ? text.slice(0, text.length - matched.length) : text;
  return (base || text) + ext;
}

/** 多选压缩时的默认包名：打包_20250101_120000.zip */
function packedName() {
  const d = new Date();
  const p = function (n) { return (n < 10 ? '0' : '') + n; };
  return '打包_' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) +
    '_' + p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '.zip';
}

/** 单个条目压缩时的默认包名：文件夹用全名，文件去掉扩展名，都加 .zip */
function singleArchiveName(entry) {
  const name = entry.name || '';
  if (entry.is_dir) {
    return name + '.zip';
  }
  const dot = name.lastIndexOf('.');
  // dot > 0：'.gitignore' 这类点开头的名字不算有扩展名，别裁成空串
  const stem = dot > 0 ? name.slice(0, dot) : name;
  return stem + '.zip';
}

export class ExplorerWindow {
  constructor(record, desktop) {
    this.record = record;
    this.desktop = desktop;
    this.el = record.content;
    this.el.className = 'explorer';

    this.mode = 'computer';     // computer | dir
    this.rootId = '';
    this.relPath = '';
    this.absPath = '';
    this.entries = [];
    this.selection = new Set();
    this.lastClicked = -1;
    this.view = (desktop && desktop.info && desktop.info.ui && desktop.info.ui.default_view) || 'icons';
    this.sort = 'name';
    this.order = 'asc';
    this.history = [];
    this.historyIndex = -1;
    this.atRoot = true;
    this.readonly = false;
    this.destroyed = false;
    this.uploads = [];
    this.loading = false;

    openExplorers.set(record.id, this);
  }

  /* =========================================================================
     初始化
     ========================================================================= */

  start(rootId, rel) {
    this.build();
    this.bind();

    // 关闭前若有上传未完成，拦一下并询问
    this.record.onBeforeClose = () => {
      const active = this.uploads.filter((u) => u.state === 'running');
      if (!active.length) {
        return false;
      }
      ui.showConfirm(
        '仍有上传任务',
        '当前还有 ' + active.length + ' 个文件正在上传，关闭窗口会中断上传。确定要关闭吗？',
        { okText: '中断并关闭', danger: true }
      ).then((ok) => {
        if (ok) {
          this.cancelUploads();
          this.record.onBeforeClose = null;
          wm.close(this.record.id);
        }
      });
      return true;
    };

    this.record.onClosed = () => {
      this.destroyed = true;
      openExplorers.delete(this.record.id);
    };

    if (rootId) {
      this.navigate(rootId, rel || '', { push: false });
    } else {
      this.showComputer();
    }
  }

  /* =========================================================================
     DOM 构建
     ========================================================================= */

  build() {
    this.el.innerHTML = [
      '<div class="exp-toolbar">',
      '  <div class="exp-nav">',
      '    <button type="button" class="nav-btn" data-act="back" title="后退">', icon('arrow-left'), '</button>',
      '    <button type="button" class="nav-btn" data-act="forward" title="前进">', icon('arrow-right'), '</button>',
      '    <button type="button" class="nav-btn" data-act="up" title="向上">', icon('arrow-up'), '</button>',
      '    <button type="button" class="nav-btn" data-act="refresh" title="刷新 (F5)">', icon('refresh'), '</button>',
      '  </div>',
      '  <div class="exp-address">',
      '    <span class="addr-ico">', icon('address'), '</span>',
      '    <div class="exp-crumbs"></div>',
      '    <input type="text" class="addr-input" spellcheck="false" autocomplete="off">',
      '    <button type="button" class="addr-go" title="转到">', icon('arrow-right'), '</button>',
      '  </div>',
      '  <div class="exp-view-toggle">',
      '    <button type="button" class="view-btn" data-view="icons" title="图标视图">', icon('grid'), '</button>',
      '    <button type="button" class="view-btn" data-view="list" title="详细列表">', icon('list'), '</button>',
      '  </div>',
      '</div>',

      '<div class="exp-readonly">当前根目录为只读，不能修改其中的内容。</div>',

      '<div class="exp-actions">',
      '  <button type="button" class="act-btn" data-act="newfolder">', icon('folder-plus'), '<span>新建文件夹</span></button>',
      '  <button type="button" class="act-btn" data-act="newfile">', icon('file-text'), '<span>新建文件</span></button>',
      '  <button type="button" class="act-btn" data-act="upload">', icon('upload'), '<span>上传文件</span></button>',
      '  <button type="button" class="act-btn" data-act="download">', icon('download'), '<span>下载</span></button>',
      '  <span class="act-sep"></span>',
      '  <button type="button" class="act-btn" data-act="rename">', icon('pencil'), '<span>重命名</span></button>',
      '  <button type="button" class="act-btn danger" data-act="delete">', icon('trash'), '<span>删除</span></button>',
      '  <span class="spacer"></span>',
      '  <button type="button" class="act-btn" data-act="refresh">', icon('refresh'), '<span>刷新</span></button>',
      '</div>',

      '<div class="upload-panel"></div>',

      '<div class="exp-body">',
      '  <div class="exp-loading"></div>',
      '  <div class="exp-view exp-icons active"></div>',
      '  <div class="exp-view exp-list"></div>',
      '  <div class="exp-view computer-view"></div>',
      '  <div class="exp-empty">', icon('empty-folder'), '<div>此文件夹为空</div></div>',
      '</div>',

      '<div class="exp-status">',
      '  <div class="st-left"></div>',
      '  <div class="st-right"></div>',
      '</div>',

      '<input type="file" class="file-input" multiple hidden>'
    ].join('');

    this.$toolbar = this.el.querySelector('.exp-toolbar');
    this.$address = this.el.querySelector('.exp-address');
    this.$crumbs = this.el.querySelector('.exp-crumbs');
    this.$addrInput = this.el.querySelector('.addr-input');
    this.$addrGo = this.el.querySelector('.addr-go');
    this.$body = this.el.querySelector('.exp-body');
    this.$icons = this.el.querySelector('.exp-icons');
    this.$list = this.el.querySelector('.exp-list');
    this.$computer = this.el.querySelector('.computer-view');
    this.$empty = this.el.querySelector('.exp-empty');
    this.$status = this.el.querySelector('.exp-status');
    this.$loading = this.el.querySelector('.exp-loading');
    this.$uploadPanel = this.el.querySelector('.upload-panel');
    this.$readonly = this.el.querySelector('.exp-readonly');
    this.$fileInput = this.el.querySelector('.file-input');

    this.applyView();
  }

  bind() {
    const self = this;

    // ---- 工具栏 ----
    this.el.querySelectorAll('.nav-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const act = btn.dataset.act;
        if (act === 'back') { self.goBack(); }
        else if (act === 'forward') { self.goForward(); }
        else if (act === 'up') { self.goUp(); }
        else if (act === 'refresh') { self.refresh(); }
      });
    });

    this.el.querySelectorAll('.view-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        self.setView(btn.dataset.view);
      });
    });

    this.el.querySelectorAll('.act-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const act = btn.dataset.act;
        if (act === 'newfolder') { self.newFolder(); }
        else if (act === 'newfile') { self.newFile(); }
        else if (act === 'upload') { self.$fileInput.click(); }
        else if (act === 'download') { self.downloadSelected(); }
        else if (act === 'rename') { self.renameSelected(); }
        else if (act === 'delete') { self.deleteSelected(); }
        else if (act === 'refresh') { self.refresh(); }
      });
    });

    // ---- 地址栏 ----
    this.$crumbs.addEventListener('click', function (e) {
      const crumb = e.target.closest('.crumb');
      if (crumb) {
        self.gotoCrumb(crumb);
        return;
      }
      // 点空白处进入编辑模式
      self.startAddressEdit();
    });

    this.$addrInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') {
        e.preventDefault();
        self.commitAddress();
      } else if (e.key === 'Escape') {
        e.preventDefault();
        self.endAddressEdit();
      }
    });
    this.$addrInput.addEventListener('blur', function () {
      // 失焦时退出编辑，避免长时间停留
      setTimeout(function () {
        if (document.activeElement !== self.$addrInput) {
          self.endAddressEdit();
        }
      }, 120);
    });
    this.$addrGo.addEventListener('click', function () {
      self.commitAddress();
    });

    // ---- 内容区点击 ----
    this.$icons.addEventListener('mousedown', function (e) {
      self.handleItemMouseDown(e);
    });
    this.$icons.addEventListener('dblclick', function (e) {
      self.handleItemDblClick(e);
    });
    this.$icons.addEventListener('contextmenu', function (e) {
      self.handleContextMenu(e);
    });

    this.$list.addEventListener('mousedown', function (e) {
      self.handleItemMouseDown(e);
    });
    this.$list.addEventListener('dblclick', function (e) {
      self.handleItemDblClick(e);
    });
    this.$list.addEventListener('contextmenu', function (e) {
      self.handleContextMenu(e);
    });
    this.$list.addEventListener('click', function (e) {
      const th = e.target.closest('th[data-sort]');
      if (th) {
        self.sortBy(th.dataset.sort);
      }
    });

    this.$computer.addEventListener('mousedown', function (e) {
      const item = e.target.closest('.cv-item');
      self.$computer.querySelectorAll('.cv-item').forEach(function (el) {
        el.classList.toggle('selected', el === item);
      });
    });
    this.$computer.addEventListener('dblclick', function (e) {
      const item = e.target.closest('.cv-item');
      if (item && item.dataset.root) {
        self.navigate(item.dataset.root, '', { push: true });
      }
    });
    this.$computer.addEventListener('contextmenu', function (e) {
      const item = e.target.closest('.cv-item');
      if (item && item.dataset.root) {
        e.preventDefault();
        ui.showContextMenu(e.clientX, e.clientY, [
          { label: '打开', iconName: 'open', onClick: function () { self.navigate(item.dataset.root, '', { push: true }); } },
          'separator',
          {
            label: '复制路径', iconName: 'copy',
            onClick: function () {
              ui.copyText(item.dataset.path || '').then(function () {
                ui.toast('路径已复制到剪贴板', 'success');
              }).catch(function () {
                ui.toast('复制失败，请手动复制', 'error');
              });
            }
          }
        ]);
      } else {
        self.showBlankMenu(e);
      }
    });

    // ---- 空白区域右键 ----
    this.$icons.addEventListener('contextmenu', function (e) {
      if (!e.target.closest('.icon-item')) {
        self.showBlankMenu(e);
      }
    });
    this.$list.addEventListener('contextmenu', function (e) {
      if (!e.target.closest('tr[data-name]')) {
        self.showBlankMenu(e);
      }
    });

    // ---- 空白处单击取消选择 ----
    this.$icons.addEventListener('click', function (e) {
      if (!e.target.closest('.icon-item')) {
        self.clearSelection();
      }
    });
    this.$list.addEventListener('click', function (e) {
      if (!e.target.closest('tr[data-name]') && !e.target.closest('th')) {
        self.clearSelection();
      }
    });

    // ---- 上传文件选择 ----
    this.$fileInput.addEventListener('change', function () {
      if (self.$fileInput.files && self.$fileInput.files.length) {
        self.uploadFiles(self.$fileInput.files);
      }
      self.$fileInput.value = '';
    });

    // ---- 拖拽：外部拖入文件 = 上传；内部拖拽选中项 = 移动 / 复制 ----
    let dragDepth = 0;

    this.$body.addEventListener('dragstart', function (e) {
      // 在重命名输入框里拖选文字时不要触发条目拖拽
      if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) {
        e.preventDefault();
        return;
      }
      const item = e.target && e.target.closest ? e.target.closest('[data-name]') : null;
      if (!item || self.mode !== 'dir') {
        return;
      }

      const name = item.dataset.name;
      // 拖的是没被选中的条目时先选中它，和资源管理器一致
      if (!self.selection.has(name)) {
        self.selectOnly(name);
        self.lastClicked = self.indexOf(name);
      }
      const list = self.selectedEntries();
      if (!list.length) {
        return;
      }

      const paths = list.map((entry) => entry.rel);
      internalDrag = { root: self.rootId, paths: paths };

      try {
        // 自定义 MIME 是「内部拖拽」的唯一标记，外部文件拖拽不会有它
        e.dataTransfer.setData(INTERNAL_DRAG_TYPE, JSON.stringify(internalDrag));
        e.dataTransfer.setData('text/plain', list.map((entry) => entry.name).join('\r\n'));
        e.dataTransfer.effectAllowed = 'copyMove';
      } catch (err) {
        /* 个别浏览器不允许写自定义类型，此时靠 internalDrag 兜底 */
      }

      self.$body.classList.remove('dragover');
      self.clearDropHighlights();
    });

    this.$body.addEventListener('dragenter', function (e) {
      if (isInternalDrag(e)) {
        return; // 内部拖拽由 dragover 负责高亮，不能弹上传遮罩
      }
      e.preventDefault();
      dragDepth += 1;
      if (self.mode === 'dir' && !self.readonly) {
        self.$body.classList.add('dragover');
      }
    });

    this.$body.addEventListener('dragover', function (e) {
      if (isInternalDrag(e)) {
        self.handleInternalDragOver(e);
        return;
      }
      e.preventDefault();
      if (self.mode === 'dir' && !self.readonly) {
        e.dataTransfer.dropEffect = 'copy';
      }
    });

    this.$body.addEventListener('dragleave', function (e) {
      if (isInternalDrag(e)) {
        // 行的进入/离开会频繁触发，这里只做「真的离开内容区」的粗清理，
        // 最终清理交给 drop / dragend
        const to = e.relatedTarget;
        if (!to || !self.$body.contains(to)) {
          self.clearDropHighlights();
        }
        return;
      }
      e.preventDefault();
      dragDepth = Math.max(0, dragDepth - 1);
      if (dragDepth === 0) {
        self.$body.classList.remove('dragover');
      }
    });

    this.$body.addEventListener('drop', function (e) {
      if (isInternalDrag(e)) {
        self.handleInternalDrop(e);
        return;
      }
      e.preventDefault();
      dragDepth = 0;
      self.$body.classList.remove('dragover');
      if (self.mode !== 'dir' || self.readonly) {
        return;
      }
      const files = e.dataTransfer && e.dataTransfer.files;
      if (files && files.length) {
        // 拖进来的如果既有文件又有目录，只处理文件（浏览器不支持目录上传）
        const list = [];
        for (let i = 0; i < files.length; i++) {
          list.push(files[i]);
        }
        self.uploadFiles(list);
      }
    });

    // 拖拽结束（无论成败）都要把高亮清掉，避免留下一个亮着的框
    this.$body.addEventListener('dragend', function () {
      self.clearDropHighlights();
      // 下一次拖拽前不保留旧数据，免得误当成当前这次拖拽的内容
      setTimeout(function () {
        internalDrag = null;
      }, 0);
    });

    // ---- 键盘快捷键 ----
    this.el.addEventListener('keydown', function (e) {
      self.handleKeyDown(e);
    });
    this.el.setAttribute('tabindex', '-1');
  }

  /* =========================================================================
     导航
     ========================================================================= */

  /** 显示「此电脑」视图 */
  showComputer() {
    this.mode = 'computer';
    this.rootId = '';
    this.relPath = '';
    this.absPath = '';
    this.atRoot = true;
    this.readonly = false;
    this.selection.clear();
    this.entries = [];

    this.renderCrumbs();
    this.renderComputer();
    this.updateButtons();
    this.updateWindowTitle('此电脑');
    this.renderStatus({ dirCount: 0, fileCount: 0, total: 0 });
    this.setLoading(false);
  }

  /**
   * 跳到指定位置。
   *
   * @param {string} rootId 根标识；传空且给了绝对路径时由后端解析
   * @param {string} path   相对路径或绝对路径
   * @param {object} opts   {push: 是否记录到历史}
   */
  navigate(rootId, path, opts) {
    const options = opts || {};
    const self = this;

    this.setLoading(true);

    return api.listDir({
      root: rootId || '',
      path: path || '',
      sort: this.sort,
      order: this.order
    }).then(function (data) {
      if (self.destroyed) {
        return;
      }

      self.mode = 'dir';
      self.rootId = data.root.id;
      self.relPath = data.rel || '';
      self.absPath = data.abs || '';
      self.atRoot = !!data.at_root;
      self.readonly = !!data.readonly;
      self.entries = data.entries || [];
      self.selection.clear();
      self.lastClicked = -1;

      if (options.push !== false) {
        // 截断前进历史，压入新位置
        self.history = self.history.slice(0, self.historyIndex + 1);
        self.history.push({ root: self.rootId, path: self.relPath });
        self.historyIndex = self.history.length - 1;
      } else {
        self.history = [{ root: self.rootId, path: self.relPath }];
        self.historyIndex = 0;
      }

      self.renderCrumbs();
      self.renderEntries();
      self.updateButtons();
      self.updateWindowTitle(data.root.name || '文件资源管理器');
      self.renderStatus(data);
      self.setLoading(false);
    }).catch(function (err) {
      if (self.destroyed) {
        return;
      }
      self.setLoading(false);
      // 会话过期时 api 层已经跳转登录页，这里不再弹窗打扰
      if (err && err.status === 401) {
        return;
      }
      if (self.mode === 'computer' || !self.absPath) {
        self.showComputer();
      }
      console.error('[explorer] 打开位置失败:', err);
      ui.showAlert('无法打开该位置', (err && err.message) || '未知错误', 'error');
    });
  }

  refresh() {
    if (this.mode === 'computer') {
      this.showComputer();
      return Promise.resolve();
    }
    return this.navigate(this.rootId, this.relPath, { push: false }).then(() => {
      // navigate(push:false) 会重置历史，这里恢复原历史以便后退仍然可用
    });
  }

  /**
   * 刷新但保留历史（refresh 的替代实现，供内部调用）
   */
  reload(preserveHistory) {
    const self = this;
    this.setLoading(true);
    return api.listDir({
      root: this.rootId,
      path: this.relPath,
      sort: this.sort,
      order: this.order
    }).then(function (data) {
      if (self.destroyed) {
        return;
      }
      const keep = new Set(self.selection);
      self.entries = data.entries || [];
      self.atRoot = !!data.at_root;
      self.readonly = !!data.readonly;
      self.absPath = data.abs || self.absPath;
      self.relPath = data.rel || '';
      // 保留仍然存在的选中项
      self.selection = new Set(self.entries.filter((e) => keep.has(e.name)).map((e) => e.name));
      self.renderEntries();
      self.updateButtons();
      self.renderStatus(data);
      if (!preserveHistory) {
        self.renderCrumbs();
      }
      self.setLoading(false);
    }).catch(function (err) {
      self.setLoading(false);
      if (err && err.status === 401) {
        return;
      }
      ui.showAlert('刷新失败', (err && err.message) || '未知错误', 'error');
    });
  }

  goBack() {
    if (this.historyIndex <= 0) {
      return;
    }
    this.historyIndex -= 1;
    const target = this.history[this.historyIndex];
    this.navigateToHistory(target);
  }

  goForward() {
    if (this.historyIndex >= this.history.length - 1) {
      return;
    }
    this.historyIndex += 1;
    const target = this.history[this.historyIndex];
    this.navigateToHistory(target);
  }

  navigateToHistory(target) {
    const self = this;
    if (!target.root) {
      this.showComputer();
      this.historyIndex = this.historyIndex; // 保持不变
      this.updateButtons();
      return;
    }
    api.listDir({ root: target.root, path: target.path, sort: this.sort, order: this.order })
      .then(function (data) {
        self.mode = 'dir';
        self.rootId = data.root.id;
        self.relPath = data.rel || '';
        self.absPath = data.abs || '';
        self.atRoot = !!data.at_root;
        self.readonly = !!data.readonly;
        self.entries = data.entries || [];
        self.selection.clear();
        self.renderCrumbs();
        self.renderEntries();
        self.updateButtons();
        self.updateWindowTitle(data.root.name || '文件资源管理器');
        self.renderStatus(data);
      })
      .catch(function (err) {
        if (err && err.status !== 401) {
          ui.showAlert('无法打开该位置', (err && err.message) || '未知错误', 'error');
        }
      });
  }

  goUp() {
    if (this.mode !== 'dir') {
      return;
    }
    if (this.atRoot) {
      // 已经在根目录顶层 -> 回到「此电脑」
      this.history = this.history.slice(0, this.historyIndex + 1);
      this.history.push({ root: '', path: '' });
      this.historyIndex = this.history.length - 1;
      this.showComputer();
      return;
    }
    const parts = this.relPath ? this.relPath.split('/') : [];
    parts.pop();
    this.navigate(this.rootId, parts.join('/'), { push: true });
  }

  gotoCrumb(crumb) {
    if (crumb.dataset.kind === 'computer') {
      this.history = this.history.slice(0, this.historyIndex + 1);
      this.history.push({ root: '', path: '' });
      this.historyIndex = this.history.length - 1;
      this.showComputer();
      return;
    }
    this.navigate(crumb.dataset.root, crumb.dataset.rel || '', { push: true });
  }

  /* ---- 地址栏 ---- */

  renderCrumbs() {
    const parts = [];

    parts.push('<span class="crumb" data-kind="computer">此电脑</span>');

    if (this.mode === 'computer') {
      this.$crumbs.innerHTML = parts.join('');
      this.$addrInput.value = '此电脑';
      return;
    }

    const rootCfg = (this.desktop.info.roots || []).find((r) => r.id === this.rootId);
    const rootName = rootCfg ? rootCfg.name : this.rootId;

    parts.push('<span class="crumb-sep">›</span>');
    parts.push('<span class="crumb" data-kind="root" data-root="' + ui.escapeHtml(this.rootId) +
      '" data-rel="">' + ui.escapeHtml(rootName) + '</span>');

    if (this.relPath) {
      const segments = this.relPath.split('/');
      // 注意：普通 function 回调里的 this 不是 ExplorerWindow（模块是严格模式，
      // this 为 undefined），所以要先把 rootId 取出来再用。
      const rootId = this.rootId;
      let acc = '';
      segments.forEach(function (seg, index) {
        acc = acc ? acc + '/' + seg : seg;
        const isLast = index === segments.length - 1;
        parts.push('<span class="crumb-sep">›</span>');
        parts.push('<span class="crumb' + (isLast ? ' current' : '') +
          '" data-kind="dir" data-root="' + ui.escapeHtml(rootId) +
          '" data-rel="' + ui.escapeHtml(acc) + '">' + ui.escapeHtml(seg) + '</span>');
      });
    }

    this.$crumbs.innerHTML = parts.join('');
    // 地址栏显示真实绝对路径，方便直接复制给别的程序使用
    this.$addrInput.value = this.absPath || '';
  }

  startAddressEdit() {
    this.$address.classList.add('editing');
    this.$addrInput.value = this.mode === 'computer' ? '此电脑' : (this.absPath || '');
    this.$addrInput.focus();
    this.$addrInput.select();
  }

  endAddressEdit() {
    this.$address.classList.remove('editing');
  }

  commitAddress() {
    const raw = (this.$addrInput.value || '').trim();
    this.endAddressEdit();

    if (!raw || raw === '此电脑' || raw === '我的电脑') {
      this.history = this.history.slice(0, this.historyIndex + 1);
      this.history.push({ root: '', path: '' });
      this.historyIndex = this.history.length - 1;
      this.showComputer();
      return;
    }

    // 绝对路径交给后端解析；相对路径按当前根目录解析
    const looksAbsolute = /^[A-Za-z]:[\\/]/.test(raw) || raw.startsWith('\\\\');
    if (looksAbsolute) {
      this.navigate('', raw, { push: true });
    } else if (this.mode === 'dir') {
      this.navigate(this.rootId, raw.replace(/\\/g, '/'), { push: true });
    } else {
      ui.showAlert('无法打开该位置', '请输入完整的绝对路径，例如 D:\\Share\\文档', 'warning');
    }
  }

  /* =========================================================================
     视图渲染
     ========================================================================= */

  setView(view) {
    this.view = view === 'list' ? 'list' : 'icons';
    this.applyView();
    if (this.mode === 'dir') {
      this.renderEntries();
    }
  }

  applyView() {
    this.el.querySelectorAll('.view-btn').forEach((btn) => {
      btn.classList.toggle('active', btn.dataset.view === this.view);
    });
    this.$icons.classList.toggle('active', this.view === 'icons' && this.mode === 'dir');
    this.$list.classList.toggle('active', this.view === 'list' && this.mode === 'dir');
    this.$computer.classList.toggle('active', this.mode === 'computer');
  }

  setLoading(on) {
    this.loading = !!on;
    this.$loading.classList.toggle('show', !!on);
  }

  updateButtons() {
    const isDir = this.mode === 'dir';
    const count = this.selection.size;

    this.el.querySelector('[data-act="back"]').disabled = this.historyIndex <= 0;
    this.el.querySelector('[data-act="forward"]').disabled = this.historyIndex >= this.history.length - 1;
    this.el.querySelector('[data-act="up"]').disabled = !isDir;
    this.el.querySelector('[data-act="refresh"]').disabled = !isDir;

    const canWrite = isDir && !this.readonly;
    this.el.querySelector('[data-act="newfolder"]').disabled = !canWrite;
    this.el.querySelector('[data-act="newfile"]').disabled = !canWrite;
    this.el.querySelector('[data-act="upload"]').disabled = !canWrite;
    this.el.querySelector('[data-act="rename"]').disabled = !canWrite || count !== 1;
    this.el.querySelector('[data-act="delete"]').disabled = !canWrite || count === 0;
    this.el.querySelector('[data-act="download"]').disabled = count === 0;

    this.$readonly.classList.toggle('show', isDir && this.readonly);
  }

  updateWindowTitle(title) {
    const finalTitle = title || '文件资源管理器';
    try {
      this.record.win.setTitle(finalTitle);
    } catch (err) {
      /* 窗口可能已关闭 */
    }
    const label = this.record.taskBtn && this.record.taskBtn.querySelector('.tb-label');
    if (label) {
      label.textContent = finalTitle;
    }
  }

  /* ---- 「此电脑」视图 ---- */

  renderComputer() {
    const self = this;
    const roots = this.desktop.info.roots || [];

    let html = '<div class="cv-group-title">设备和驱动器（允许访问的根目录）</div><div class="cv-grid">';

    if (!roots.length) {
      html += '<div style="padding:16px;color:#9096a0">未配置任何可访问的根目录，请检查 config.json 的 roots 字段。</div>';
    }

    roots.forEach(function (root) {
      const disk = root.disk || {};
      let bar = '';
      let sub = root.path;
      if (disk.total) {
        const usedPct = Math.min(100, Math.round((disk.used / disk.total) * 100));
        bar = '<div class="cv-bar"><i style="width:' + usedPct + '%"></i></div>';
        sub = ui.formatSize(disk.free) + ' 可用，共 ' + ui.formatSize(disk.total);
      }
      // 自动挂载的磁盘用盘符图标，配置里手写的目录用文件夹图标
      const iconName = root.kind === 'drive' ? 'drive' : 'folder';
      const typeLabel = root.type_label ? root.type_label + ' · ' : '';

      html += '<div class="cv-item" data-root="' + ui.escapeHtml(root.id) +
        '" data-path="' + ui.escapeHtml(root.path) + '">' +
        '<div class="cv-icon">' + icon(iconName) + '</div>' +
        '<div class="cv-main">' +
        '<div class="cv-name">' + ui.escapeHtml(root.name) + (root.readonly ? '（只读）' : '') + '</div>' +
        '<div class="cv-sub">' + ui.escapeHtml(typeLabel + sub) + '</div>' + bar +
        '</div></div>';
    });

    html += '</div>';
    this.$computer.innerHTML = html;
    this.applyView();
    this.$empty.classList.remove('show');
  }

  /* ---- 目录内容 ---- */

  renderEntries() {
    this.applyView();

    const entries = this.entries;
    this.$empty.classList.toggle('show', entries.length === 0);

    if (this.view === 'icons') {
      this.renderIconsView();
    } else {
      this.renderListView();
    }
    this.attachThumbFallback();
    // 重新渲染后要把「剪切待粘贴」的半透明标记补回去
    this.syncCutClasses();
  }

  iconCellHtml(entry) {
    // 图片走缩略图；加载失败时由 attachThumbFallback 换回类型图标
    if (entry.thumb_url && !entry.is_dir) {
      return '<img src="' + ui.escapeHtml(entry.thumb_url) + '" alt="" loading="lazy" ' +
        'data-thumb="1" data-type="' + ui.escapeHtml(entry.type) + '">';
    }
    return fileIcon(entry.type);
  }

  renderIconsView() {
    const self = this;
    const html = this.entries.map(function (entry) {
      const selected = self.selection.has(entry.name) ? ' selected' : '';
      const hidden = entry.hidden ? ' hidden-file' : '';
      return '<div class="icon-item' + selected + hidden + '" data-name="' + ui.escapeHtml(entry.name) +
        '" draggable="true">' +
        '<div class="item-icon">' + self.iconCellHtml(entry) + '</div>' +
        '<div class="item-name" title="' + ui.escapeHtml(entry.name) + '">' +
        ui.escapeHtml(entry.name) + '</div>' +
        '</div>';
    }).join('');
    this.$icons.innerHTML = html;
  }

  renderListView() {
    const self = this;
    const arrowFor = function (field) {
      if (self.sort !== field) {
        return '';
      }
      return '<span class="sort-arrow">' + (self.order === 'asc' ? '▲' : '▼') + '</span>';
    };

    let html = '<table><thead><tr>' +
      '<th class="c-name" data-sort="name"><span class="th-inner">名称' + arrowFor('name') + '</span></th>' +
      '<th class="c-mtime" data-sort="mtime"><span class="th-inner">修改日期' + arrowFor('mtime') + '</span></th>' +
      '<th class="c-type" data-sort="type"><span class="th-inner">类型' + arrowFor('type') + '</span></th>' +
      '<th class="c-size" data-sort="size"><span class="th-inner">大小' + arrowFor('size') + '</span></th>' +
      '</tr></thead><tbody>';

    this.entries.forEach(function (entry) {
      const selected = self.selection.has(entry.name) ? ' selected' : '';
      const hidden = entry.hidden ? ' hidden-file' : '';
      html += '<tr class="' + (selected + hidden).trim() + '" data-name="' + ui.escapeHtml(entry.name) +
        '" draggable="true">' +
        '<td class="col-name c-name">' +
        '<span class="li-icon">' + self.iconCellHtml(entry) + '</span>' +
        '<span class="li-text" title="' + ui.escapeHtml(entry.name) + '">' + ui.escapeHtml(entry.name) + '</span>' +
        '</td>' +
        '<td class="c-mtime num">' + ui.escapeHtml(entry.mtime_text || '') + '</td>' +
        '<td class="c-type">' + ui.escapeHtml(entry.is_dir ? '文件夹' : typeLabel(entry.type, entry.ext)) + '</td>' +
        '<td class="c-size num">' + ui.escapeHtml(entry.is_dir ? '' : (entry.size_text || '')) + '</td>' +
        '</tr>';
    });

    html += '</tbody></table>';
    this.$list.innerHTML = html;
  }

  /** 缩略图加载失败 -> 换成类型图标 */
  attachThumbFallback() {
    [this.$icons, this.$list].forEach(function (container) {
      container.querySelectorAll('img[data-thumb]').forEach(function (img) {
        img.addEventListener('error', function () {
          const type = img.dataset.type || 'other';
          const span = document.createElement('span');
          span.innerHTML = fileIcon(type);
          const svg = span.firstChild;
          if (svg && img.parentNode) {
            img.parentNode.replaceChild(svg, img);
          }
        }, { once: true });
      });
    });
  }

  renderStatus(data) {
    const info = data || {};
    const selected = this.selection.size;
    const total = info.total || 0;

    let left = '';
    if (this.mode === 'computer') {
      const roots = this.desktop.info.roots || [];
      left = '<span class="st-item">' + icon('drive') + '<span>' + roots.length + ' 个根目录</span></span>';
    } else if (total === 0) {
      left = '<span class="st-item">此文件夹为空</span>';
    } else {
      left = '<span class="st-item">' + (info.dir_count || 0) + ' 个文件夹，' +
        (info.file_count || 0) + ' 个文件</span>';
      if (selected > 0) {
        left += '<span class="st-item">已选择 ' + selected + ' 项</span>';
      }
      if (info.skipped) {
        left += '<span class="st-item">' + info.skipped + ' 项无法读取</span>';
      }
    }

    let right = '';
    if (this.mode === 'dir') {
      const disk = info.disk || {};
      if (disk.free) {
        right += '<span class="st-item">' + icon('drive') + '<span>可用 ' +
          ui.formatSize(disk.free) + '</span></span>';
      }
      right += '<span class="st-item">' + (this.readonly ? '只读' : '可写') + '</span>';
    }

    this.$status.querySelector('.st-left').innerHTML = left;
    this.$status.querySelector('.st-right').innerHTML = right;
  }

  sortBy(field) {
    if (this.sort === field) {
      this.order = this.order === 'asc' ? 'desc' : 'asc';
    } else {
      this.sort = field;
      this.order = 'asc';
    }
    this.reload(true);
  }

  /* =========================================================================
     选择
     ========================================================================= */

  entriesByName() {
    const map = {};
    this.entries.forEach(function (e) {
      map[e.name] = e;
    });
    return map;
  }

  indexOf(name) {
    for (let i = 0; i < this.entries.length; i++) {
      if (this.entries[i].name === name) {
        return i;
      }
    }
    return -1;
  }

  selectedEntries() {
    return this.entries.filter((e) => this.selection.has(e.name));
  }

  clearSelection() {
    this.selection.clear();
    this.lastClicked = -1;
    this.syncSelectionClasses();
    this.updateButtons();
    this.renderStatus({ total: this.entries.length, dir_count: this.countDirs(), file_count: this.countFiles() });
  }

  countDirs() {
    return this.entries.filter((e) => e.is_dir).length;
  }

  countFiles() {
    return this.entries.filter((e) => !e.is_dir).length;
  }

  selectOnly(name) {
    this.selection.clear();
    this.selection.add(name);
    this.syncSelectionClasses();
    this.updateButtons();
    this.renderStatus({ total: this.entries.length, dir_count: this.countDirs(), file_count: this.countFiles() });
  }

  toggleSelect(name) {
    if (this.selection.has(name)) {
      this.selection.delete(name);
    } else {
      this.selection.add(name);
    }
    this.syncSelectionClasses();
    this.updateButtons();
    this.renderStatus({ total: this.entries.length, dir_count: this.countDirs(), file_count: this.countFiles() });
  }

  selectRange(name) {
    const from = this.lastClicked >= 0 ? this.lastClicked : 0;
    const to = this.indexOf(name);
    if (to < 0) {
      return;
    }
    const start = Math.min(from, to);
    const end = Math.max(from, to);
    this.selection.clear();
    for (let i = start; i <= end; i++) {
      this.selection.add(this.entries[i].name);
    }
    this.syncSelectionClasses();
    this.updateButtons();
    this.renderStatus({ total: this.entries.length, dir_count: this.countDirs(), file_count: this.countFiles() });
  }

  selectAll() {
    this.selection.clear();
    this.entries.forEach((e) => this.selection.add(e.name));
    this.syncSelectionClasses();
    this.updateButtons();
    this.renderStatus({ total: this.entries.length, dir_count: this.countDirs(), file_count: this.countFiles() });
  }

  /* =========================================================================
     键盘导航（方向键 / Home / End / PageUp / PageDown / 首字母跳转）
     ========================================================================= */

  /** 当前「光标」的条目下标：优先 lastClicked，其次唯一的选中项，都没有则 -1 */
  cursorIndex() {
    if (this.lastClicked >= 0 && this.lastClicked < this.entries.length) {
      return this.lastClicked;
    }
    if (this.selection.size === 1) {
      return this.indexOf(this.selection.values().next().value);
    }
    return -1;
  }

  /** 图标视图里一行有几个条目（把「上下」换算成条目步长要用它） */
  iconColumns() {
    const items = this.$icons.querySelectorAll('[data-name]');
    if (items.length < 2) {
      return 1;
    }
    const firstTop = items[0].offsetTop;
    let columns = 0;
    for (let i = 0; i < items.length; i++) {
      if (items[i].offsetTop !== firstTop) {
        break;      // 换行了，说明一行的个数已经数完
      }
      columns++;
    }
    return Math.max(1, columns);
  }

  /** 一屏大约能显示多少个条目（PageUp / PageDown 用） */
  pageStep() {
    const container = this.view === 'icons' ? this.$icons : this.$list;
    const first = container.querySelector('[data-name]');
    const itemHeight = first ? first.getBoundingClientRect().height : 0;
    const viewHeight = container.clientHeight || 0;

    const rows = (itemHeight > 0 && viewHeight > 0)
      ? Math.max(1, Math.floor(viewHeight / itemHeight))
      : 10;
    return this.view === 'icons' ? rows * this.iconColumns() : rows;
  }

  /** 把某个条目滚进可视区 —— 方向键移动后必须跟上，否则选中项会在视野之外 */
  scrollEntryIntoView(name) {
    const container = this.view === 'icons' ? this.$icons : this.$list;
    const cell = container.querySelector('[data-name="' + cssEscape(name) + '"]');
    if (cell && cell.scrollIntoView) {
      cell.scrollIntoView({ block: 'nearest' });
    }
  }

  /**
   * 相对移动选择。
   * @param {number} step 条目步长（正数向下、负数向上）
   * @param {boolean} extend 是否按住 Shift 扩展选区
   */
  moveSelection(step, extend) {
    const total = this.entries.length;
    if (!total) {
      return;
    }

    const current = this.cursorIndex();
    if (current < 0) {
      // 还没有光标：向下从第一项开始、向上从最后一项开始（与 Windows 一致）
      this.moveSelectionTo(step > 0 ? 0 : total - 1, extend);
      return;
    }
    this.moveSelectionTo(current + step, extend);
  }

  /** 把选择移到指定下标（Home / End / 方向键最终都汇到这里） */
  moveSelectionTo(index, extend) {
    const total = this.entries.length;
    if (!total) {
      return;
    }
    const target = Math.min(total - 1, Math.max(0, index));
    const name = this.entries[target].name;

    if (extend) {
      // Shift+方向键：以原锚点为准扩展范围。锚点不存在时先把当前项立成锚点，
      // 否则 selectRange 会从第 0 项算起，按一下就顺手选走一大片。
      if (this.lastClicked < 0) {
        const current = this.cursorIndex();
        this.lastClicked = current >= 0 ? current : target;
      }
      this.selectRange(name);
    } else {
      this.selectOnly(name);
      this.lastClicked = target;
    }

    this.scrollEntryIntoView(name);
  }

  /**
   * 首字母跳转（type-ahead）。
   *
   * 连续敲入的字符拼成一个前缀（与 Windows 资源管理器一致），
   * 停手超过一小段时间就重新开始。搜索从「当前项的下一个」起绕一圈，
   * 所以反复按同一个字母能在同首字母的条目之间轮转。
   *
   * @returns {boolean} 是否找到目标（没找到就不消费这次按键）
   */
  typeAhead(char) {
    const TYPE_AHEAD_RESET_MS = 800;

    const now = Date.now();
    if (now - (this.typeAheadAt || 0) > TYPE_AHEAD_RESET_MS) {
      this.typeAheadText = '';
    }
    this.typeAheadAt = now;
    this.typeAheadText = (this.typeAheadText || '') + String(char).toLowerCase();

    const query = this.typeAheadText;
    const total = this.entries.length;
    if (!total || !query) {
      return false;
    }

    const start = this.cursorIndex() + 1;
    for (let offset = 0; offset < total; offset++) {
      const index = ((start + offset) % total + total) % total;
      const name = String(this.entries[index].name || '').toLowerCase();
      if (name.indexOf(query) === 0) {
        this.selectOnly(this.entries[index].name);
        this.lastClicked = index;
        this.scrollEntryIntoView(this.entries[index].name);
        return true;
      }
    }
    return false;
  }

  syncSelectionClasses() {
    const self = this;
    [this.$icons, this.$list].forEach(function (container) {
      container.querySelectorAll('[data-name]').forEach(function (el) {
        el.classList.toggle('selected', self.selection.has(el.dataset.name));
      });
    });
  }

  /* =========================================================================
     交互事件
     ========================================================================= */

  handleItemMouseDown(e) {
    const item = e.target.closest('[data-name]');
    if (!item) {
      return;
    }
    const name = item.dataset.name;
    const index = this.indexOf(name);

    if (e.ctrlKey || e.metaKey) {
      this.toggleSelect(name);
      this.lastClicked = index;
    } else if (e.shiftKey && this.lastClicked >= 0) {
      this.selectRange(name);
    } else {
      // 已被多选时拖动/点击不立即清空，交给 dblclick 处理更符合直觉
      if (!this.selection.has(name)) {
        this.selectOnly(name);
      }
      this.lastClicked = index;
    }
    // 让窗口获得焦点，保证键盘快捷键可用
    this.el.focus({ preventScroll: true });
  }

  handleItemDblClick(e) {
    const item = e.target.closest('[data-name]');
    if (!item) {
      return;
    }
    const entry = this.entriesByName()[item.dataset.name];
    if (entry) {
      this.openEntry(entry);
    }
  }

  handleContextMenu(e) {
    const item = e.target.closest('[data-name]');
    if (!item) {
      return; // 交给空白菜单处理
    }
    e.preventDefault();
    e.stopPropagation();

    const name = item.dataset.name;
    if (!this.selection.has(name)) {
      this.selectOnly(name);
      this.lastClicked = this.indexOf(name);
    }

    const entry = this.entriesByName()[name];
    if (entry) {
      this.showItemMenu(e.clientX, e.clientY, entry);
    }
  }

  handleKeyDown(e) {
    // 正在重命名时不要抢按键
    if (e.target && (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA')) {
      return;
    }
    if (this.mode !== 'dir') {
      return;
    }

    // Ctrl+C / Ctrl+X / Ctrl+V：复制、剪切、粘贴（与资源管理器一致）
    if (e.ctrlKey || e.metaKey) {
      const combo = String(e.key || '').toLowerCase();
      if (combo === 'c') {
        e.preventDefault();
        this.copySelection(false);
        return;
      }
      if (combo === 'x') {
        e.preventDefault();
        this.copySelection(true);
        return;
      }
      if (combo === 'v') {
        e.preventDefault();
        this.pasteClipboard();
        return;
      }
      if (combo === 'a') {
        e.preventDefault();
        this.selectAll();
        return;
      }
    }

    // ---- 方向键 / Home / End / PageUp / PageDown：移动选择 ----
    // 放在功能键之前判：这几个是最高频的浏览操作，先判掉最省事
    if (this.handleNavigationKey(e)) {
      return;
    }

    if (e.key === 'F5') {
      e.preventDefault();
      this.reload(false);
    } else if (e.key === 'F2') {
      e.preventDefault();
      this.renameSelected();
    } else if (e.key === 'Delete') {
      e.preventDefault();
      this.deleteSelected();
    } else if (e.key === 'Backspace') {
      e.preventDefault();
      this.goUp();
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const list = this.selectedEntries();
      if (list.length === 1) {
        this.openEntry(list[0]);
      }
    } else if (e.key === 'Escape') {
      // Esc 取消「剪切」状态：半透明标记随之消失，不再处于待移动状态
      if (clipboard.mode === 'cut') {
        clearClipboard();
        ui.toast('已取消剪切', 'info');
      }
      this.clearSelection();
      ui.hideContextMenu();
    } else if (!e.ctrlKey && !e.metaKey && !e.altKey && e.key && e.key.length === 1) {
      // 首字母跳转（type-ahead）。放在整条链的最后：只有前面谁都没认领，
      // 才把单个可打印字符当成「跳到以它开头的条目」。
      if (this.typeAhead(e.key)) {
        e.preventDefault();
      }
    }
  }

  /**
   * 处理方向键 / Home / End / PageUp / PageDown。
   *
   * @returns {boolean} 是否消费了这次按键（false 表示交给后面的分支）
   */
  handleNavigationKey(e) {
    const total = this.entries.length;
    if (!total) {
      return false;
    }

    // 图标视图是**二维网格**：上下要按「一行几个」跳，左右才是 ±1。
    // 列表视图只有一维，左右键不参与选择 —— 与 Windows 资源管理器一致。
    const isGrid = this.view === 'icons';
    const step = isGrid ? this.iconColumns() : 1;

    switch (e.key) {
      case 'ArrowDown':
        this.moveSelection(step, e.shiftKey);
        break;
      case 'ArrowUp':
        this.moveSelection(-step, e.shiftKey);
        break;
      case 'ArrowLeft':
        if (!isGrid) { return false; }
        this.moveSelection(-1, e.shiftKey);
        break;
      case 'ArrowRight':
        if (!isGrid) { return false; }
        this.moveSelection(1, e.shiftKey);
        break;
      case 'Home':
        this.moveSelectionTo(0, e.shiftKey);
        break;
      case 'End':
        this.moveSelectionTo(total - 1, e.shiftKey);
        break;
      case 'PageDown':
        this.moveSelection(this.pageStep(), e.shiftKey);
        break;
      case 'PageUp':
        this.moveSelection(-this.pageStep(), e.shiftKey);
        break;
      default:
        return false;
    }

    e.preventDefault();
    return true;
  }

  openEntry(entry) {
    if (entry.is_dir) {
      const target = this.relPath ? this.relPath + '/' + entry.name : entry.name;
      this.navigate(this.rootId, target, { push: true });
      return;
    }

    openPreview({
      desktop: this.desktop,
      explorer: this,
      rootId: this.rootId,
      rel: entry.rel || (this.relPath ? this.relPath + '/' + entry.name : entry.name),
      name: entry.name,
      sizeText: entry.size_text || ui.formatSize(entry.size),
      entry: entry
    });
  }

  /**
   * 用编辑器窗口打开选中的文件（右键「编辑」）。
   *
   * 只读根目录不该走到这里（菜单项已经置灰），但目录双击、快捷键之类
   * 将来若也会调进来，这里再兜一次，给一句人话而不是让后端返回 403。
   */
  editEntry(entry) {
    if (this.readonly) {
      ui.toast('当前根目录为只读，不能编辑其中的文件', 'warn');
      return null;
    }

    return openEditor({
      desktop: this.desktop,
      rootId: this.rootId,
      rel: entry.rel || (this.relPath ? this.relPath + '/' + entry.name : entry.name),
      name: entry.name
    });
  }

  /* =========================================================================
     右键菜单
     ========================================================================= */

  showItemMenu(x, y, entry) {
    const self = this;
    const selected = this.selectedEntries();
    const many = selected.length > 1;
    const canWrite = !this.readonly;
    /* 命令行功能是否可用（服务端 features.terminal）。
       关闭时不显示「运行 / 在此处打开命令行」—— 点了只会拿到 403。 */
    const terminalEnabled =
      !!((this.desktop && this.desktop.info &&
          this.desktop.info.features && this.desktop.info.features.terminal));

    const items = [];

    if (!many) {
      items.push({
        label: entry.is_dir ? '打开' : '打开（预览）',
        iconName: 'open',
        onClick: function () { self.openEntry(entry); }
      });
    } else {
      items.push({
        label: '打开选中的 ' + selected.length + ' 项',
        iconName: 'open',
        onClick: function () {
          // 多选时只打开文件夹，避免一次弹出十几个窗口
          const dirs = selected.filter((e) => e.is_dir);
          if (dirs.length) {
            self.openEntry(dirs[0]);
          } else {
            ui.toast('多选状态下不支持批量打开文件', 'warn');
          }
        }
      });
    }

    items.push({
      label: many ? '打包下载 ' + selected.length + ' 项' : '下载',
      iconName: 'download',
      onClick: function () { self.downloadSelected(); }
    });

    // 下面这几项才是「在用户自己的目录里留下压缩包」，和上面的「打包下载」不是一回事：
    // 打包下载只是临时压缩给浏览器，这里会真的多出一个文件，所以放在一起但要能分清
    items.push({
      label: many ? '压缩选中的 ' + selected.length + ' 项…' : '压缩…',
      iconName: 'archive',
      disabled: !canWrite,
      onClick: function () { self.compressSelected(); }
    });

    if (!many && !entry.is_dir && archiveExtOf(entry.name)) {
      // 解压要往目录里写东西；后端遇到同名顶层项会整体拒绝，
      // 所以多给一条「解到新建的子文件夹」，那是绕开冲突最省事的出路
      const folder = archiveBaseName(entry.name) || '解压结果';
      items.push({
        label: '解压到当前文件夹',
        iconName: 'folder-open',
        disabled: !canWrite,
        onClick: function () { self.extractHere(entry); }
      });
      items.push({
        label: '解压到「' + folder + '」文件夹',
        iconName: 'folder-plus',
        disabled: !canWrite,
        onClick: function () { self.extractToOwnFolder(entry); }
      });
    }

    // 在窗口里直接改文本文件。和上面的「压缩 / 解压」一样属于**会写服务端**的操作，
    // 所以：
    //   * 只对文件出现（目录没有扩展名，编辑它没有意义）；
    //   * 扩展名不在支持列表里就不出现 —— 与其给一个点了才知道不支持的菜单项，
    //     不如干脆不显示（是不是支持由 editor.js 的 canEditText 统一说了算）；
    //   * 只读根目录下置灰（disabled: !canWrite），保存本来也会被后端 403 挡掉，
    //     置灰是为了别让用户走到那一步才发现。
    if (!entry.is_dir && canEditText(entry.name)) {
      items.push({
        label: '编辑',
        iconName: 'pencil',
        disabled: !canWrite,
        onClick: function () { self.editEntry(entry); }
      });
    }

    // ---- 「运行脚本」/「在此处打开命令行」--------------------------------
    // 虚拟桌面能看见服务器上的文件，但**跑起来**是另一件事：
    //   * 双击 .bat 目前只会进编辑器（.bat 在 fsops.py 里登记成 code 类型），
    //     那是「改脚本」不是「跑脚本」；
    //   * 批处理里几乎都用相对路径引用同目录的文件（`java -jar server.jar`），
    //     所以必须能**以脚本所在目录为工作目录**开命令行。
    // 这两项都只在功能开启时出现（features.terminal），否则点了会弹 403。
    if (terminalEnabled) {
      if (!many && !entry.is_dir && RUNNABLE_EXTS.test(entry.name)) {
        items.push({
          label: '运行',
          iconName: 'play',
          onClick: function () {
            self.runScript(entry);
          }
        });
      }
      if (!many && entry.is_dir) {
        items.push({
          label: '在此处打开命令行',
          iconName: 'code',
          onClick: function () {
            self.openTerminalHere(entry);
          }
        });
      }
    }

    items.push('separator');

    items.push({
      label: '复制',
      iconName: 'copy',
      shortcut: 'Ctrl+C',
      onClick: function () { self.copySelection(false); }
    });

    items.push({
      label: '剪切',
      iconName: 'external',
      shortcut: 'Ctrl+X',
      // 剪切之后要把源删掉，只读根目录不允许
      disabled: !canWrite,
      onClick: function () { self.copySelection(true); }
    });

    items.push('separator');

    items.push({
      label: '重命名',
      iconName: 'pencil',
      shortcut: 'F2',
      disabled: !canWrite || many,
      onClick: function () { self.renameSelected(); }
    });

    items.push({
      label: '删除',
      iconName: 'trash',
      shortcut: 'Delete',
      danger: true,
      disabled: !canWrite,
      onClick: function () { self.deleteSelected(); }
    });

    items.push('separator');

    items.push({
      label: '发送到桌面快捷方式',
      iconName: 'shortcut',
      disabled: many,
      onClick: function () { self.sendToDesktop(entry); }
    });

    items.push({
      label: '复制完整路径',
      iconName: 'copy',
      onClick: function () {
        const paths = selected.map(function (e) {
          return self.absPath + '\\' + e.name;
        });
        ui.copyText(paths.join('\r\n')).then(function () {
          ui.toast('已复制 ' + paths.length + ' 条路径', 'success');
        }).catch(function () {
          ui.toast('复制失败，请手动复制', 'error');
        });
      }
    });

    ui.showContextMenu(x, y, items);
  }

  showBlankMenu(e) {
    e.preventDefault();
    e.stopPropagation();

    const self = this;
    const canWrite = this.mode === 'dir' && !this.readonly;

    ui.showContextMenu(e.clientX, e.clientY, [
      {
        label: '刷新', iconName: 'refresh', shortcut: 'F5',
        disabled: this.mode !== 'dir',
        onClick: function () { self.reload(false); }
      },
      'separator',
      {
        label: '新建文件夹', iconName: 'folder-plus',
        disabled: !canWrite,
        onClick: function () { self.newFolder(); }
      },
      {
        label: '新建文件', iconName: 'file-text',
        disabled: !canWrite,
        onClick: function () { self.newFile(); }
      },
      {
        label: '上传文件', iconName: 'upload',
        disabled: !canWrite,
        onClick: function () { self.$fileInput.click(); }
      },
      'separator',
      {
        label: '粘贴', iconName: 'copy', shortcut: 'Ctrl+V',
        // 剪贴板为空、或当前目录只读时都不能粘贴
        disabled: !canWrite || !clipboard.items.length,
        onClick: function () { self.pasteClipboard(); }
      },
      'separator',
      {
        label: '全选', iconName: 'check', shortcut: 'Ctrl+A',
        disabled: this.mode !== 'dir',
        onClick: function () { self.selectAll(); }
      },
      'separator',
      {
        label: '图标视图', iconName: 'grid',
        disabled: this.mode !== 'dir',
        onClick: function () { self.setView('icons'); }
      },
      {
        label: '详细列表', iconName: 'list',
        disabled: this.mode !== 'dir',
        onClick: function () { self.setView('list'); }
      }
    ]);
  }

  /* =========================================================================
     复制 / 剪切 / 粘贴
     ========================================================================= */

  /**
   * 把「剪切待粘贴」的半透明标记同步到本窗口的条目上。
   * 不传 keys 时按当前剪贴板算；每次重新渲染、切换目录后都会调用一次。
   */
  syncCutClasses(keys) {
    const self = this;
    const set = keys || cutKeys();
    const map = this.entriesByName();

    [this.$icons, this.$list].forEach(function (container) {
      container.querySelectorAll('[data-name]').forEach(function (el) {
        const entry = map[el.dataset.name];
        if (!entry) {
          el.classList.remove(CUT_CLASS);
          return;
        }
        const rel = entry.rel || (self.relPath ? self.relPath + '/' + entry.name : entry.name);
        el.classList.toggle(CUT_CLASS, set.has(self.rootId + '|' + rel));
      });
    });
  }

  /** 把当前选中项放进剪贴板；cut=true 为剪切，否则为复制 */
  copySelection(cut) {
    if (this.mode !== 'dir') {
      return;
    }
    const list = this.selectedEntries();
    if (!list.length) {
      ui.toast('请先选择要' + (cut ? '剪切' : '复制') + '的项目', 'warn');
      return;
    }
    if (cut && this.readonly) {
      // 剪切意味着随后要删掉源，只读根目录不允许
      ui.toast('当前根目录为只读，不能剪切', 'warn');
      return;
    }

    clipboard.mode = cut ? 'cut' : 'copy';
    clipboard.items = list.map((entry) => ({
      root: this.rootId,
      rel: entry.rel || (this.relPath ? this.relPath + '/' + entry.name : entry.name),
      name: entry.name
    }));
    syncCutMarkers();

    ui.toast('已' + (cut ? '剪切' : '复制') + ' ' + clipboard.items.length +
      ' 项，在目标文件夹按 Ctrl+V 或右键「粘贴」即可' + (cut ? '移动' : '复制'), 'info');
  }

  /** 把剪贴板里的内容粘贴到当前目录 */
  pasteClipboard() {
    if (this.mode !== 'dir') {
      return;
    }
    if (!clipboard.items.length) {
      ui.toast('剪贴板是空的', 'warn');
      return;
    }
    if (this.readonly) {
      ui.toast('当前根目录为只读，不能粘贴', 'warn');
      return;
    }

    // 一次请求只能有一个源根；正常情况下剪贴板里的内容都来自同一个窗口
    const srcRoot = clipboard.items[0].root;
    for (let i = 0; i < clipboard.items.length; i++) {
      if (clipboard.items[i].root !== srcRoot) {
        ui.toast('剪贴板里的项目来自不同位置，请分开粘贴', 'warn');
        return;
      }
    }

    // 剪切 = 移动，复制 = 复制
    this.transferTo(srcRoot, clipboard.items.map((it) => it.rel), this.relPath,
      clipboard.mode !== 'cut');
  }

  /**
   * 把一组条目复制 / 移动到本窗口内指定目录（相对当前根目录）。
   *
   * 粘贴和拖拽都走这里，保证两条入口的行为完全一致：
   * 重名由服务端自动改名，失败项由服务端回报，最后统一刷新并提示。
   */
  transferTo(srcRoot, paths, targetRel, isCopy) {
    const self = this;
    if (this.mode !== 'dir' || this.readonly || !paths.length) {
      return;
    }

    // 目标是条目本身、或它自己的子目录时先剔除：
    // 后端遇到这种情况会整批拒绝，先在前端挡掉才不会连累同批的其它项。
    const target = targetRel || '';
    const filtered = paths.filter(function (rel) {
      return rel !== target && target.indexOf(rel + '/') !== 0;
    });
    if (!filtered.length) {
      ui.toast('不能把文件夹' + (isCopy ? '复制' : '移动') + '到它自己或它的子文件夹里面', 'warn');
      return;
    }

    // 走后台队列：立刻拿到 job_id，进度与取消交给右下角的进度面板。
    // 之所以不再用整屏遮罩：复制几十 GB 时那层遮罩会把整个桌面锁住，
    // 用户既看不到进度、也没法继续做别的事 —— 这正是当初最难受的地方。
    const submit = isCopy
      ? api.copyEntries(srcRoot, filtered, this.rootId, target, true)
      : api.moveEntries(srcRoot, filtered, this.rootId, target, true);

    submit.then(function (res) {
      // 后台模式只返回 {job_id}；trackJob 会把终态结果交回来，形状与原来的
      // 同步接口完全一致，所以下面这段处理逻辑一个字都不用改。
      return trackJob(res.job_id);
    }).then(function (res) {
      if (res.failures && res.failures.length) {
        ui.showAlert(isCopy ? '部分项目复制失败' : '部分项目移动失败',
          res.failures.join('\n'), 'warning');
      } else {
        ui.toast(res.message || (isCopy ? '复制完成' : '移动完成'), 'success');
        if (res.renamed && res.renamed.length) {
          // 重名自动改名也要明确告诉用户，否则会以为覆盖掉了原文件
          ui.toast('有 ' + res.renamed.length + ' 项存在同名，已自动改名', 'warn');
        }
      }

      // 移动完成后剪贴板里的东西已经不在原位，清掉以免重复操作；
      // 有失败项时保留，方便用户重试
      if (!isCopy && !(res.failures && res.failures.length)) {
        clearClipboard();
      }
      return self.reload(true);
    }).catch(function (err) {
      if (err && err.cancelled) {
        // 用户主动取消不是故障，别弹错误框吓人
        ui.toast(isCopy ? '已取消复制' : '已取消移动', 'info');
        return self.reload(true);
      }
      if (err && err.status !== 401) {
        ui.showAlert(isCopy ? '复制失败' : '移动失败',
          (err && err.message) || '未知错误', 'error');
      }
    });
  }

  /* =========================================================================
     内部拖拽（把条目拖到文件夹或空白处）
     ========================================================================= */

  /** 拖拽经过内容区：算出落点并高亮；落点无效时明确禁止放下 */
  handleInternalDragOver(e) {
    if (this.mode !== 'dir' || this.readonly) {
      return;
    }

    const target = internalDropTarget(e, this);
    if (!target) {
      this.clearDropHighlights();
      if (e.dataTransfer) {
        e.dataTransfer.dropEffect = 'none';
      }
      return;
    }

    // 必须 preventDefault，浏览器才允许在这里放下
    e.preventDefault();
    if (e.dataTransfer) {
      // 按住 Ctrl 是复制，否则是移动
      e.dataTransfer.dropEffect = (e.ctrlKey || e.metaKey) ? 'copy' : 'move';
    }
    this.setDropHighlight(target.el);
  }

  /** 松手：按落点执行复制 / 移动 */
  handleInternalDrop(e) {
    const payload = readDragPayload(e);
    this.clearDropHighlights();

    if (!payload) {
      return;
    }
    if (this.mode !== 'dir' || this.readonly) {
      ui.toast('当前根目录为只读，不能接收拖入的项目', 'warn');
      return;
    }

    const target = internalDropTarget(e, this);
    if (!target) {
      ui.toast('只能把项目拖放到文件夹上', 'warn');
      return;
    }

    e.preventDefault();
    e.stopPropagation();

    // 按住 Ctrl 拖是复制，否则是移动（与资源管理器一致）
    const isCopy = !!(e.ctrlKey || e.metaKey);
    this.transferTo(payload.root, payload.paths, target.rel, isCopy);
  }

  /** 高亮唯一的落点（某个文件夹行，或整个空白区） */
  setDropHighlight(el) {
    this.clearDropHighlights();
    if (el) {
      el.classList.add(DROP_CLASS);
    }
  }

  clearDropHighlights() {
    this.$body.classList.remove(DROP_CLASS);
    [this.$icons, this.$list].forEach(function (container) {
      container.querySelectorAll('.' + DROP_CLASS).forEach(function (el) {
        el.classList.remove(DROP_CLASS);
      });
    });
  }

  /* =========================================================================
     文件操作
     ========================================================================= */

  /* =========================================================================
     在虚拟桌面里运行脚本 / 在此处开命令行
     ========================================================================= */

  /**
   * 「运行」：新开一个命令行窗口，并在其中执行这个脚本。
   *
   * ★ 为什么必须「新开一个终端窗口并让它去跑」而不是后端静默执行：
   *   运行结果（输出、报错、以及脚本自己要不要继续读输入）都在那个
   *   命令行窗口里，用户看得见、也能接着操作 —— 这正是「虚拟桌面上
   *   双击 bat」该有的样子。后端把脚本串进 cmd 的 /K 参数里，
   *   所以输出留在**这个会话**里（另起进程会跑到别的控制台去）。
   *
   * 工作目录由服务端设成脚本所在目录：批处理里几乎都用相对路径引用
   * 同目录的文件（`java -jar server.jar`），cwd 不对就会直接失败。
   */
  runScript(entry) {
    const rel = entry.rel || (this.relPath ? this.relPath + '/' + entry.name : entry.name);
    return openTerminal(this.desktop, {
      run: { root: this.rootId, path: rel }
    });
  }

  /** 「在此处打开命令行」：新开一个命令行窗口，工作目录就是这个文件夹 */
  openTerminalHere(entry) {
    const rel = entry.rel || (this.relPath ? this.relPath + '/' + entry.name : entry.name);
    return openTerminal(this.desktop, {
      startDir: { root: this.rootId, path: rel }
    });
  }

  /**
   * 把条目发送到虚拟桌面。
   *
   * 只会往服务端的 desktop_shortcuts.json 写一条记录，
   * 不会在 Windows 真实桌面上创建任何东西；重启服务后依然存在。
   */
  sendToDesktop(entry) {
    const self = this;
    const rel = entry.rel || (this.relPath ? this.relPath + '/' + entry.name : entry.name);

    api.createShortcut(this.rootId, rel, entry.name).then(function (res) {
      ui.toast(res.message || '已发送到桌面', 'success');
      // 让桌面立刻刷新出新图标，不用手动 F5
      if (self.desktop && typeof self.desktop.reloadShortcuts === 'function') {
        self.desktop.reloadShortcuts();
      }
    }).catch(function (err) {
      if (err && err.status !== 401) {
        ui.showAlert('创建快捷方式失败', (err && err.message) || '未知错误', 'error');
      }
    });
  }

  newFolder() {
    const self = this;
    if (this.mode !== 'dir' || this.readonly) {
      return;
    }

    ui.showPrompt('新建文件夹', '请输入文件夹名称：', '新建文件夹').then(function (name) {
      if (name === null) {
        return;
      }
      const trimmed = String(name).trim();
      if (!trimmed) {
        ui.toast('名称不能为空', 'warn');
        return;
      }

      api.makeDir(self.rootId, self.relPath, trimmed).then(function (res) {
        ui.toast(res.message || '文件夹已创建', 'success');
        return self.reload(true);
      }).then(function () {
        // 新建后自动进入重命名状态，和资源管理器一致
        self.startInlineRename(trimmed);
      }).catch(function (err) {
        if (err && err.status !== 401) {
          ui.showAlert('新建失败', err.message, 'error');
        }
      });
    });
  }

  /**
   * 新建空文件。
   *
   * 与「新建文件夹」的两点不同：
   *   1. 文件名带扩展名，且**扩展名由用户自己决定** —— 对话框里的「常用类型」
   *      下拉只是快捷补全，照样可以直接敲列表里没有的后缀（例如 .tsv）；
   *   2. 创建后**不**进入就地重命名（名字刚在对话框里确认过），改为选中新条目，
   *      方便紧接着双击打开编辑。
   */
  newFile() {
    const self = this;
    if (this.mode !== 'dir' || this.readonly) {
      return;
    }

    ui.showFileNameDialog({
      title: '新建文件',
      label: '请输入文件名（含扩展名）：',
      defaultName: '新建文本文档.txt'
    }).then(function (name) {
      if (name === null) {
        return;
      }
      const trimmed = String(name).trim();
      if (!trimmed) {
        ui.toast('文件名不能为空', 'warn');
        return;
      }

      api.newFile(self.rootId, self.relPath, trimmed).then(function (res) {
        ui.toast(res.message || '文件已创建', 'success');
        return self.reload(true).then(function () {
          // 用服务端回传的真实名字选中，避免前后端对文件名的处理不一致
          self.selectOnly(res.name || trimmed);
        });
      }).catch(function (err) {
        if (err && err.status !== 401) {
          ui.showAlert('新建失败', err.message, 'error');
        }
      });
    });
  }

  /** 在列表/图标里就地重命名某个条目 */
  startInlineRename(name) {
    const self = this;
    const container = this.view === 'icons' ? this.$icons : this.$list;
    const cell = container.querySelector('[data-name="' + cssEscape(name) + '"]');
    if (!cell) {
      return;
    }

    const isList = this.view === 'list';
    const target = isList ? cell.querySelector('.li-text') : cell.querySelector('.item-name');
    if (!target) {
      return;
    }

    const input = document.createElement('input');
    input.type = 'text';
    input.className = 'item-rename';
    input.value = name;
    target.replaceWith(input);

    const dotIndex = name.lastIndexOf('.');
    input.focus();
    if (dotIndex > 0) {
      input.setSelectionRange(0, dotIndex);   // 只选中主文件名，不选扩展名
    } else {
      input.select();
    }

    let finished = false;

    function finish(commit) {
      if (finished) {
        return;
      }
      finished = true;
      const newName = input.value.trim();
      input.replaceWith(target);

      if (!commit || !newName || newName === name) {
        return;
      }

      const rel = self.relPath ? self.relPath + '/' + name : name;
      api.renameEntry(self.rootId, rel, newName).then(function (res) {
        ui.toast(res.message || '重命名成功', 'success');
        return self.reload(true);
      }).catch(function (err) {
        if (err && err.status !== 401) {
          ui.showAlert('重命名失败', err.message, 'error');
        }
      });
    }

    input.addEventListener('keydown', function (e) {
      e.stopPropagation();
      if (e.key === 'Enter') {
        e.preventDefault();
        finish(true);
      } else if (e.key === 'Escape') {
        e.preventDefault();
        finish(false);
      }
    });
    input.addEventListener('blur', function () {
      finish(true);
    });
  }

  renameSelected() {
    const list = this.selectedEntries();
    if (list.length !== 1 || this.readonly) {
      return;
    }
    this.startInlineRename(list[0].name);
  }

  deleteSelected() {
    const self = this;
    const list = this.selectedEntries();
    if (!list.length || this.readonly) {
      return;
    }

    const names = list.map((e) => e.name);
    const preview = names.slice(0, 12).map((n) => '· ' + n).join('\n') +
      (names.length > 12 ? '\n… 等共 ' + names.length + ' 项' : '');

    const recycle = (this.desktop.info.features || {}).recycle_bin;
    const tip = recycle
      ? '删除后可以从系统回收站恢复。'
      : '注意：当前为永久删除，删除后无法恢复。';

    ui.showConfirm(
      '确认删除',
      '确定要删除以下 ' + names.length + ' 项吗？\n\n' + preview + '\n\n' + tip,
      { okText: '删除', danger: true, iconName: 'warning' }
    ).then(function (ok) {
      if (!ok) {
        return;
      }
      const paths = list.map((e) => e.rel);

      // 删除失败时统一走这里。回收站失败不代表没救了：
      // 永久删除走的是另一条路（不经过 Windows 外壳），通常能删掉，
      // 所以给一个重试出口 —— 以前失败后用户除了改 config.json 重启服务
      // 之外没有任何办法，这是真正的体验断点。
      const reportFailures = function (res) {
        if (!res.failures || !res.failures.length) {
          ui.toast(res.message || '删除完成', 'success');
          return Promise.resolve();
        }

        const detail = res.failures.join('\n');
        if (!res.can_retry_permanent) {
          // 已经是永久删除，再失败就没有别的退路了，如实展示
          ui.showAlert('删除失败', detail, 'warning');
          return Promise.resolve();
        }

        return ui.showConfirm(
          '删除未完成',
          detail + '\n\n是否改用【永久删除】重试？\n' +
            '永久删除不经过回收站，通常能删掉，但删除之后无法恢复。',
          { okText: '永久删除重试', cancelText: '取消', danger: true, iconName: 'warning' }
        ).then(function (yes) {
          if (!yes) {
            return;
          }
          return api.deleteEntries(self.rootId, paths, true).then(function (again) {
            if (again.failures && again.failures.length) {
              ui.showAlert('永久删除也失败了', again.failures.join('\n'), 'error');
            } else {
              ui.toast(again.message || '已永久删除', 'success');
            }
          });
        });
      };

      return api.deleteEntries(self.rootId, paths, !recycle)
        .then(reportFailures)
        .then(function () {
          return self.reload(true);
        });
    }).catch(function (err) {
      if (err && err.status !== 401) {
        ui.showAlert('删除失败', err.message, 'error');
      }
    });
  }

  downloadSelected() {
    const list = this.selectedEntries();
    if (!list.length) {
      return;
    }

    if (list.length === 1 && !list[0].is_dir) {
      api.triggerDownload(api.downloadUrl(this.rootId, list[0].rel));
      ui.toast('已开始下载：' + list[0].name, 'success');
      return;
    }

    const self = this;
    const paths = list.map((e) => e.rel);

    ui.setBusy(true, '正在服务器端打包，请稍候…');
    api.createZip(this.rootId, paths).then(function (res) {
      ui.setBusy(false);
      api.triggerDownload(res.download_url);
      let msg = res.filename + '（' + res.file_count + ' 个文件，' + res.size_text + '）';
      if (res.skipped && res.skipped.length) {
        msg += '\n有 ' + res.skipped.length + ' 项被跳过';
      }
      ui.toast(msg, 'success', '打包完成');
    }).catch(function (err) {
      ui.setBusy(false);
      if (err && err.status !== 401) {
        ui.showAlert('打包失败', err.message, 'error');
      }
    });
  }

  /* ---- 压缩 / 解压（产物落在用户自己的目录里） ---- */

  /**
   * 把选中项压缩成压缩包，存在当前文件夹里。
   *
   * 目标固定为当前文件夹而不做「压缩到别的目录」：那需要再来一个目录选择器，
   * 而当前文件夹是用户此刻唯一有把握的地方。
   */
  compressSelected() {
    const self = this;
    const list = this.selectedEntries();
    if (this.mode !== 'dir' || !list.length) {
      return;
    }
    if (this.readonly) {
      ui.toast('当前根目录为只读，不能压缩', 'warn');
      return;
    }

    const paths = list.map((e) => e.rel || (this.relPath ? this.relPath + '/' + e.name : e.name));
    const defaultName = list.length === 1 ? singleArchiveName(list[0]) : packedName();

    openCompressDialog(defaultName).then(function (opts) {
      if (!opts) {
        return;
      }

      ui.setBusy(true, '正在压缩…');
      api.compressEntries({
        root: self.rootId,
        paths: paths,
        target_root: self.rootId,
        target_path: self.relPath,
        name: opts.name,
        format: opts.format,
        level: 3
      }).then(function (res) {
        ui.setBusy(false);
        // 名字以服务端返回的为准：同名时它会自动加序号，拿用户输入的名字去猜会报错名字
        ui.toast(res.name + '（' + res.file_count + ' 个文件，' + res.size_text + '）',
          'success', '压缩完成');
        if (res.renamed) {
          ui.toast('已有同名压缩包，本次保存为 ' + res.name, 'warn');
        }
        return self.reload(true);
      }).catch(function (err) {
        ui.setBusy(false);
        if (err && err.status !== 401) {
          ui.showAlert('压缩失败', err.message, 'error');
        }
      });
    });
  }

  /**
   * 解压到压缩包自己所在的文件夹（就是当前文件夹）。
   *
   * 目标留空交给服务端按「压缩包所在目录」算，和当前目录是同一个地方；
   * 真撞名的话后端会整体拒绝并列出名来，那时再走 extractToOwnFolder。
   */
  extractHere(entry) {
    this.runExtract(entry, '', '');
  }

  /**
   * 解压到以压缩包名新建的子文件夹。
   *
   * 后端不替我们建目录（目标不存在直接 404），所以先建再用；目录已经存在就直接
   * 拿它来解压 —— 里面真有重名项的话，解压那一步报出来的名字比「文件夹已存在」有用。
   */
  extractToOwnFolder(entry) {
    const self = this;
    const folder = archiveBaseName(entry.name) || '解压结果';
    const targetRel = this.relPath ? this.relPath + '/' + folder : folder;

    if (this.entries.some((e) => e.is_dir && e.name === folder)) {
      this.runExtract(entry, this.rootId, targetRel);
      return;
    }

    ui.setBusy(true, '正在新建文件夹…');
    api.makeDir(this.rootId, this.relPath, folder).then(function () {
      ui.setBusy(false);
      self.runExtract(entry, self.rootId, targetRel);
    }).catch(function (err) {
      ui.setBusy(false);
      if (!err || err.status === 401) {
        return;
      }
      // 目录已存在（列表可能是旧的）：照旧往里解，冲突交给解压那一步去说
      if (err.status === 409) {
        self.runExtract(entry, self.rootId, targetRel);
        return;
      }
      ui.showAlert('新建文件夹失败', err.message, 'error');
    });
  }

  /**
   * 调后端解压，并统一处理「重名整体拒绝」这种产品化的失败。
   *
   * @param {object} entry      选中的压缩包条目
   * @param {string} targetRoot 目标根目录 id；和 targetPath 一起留空表示解到包所在的目录
   * @param {string} targetPath 目标相对路径
   */
  runExtract(entry, targetRoot, targetPath) {
    const self = this;
    const rel = entry.rel || (this.relPath ? this.relPath + '/' + entry.name : entry.name);

    // 同样走后台队列：解压一个大包可能要好几分钟，用整屏遮罩锁住界面
    // 是没法接受的（而且重名冲突是在提交那一刻就报回来的，不受影响）。
    api.extractArchive({
      root: this.rootId,
      path: rel,
      target_root: targetRoot,
      target_path: targetPath,
      // 覆盖一批文件几乎没法回退，前端保持「不覆盖」这个默认，让用户换个目录重来
      overwrite: false,
      background: true
    }).then(function (res) {
      return trackJob(res.job_id);
    }).then(function (res) {
      ui.toast((res.target ? res.target + '：' : '') + '已解压 ' + res.extracted +
        ' 项（' + res.size_text + '）', 'success', '解压完成');
      if (res.skipped && res.skipped.length) {
        // 符号链接之类的条目是后端有意跳过的，得让用户知道少了什么
        ui.showAlert('部分项目已跳过', res.skipped.join('\n'), 'warning');
      }
      return self.reload(true);
    }).catch(function (err) {
      if (err && err.cancelled) {
        ui.toast('已取消解压', 'info');
        return self.reload(true);
      }
      if (!err || err.status === 401) {
        return;
      }
      if (err.status === 409) {
        self.offerExtractToNewFolder(entry, err.message, targetPath);
        return;
      }
      ui.showAlert('解压失败', (err && err.message) || '未知错误', 'error');
    });
  }

  /**
   * 撞名时的出路：问一句要不要解压到以包名新建的子文件夹。
   *
   * 已经在那个子文件夹里了就不再劝（会变成死循环式的重复提问），
   * 直接把后端那句话摆出来。
   */
  offerExtractToNewFolder(entry, message, targetPath) {
    const self = this;
    const folder = archiveBaseName(entry.name) || '解压结果';
    const ownRel = this.relPath ? this.relPath + '/' + folder : folder;

    if (targetPath === ownRel) {
      ui.showAlert('解压失败', message, 'error');
      return;
    }

    ui.showConfirm(
      '目标文件夹里已有同名项',
      message + '\n\n是否改为解压到新建的「' + folder + '」文件夹？',
      { okText: '解压到「' + folder + '」', iconName: 'folder-plus' }
    ).then(function (ok) {
      if (ok) {
        self.extractToOwnFolder(entry);
      }
    });
  }

  /* ---- 上传 ---- */

  uploadFiles(files) {
    if (this.mode !== 'dir' || this.readonly) {
      return;
    }
    const self = this;
    const list = Array.prototype.slice.call(files);
    if (!list.length) {
      return;
    }

    const maxMb = (this.desktop.info.limits || {}).max_upload_mb || 2048;
    const blocked = (this.desktop.info.limits || {}).blocked_extensions || [];

    let skipped = 0;
    const accepted = [];

    list.forEach(function (file) {
      const dot = file.name.lastIndexOf('.');
      const ext = dot >= 0 ? file.name.slice(dot).toLowerCase() : '';

      if (blocked.indexOf(ext) !== -1) {
        ui.toast('已跳过可执行文件：' + file.name, 'warn');
        skipped += 1;
        return;
      }
      if (file.size > maxMb * 1024 * 1024) {
        ui.toast('已跳过超过 ' + maxMb + 'MB 的文件：' + file.name, 'warn');
        skipped += 1;
        return;
      }
      accepted.push(file);
    });

    if (!accepted.length) {
      if (skipped) {
        ui.toast('没有可上传的文件', 'warn');
      }
      return;
    }

    // 依次上传：串行可以让进度条更清晰，也避免把服务器磁盘 IO 打满
    let chain = Promise.resolve();

    accepted.forEach(function (file) {
      chain = chain.then(function () {
        return self.uploadOne(file);
      });
    });

    chain.then(function () {
      ui.toast('上传任务已全部结束', 'success');
      return self.reload(true);
    });
  }

  uploadOne(file) {
    const self = this;

    const row = document.createElement('div');
    row.className = 'up-row';
    row.innerHTML =
      '<span class="up-name" title="' + ui.escapeHtml(file.name) + '">' + ui.escapeHtml(file.name) + '</span>' +
      '<span class="up-bar"><i></i></span>' +
      '<span class="up-text">等待中</span>';
    this.$uploadPanel.appendChild(row);
    this.$uploadPanel.classList.add('show');

    const bar = row.querySelector('.up-bar i');
    const text = row.querySelector('.up-text');

    const task = { name: file.name, state: 'running', row: row, abort: null };
    this.uploads.push(task);

    return api.uploadFile(this.rootId, this.relPath, file, function (loaded, total) {
      const pct = total ? Math.round((loaded / total) * 100) : 0;
      bar.style.width = pct + '%';
      text.textContent = pct + '% · ' + ui.formatSize(loaded) + '/' + ui.formatSize(total);
    }, task).then(function (res) {
      task.state = 'done';
      bar.style.width = '100%';
      row.classList.add('done');
      text.textContent = '完成';
      if (res.renamed) {
        text.textContent = '已重命名';
        ui.toast('存在同名文件，已保存为 ' + res.name, 'warn');
      }
      self.scheduleUploadPanelCleanup();
    }).catch(function (err) {
      task.state = 'error';
      row.classList.add('error');
      text.textContent = (err && err.code === 'aborted') ? '已取消' : '失败';
      if (err && err.status !== 401 && err.code !== 'aborted') {
        ui.toast('上传失败：' + file.name + ' —— ' + err.message, 'error');
      }
    });
  }

  /** 全部完成后过几秒自动收起进度面板 */
  scheduleUploadPanelCleanup() {
    const self = this;
    if (!this.uploads.length || this.uploads.some((u) => u.state === 'running')) {
      return;
    }
    setTimeout(function () {
      if (self.destroyed) {
        return;
      }
      if (self.uploads.some((u) => u.state === 'running')) {
        return;
      }
      self.uploads = [];
      self.$uploadPanel.innerHTML = '';
      self.$uploadPanel.classList.remove('show');
    }, 2600);
  }

  cancelUploads() {
    this.uploads.forEach(function (task) {
      if (task.state === 'running' && typeof task.abort === 'function') {
        task.abort();
      }
    });
  }

  /* =========================================================================
     布局持久化（供 sessionstate.js 调用）
     ========================================================================= */

  /**
   * 导出「值得记住」的位置信息。
   *
   * 只导出这些字段是有意的：
   *   - 不导出选中项、上传队列、加载中状态 —— 这些是瞬时的，还原回来反而
   *     会让用户困惑（上次选中的文件现在为什么是选中的？）。
   *   - 不导出排序字段 sort/order：本次需求没要求，且后端 listDir 每次都
   *     重新排序，存了也只影响首次请求。
   */
  serialize() {
    return {
      mode: this.mode === 'computer' ? 'computer' : 'dir',
      root: this.rootId || '',
      path: this.relPath || '',
      view: this.view === 'list' ? 'list' : 'icons',
      // 历史栈整体带走，还原后才能继续用「后退 / 前进」
      history: (this.history || []).map(function (item) {
        return { root: item.root || '', path: item.path || '' };
      }),
      historyIndex: this.historyIndex
    };
  }

  /**
   * 从保存的状态还原窗口位置。
   *
   * 关键点：
   *   1. 目录可能已经被删除、或者盘符已经拔掉 —— 那就退回「此电脑」，
   *      绝不能让还原过程抛异常（调用方还有别的窗口要还原）。
   *   2. 历史栈里失效的条目直接丢掉（保留仍然存在的），否则用户点「后退」
   *      会撞进一个错误的弹窗。
   *
   * @param {object} saved serialize() 产出的对象
   * @returns {Promise<void>}
   */
  restoreFromState(saved) {
    const self = this;
    const data = saved || {};
    const view = data.view === 'list' ? 'list' : 'icons';

    if (data.mode === 'computer' || !data.root) {
      this.showComputer();
      if (view !== this.view) {
        this.setView(view);
      }
      return Promise.resolve();
    }

    return this.navigate(data.root, data.path || '', { push: false }).then(function () {
      if (self.destroyed) {
        return;
      }
      // navigate 失败时会自己退回「此电脑」并弹提示，这里不再覆盖用户看到的界面
      if (self.mode !== 'dir') {
        return;
      }

      // 还原历史栈：过滤掉失效条目，并把 index 重新指向当前目录
      const items = (data.history || []).filter(function (item) {
        return item && typeof item.root === 'string';
      });
      if (items.length) {
        self.history = items;
        const idx = Number(data.historyIndex);
        self.historyIndex = (Number.isFinite(idx) && idx >= 0 && idx < items.length)
          ? idx
          : items.length - 1;
      }

      if (view !== self.view) {
        self.setView(view);
      }
      self.updateButtons();
    });
  }

  destroy() {
    this.destroyed = true;
    openExplorers.delete(this.record.id);
  }
}

/**
 * 压缩选项对话框：压缩包名字 + 格式。
 *
 * ui.js 的 showPrompt 只有一个文本框，塞不下格式下拉框；这里照它的 openDialog
 * 自己拼一个，但复用同一套 .dialog-* 样式，外观和别的对话框保持一致，
 * 关闭时机（Esc / 点遮罩 / Enter 确认）也对齐。
 *
 * @param {string} defaultName 默认压缩包名（含扩展名）
 * @returns {Promise<{name:string, format:string}|null>} 取消返回 null
 */
function openCompressDialog(defaultName) {
  // 对话框样式都在 desktop.css 里，.dialog-body 只给 input 做了皮肤；
  // 这次改动不碰样式文件，所以就地内联一份同样的外观给下拉框用
  const selectStyle = 'width:100%;height:32px;padding:0 8px;font-size:13px;' +
    'border:1px solid #b9bfc7;border-radius:3px;outline:none;font-family:inherit;' +
    'background:#fff;color:#2b3038';

  let options = '';
  COMPRESS_FORMATS.forEach(function (fmt) {
    options += '<option value="' + fmt.value + '">' + ui.escapeHtml(fmt.label) + '</option>';
  });

  const mask = document.createElement('div');
  mask.className = 'dialog-mask';

  const dialog = document.createElement('div');
  dialog.className = 'dialog';
  dialog.innerHTML =
    '<div class="dialog-title"><span class="d-ico">' + icon('archive') +
      '</span><span>压缩</span></div>' +
    '<div class="dialog-body">' +
      '<div style="margin-bottom:8px">压缩包名称：</div>' +
      '<input type="text" id="zipNameInput" spellcheck="false" value="' +
        ui.escapeHtml(defaultName) + '">' +
      '<div style="margin:12px 0 8px">压缩格式：</div>' +
      '<select id="zipFormatSelect" style="' + selectStyle + '">' + options + '</select>' +
      '<div class="dialog-hint">压缩包会生成在当前文件夹里，不会改动被压缩的文件。</div>' +
    '</div>' +
    '<div class="dialog-foot">' +
      '<button type="button" class="btn" data-ok="0">取消</button>' +
      '<button type="button" class="btn primary" data-ok="1">开始压缩</button>' +
    '</div>';

  mask.appendChild(dialog);
  document.body.appendChild(mask);

  requestAnimationFrame(function () {
    mask.classList.add('open');
  });

  const nameEl = dialog.querySelector('#zipNameInput');
  const formatEl = dialog.querySelector('#zipFormatSelect');

  let settled = false;
  let resolve;
  const promise = new Promise(function (res) { resolve = res; });

  function close(value) {
    if (settled) {
      return;
    }
    settled = true;
    document.removeEventListener('keydown', onKey, true);
    mask.classList.remove('open');
    setTimeout(function () {
      mask.remove();
    }, 160);
    resolve(value);
  }

  function currentValue() {
    // 名字留空不算错：服务端会自动起名，格式仍然由下拉框决定
    return { name: String(nameEl.value || '').trim(), format: formatEl.value };
  }

  // 换格式必须同步改后缀：后端只在名字没有可识别后缀时才用 format 字段，
  // 两边都给了却矛盾时会以后缀为准 —— 不同步就会「选了 7z，存出来却是 .zip」
  formatEl.addEventListener('change', function () {
    const fmt = COMPRESS_FORMATS.find((item) => item.value === formatEl.value);
    if (fmt && nameEl.value.trim()) {
      nameEl.value = withArchiveExt(nameEl.value, fmt.ext);
    }
  });

  function onKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      close(null);
    } else if (e.key === 'Enter') {
      e.preventDefault();
      e.stopPropagation();
      close(currentValue());
    }
  }
  document.addEventListener('keydown', onKey, true);

  dialog.querySelectorAll('.dialog-foot .btn').forEach(function (btn) {
    btn.addEventListener('click', function () {
      close(btn.dataset.ok === '1' ? currentValue() : null);
    });
  });

  // 点遮罩等同取消，和 ui.js 的对话框表现一致
  mask.addEventListener('mousedown', function (e) {
    if (e.target === mask) {
      close(null);
    }
  });

  setTimeout(function () {
    nameEl.focus();
    nameEl.select();
  }, 60);

  return promise;
}

/** CSS 属性选择器转义（文件名里可能有引号等特殊字符） */
function cssEscape(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
}

/* ===========================================================================
   对外入口
   =========================================================================== */

/**
 * 打开文件资源管理器窗口。
 *
 * @param {object} desktop 桌面实例
 * @param {string} rootId  根目录 id，空则显示「此电脑」
 * @param {string} rel     相对路径
 * @param {object} geom    可选，{x, y, width, height}；还原上次布局时用
 */
export function openExplorer(desktop, rootId, rel, geom) {
  const targetRoot = rootId || '';
  const targetRel = rel || '';

  // 同一位置已经开着窗口就聚焦，避免重复开一堆
  for (const explorer of openExplorers.values()) {
    if (explorer.mode === 'dir' && explorer.rootId === targetRoot && explorer.relPath === targetRel) {
      explorer.record.win.focus();
      return explorer;
    }
  }
  for (const explorer of openExplorers.values()) {
    if (explorer.mode === 'computer' && !targetRoot) {
      explorer.record.win.focus();
      return explorer;
    }
  }

  const box = geom || {};

  const content = document.createElement('div');
  const record = wm.create({
    title: '文件资源管理器',
    iconName: 'app-explorer',
    content: content,
    width: Number.isFinite(Number(box.width)) ? Number(box.width) : 1060,
    height: Number.isFinite(Number(box.height)) ? Number(box.height) : 660,
    x: Number.isFinite(Number(box.x)) ? Number(box.x) : undefined,
    y: Number.isFinite(Number(box.y)) ? Number(box.y) : undefined,
    // 还原布局时不要播进入动画，否则会看到一屏窗口在「滑动」
    silent: !!box.silent,
    minWidth: 560,
    minHeight: 360,
    taskLabel: '文件资源管理器'
  });

  const explorer = new ExplorerWindow(record, desktop);
  // 登记「这个窗口由哪个 ExplorerWindow 管」，供 sessionstate.js 保存布局时
  // 读取当前目录 / 视图模式 / 历史栈。手动打开和还原走的是同一条路径，
  // 所以两种情况都能被正确保存。
  registerWindowOwner(record, explorer);
  explorer.start(targetRoot, targetRel);
  return explorer;
}

export default openExplorer;

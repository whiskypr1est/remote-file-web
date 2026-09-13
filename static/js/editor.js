/* ==========================================================================
   文本编辑器窗口
   --------------------------------------------------------------------------
   在窗口里直接改服务端的文本文件，带语法高亮。用的是**CodeMirror 5**（不是 6）：
   CM6 是一堆必须经打包器处理的 ESM 包，而本项目的前端没有构建步骤，
   CM5 则是 UMD + 每个语言一个 mode 文件，和已经这么用的 xterm.js 完全同构。

   窗口骨架（工具条 / 编辑区 / 状态条）和预览窗口同构，工具条按钮直接复用
   preview.css 里的 .pv-btn 系列样式。

   ★ 两条安全护栏（这是本模块最该被读懂的部分）
   --------------------------------------------------------------------------
   1. **截断的文件一律不给编辑。**
      GET /api/fs/text 在文件超过 preview.text_max_kb 时只返回前 max_kb，
      另外把 truncated 标成 true。此时缓冲区里只有文件的开头，一旦保存，
      后面那些没读到的内容就被**永久删掉**了 —— 而用户完全看不出来，
      在他眼里这只是一次普通的保存。所以这里不是「把保存按钮置灰」了事，
      而是用一整层遮罩把编辑器挡住，写清楚为什么不能改、并给出安全出路
      （下载到本地 → 本地编辑 → 上传回来）。

   2. **磁盘上的文件变了就不许覆盖。**
      打开时从 GET 拿到的 mtime / size 会作为 base_mtime / base_size 回传，
      服务端比对不一致就返回 409，并且**一个字节都不写**。前端收到 409 时
      只做一件事：把服务端的话告诉用户，再给一个「重新载入」——重新载入会
      丢掉本地改动，所以必须由用户自己按下去，绝不能替他决定。
      也**绝不自动重试**：那等于绕开这道护栏，把并发修改直接覆盖掉。

   编码 / BOM / 行尾
   --------------------------------------------------------------------------
   服务端按原编码解码后把 encoding / bom / newline 一起回传，保存时原样送回去，
   由服务端重新套用 —— 所以这里对 GB18030、带 BOM 的 UTF-8、CRLF 文件都是
   透明往返的。编辑器内部统一用 LF（CM5 也要求这样），只在显示时把行尾的风格
   告诉用户。
   ========================================================================== */

import { icon } from './icons.js';
import * as api from './api.js';
import * as ui from './ui.js';
import { wm, registerWindowOwner } from './wins.js';
import { refreshTextPreviews } from './preview.js';

/* ---------------------------------------------------------------------------
   可编辑的文件类型
   ---------------------------------------------------------------------------
   注意这里的键都是**小写、带前导点**的扩展名：extensionOf() 取出来时已经转成
   小写，所以 .TXT / .Py 这类大写后缀也能命中（Windows 上很常见）。
   .bat / .cmd 没有对应的高亮方案，用 shell 顶上 —— 见 SHELL_LIKE_EXTS 的说明。
   --------------------------------------------------------------------------- */

/** 取文件名上的扩展名（小写、带点）；没有扩展名返回 '' */
function extensionOf(name) {
  const text = String(name || '');
  const dot = text.lastIndexOf('.');
  // dot > 0：'.gitignore' 这种点开头的名字不算「有扩展名」，和图标那边的口径一致
  return dot > 0 ? text.slice(dot).toLowerCase() : '';
}

/**
 * extension -> CodeMirror 的 MIME（用 MIME 而不是 mode 名，能顺带带上 json 这类解析参数）
 *
 * ★ 表里的每个 MIME 都必须在 index.html 里已经加载了对应的 mode，
 *   否则 CM5 不会报错，只会安静地不高亮 —— 改这张表时请同步改
 *   tools/fetch_vendor.py 的 CODEMIRROR_MODES。
 *   也正因为如此，像 Dockerfile / Makefile 这类「按文件名而不是扩展名」识别的类型
 *   这里**故意不支持**：CM5 的 dockerfile / makefile / cmake mode 都没在离线包里，
 *   收进来只会变成一个点了没有高亮的假功能。
 */
const MODE_BY_EXT = {
  /* 纯文本：不给 mode，浏览器/CM 直接按纯文本渲染，不折腾高亮 */
  '.txt': null,
  '.log': null,
  '.csv': null,

  /* ini 系：CM5 的 properties mode 同时注册了 text/x-ini，正好覆盖这一族 */
  '.ini': 'text/x-ini',
  '.cfg': 'text/x-ini',
  '.conf': 'text/x-properties',
  '.properties': 'text/x-properties',

  '.py': 'text/x-python',
  '.bat': 'text/x-sh',          // 见下方 SHELL_LIKE_EXTS
  '.cmd': 'text/x-sh',
  '.ps1': 'application/x-powershell',
  '.sh': 'text/x-sh',
  '.bash': 'text/x-sh',

  '.js': 'text/javascript',
  '.mjs': 'text/javascript',
  /* ★ .json 走的是 javascript mode（带 json 解析参数），不是独立的 json mode ——
     CM5 里根本没有 mode/json/，用 MIME 让 CM 自己挑对参数最稳 */
  '.json': 'application/json',

  '.html': 'text/html',
  '.htm': 'text/html',
  '.xml': 'text/xml',
  '.svg': 'text/xml',           // SVG 就是 XML，用 xml 而不是 htmlmixed

  '.css': 'text/css',
  '.md': 'text/x-markdown',
  '.yml': 'text/x-yaml',
  '.yaml': 'text/x-yaml',
  '.sql': 'text/x-sql'
};

/**
 * 归到 shell mode 的扩展名。
 *
 * bat / cmd 是**刻意**这么选的：CM5 没有批处理 mode（meta.js 里也没有），
 * 在 ship 的全部 mode 里 shell 是最接近的一个。它至少能把 echo / del / copy
 * 这类命令词、字符串和 REM 注释标出来；批处理的 %% 变量和 :label 不会高亮，
 * 属于已知的取舍 —— 总比「加载了却完全不高亮」要好，也不会因为 getMode
 * 拿到不存在的 mode 而在中文文件名上抛出难懂的异常。
 */
const SHELL_LIKE_EXTS = ['.bat', '.cmd'];

/** 缺省窗口尺寸 */
const DEFAULT_WIDTH = 900;
const DEFAULT_HEIGHT = 660;

/* ---------------------------------------------------------------------------
   对外：判断一个文件能不能用编辑器打开
   --------------------------------------------------------------------------- */

/**
 * 按文件名（或路径）取编辑器类型。
 *
 * @param {string} name 文件名或相对路径（两者都行，只看最后一段的扩展名）
 * @returns {{kind: 'mode'|'plain'|'unsupported', ext: string, mode: string|null}}
 *   kind 为 unsupported 表示「这个扩展名不在支持列表里」，入口应当据此隐藏「编辑」
 */
function detectEditorKind(name) {
  const text = String(name || '');
  // 用最后一段算扩展名：传进来的可能是 'a/b/c.txt'
  const base = text.split(/[\\/]/).pop() || text;

  const ext = extensionOf(base);
  if (!ext || !(ext in MODE_BY_EXT)) {
    // 支持列表里没有这个扩展名（含没有扩展名的文件）
    return { kind: 'unsupported', ext: ext, mode: null };
  }

  const mode = MODE_BY_EXT[ext];
  return {
    kind: mode ? 'mode' : 'plain',
    ext: ext,
    mode: mode
  };
}

/** 这个文件名能不能用编辑器打开（资源管理器右键菜单用它决定要不要出现「编辑」） */
export function canEditText(name) {
  return detectEditorKind(name).kind !== 'unsupported';
}

/**
 * 这个文件对外只导出两样东西，分别被谁用：
 *   openEditor   —— explorer.js（右键「编辑」）、preview.js（工具条「编辑」）、
 *                   sessionstate.js（还原窗口）
 *   canEditText  —— explorer.js 用它决定右键菜单里要不要出现「编辑」
 *
 * 内部还有 detectEditorKind（扩展名 -> mode）和 extensionOf，都只在本模块用，
 * 刻意不导出：能编辑哪些扩展名这件事只应该有**一个**出口（canEditText），
 * 多开几个口子迟早出现「菜单里能点、打开却说不支持」这类不一致。
 *
 * 说明一个**故意保留**的循环依赖：本模块与 preview.js 互相 import
 * （这里要 refreshTextPreviews 去刷新同文件的预览窗口，
 *  preview 要 openEditor 去响应它工具条上的「编辑」按钮）。
 * 它是安全的 —— 两边的引用都只出现在运行时（保存成功回调 / 按钮点击）里，
 * 模块求值阶段不会去读对方的导出，所以不存在 TDZ 访问问题。
 */

/* ---------------------------------------------------------------------------
   小工具
   --------------------------------------------------------------------------- */

/**
 * 把 CRLF / CR 统一成 LF。
 *
 * CM5 的 setValue 遇到孤立的 \r 会当成换行处理，而服务端**要求**提交上来的
 * text 是 LF 归一化的（它会自己按 newline 还原），所以这里必须自己先归一化，
 * 不能指望 CM 帮我们做对。
 */
function normalizeToLf(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n');
}

/** 从磁盘原文里猜行尾风格；猜不出来返回 '\n' */
function sniffNewline(raw) {
  const text = String(raw || '');
  const crlf = (text.match(/\r\n/g) || []).length;
  const lf = (text.match(/\n/g) || []).length;
  if (crlf > 0 && crlf >= lf / 2) {
    return '\r\n';
  }
  if (lf === 0 && text.indexOf('\r') !== -1) {
    return '\r';
  }
  return '\n';
}

/** 行尾的显示名 */
function newlineLabel(newline) {
  if (newline === '\r\n') {
    return 'CRLF';
  }
  if (newline === '\r') {
    return 'CR';
  }
  return 'LF';
}

/** 取文件名（用于标题） */
function baseName(rel) {
  const text = String(rel || '');
  const parts = text.split('/');
  return parts[parts.length - 1] || text;
}

/** 当前是否真的处于「有未保存修改」状态（这个类唯一的判定口径） */
function isDirty() {
  if (!this.readOnly && this.editor) {
    return this.editor.getValue() !== this.baseline;
  }
  return false;
}

/* ---------------------------------------------------------------------------
   窗口内容控制器
   --------------------------------------------------------------------------- */

class EditorWindow {
  constructor(record, ctx) {
    this.record = record;
    // 窗口内容容器就是 wm.create() 传入的那个元素（和 explorer.js 用的是同一套约定）。
    // buildDom() 与下面 11 处 querySelector 全部挂在 this.el 上，少了这一句就会在
    // buildDom 的第一行抛 "Cannot set properties of undefined (setting 'innerHTML')"，
    // 表现为「点『编辑』毫无反应」——窗口建出来了，里面却什么都没渲染。
    this.el = record.content;
    this.rootId = ctx.rootId || '';
    this.rel = ctx.rel || '';
    this.name = ctx.name || baseName(ctx.rel);
    this.desktop = ctx.desktop || null;

    this.editor = null;
    this.readOnly = true;      // 载入成功前一律当成只读，防止半截内容被改
    this.truncated = false;
    this.saving = false;
    this.destroyed = false;
    this.loadToken = 0;

    /** 元信息：保存时要原样回传给服务端的那几项 */
    this.meta = {
      encoding: 'utf-8',
      bom: false,
      newline: '\n',
      mtime: null,
      size: null,
      sizeText: '',
      serverNewline: ''
    };

    this.baseline = '';        // 载入（或上次保存）时的内容，用来算「有没有改过」
    this.dirty = false;
    this.dirtySynced = false;  // 首次 sync() 必须刷新一次界面，哪怕结果是「不脏」

    this.buildDom();
    this.setupEditor();
    this.bindDom();
    this.sync();
  }

  /* -------------------------------------------------------------------------
     DOM
     ------------------------------------------------------------------------- */

  buildDom() {
    const info = detectEditorKind(this.name);
    const self = this;

    // 工具条沿用预览窗口的 .pv-btn / .pv-label（定义在 preview.css 里，两个窗口共用）
    this.el.innerHTML =
      '<div class="preview-toolbar">' +
      '<button type="button" class="pv-btn" data-act="save" title="保存 (Ctrl+S)">' +
      icon('check') + '<span>保存</span></button>' +
      '<button type="button" class="pv-btn" data-act="reload" title="放弃修改，重新从磁盘读取">' +
      icon('refresh') + '<span>还原</span></button>' +
      '<span class="pv-label info-label">正在读取…</span>' +
      '<span class="ed-dirty-label" style="display:none">' +
      '<span class="ed-dot"></span>未保存修改</span>' +
      '<span class="spacer"></span>' +
      '<span class="pv-label mode-label">' +
      ui.escapeHtml(info.mode || (info.kind === 'plain' ? '纯文本' : '')) + '</span>' +
      '<button type="button" class="pv-btn" data-act="download" title="下载到本地">' +
      icon('download') + '<span>下载</span></button>' +
      '</div>' +
      '<div class="editor-body">' +
      '<div class="cm-editor-host"><textarea></textarea></div>' +
      '</div>' +
      '<div class="editor-status">' +
      '<span class="es-item es-lines">—</span>' +
      '<span class="es-item es-pos">—</span>' +
      '<span class="es-grow"></span>' +
      '<span class="es-item es-encoding"></span>' +
      '<span class="es-item es-newline"></span>' +
      '<span class="es-item es-size"></span>' +
      '</div>';

    this.$host = this.el.querySelector('.cm-editor-host');
    this.$info = this.el.querySelector('.info-label');
    this.$dirtyLabel = this.el.querySelector('.ed-dirty-label');
    this.$modeLabel = this.el.querySelector('.mode-label');
    this.$lines = this.el.querySelector('.es-lines');
    this.$pos = this.el.querySelector('.es-pos');
    this.$encoding = this.el.querySelector('.es-encoding');
    this.$newline = this.el.querySelector('.es-newline');
    this.$size = this.el.querySelector('.es-size');
    this.$save = this.el.querySelector('[data-act="save"]');
    this.$reload = this.el.querySelector('[data-act="reload"]');

    this.el.querySelector('[data-act="download"]').addEventListener('click', function () {
      api.triggerDownload(api.downloadUrl(self.rootId, self.rel));
    });
  }

  bindDom() {
    const self = this;

    this.$save.addEventListener('click', function () {
      self.save();
    });
    this.$reload.addEventListener('click', function () {
      self.reload();
    });
  }

  /* -------------------------------------------------------------------------
     CodeMirror
     ------------------------------------------------------------------------- */

  /**
   * 建编辑器实例。
   *
   * 注意用 window.CodeMirror（普通 <script> 挂上去的全局），而不是 import ——
   * index.html 里那些 <script src=...codemirror...> 已经保证它在 main.js 之前
   * 就位了。这里再判一次是为了在 vendor 文件缺失时给出能看懂的提示，
   * 而不是在用户点「编辑」时抛一个 'CodeMirror is not defined'。
   */
  setupEditor() {
    const CodeMirrorCtor = window.CodeMirror;

    if (typeof CodeMirrorCtor !== 'function') {
      this.$host.innerHTML =
        '<div class="pv-message" style="padding:24px">' +
        icon('error') +
        '<div class="pv-msg-text">编辑器组件未加载成功：' +
        ui.escapeHtml('/static/vendor/codemirror/lib/codemirror.js') +
        ' 不存在或加载失败。请重新运行 tools/fetch_vendor.py 补齐离线资源。</div>' +
        '</div>';
      this.$info.textContent = '';
      return;
    }

    const textarea = this.$host.querySelector('textarea');
    const info = detectEditorKind(this.name);

    // extraKeys 的处理函数是 CodeMirror 调用的，那里的 this 是 CM 实例、不是
    // EditorWindow，所以先把 this 存进闭包；后面 on('change') 复用同一个。
    const self = this;

    this.editor = CodeMirrorCtor.fromTextArea(textarea, {
      value: '',
      mode: info.mode || null,
      theme: 'eclipse',
      lineNumbers: true,
      lineWrapping: false,
      indentUnit: 4,
      tabSize: 4,
      indentWithTabs: false,
      styleActiveLine: true,
      matchBrackets: true,
      autoCloseBrackets: false,
      readOnly: true,          // 载入成功后才放开，见 loadText()
      viewportMargin: 30,
      // Ctrl+S / Cmd+S 只在编辑器有焦点时生效，正好避开了资源管理器的
      // Ctrl+C / Ctrl+V / Ctrl+F 那套快捷键。
      // ★ 必须用 self.save()：写成 this.save() 会抛
      // "Cannot read properties of undefined (reading 'save')"，Ctrl+S 直接失效。
      extraKeys: {
        'Ctrl-S': function () { self.save(); },
        'Cmd-S': function () { self.save(); }
      }
    });

    this.editor.setSize('100%', '100%');

    this.editor.on('change', function () {
      self.sync();
    });
    this.editor.on('cursorActivity', function () {
      self.updateCursor();
    });

    this.observeResize();
  }

  /**
   * 跟着窗口尺寸刷新编辑器布局。
   *
   * winbox 拖动/缩放窗口时改的是 .winbox 的尺寸，CM5 自己不知道，
   * 不 refresh() 的话会出现「内容还按旧宽度排版、右侧一片空白」或者
   * 光标与文字错位。ResizeObserver 是标准做法；refresh() 内部的
   * requestAnimationFrame 已经把真正的测量推迟到布局结束之后了。
   */
  observeResize() {
    if (typeof ResizeObserver !== 'function') {
      return;   // 极老的浏览器：退化成「手动改窗口大小时不刷新」
    }
    const self = this;
    try {
      this.resizeObserver = new ResizeObserver(function () {
        if (self.editor) {
          self.editor.refresh();
        }
      });
      this.resizeObserver.observe(this.$host);
    } catch (err) {
      /* 观察失败不影响编辑功能 */
    }
  }

  /* -------------------------------------------------------------------------
     读取
     ------------------------------------------------------------------------- */

  /**
   * 从磁盘读取。
   *
   * @param {boolean} initial 是否首次载入（首次载入失败时窗口会自己关掉）
   */
  loadText(initial) {
    const self = this;
    const token = ++this.loadToken;

    this.$info.textContent = '正在读取…';

    return api.getText(this.rootId, this.rel).then(function (res) {
      // 期间又触发了一次读取（比如连点了两下「还原」）：这份结果作废
      if (token !== self.loadToken || self.destroyed) {
        return;
      }
      self.applyLoaded(res);
    }).catch(function (err) {
      if (token !== self.loadToken || self.destroyed) {
        return;
      }
      self.handleLoadError(err, initial);
    });
  }

  /** 把 GET 的结果落到界面上 */
  applyLoaded(res) {
    const data = res || {};

    // 上一次可能是「被盖住」的状态（截断 / 读取失败），先清干净再按这次的结果算
    this.clearBlocked();

    // 二进制文件：服务端已经给了人话，直接展示，不开编辑器
    if (data.binary) {
      this.showBlocked('file', '这个文件不能当文本编辑',
        data.message || '服务端判定这是二进制文件，无法以文本方式打开。',
        [{ act: 'download', label: '下载文件', iconName: 'download' }]);
      this.$info.textContent = '';
      return;
    }

    // 服务端不给 content 时当空文件处理，但不要静默 —— 至少日志里留一条
    if (typeof data.content !== 'string') {
      console.warn('[editor] 响应缺少 content 字段，按空文件处理:', this.rel);
    }

    const raw = typeof data.content === 'string' ? data.content : '';
    const truncated = data.truncated === true;

    // ★ 元信息：保存时要原样回传，缺一项都可能把文件写坏
    this.meta = {
      encoding: typeof data.encoding === 'string' && data.encoding ? data.encoding : 'utf-8',
      bom: data.bom === true,
      // 服务端没给 newline（老后端）时，从原文里猜一个，避免把 CRLF 文件写成 LF
      newline: (data.newline === '\r\n' || data.newline === '\n' || data.newline === '\r')
        ? data.newline
        : sniffNewline(raw),
      mtime: typeof data.mtime === 'number' ? data.mtime : null,
      size: typeof data.size === 'number' ? data.size : null,
      sizeText: typeof data.size_text === 'string' ? data.size_text : '',
      serverNewline: typeof data.newline === 'string' ? data.newline : ''
    };

    this.truncated = truncated;

    // 统一成 LF 再交给 CM5；保存时服务端会按 meta.newline 还原
    this.setText(normalizeToLf(raw));

    this.updateStatus(data);

    if (truncated) {
      // ★ 只读护栏：绝不能让人在「只有前 N KB」的缓冲区上按保存
      this.enterBlockedMode(data);
    } else {
      this.readOnly = false;
      this.setCustomReadOnly(false);
      this.$info.textContent = data.encoding
        ? (data.encoding + ' · ' + (data.line_count || 0) + ' 行')
        : '';
    }

    // ★ 必须放在 readOnly 定下来之后再调：上面 setText() 里的那次 sync() 发生在
    //   readOnly 还是 true 的时候，保存按钮会被算成「不可用」。若不再刷新一次，
    //   正常载入的文件也会一直点不动「保存」——按钮看着在，其实按了没反应。
    this.sync();
  }

  /** 让 CM5 在「只读 / 可写」之间切换（不重建实例，保住滚动位置） */
  setCustomReadOnly(flag) {
    if (this.editor) {
      this.editor.setOption('readOnly', flag ? 'nocursor' : false);
    }
    if (this.$host) {
      this.$host.classList.toggle('is-readonly', !!flag);
    }
  }

  /**
   * ★ 截断文件专用：把编辑器盖起来，并说清楚为什么。
   *
   * 刻意做成「盖一层遮罩 + 解释 + 给出路」，而不是「把保存按钮置灰」：
   * 置灰的按钮不会告诉用户原因，用户只会以为编辑器坏了，
   * 然后去找别的办法写这个文件 —— 那才真的会把文件写坏。
   */
  enterBlockedMode(data) {
    this.readOnly = true;
    this.setCustomReadOnly(true);

    const maxKb = data.max_kb || 0;
    const fullSize = typeof data.size_text === 'string' ? data.size_text : '';
    const readKb = maxKb ? maxKb + 'KB' : '一段';

    this.showBlocked('warning', '文件太大，不能在这里改动',
      '这个文件' + (fullSize ? '有 ' + fullSize + '，' : '') +
      '超过了「在内置编辑器里安全打开的』上限，' +
      '下面显示的**只是前 ' + readKb + ' 的内容**，文件剩下的部分并没有读进来。\n\n' +
      '如果现在保存，服务端会用这前半段覆盖整个文件 —— 后半段会被永久删掉。' +
      '为了避免这种数据丢失，编辑器已锁定为只读。\n\n' +
      '要改它，请按下面的顺序来：下载到本地 → 用本地编辑器改好 → 上传覆盖。',
      [
        { act: 'download', label: '下载到本地', iconName: 'download' },
        { act: 'close', label: '关闭窗口', iconName: 'close' }
      ]);
  }

  /** 统一的「盖住编辑区 + 说明 + 按钮」渲染 */
  showBlocked(iconName, title, text, actions) {
    const self = this;
    const overlay = document.createElement('div');
    overlay.className = 'editor-blocked';

    // 文本里的换行转成 <br>：ui.escapeHtml 之后是安全的
    const body = ui.escapeHtml(text).replace(/\n/g, '<br>');

    let html = '<div class="eb-icon">' + icon(iconName) + '</div>';
    html += '<div class="eb-title">' + ui.escapeHtml(title) + '</div>';
    html += '<div class="eb-text">' + body + '</div>';
    if (actions && actions.length) {
      html += '<div class="eb-actions">' + actions.map(function (a, i) {
        return '<button type="button" class="pv-btn" data-idx="' + i + '">' +
          icon(a.iconName || 'info') + '<span>' + ui.escapeHtml(a.label) + '</span></button>';
      }).join('') + '</div>';
    }
    overlay.innerHTML = html;

    overlay.querySelectorAll('.eb-actions .pv-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const action = actions[Number(btn.dataset.idx)];
        if (!action) {
          return;
        }
        if (action.act === 'download') {
          api.triggerDownload(api.downloadUrl(self.rootId, self.rel));
        } else if (action.act === 'close') {
          wm.close(self.record.id);
        }
      });
    });

    this.$host.appendChild(overlay);
    // 状态条上也标一下，用户拖到窗口另一头时也知道这文件是只读的
    this.$modeLabel.textContent = '只读（已截断）';
  }

  /**
   * 摘掉「被盖住」的状态。
   *
   * 每次重新载入前都要走一遍：上一次可能因为截断或读取失败盖了一层遮罩，
   * 这一次说不定就能正常打开了 —— 不摘掉的话遮罩会一直压在编辑器上面。
   */
  clearBlocked() {
    const overlay = this.$host.querySelector('.editor-blocked');
    if (overlay) {
      overlay.remove();
    }
    this.$modeLabel.textContent = '';
  }

  /** 读取失败 */
  handleLoadError(err, initial) {
    if (err && err.status === 401) {
      return;   // 会话过期，api 层已经跳登录页了
    }

    const message = (err && err.message) || '未知错误';
    this.readOnly = true;
    this.setCustomReadOnly(true);

    // 文件没了：当成「这个窗口不该存在」，安静地收掉。
    // 这条路径在还原布局时最常见（上次开着某个文件，之后再也没打开过页面，
    // 期间文件被别处删掉了）—— 留一个空窗口反而更碍事。
    if (err && err.status === 404) {
      if (initial) {
        console.info('[editor] 文件已不存在，跳过该编辑器窗口:', this.rel);
        ui.toast('文件已不存在，未打开编辑器：' + this.name, 'warn');
        wm.close(this.record.id);
        return;
      }
      this.showBlocked('error', '文件已不存在',
        '这个文件（' + this.rel + '）已经不在磁盘上了。',
        [{ act: 'close', label: '关闭窗口', iconName: 'close' }]);
      this.$info.textContent = '';
      return;
    }

    // 其它错误（太大、权限、编码不支持…）：服务端的话比我们编的准，原样显示，
    // 并给一条下载出路 —— 打不开不等于拿不到
    this.showBlocked('error', '无法打开',
      message + '\n\n' + this.rel,
      [
        { act: 'download', label: '下载文件', iconName: 'download' },
        { act: 'close', label: '关闭窗口', iconName: 'close' }
      ]);
    this.$info.textContent = '';
  }

  /* -------------------------------------------------------------------------
     状态同步
     ------------------------------------------------------------------------- */

  /** 设置内容并重设基线（基线 = 「和磁盘一致」的那份内容） */
  setText(text) {
    this.baseline = text;
    if (this.editor) {
      // setValue 会触发 change 事件，但 sync() 是按值比较的，所以不会误报脏
      this.editor.setValue(text);
      this.editor.clearHistory();
      // setValue 不触发 cursorActivity，光标位置得自己同步一次
      this.updateCursor();
    }
    this.sync();
  }

  /** 刷新「未保存修改」指示 + 按钮可用状态 */
  sync() {
    const next = isDirty.call(this);

    if (next !== this.dirty || !this.dirtySynced) {
      this.dirty = next;
      this.dirtySynced = true;

      // 标题栏加一个前置圆点，和 Windows 上「有未保存改动」的习惯一致
      const title = (next ? '● ' : '') + this.name;
      this.record.title = title;
      if (this.record.win) {
        try {
          this.record.win.setTitle(title);
        } catch (err) {
          /* 标题设置失败不影响编辑 */
        }
      }
      if (this.record.taskBtn) {
        this.record.taskBtn.title = title;
        const label = this.record.taskBtn.querySelector('.tb-label');
        if (label) {
          label.textContent = title;
        }
      }
      if (this.$dirtyLabel) {
        this.$dirtyLabel.style.display = next ? '' : 'none';
      }
    }

    if (this.$save) {
      // 截断/只读时永远点不动；没有改动时也没必要点
      this.$save.disabled = this.readOnly || !this.dirty;
    }
  }

  updateCursor() {
    if (!this.editor || !this.$pos) {
      return;
    }
    const pos = this.editor.getCursor();
    this.$lines.textContent = '第 ' + (pos.line + 1) + ' 行，共 ' + this.editor.lineCount() + ' 行';
    this.$pos.textContent = '列 ' + (pos.ch + 1);
  }

  updateStatus(data) {
    const meta = this.meta;
    this.$encoding.textContent = meta.encoding ? meta.encoding.toUpperCase() : '';
    this.$newline.textContent = newlineLabel(meta.newline);
    this.$size.textContent = meta.sizeText || '';
    this.$lines.textContent = '共 ' + (this.editor ? this.editor.lineCount() : 0) + ' 行';
    this.$pos.textContent = '';

    if (data && data.line_count) {
      this.$lines.textContent = '共 ' + data.line_count + ' 行';
    }
  }

  /* -------------------------------------------------------------------------
     保存
     ------------------------------------------------------------------------- */

  /**
   * 保存。
   *
   * 三道闸门，缺一不可：
   *   1. truncated / 只读 —— 缓冲区不完整，绝不允许写回（见 enterBlockedMode）
   *   2. 没有改动 —— 不必白跑一趟
   *   3. saving —— 挡住双击和 Ctrl+S 连按造成的两次并发写
   */
  save() {
    const self = this;

    if (this.truncated) {
      ui.showAlert('不能保存', '这个文件太大，当前显示的只是它的前面一段。' +
        '保存会用这段内容覆盖整个文件，后面的内容会丢失，所以已被禁止。\n\n' +
        '请先下载到本地编辑，再上传覆盖。', 'warning');
      return Promise.resolve(false);
    }

    if (this.readOnly || !this.editor) {
      ui.showAlert('不能保存', '编辑器当前是只读状态。', 'warning');
      return Promise.resolve(false);
    }

    if (!isDirty.call(this)) {
      ui.toast('没有需要保存的改动', 'info');
      return Promise.resolve(false);
    }

    if (this.saving) {
      return Promise.resolve(false);   // 已经有一次保存在路上
    }

    this.saving = true;
    this.$save.disabled = true;
    ui.setBusy(true, '正在保存…');

    // 提交的是 LF 归一化后的全文；编码 / BOM / 行尾由服务端按回传的元信息还原
    const payload = {
      root: this.rootId,
      path: this.rel,
      text: normalizeToLf(this.editor.getValue()),
      encoding: this.meta.encoding,
      bom: this.meta.bom,
      newline: this.meta.newline,
      base_mtime: this.meta.mtime,
      base_size: this.meta.size
    };

    return api.saveText(payload).then(function (res) {
      self.saving = false;
      ui.setBusy(false);

      const data = res || {};

      // ★ 关键：用返回值刷新并发凭据，否则第二次保存会拿一个过期的 mtime
      // 去比对，服务端会误判成「文件被别处改过」而拒绝
      if (typeof data.mtime === 'number') {
        self.meta.mtime = data.mtime;
      }
      if (typeof data.size === 'number') {
        self.meta.size = data.size;
      }
      if (typeof data.size_text === 'string') {
        self.meta.sizeText = data.size_text;
      }

      // 保存成功 = 现在磁盘上就是缓冲区里这份，基线跟着往前走
      self.baseline = self.editor ? self.editor.getValue() : self.baseline;
      self.sync();
      self.$size.textContent = self.meta.sizeText || '';
      if (self.editor) {
        self.$lines.textContent = '共 ' + self.editor.lineCount() + ' 行';
      }

      // 同一个文件如果还开着一扇文本预览窗口，让它也读一遍 ——
      // 否则那边一直显示旧内容，用户会以为没保存上
      refreshTextPreviews(self.rootId, self.rel);

      ui.toast('已保存' + (data.size_text ? '（' + data.size_text + '）' : ''), 'success', '保存成功');
      return true;
    }).catch(function (err) {
      self.saving = false;
      ui.setBusy(false);
      self.sync();
      return self.handleSaveError(err);
    });
  }

  /** 保存失败：409 单独走「重新载入」那条路，其余按普通错误提示 */
  handleSaveError(err) {
    if (err && err.status === 401) {
      return false;   // 已跳登录页
    }

    // ★ 409：磁盘上的文件和打开时不一样了，**服务端一个字节都没写**。
    // 这里既不能自动重试（等于绕开护栏覆盖别人的改动），也不能默默放过，
    // 只能把服务端的话原样告诉用户，并把「重新载入」这条路摆在他面前。
    if (err && err.status === 409) {
      ui.showConfirm(
        '文件已被改动，本次保存未生效',
        ((err && err.message) || '文件在磁盘上已经变了。') +
        '\n\n为避免覆盖别人的修改，服务端拒绝了这次保存，文件没有被改动。\n\n' +
        '点「重新载入」可以读取磁盘上的最新内容 —— 注意这会丢弃你当前的改动。',
        { okText: '重新载入', cancelText: '先不重载', iconName: 'warning' }
      ).then(function (ok) {
        if (ok) {
          // 用户明确同意丢弃本地改动后才重载
          return this.reload(true);
        }
        return false;
      }.bind(this));
      return false;
    }

    ui.showAlert('保存失败', (err && err.message) || '未知错误', 'error');
    return false;
  }

  /* -------------------------------------------------------------------------
     重新载入
     ------------------------------------------------------------------------- */

  /**
   * 从磁盘重新读取。
   *
   * @param {boolean} force 已经有改动时也直接读（调用方必须已经问过用户）
   */
  reload(force) {
    const self = this;

    if (isDirty.call(this) && !force) {
      return ui.showConfirm(
        '放弃未保存的修改？',
        '「还原」会重新从磁盘读取「' + this.name + '」，你当前未保存的修改会丢失。',
        { okText: '放弃修改并还原', danger: true }
      ).then(function (ok) {
        if (ok) {
          return self.doReload();
        }
        return false;
      });
    }

    return this.doReload();
  }

  doReload() {
    const self = this;

    // 重新读取会把遮罩也一并重置，避免「上次因截断被盖住、这次却还留着」
    this.clearBlocked();
    this.truncated = false;
    this.readOnly = true;
    this.setCustomReadOnly(true);
    this.$info.textContent = '正在读取…';

    return this.loadText(false).then(function () {
      if (!self.destroyed) {
        if (!self.truncated) {
          ui.toast('已重新载入：' + self.name, 'success');
        }
      }
      return true;
    });
  }

  /* -------------------------------------------------------------------------
     关闭
     ------------------------------------------------------------------------- */

  /**
   * 关闭前的拦截。
   *
   * wins.js 的 onBeforeClose 是**同步**的（返回 true 就阻止关闭），
   * 而 ui.showConfirm 是异步的，所以先用 true 把这次关闭挡回去，
   * 等用户答完再摘掉钩子重新 close（和 explorer.js 里「上传中」的处理同一套写法）。
   */
  beforeClose() {
    const self = this;

    if (this.saving) {
      ui.showAlert('正在保存', '保存还没结束，请稍候再关闭窗口。', 'info');
      return true;
    }

    if (!isDirty.call(this)) {
      return false;
    }

    ui.showConfirm(
      '放弃未保存的修改？',
      '「' + this.name + '」还有未保存的修改，关闭窗口会丢失它们。',
      { okText: '放弃并关闭', danger: true }
    ).then(function (ok) {
      if (ok) {
        self.record.onBeforeClose = null;
        wm.close(self.record.id);
      }
    });

    return true;
  }

  /**
   * 会话持久化要记的东西 —— ★ 只有定位信息和标题，**没有缓冲区**。
   *
   * 这是本次改动里最需要说清楚的一个取舍，因为它直接关系到「会不会丢数据」：
   *
   *   布局里存的是「窗口几何 + 文件路径」，**从不存正在编辑的全文**。
   *   所以上次带着未保存改动关掉页面，这次还原出来的是一份「刚从磁盘读上来的
   *   干净内容」，那份没保存的修改就没了。
   *
   * 为什么仍然这么做：
   *   1. 这份布局是每 600ms 防抖写一次服务端的，服务端只当仓库、上限 256KB；
   *      把用户正在敲的全文塞进去，大文件会直接撑爆配额，把整个布局保存搞坏。
   *   2. 那等于在服务端悄悄存了一份用户以为只在自己浏览器里的草稿 ——
   *      他没按保存，就不该在服务器上留下内容。
   *
   * 所以口径是：**想保住改动只能按 Ctrl+S**（前端也有未保存提示和关窗确认，
   * 不会让它悄无声息地丢）。会话还原只负责把窗口摆回原处，不负责保管草稿。
   */
  serialize() {
    return {
      root: this.rootId,
      path: this.rel,
      title: baseName(this.rel) || this.name
    };
  }

  teardown() {
    this.destroyed = true;
    this.loadToken += 1;   // 让还在路上的读取回调作废

    if (this.resizeObserver) {
      try {
        this.resizeObserver.disconnect();
      } catch (err) {
        /* 忽略 */
      }
      this.resizeObserver = null;
    }

    if (this.editor) {
      try {
        // toTextArea 会把 CM 生成的包装 DOM 摘掉，并把原始 textarea 还原回来，
        // 避免已关闭窗口的编辑器结构留在内存里
        this.editor.toTextArea();
      } catch (err) {
        /* 忽略清理异常 */
      }
      this.editor = null;
    }
  }
}

/* ---------------------------------------------------------------------------
   对外入口
   --------------------------------------------------------------------------- */

/**
 * 打开（或聚焦）一个文件的编辑器窗口。
 *
 * 和 openPreview 的约定保持一致：同步返回窗口记录，文件内容异步载入 ——
 * 这样 sessionstate 可以在返回的瞬间就把几何信息贴上，不会看到窗口先出现在
 * 屏幕中央再滑过去。
 *
 * @param {object} ctx {rootId, rel, name, desktop}
 *   desktop 仅为与其它窗口入口保持一致的调用签名，编辑器本身用不到
 *   （所有操作都直接打 API，不依赖桌面实例）。
 * @returns {object|null} 窗口记录；调用方在还原流程里需要用它 applyGeometry
 */
export function openEditor(ctx) {
  const opts = ctx || {};
  const rootId = opts.rootId || '';
  const rel = opts.rel || '';
  const name = opts.name || baseName(rel);

  if (!rootId || !rel) {
    ui.showAlert('无法编辑', '缺少文件位置信息，无法打开编辑器。', 'error');
    return null;
  }

  // 同一个文件已经开着编辑器 -> 直接聚焦那个窗口，不重复开
  if (openEditors.has(rootId + '|' + rel)) {
    const existing = openEditors.get(rootId + '|' + rel);
    if (existing && existing.record && existing.record.win) {
      existing.record.win.focus();
      return existing.record;
    }
    openEditors.delete(rootId + '|' + rel);
  }

  const container = document.createElement('div');
  // 必须带 .editor：flex 纵向布局 + 撑满高度，内部的编辑区才能拿到确定高度。
  // sessionstate 也按这个类名认窗口种类。
  container.className = 'editor';

  const record = wm.create({
    title: name,
    // 文本文件一律用 code 图标，和预览/终端那边的口径一致
    iconName: 'code',
    content: container,
    width: DEFAULT_WIDTH,
    height: DEFAULT_HEIGHT,
    minWidth: 460,
    minHeight: 300,
    taskLabel: name
  });

  const editorWin = new EditorWindow(record, {
    rootId: rootId,
    rel: rel,
    name: name,
    desktop: opts.desktop || null
  });

  registerWindowOwner(record, editorWin);
  openEditors.set(record.id, editorWin);
  editorWin.record.editorWin = editorWin;

  record.onBeforeClose = function () {
    return editorWin.beforeClose();
  };
  record.onClosed = function () {
    openEditors.delete(record.id);
    editorWin.teardown();
  };

  editorWin.loadText(true);

  return record;
}

/** 已打开的编辑器：'root|rel' -> EditorWindow（同一文件不重复开） */
const openEditors = new Map();

export default openEditor;

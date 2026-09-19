/* ==========================================================================
   控制台镜像窗口（方案 A）
   --------------------------------------------------------------------------
   用途：**看见并操作真实桌面上已经开着的命令行窗口**。

   和「命令提示符」窗口的区别，别搞混
   ----------------------------------
     命令提示符（terminal.js）＝ 本服务自己新起一个 ConPTY 会话。
                                  它天生没有窗口，真实桌面看不到它。
     控制台镜像（本文件）    ＝ 去附着**别人已经有**的经典控制台窗口，
                               把那扇窗此刻的画面读出来，也可以往里打字。
                               那扇窗依然属于真实桌面 —— 我们只看和敲，
                               **不接管、不迁移**（Windows 没有这种能力）。

   三个刻意的取舍
   --------------
   1. **只对管理员开放。** 服务端 require_admin 硬限制；前端在这里再挡一道
      （status.available 为 false 时入口根本不出现）。
      理由：它能把任意控制台的屏幕内容送到浏览器，那些内容里可能有口令、
      令牌、别人的日志 —— 暴露面比「任务管理器」（当初连命令行都刻意不采集）
      大得多。

   2. **输入注入是单独开关。** 服务端 conhost.allow_input 默认 false。
      没打开时这个窗口是**纯只读**的，界面上也不会出现任何能打字的地方 ——
      因为「看一眼」和「替人在键盘上打字」不是一个量级的风险。

   3. **不参与布局持久化。** 根元素挂 .conhost 类，sessionstate.js 的
      windowKind() 认不出它（返回 'unknown'）→ 既不入库也不还原。
      理由同任务管理器：这种窗口不该每次开页面都自动弹出来并开始轮询。

   渲染为什么要自己画，而不是塞进 xterm.js
   ---------------------------------------
      读回来的是**字符栅格快照**（字符 + 属性），不是字节流。塞进 xterm 会
      让它的回滚/光标语义变成假的。所以这里按行渲染 span，
      颜色直接用服务端返回的「属性变化段」（runs）还原成 16 色。
   ========================================================================== */

import * as api from './api.js';
import * as ui from './ui.js';
import { icon } from './icons.js';
import { wm } from './wins.js';

/** Windows 经典控制台的 16 色调色板（属性低 4 位是前景，高 4 位是背景） */
const PALETTE = [
  '#000000', '#000080', '#008000', '#008080',
  '#800000', '#800080', '#808000', '#c0c0c0',
  '#808080', '#0000ff', '#00ff00', '#00ffff',
  '#ff0000', '#ff00ff', '#ffff00', '#ffffff'
];

/** 默认属性（浅灰前景 + 黑背景）—— 不产生 span，减小 DOM */
const DEFAULT_ATTR = 7;

const REFRESH_CHOICES = [1, 2, 3, 5, 10];
const DEFAULT_REFRESH = 2;

function colorOf(attr, high) {
  const v = high ? ((attr >> 4) & 0x0F) : (attr & 0x0F);
  return PALETTE[v] || PALETTE[DEFAULT_ATTR];
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) {
    node.className = className;
  }
  if (text !== undefined) {
    node.textContent = text;
  }
  return node;
}

class ConsoleMirror {
  constructor() {
    this.root = el('div', 'conhost');
    this.closed = false;
    this.timer = null;
    this.listTimer = null;
    this.selected = null;          // 选中的控制台对象
    this.mode = 'log';             // log | screen
    this.lines = 200;
    this.refreshSec = DEFAULT_REFRESH;
    this.autoScroll = true;
    this.status = null;
    this.busy = false;
    /** 请求代次号：只有最新一次请求的结果允许写界面（见 poll 的说明） */
    this.gen = 0;
    this.onVisibility = null;
    this.build();
  }

  /* ------------------------------------------------------------------ 构建 */

  build() {
    // ---- 工具栏 ----
    const bar = el('div', 'preview-toolbar');

    this.btnRefresh = el('button', 'pv-btn');
    this.btnRefresh.type = 'button';
    this.btnRefresh.title = '立刻刷新控制台列表';
    this.btnRefresh.innerHTML = icon('refresh');
    this.btnRefresh.addEventListener('click', () => {
      this.loadList(true);
    });
    bar.appendChild(this.btnRefresh);

    this.listInfo = el('span', 'pv-label', '正在读取…');
    bar.appendChild(this.listInfo);

    bar.appendChild(el('span', 'spacer'));

    this.btnMode = el('button', 'pv-btn');
    this.btnMode.type = 'button';
    this.btnMode.title = '在「日志」与「屏幕」两种视图之间切换';
    this.btnMode.addEventListener('click', () => {
      this.mode = this.mode === 'log' ? 'screen' : 'log';
      this.syncModeButton();
      // force：用户点了就立刻切，不能等在飞的轮询
      this.poll({ force: true });
    });
    bar.appendChild(this.btnMode);
    this.syncModeButton();

    // 自动刷新间隔
    const sel = el('select', 'pv-select');
    sel.title = '自动刷新间隔';
    REFRESH_CHOICES.forEach((sec) => {
      const opt = el('option', null, sec + ' 秒');
      opt.value = String(sec);
      if (sec === DEFAULT_REFRESH) {
        opt.selected = true;
      }
      sel.appendChild(opt);
    });
    sel.addEventListener('change', () => {
      this.refreshSec = Number(sel.value) || DEFAULT_REFRESH;
      this.restartTimer();
    });
    bar.appendChild(sel);

    this.root.appendChild(bar);

    // ---- 主体：左列表 + 右内容 ----
    const body = el('div', 'conhost-body');

    this.listEl = el('div', 'conhost-list');
    body.appendChild(this.listEl);

    const right = el('div', 'conhost-right');

    this.headEl = el('div', 'conhost-head');
    this.headEl.textContent = '请从左侧选择一个控制台';
    right.appendChild(this.headEl);

    this.viewEl = el('div', 'conhost-view');
    right.appendChild(this.viewEl);

    // ---- 输入区（只在 allow_input 打开时创建）----
    this.inputEl = null;
    right.appendChild(this.buildInputArea());

    body.appendChild(right);
    this.root.appendChild(body);

    this.statusEl = el('div', 'conhost-status');
    this.root.appendChild(this.statusEl);

    this.showMessage('info', '正在获取功能状态…');
  }

  syncModeButton() {
    this.btnMode.textContent = this.mode === 'log' ? '视图：日志' : '视图：屏幕';
  }

  /**
   * 输入区。★ 服务端没打开 allow_input 时**一个输入控件都不建** ——
   * 界面上没有能打字的地方，就不会有人误以为可以在这里操作真实窗口。
   */
  buildInputArea() {
    const wrap = el('div', 'conhost-input');
    wrap.style.display = 'none';        // 等 status 回来再决定是否显示
    this.inputWrap = wrap;
    return wrap;
  }

  renderInputArea() {
    const wrap = this.inputWrap;
    wrap.innerHTML = '';
    if (!this.status || !this.status.allow_input) {
      wrap.style.display = 'none';
      return;
    }
    wrap.style.display = '';

    const row = el('div', 'conhost-input-row');

    const box = el('input', 'conhost-input-box');
    box.type = 'text';
    box.placeholder = '输入要发送到那个真实窗口的内容，回车即发送（会同时发一个回车键）';
    box.addEventListener('keydown', (ev) => {
      if (ev.key === 'Enter') {
        ev.preventDefault();
        this.sendText(box.value);
        box.value = '';
      }
    });
    this.inputBox = box;
    row.appendChild(box);

    const send = el('button', 'pv-btn', '发送');
    send.type = 'button';
    send.addEventListener('click', () => {
      this.sendText(box.value);
      box.value = '';
    });
    row.appendChild(send);

    wrap.appendChild(row);

    // 常用功能键
    const keys = el('div', 'conhost-keys');
    [
      ['回车', ['enter']],
      ['Ctrl+C', ['ctrl-c']],
      ['Esc', ['esc']],
      ['Tab', ['tab']],
      ['↑', ['up']],
      ['↓', ['down']]
    ].forEach((item) => {
      const b = el('button', 'pv-btn', item[0]);
      b.type = 'button';
      b.addEventListener('click', () => this.sendKeys(item[1]));
      keys.appendChild(b);
    });
    wrap.appendChild(keys);

    const warn = el('div', 'conhost-warn',
      '★ 输入会**真的**送进那个窗口 —— 它等同于有人坐在那台机器前敲键盘。'
      + '发送前请确认那个窗口当前停在什么提示符上。');
    wrap.appendChild(warn);
  }

  /* -------------------------------------------------------------- 生命周期 */

  start() {
    // 页面切到后台就停止轮询：看不见的时候没必要每秒去附着一次别人的控制台
    this.onVisibility = () => {
      if (document.hidden) {
        this.stopTimer();
      } else {
        this.restartTimer();
        this.poll();
      }
    };
    document.addEventListener('visibilitychange', this.onVisibility);

    api.conhostStatus().then((st) => {
      if (this.closed) {
        return;
      }
      this.status = st;
      this.renderInputArea();
      if (!st.available) {
        this.showMessage('error', st.reason || '该功能当前不可用');
        return;
      }
      this.loadList(true);
      this.restartTimer();
    }).catch((err) => {
      if (!this.closed) {
        this.showMessage('error', '无法获取功能状态：' + ((err && err.message) || err));
      }
    });
  }

  destroy() {
    this.closed = true;
    this.stopTimer();
    if (this.onVisibility) {
      document.removeEventListener('visibilitychange', this.onVisibility);
      this.onVisibility = null;
    }
  }

  stopTimer() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  restartTimer() {
    this.stopTimer();
    if (this.closed || document.hidden) {
      return;
    }
    this.timer = setTimeout(() => {
      this.poll();
    }, Math.max(1, this.refreshSec) * 1000);
  }

  setStatus(text, kind) {
    this.statusEl.textContent = text || '';
    this.statusEl.className = 'conhost-status' + (kind ? ' is-' + kind : '');
  }

  showMessage(kind, text) {
    this.viewEl.innerHTML = '';
    const box = el('div', 'conhost-msg ' + (kind === 'error' ? 'is-error' : ''));
    box.textContent = text;
    this.viewEl.appendChild(box);
  }

  /* ------------------------------------------------------------------ 列表 */

  loadList(force) {
    if (this.closed) {
      return;
    }
    this.btnRefresh.disabled = true;
    api.conhostList(false).then((data) => {
      if (this.closed) {
        return;
      }
      this.btnRefresh.disabled = false;
      this.consoles = data.items || [];
      this.listInfo.textContent = '共 ' + data.count + ' 个控制台（其中有窗口 '
        + data.windows + ' 个）';
      this.renderList();
      // 选中的那个还在吗？不在就自动选第一个（只读预览，不改动任何东西）
      if (this.selected) {
        const still = this.consoles.find((c) => c.key === this.selected.key);
        if (still) {
          this.selected = still;
        } else {
          this.selected = null;
        }
      }
      if (!this.selected && this.consoles.length) {
        this.select(this.consoles[0]);
      } else if (!this.consoles.length) {
        this.showMessage('info', '没有找到任何控制台。'
          + '（本服务自己的命令行窗口不会列在这里 —— 那些在「命令提示符」里看。）');
      } else if (force) {
        this.poll();
      }
    }).catch((err) => {
      if (this.closed) {
        return;
      }
      this.btnRefresh.disabled = false;
      this.showMessage('error', '读取控制台列表失败：'
        + ((err && err.message) || err));
    });
  }

  renderList() {
    const box = this.listEl;
    box.innerHTML = '';
    (this.consoles || []).forEach((item) => {
      const row = el('div', 'conhost-item');
      if (this.selected && this.selected.key === item.key) {
        row.classList.add('is-active');
      }
      const title = item.title || '(无标题)';
      row.appendChild(el('div', 'conhost-item-title', title));

      const meta = el('div', 'conhost-item-meta');
      const bits = [];
      if (item.has_window) {
        bits.push(item.visible ? (item.minimized ? '已最小化' : '可见') : '隐藏窗口');
      } else {
        bits.push('无窗口（后台控制台）');
      }
      bits.push('pid ' + item.pid);
      bits.push(item.member_count + ' 个进程');
      meta.textContent = bits.join(' · ');
      row.appendChild(meta);

      const names = (item.members || [])
        .map((m) => m.name || ('pid ' + m.pid))
        .slice(0, 4)
        .join(', ');
      row.appendChild(el('div', 'conhost-item-procs', names));

      row.addEventListener('click', () => this.select(item));
      box.appendChild(row);
    });
  }

  select(item) {
    this.selected = item;
    this.renderList();
    this.headEl.textContent = (item.title || '(无标题)')
      + '   ·   pid ' + item.pid
      + (item.has_window ? '' : '   ·   无窗口控制台');
    // force：刚点的那个控制台要立刻显示，不能等下一个轮询周期
    this.poll({ force: true });
  }

  /* ------------------------------------------------------------------ 读取 */

  poll(options) {
    if (this.closed || !this.selected || document.hidden) {
      return;
    }
    const force = !!(options && options.force);
    // 轮询周期到点时如果上一次还没回来，跳过这一次就够了（不必排队）；
    // 但**用户操作**（点某个控制台、切视图）触发的必须立刻发出去 ——
    // 否则界面上会出现「点了没反应，要等两秒」。
    if (this.busy && !force) {
      return;
    }
    this.busy = true;
    /* 代次号：只有最后一次发出的请求才允许写界面。
       否则「先点的 A、后点的 B」可能因为 A 的响应更晚到达而被 A 覆盖，
       表现是「内容闪一下又变回上一个控制台」。 */
    const gen = ++this.gen;
    const pid = this.selected.pid;
    const mode = this.mode;
    api.conhostRead(pid, mode, this.lines).then((data) => {
      if (this.closed || gen !== this.gen) {
        return;
      }
      this.busy = false;
      this.renderFrame(data);
      this.setStatus('已更新 ' + new Date().toLocaleTimeString()
        + ' · ' + data.cols + 'x' + data.rows
        + (data.mode === 'log' ? ' · 日志视图' : ' · 屏幕视图'));
      this.restartTimer();
    }).catch((err) => {
      if (this.closed || gen !== this.gen) {
        return;
      }
      this.busy = false;
      this.showMessage('error', '读取失败：' + ((err && err.message) || err));
      this.setStatus('读取失败，稍后重试', 'error');
      this.restartTimer();
    });
  }

  /** 把服务端返回的字符栅格画出来（含 16 色还原） */
  renderFrame(data) {
    const view = this.viewEl;
    const keepScroll = view.scrollHeight - view.scrollTop - view.clientHeight;
    const atBottom = keepScroll < 40;

    const frag = document.createDocumentFragment();
    const lines = data.lines || [];
    const runs = data.runs || [];

    lines.forEach((text, i) => {
      const row = el('div', 'conhost-line');
      const segs = runs[i] || [];
      if (!segs.length || (segs.length === 1 && segs[0][2] === DEFAULT_ATTR)) {
        row.textContent = text || ' ';
      } else {
        segs.forEach((seg) => {
          const start = seg[0];
          const len = seg[1];
          const attr = seg[2];
          const slice = (text || '').substr(start, len);
          if (!slice) {
            return;
          }
          if (attr === DEFAULT_ATTR) {
            row.appendChild(document.createTextNode(slice));
            return;
          }
          const span = el('span', null, slice);
          span.style.color = colorOf(attr, false);
          const bg = (attr >> 4) & 0x0F;
          if (bg) {
            span.style.background = colorOf(attr, true);
          }
          row.appendChild(span);
        });
      }
      frag.appendChild(row);
    });

    view.innerHTML = '';
    view.appendChild(frag);

    if (atBottom || this.autoScroll) {
      view.scrollTop = view.scrollHeight;
    } else {
      view.scrollTop = Math.max(0, view.scrollHeight - view.clientHeight - keepScroll);
    }
  }

  /* ------------------------------------------------------------------ 输入 */

  sendText(text) {
    if (!this.selected) {
      ui.toast('请先选择一个控制台');
      return;
    }
    const value = String(text === undefined || text === null ? '' : text);
    if (!value) {
      return;
    }
    // 同时发一个回车：绝大多数场景就是「敲一条命令」
    this.doInput({ text: value, keys: ['enter'] });
  }

  sendKeys(keys) {
    if (!this.selected) {
      ui.toast('请先选择一个控制台');
      return;
    }
    this.doInput({ text: '', keys: keys });
  }

  doInput(payload) {
    const pid = this.selected.pid;
    this.setStatus('正在发送…');
    api.conhostInput(pid, payload.text, payload.keys).then((res) => {
      if (this.closed) {
        return;
      }
      this.setStatus('已发送 ' + (res.written || 0) + ' 个按键事件');
      // 立刻补一次读取，让用户马上看到效果（而不是等下一个轮询周期）
      setTimeout(() => this.poll({ force: true }), 220);
    }).catch((err) => {
      if (this.closed) {
        return;
      }
      const msg = (err && err.message) || String(err);
      ui.toast(msg, 'error');
      this.setStatus('发送失败：' + msg, 'error');
    });
  }
}

/* ---------------------------------------------------------------------------
   对外入口
   --------------------------------------------------------------------------- */

/**
 * 打开一个控制台镜像窗口。
 *
 * 允许同时开多个（与资源管理器 / 命令行一致）：一个看服务器日志、
 * 一个看构建输出是常见用法。
 */
export function openConsoleMirror(desktop) {
  const view = new ConsoleMirror();

  const record = wm.create({
    title: '控制台镜像',
    iconName: 'code',
    content: view.root,
    width: 980,
    height: 620,
    minWidth: 640,
    minHeight: 380,
    windowClass: 'conhost-win',
    taskLabel: '控制台镜像',
    onClosed: function () {
      view.destroy();
    }
  });

  view.start();
  return record;
}

export default { openConsoleMirror };

/* ==========================================================================
   任务管理器窗口
   --------------------------------------------------------------------------
   仿 Windows 10 任务管理器：上半部分是「性能」卡片（CPU / 内存 / 磁盘 /
   网络 / GPU），下半部分是可按 CPU 或内存排序的进程表。

   三个刻意的取舍
   --------------
   1. **只读，不提供「结束进程」。**
      本项目已经有一个真终端窗口，真要动进程时用它就行；而一个能按 PID
      杀任意进程的 HTTP 接口，风险与收益完全不成比例 —— 那等于给任何能
      登录的人一个不用交互就能把服务器搞瘫的按钮。

   2. **不参与布局持久化。**
      根元素挂了 .taskmgr 类，sessionstate.js 的 windowKind() 认不出它，
      返回 'unknown'，于是既不入库也不还原（见该文件 save 分支的
      `if (item.kind === 'unknown') return;`）。
      这是有意的：监控窗口每打开一次页面就自动弹出来并开始轮询，
      并不是用户想要的默认行为；想常驻点一下开始菜单即可。

   3. **页面切到后台就停止轮询。**
      看不见的时候没必要每 2 秒采一次（服务端还得枚举一遍进程）。
      重新可见时立刻补一次，用户感觉不到断档。
   ========================================================================== */

import * as api from './api.js';
import * as ui from './ui.js';
import { icon } from './icons.js';
import { wm } from './wins.js';

/** 刷新间隔可选值（秒） */
const REFRESH_CHOICES = [1, 2, 5, 10];
const DEFAULT_REFRESH = 2;
const DEFAULT_TOP = 30;

/** psutil 的状态字符串 -> 中文（对齐任务管理器的叫法） */
const STATUS_LABELS = {
  running: '正在运行',
  sleeping: '睡眠',
  'disk-sleep': '磁盘等待',
  stopped: '已停止',
  'tracing-stop': '已跟踪停止',
  zombie: '僵尸',
  dead: '已终止',
  waking: '唤醒中',
  'wake-killing': '唤醒中止',
  parked: '已停放',
  idle: '空闲',
  locked: '已锁定',
  waiting: '等待'
};

/* ---------------------------------------------------------------------------
   格式化小工具
   --------------------------------------------------------------------------- */

function num(value, digits) {
  const n = Number(value);
  if (!isFinite(n)) {
    return '--';
  }
  return n.toFixed(digits === undefined ? 1 : digits);
}

/** 字节/秒 -> 「1.2 MB/s」 */
function rate(bytesPerSecond) {
  const v = Math.max(0, Number(bytesPerSecond) || 0);
  if (v < 1024) {
    return Math.round(v) + ' B/s';
  }
  return ui.formatSize(Math.round(v)) + '/s';
}

/** 秒 -> 「3 天 4 小时」/「5 分 12 秒」 */
function duration(seconds) {
  let left = Math.max(0, Math.floor(Number(seconds) || 0));
  const day = Math.floor(left / 86400);
  left -= day * 86400;
  const hour = Math.floor(left / 3600);
  left -= hour * 3600;
  const min = Math.floor(left / 60);
  if (day > 0) {
    return day + ' 天 ' + hour + ' 小时';
  }
  if (hour > 0) {
    return hour + ' 小时 ' + min + ' 分';
  }
  return min + ' 分 ' + (left - min * 60) + ' 秒';
}

/** 进度条配色档位：压力越大越暖（和真实任务管理器一个思路） */
function levelClass(percent) {
  const p = Number(percent) || 0;
  if (p >= 85) {
    return ' hot';
  }
  if (p >= 60) {
    return ' warn';
  }
  return '';
}

function statusLabel(status) {
  const key = String(status || '').toLowerCase();
  return STATUS_LABELS[key] || status || '—';
}

/** 进程表里那种「单元格内小进度条 + 数字」 */
function cellBar(percent) {
  const p = Math.max(2, Math.min(100, Number(percent) || 0));
  return '<span class="tm-cell-bar" style="width:32px"><i style="width:' + p + '%"></i></span>';
}

/* ---------------------------------------------------------------------------
   窗口内的控制器
   --------------------------------------------------------------------------- */

class TaskManager {

  constructor() {
    this.root = document.createElement('div');
    // ★ .taskmgr 这个类名是「不参与布局持久化」的依据（见文件头第 2 点）
    this.root.className = 'tm-root taskmgr';
    this.root.innerHTML = this.template();

    this.cardsEl = this.root.querySelector('.tm-cards');
    this.tbodyEl = this.root.querySelector('.tm-table tbody');
    this.headEl = this.root.querySelector('.tm-proc-head');
    this.statusEl = this.root.querySelector('[data-role="status"]');
    this.sysEl = this.root.querySelector('[data-role="sysinfo"]');
    this.toggleBtn = this.root.querySelector('[data-act="toggle"]');

    this.sort = 'cpu';
    this.top = DEFAULT_TOP;
    this.interval = DEFAULT_REFRESH;
    this.paused = false;
    this.closed = false;
    this.busy = false;
    this.timer = null;
    this.degraded = false;
    this.selfPid = null;
    this.cardNodes = {};

    this.buildCards();
    this.bind();

    const self = this;
    // 页面重新可见时立刻补一次，避免「切回来还是几分钟前的数字」
    this.onVisibility = function () {
      if (!self.closed && !self.paused && !document.hidden) {
        self.tick();
      }
    };
    document.addEventListener('visibilitychange', this.onVisibility);
  }

  template() {
    let options = '';
    REFRESH_CHOICES.forEach(function (sec) {
      options += '<option value="' + sec + '"' +
        (sec === DEFAULT_REFRESH ? ' selected' : '') + '>' + sec + ' 秒</option>';
    });

    return [
      '<div class="tm-toolbar">',
      '  <button type="button" class="tm-btn" data-act="toggle">暂停</button>',
      '  <button type="button" class="tm-btn" data-act="refresh">', icon('refresh'), '<span>立即刷新</span></button>',
      '  <span class="tm-spacer"></span>',
      '  <span class="tm-status" data-role="sysinfo"></span>',
      '  <select class="tm-select" data-role="interval" title="刷新间隔">', options, '</select>',
      '  <span class="tm-status" data-role="status"></span>',
      '</div>',

      '<div class="tm-cards"></div>',

      '<div class="tm-proc">',
      '  <div class="tm-proc-head">进程</div>',
      '  <div class="tm-table-wrap">',
      '    <table class="tm-table">',
      '      <thead><tr>',
      '        <th class="no-sort" style="width:28%">名称</th>',
      '        <th class="no-sort" style="width:70px">PID</th>',
      '        <th style="width:110px" data-sort="cpu">CPU<span class="tm-sort"></span></th>',
      '        <th class="no-sort" style="width:72px">内存%</th>',
      '        <th style="width:96px" data-sort="memory">内存<span class="tm-sort"></span></th>',
      '        <th class="no-sort" style="width:120px">用户</th>',
      '        <th class="no-sort" style="width:84px">状态</th>',
      '      </tr></thead>',
      '      <tbody></tbody>',
      '    </table>',
      '  </div>',
      '</div>'
    ].join('');
  }

  /** 建好 5 张卡片的骨架，之后每轮只改文字与宽度，避免闪 */
  buildCards() {
    const specs = [
      { key: 'cpu', name: 'CPU' },
      { key: 'memory', name: '内存' },
      { key: 'disk', name: '磁盘' },
      { key: 'network', name: '网络' },
      { key: 'gpu', name: 'GPU' }
    ];

    this.cardsEl.innerHTML = '';
    this.cardNodes = {};
    const self = this;

    specs.forEach(function (spec) {
      const card = document.createElement('div');
      card.className = 'tm-card';
      card.innerHTML =
        '<div class="tm-card-head">' +
        '  <span class="tm-card-name">' + ui.escapeHtml(spec.name) + '</span>' +
        '  <span class="tm-card-meta"></span>' +
        '</div>' +
        '<div class="tm-card-value">--</div>' +
        '<div class="tm-bar"><div class="tm-bar-fill"></div></div>' +
        '<div class="tm-cores"></div>' +
        '<div class="tm-sub"></div>';
      self.cardsEl.appendChild(card);
      self.cardNodes[spec.key] = {
        card: card,
        meta: card.querySelector('.tm-card-meta'),
        value: card.querySelector('.tm-card-value'),
        bar: card.querySelector('.tm-bar'),
        fill: card.querySelector('.tm-bar-fill'),
        cores: card.querySelector('.tm-cores'),
        sub: card.querySelector('.tm-sub')
      };
    });
  }

  bind() {
    const self = this;

    if (this.toggleBtn) {
      this.toggleBtn.addEventListener('click', function () {
        self.paused = !self.paused;
        self.toggleBtn.textContent = self.paused ? '继续' : '暂停';
        if (self.paused) {
          self.cancelTimer();
          self.setStatus('已暂停');
        } else {
          self.tick();
        }
      });
    }

    const refreshBtn = this.root.querySelector('[data-act="refresh"]');
    if (refreshBtn) {
      refreshBtn.addEventListener('click', function () {
        if (self.paused) {
          // 暂停状态下点「立即刷新」＝刷一次并保持暂停，符合直觉
          self.tick(true);
        } else {
          self.tick();
        }
      });
    }

    const intervalSel = this.root.querySelector('[data-role="interval"]');
    if (intervalSel) {
      intervalSel.addEventListener('change', function () {
        const sec = Number(intervalSel.value);
        if (sec > 0) {
          self.interval = sec;
          self.schedule();
        }
      });
    }

    // 表头点击切换排序维度（服务端排序，只支持降序：最占资源的排最前）
    this.root.querySelectorAll('th[data-sort]').forEach(function (th) {
      th.addEventListener('click', function () {
        const key = th.dataset.sort;
        if (key && key !== self.sort) {
          self.sort = key;
          self.cancelTimer();
          self.tick();
        }
      });
    });
  }

  /* ---- 轮询 ---------------------------------------------------------- */

  start() {
    this.closed = false;
    this.tick();
  }

  cancelTimer() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  schedule() {
    const self = this;
    this.cancelTimer();
    if (this.closed || this.paused || document.hidden) {
      return;
    }
    this.timer = setTimeout(function () { self.tick(); }, this.interval * 1000);
  }

  /**
   * 取一次数据并渲染。
   * @param {boolean} once 只刷这一次、不安排下一轮（暂停状态下手动刷新用）
   */
  tick(once) {
    const self = this;
    if (this.closed || this.busy) {
      return;
    }
    this.busy = true;

    api.sysmonSnapshot(this.top, this.sort).then(function (data) {
      self.busy = false;
      if (self.closed) {
        return;
      }
      self.render(data);
      if (once && self.paused) {
        self.setStatus('已暂停（已手动刷新）');
        return;
      }
      if (!self.paused) {
        self.schedule();
      }
    }).catch(function (err) {
      self.busy = false;
      if (self.closed) {
        return;
      }
      // 403 = 服务端把这个功能关了，再轮询也没意义，停下来并说明原因
      if (err && err.status === 403) {
        self.paused = true;
        if (self.toggleBtn) {
          self.toggleBtn.textContent = '继续';
        }
        self.renderUnavailable(err.message || '系统监控已在服务端关闭');
        return;
      }
      self.setStatus('读取失败：' + ((err && err.message) || '未知错误'));
      if (once && self.paused) {
        return;
      }
      if (!self.paused) {
        self.schedule();
      }
    });
  }

  setStatus(text) {
    if (this.statusEl) {
      this.statusEl.textContent = text || '';
    }
  }

  /* ---- 渲染 ---------------------------------------------------------- */

  render(data) {
    if (!data || data.available === false) {
      this.renderUnavailable((data && data.reason) || '未知原因');
      return;
    }
    // 从「不可用」恢复时要重建卡片骨架（上一轮把它换成提示文字了）
    if (this.degraded) {
      this.degraded = false;
      this.buildCards();
    }
    // 先记下自己（服务进程）的 pid，进程表要据此加标记
    this.selfPid = (data.system || {}).pid || null;
    this.renderCards(data);
    this.renderProcesses(data);
    this.setStatus('更新于 ' + new Date().toLocaleTimeString());
  }

  renderUnavailable(reason) {
    this.degraded = true;
    this.cardsEl.innerHTML = '<div class="tm-note" style="grid-column:1/-1">' +
      ui.escapeHtml(reason) + '</div>';
    if (this.tbodyEl) {
      this.tbodyEl.innerHTML = '';
    }
    if (this.headEl) {
      this.headEl.textContent = '进程 — 不可用';
    }
    this.setStatus('');
  }

  applyBar(node, percent) {
    const p = Math.max(0, Math.min(100, Number(percent) || 0));
    node.bar.style.display = '';
    node.fill.style.width = p + '%';
    node.fill.className = 'tm-bar-fill' + levelClass(percent);
  }

  renderCards(data) {
    const self = this;
    const cpu = data.cpu || {};
    const mem = data.memory || {};
    const disk = data.disk || {};
    const net = data.network || {};
    const gpu = data.gpu || {};
    const sys = data.system || {};

    // ---- CPU ----
    const c = this.cardNodes.cpu;
    if (c) {
      c.value.innerHTML = num(cpu.percent) + '%' +
        (cpu.freq_mhz ? ' <small>' + Math.round(cpu.freq_mhz) + ' MHz</small>' : '');
      this.applyBar(c, cpu.percent);
      c.cores.style.display = '';

      const cores = cpu.per_cpu || [];
      if (c.cores.children.length !== cores.length) {
        let html = '';
        cores.forEach(function () { html += '<span class="tm-core"><i></i></span>'; });
        c.cores.innerHTML = html;
      }
      cores.forEach(function (pct, index) {
        const cell = c.cores.children[index];
        const bar = cell && cell.firstChild;
        if (bar) {
          bar.style.height = Math.max(0, Math.min(100, Number(pct) || 0)) + '%';
        }
      });

      const bits = [];
      if (cpu.count_logical) {
        bits.push(cpu.count_logical + ' 逻辑核' +
          (cpu.count_physical ? ' / ' + cpu.count_physical + ' 物理核' : ''));
      }
      if (cpu.load_avg && cpu.load_avg.length) {
        bits.push('负载 ' + cpu.load_avg.join(' / '));
      }
      if (sys.cpu_model) {
        bits.push(ui.escapeHtml(sys.cpu_model.slice(0, 60)));
      }
      c.sub.innerHTML = bits.join(' · ');
    }

    // ---- 内存 ----
    const m = this.cardNodes.memory;
    if (m) {
      m.cores.style.display = 'none';
      m.value.innerHTML = ui.formatSize(mem.used || 0) +
        ' <small>/ ' + ui.formatSize(mem.total || 0) + '</small>';
      this.applyBar(m, mem.percent);
      m.sub.textContent = '使用率 ' + num(mem.percent) + '% · 可用 ' +
        ui.formatSize(mem.available || 0) +
        (mem.swap_total ? ' · 交换分区 ' + ui.formatSize(mem.swap_used || 0) +
          ' / ' + ui.formatSize(mem.swap_total) : '');
    }

    // ---- 磁盘（吞吐是瞬时速率，没有「总量」可作分母，因此不画进度条）----
    const d = this.cardNodes.disk;
    if (d) {
      d.cores.style.display = 'none';
      d.bar.style.display = 'none';
      d.value.innerHTML = '读 ' + rate(disk.read_bps) +
        ' <small>写 ' + rate(disk.write_bps) + '</small>';
      d.sub.textContent = '本机所有磁盘的合计吞吐';
    }

    // ---- 网络 ----
    const n = this.cardNodes.network;
    if (n) {
      n.cores.style.display = 'none';
      n.bar.style.display = 'none';
      n.value.innerHTML = '↓ ' + rate(net.recv_bps) +
        ' <small>↑ ' + rate(net.sent_bps) + '</small>';

      const lines = ['累计 ↓' + ui.formatSize(net.recv_total || 0) +
        ' ↑' + ui.formatSize(net.sent_total || 0)];
      const busy = (net.per_nic || []).filter(function (nic) {
        return (nic.recv_bps + nic.sent_bps) > 0;
      }).slice(0, 3);
      busy.forEach(function (nic) {
        lines.push(ui.escapeHtml(nic.name) + ' ↓' + rate(nic.recv_bps) +
          ' ↑' + rate(nic.sent_bps));
      });
      n.sub.innerHTML = lines.join('<br>');
    }

    // ---- GPU（读不到就如实说明，不编一个 0%）----
    const g = this.cardNodes.gpu;
    if (g) {
      const devices = gpu.devices || [];
      if (gpu.available && devices.length) {
        const dev = devices[0];
        g.cores.style.display = 'none';
        g.value.innerHTML = (dev.utilization_percent === null ||
          dev.utilization_percent === undefined)
          ? '--' : num(dev.utilization_percent) + '%';
        this.applyBar(g, dev.utilization_percent);

        const bits = [ui.escapeHtml(dev.name || 'GPU')];
        if (dev.memory_total) {
          bits.push('显存 ' + ui.formatSize(dev.memory_used || 0) + ' / ' +
            ui.formatSize(dev.memory_total) +
            (dev.memory_percent === null || dev.memory_percent === undefined
              ? '' : '（' + num(dev.memory_percent) + '%）'));
        }
        if (dev.temperature_c !== null && dev.temperature_c !== undefined) {
          bits.push(num(dev.temperature_c, 0) + ' °C');
        }
        if (devices.length > 1) {
          bits.push('共 ' + devices.length + ' 块，此处显示第 1 块');
        }
        g.sub.textContent = bits.join(' · ');
      } else {
        g.cores.style.display = 'none';
        g.bar.style.display = 'none';
        g.value.textContent = '不可用';
        g.sub.innerHTML = '<div class="tm-note">' +
          ui.escapeHtml(gpu.reason || '未能读取 GPU 利用率') + '</div>';
      }
    }

    // ---- 顶部系统信息 ----
    if (this.sysEl) {
      const bits = [];
      if (sys.hostname) {
        bits.push(sys.hostname);
      }
      if (sys.os) {
        bits.push(sys.os);
      }
      if (sys.uptime_seconds !== null && sys.uptime_seconds !== undefined) {
        bits.push('已运行 ' + duration(sys.uptime_seconds));
      }
      this.sysEl.textContent = bits.join(' · ');
    }
  }

  renderProcesses(data) {
    const proc = data.processes || {};
    const rows = proc.list || [];
    const wrap = this.root.querySelector('.tm-table-wrap');
    const keepScroll = wrap ? wrap.scrollTop : 0;

    // 排序指示箭头
    this.root.querySelectorAll('th[data-sort]').forEach(function (th) {
      const mark = th.querySelector('.tm-sort');
      if (mark) {
        mark.textContent = (th.dataset.sort === (proc.sort || 'cpu')) ? '▼' : '';
      }
    });

    if (this.headEl) {
      const sortName = (proc.sort === 'memory') ? '内存' : 'CPU';
      this.headEl.textContent = '进程 — 共 ' + (proc.total || 0) + ' 个，按' + sortName +
        '排序显示前 ' + (proc.shown || rows.length) + ' 个' +
        (proc.cpu_ready === false ? '（首次采样中，CPU 列下一轮生效）' : '');
    }

    let maxCpu = 0;
    let maxMem = 0;
    rows.forEach(function (row) {
      maxCpu = Math.max(maxCpu, Number(row.cpu_percent) || 0);
      maxMem = Math.max(maxMem, Number(row.memory) || 0);
    });
    if (maxCpu <= 0) {
      maxCpu = 1;
    }
    if (maxMem <= 0) {
      maxMem = 1;
    }

    const self = this;
    let html = '';
    rows.forEach(function (row) {
      const cpuPct = Number(row.cpu_percent) || 0;
      const memBytes = Number(row.memory) || 0;
      html += '<tr' + (row.pid === self.selfPid ? ' class="is-self"' : '') + '>' +
        '<td class="name" title="' + ui.escapeHtml(row.name) + '">' +
          ui.escapeHtml(row.name) + '</td>' +
        '<td class="num">' + row.pid + '</td>' +
        '<td class="num">' + cellBar(cpuPct / maxCpu * 100) + num(cpuPct) + '%</td>' +
        '<td class="num">' + num(row.memory_percent) + '%</td>' +
        '<td class="num">' + cellBar(memBytes / maxMem * 100) +
          ui.formatSize(memBytes) + '</td>' +
        '<td title="' + ui.escapeHtml(row.username || '') + '">' +
          ui.escapeHtml(row.username || '—') + '</td>' +
        '<td>' + ui.escapeHtml(statusLabel(row.status)) + '</td>' +
        '</tr>';
    });

    if (!html) {
      html = '<tr><td colspan="7" class="tm-empty">没有取到进程列表</td></tr>';
    }

    this.tbodyEl.innerHTML = html;
    // 重排 innerHTML 会让滚动位置归零，这里还原回去，免得每 2 秒跳回顶部
    if (wrap) {
      wrap.scrollTop = keepScroll;
    }
  }

  destroy() {
    this.closed = true;
    this.cancelTimer();
    if (this.onVisibility) {
      document.removeEventListener('visibilitychange', this.onVisibility);
      this.onVisibility = null;
    }
  }
}

/**
 * 打开一个任务管理器窗口。
 *
 * 允许同时开多个（与资源管理器 / 命令行一致），不做单例复用 ——
 * 想聚焦已有窗口的话得先给 wins.js 加一个 focus 接口，那是另一件事。
 */
export function openTaskManager(desktop) {
  const manager = new TaskManager();

  const record = wm.create({
    title: '任务管理器',
    iconName: 'activity',
    content: manager.root,
    width: 800,
    height: 580,
    minWidth: 520,
    minHeight: 340,
    windowClass: 'taskmgr-win',
    taskLabel: '任务管理器',
    onClosed: function () {
      manager.destroy();
    }
  });

  manager.start();
  return record;
}

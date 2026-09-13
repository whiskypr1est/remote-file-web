/* ==========================================================================
   窗口管理器
   --------------------------------------------------------------------------
   在 winbox.js 之上做一层薄封装，负责：
     * 创建 / 关闭 / 聚焦 / 最小化窗口
     * 任务栏按钮的同步（新建、关闭、高亮、最小化状态）
     * 层叠式初始位置、双击标题栏最大化
     * 关闭前的拦截钩子（例如上传中提示）

   关于最大化的重要细节：
     winbox 的 maximize() 是按 document.documentElement 的尺寸计算的，
     与 windows 挂在哪个父元素无关。因此每个窗口都传入
     top/left/right/bottom 作为内边距，把底部任务栏高度减掉，
     最大化后窗口才会正好停在任务栏上方。
   ========================================================================== */

import { icon } from './icons.js';

/** 任务栏高度，必须与 CSS 的 --taskbar-h 保持一致 */
export const TASKBAR_HEIGHT = 48;

/** 窗口与视口边缘至少留一点空隙，避免贴着边缘没法拖 */
const EDGE_MIN = 6;

function clampNum(value, min, max) {
  return Math.max(min, Math.min(value, max));
}

/**
 * 把窗口几何信息夹回可视区域。
 *
 * 为什么需要：状态可能是上一次用大屏存的，换到小屏/缩小窗口后还原，
 * 窗口就会整个跑到视口外面 —— 用户看不到也点不到，只能用「显示桌面」
 * 才能把最小化的窗口捞回来，体验极差。
 *
 * 注意这里只按**当前浏览器视口**夹，不判断多显示器：
 * 多屏时浏览器会被拉得很宽，此时夹取的结果依然落在可视区内。
 *
 * @param {object} geom {x, y, width, height}
 * @returns {object} {x, y, width, height}
 */
export function clampGeometry(geom) {
  const availW = Math.max(320, window.innerWidth);
  const availH = Math.max(240, window.innerHeight - TASKBAR_HEIGHT);
  const source = geom || {};

  const width = clampNum(Number(source.width) || 940, 360, Math.max(360, availW - 2 * EDGE_MIN));
  const height = clampNum(Number(source.height) || 620, 240, Math.max(240, availH - 2 * EDGE_MIN));

  // 没给位置时按居中算，和下面 create() 里不传几何信息时的视觉位置一致
  const rawX = Number.isFinite(Number(source.x))
    ? Number(source.x)
    : Math.round((availW - width) / 2);
  const rawY = Number.isFinite(Number(source.y))
    ? Number(source.y)
    : Math.round((availH - height) / 2);

  return {
    width: width,
    height: height,
    x: clampNum(rawX, 0, Math.max(0, availW - width)),
    y: clampNum(rawY, 0, Math.max(0, availH - height))
  };
}

/**
 * 把几何信息应用到已经存在的窗口上。
 *
 * 还原窗口用它：窗口必须走正常的创建流程（这样和手动打开的窗口没有任何
 * 区别），位置尺寸则在创建之后再补上。
 *
 * 两个细节：
 *   1. 调用时会临时加上 .dragging，因为 .winbox 有 0.18s 的位置/尺寸过渡，
 *      不加的话还原过程会看到窗口从默认位置「滑」过去。
 *   2. 窗口处于最小化时 winbox 用 transform 把它挪到屏幕外，此时 x/y 会失去
 *      意义，所以要跳过位置只改尺寸。
 *
 * @param {object} record wm.create 返回的窗口记录
 * @param {object} geom   {x, y, width, height}
 * @param {boolean} keepSilent 传 true 表示后面还会继续改，先不要恢复过渡动画
 */
export function applyGeometry(record, geom, keepSilent) {
  if (!record || !record.win) {
    return;
  }
  const win = record.win;
  if (!win.g) {
    return;
  }

  const target = clampGeometry(geom);
  const hadDragging = win.g.classList.contains('dragging');

  win.g.classList.add('dragging');
  try {
    // winbox 内部维护的 width/height 是内容区的值，两个 setter 会自动补上
    // 标题栏和边框的高度，所以这里直接传整窗尺寸即可。
    win.resize(target.width, target.height);
    if (!win.min) {
      // 第三个参数刻意不传：走默认的边界夹取，保证标题栏始终可点。
      win.move(target.x, target.y);
    } else {
      // 最小化状态下位置由 winbox 自己接管，但「还原后该回到哪里」要记下来
      win.x = Number.isFinite(Number(geom && geom.x)) ? Number(geom.x) : win.x;
      win.y = Number.isFinite(Number(geom && geom.y)) ? Number(geom.y) : win.y;
    }
  } catch (err) {
    /* 几何信息异常不应该影响窗口本身，忽略 */
  } finally {
    // keepSilent：调用方后面还会再调一次（例如等目录加载完再贴合），
    // 这里就不抢先摘掉 .dragging，免得中间那一瞬恢复过渡动画。
    // 若 .dragging 本来就挂着（新建窗口时会挂 200ms），就交给原来那个定时器摘，
    // 避免在这里提前摘掉。
    if (!keepSilent && !hadDragging) {
      win.g.classList.remove('dragging');
    }
  }
}

/**
 * 桌面布局变化通知（窗口增减 / 移动 / 缩放 / 最大化 / 最小化都会触发）。
 *
 * 用「注册回调」而不是让 wins.js 直接 import sessionstate.js：后者会形成
 * wins ⇄ sessionstate 的循环依赖，虽然 ES 模块能扛住，但没必要留这个雷。
 */
let changeListener = null;

export function onWindowsChanged(fn) {
  changeListener = (typeof fn === 'function') ? fn : null;
}

function notifyChanged() {
  if (!changeListener) {
    return;
  }
  try {
    changeListener();
  } catch (err) {
    /* 订阅者异常不影响窗口操作 */
  }
}

/**
 * 窗口内容控制器登记表。
 *
 * explorer.js 建好窗口后会把自己的实例登记进来，sessionstate.js 保存布局时
 * 再取出来，问它「你在看哪个目录、什么视图、历史是什么」。
 *
 * 用 WeakMap 是为了不给窗口记录加"只在保存时才用得上"的字段：窗口一销毁，
 * 引用自动释放，也不会有内存泄漏。
 */
const windowOwners = new WeakMap();

export function registerWindowOwner(record, owner) {
  if (record && owner) {
    windowOwners.set(record, owner);
  }
}

export function getWindowOwner(record) {
  return (record && windowOwners.get(record)) || null;
}

class WindowManager {
  constructor() {
    this.layer = document.getElementById('windowLayer');
    this.taskItems = document.getElementById('taskItems');
    this.windows = new Map();
    this.seq = 0;
    this.cascade = 0;
    this.subscribers = [];
  }

  /** 订阅窗口集合变化（桌面用它来切换小部件显隐等） */
  subscribe(fn) {
    if (typeof fn === 'function') {
      this.subscribers.push(fn);
    }
  }

  notify() {
    const self = this;
    this.subscribers.forEach(function (fn) {
      try {
        fn(self);
      } catch (err) {
        /* 订阅者异常不影响窗口操作 */
      }
    });
  }

  /**
   * 创建窗口。
   *
   * @param {object} options
   *   title       标题
   *   iconName    标题栏图标（icons.js 中的名字）
   *   content     内容 DOM 元素
   *   width/height      初始尺寸
   *   minWidth/minHeight 最小尺寸
   *   x/y         指定初始位置（还原上次布局时用；不传则居中 + 层叠偏移）
   *   silent      不播放「新建」时的那一小段拖动抑制（还原布局时用）
   *   windowClass 额外 CSS 类
   *   taskLabel   任务栏显示的文字（默认取 title）
   *   onBeforeClose () => boolean，返回 true 可阻止关闭
   *   onClosed    () => void
   * @returns {object} 窗口记录
   */
  create(options) {
    const opts = options || {};
    const WinBoxCtor = window.WinBox;

    if (typeof WinBoxCtor !== 'function') {
      throw new Error('winbox.js 未加载成功，请检查 /static/vendor/winbox.min.js 是否存在');
    }

    const self = this;
    const id = 'win' + (++this.seq);

    // ---- 计算尺寸与位置 ----
    // 传了 x/y 就完全按给定的几何信息来（还原上次布局）；否则退回到
    // 「居中 + 层叠偏移」，让新窗口不会一个个完全重叠。
    const explicitPos = Number.isFinite(Number(opts.x)) && Number.isFinite(Number(opts.y));

    let box;
    if (explicitPos) {
      // 还原上次布局：完全按给定的位置和尺寸来
      box = clampGeometry({
        x: Number(opts.x),
        y: Number(opts.y),
        width: opts.width || 940,
        height: opts.height || 620
      });
      // 还原时窗口数量是固定的，把层叠序号归零，
      // 免得之后第一扇手动打开的窗口被推到很偏的位置
      this.cascade = 0;
    } else {
      box = this.cascadeBox(opts.width || 940, opts.height || 620);
    }

    const width = box.width;
    const height = box.height;
    const x = box.x;
    const y = box.y;

    const content = opts.content || document.createElement('div');

    const win = new WinBoxCtor({
      title: opts.title || '',
      root: this.layer,
      mount: content,
      width: width,
      height: height,
      x: x,
      y: y,
      minwidth: opts.minWidth || 380,
      minheight: opts.minHeight || 220,
      // 关键：把任务栏高度作为底部内边距，最大化时不会盖住任务栏
      top: 0,
      left: 0,
      right: 0,
      bottom: TASKBAR_HEIGHT,
      class: ['win-app'].concat(opts.windowClass ? [].concat(opts.windowClass) : []),
      onfocus: function () {
        self.handleFocus(id);
      },
      onblur: function () {
        self.handleBlur(id);
      },
      onminimize: function () {
        self.handleStateChange(id);
      },
      onrestore: function () {
        self.handleStateChange(id);
      },
      onmaximize: function () {
        self.handleStateChange(id);
      },
      onclose: function () {
        return self.handleClose(id);
      }
    });

    // ---- 标题栏图标 ----
    const header = win.g && win.g.querySelector('.wb-header');
    const drag = header && header.querySelector('.wb-drag');
    if (drag && opts.iconName) {
      const iconEl = document.createElement('span');
      iconEl.className = 'win-title-icon';
      iconEl.innerHTML = icon(opts.iconName);
      drag.insertBefore(iconEl, drag.firstChild);

      // 双击标题栏 = 最大化 / 还原（Windows 习惯）
      drag.addEventListener('dblclick', function (e) {
        if (e.target.closest('.wb-control')) {
          return;
        }
        if (win.max) {
          win.restore();
        } else {
          win.maximize();
        }
      });
    }

    // ---- 任务栏按钮 ----
    const taskBtn = document.createElement('div');
    taskBtn.className = 'task-btn';
    taskBtn.title = opts.taskLabel || opts.title || '';
    taskBtn.innerHTML =
      '<span class="tb-icon">' + icon(opts.iconName || 'app-explorer') + '</span>' +
      '<span class="tb-label">' + escapeText(opts.taskLabel || opts.title || '窗口') + '</span>';
    taskBtn.addEventListener('click', function () {
      self.taskButtonClick(id);
    });
    if (this.taskItems) {
      this.taskItems.appendChild(taskBtn);
    }

    const record = {
      id: id,
      win: win,
      content: content,
      taskBtn: taskBtn,
      title: opts.title || '',
      iconName: opts.iconName || 'app-explorer',
      wasMax: false,
      onBeforeClose: opts.onBeforeClose,
      onClosed: opts.onClosed
    };
    this.windows.set(id, record);

    // 新建窗口时短暂抑制过渡动画，避免窗口「滑」到位。
    // 还原上次布局时传 silent，省掉这段动画（位置本来就是用户熟悉的位置）。
    if (!opts.silent) {
      win.g.classList.add('dragging');
      setTimeout(function () {
        win.g && win.g.classList.remove('dragging');
      }, 200);
    }

    this.watchGeometry(record);

    this.syncTaskbar();
    this.notify();
    notifyChanged();

    return record;
  }

  /**
   * 监听窗口的几何变化，任何变化都通知布局保存逻辑。
   *
   * 为什么不能只靠 winbox 的回调：winbox 只提供 onfocus / onminimize /
   * onmaximize 这类状态回调，**没有 onmove / onresize**。而拖动和缩放正是
   * 最需要保存的两件事，所以这里用 MutationObserver 盯着 .winbox 元素上
   * 被 winbox 改写的 style 属性和表示状态的 class。
   *
   * 拖动过程中回调会很密集，但下游有防抖兜底（见 sessionstate.scheduleSave），
   * 所以这里不做节流，保证「最后一次该保存的一定被保存到」。
   */
  watchGeometry(record) {
    if (!record || !record.win || !record.win.g) {
      return;
    }
    const observe = window.MutationObserver;
    if (typeof observe !== 'function') {
      return; // 极老的浏览器：退化成「只在状态回调时保存」
    }

    const el = record.win.g;
    try {
      const observer = new observe(function () {
        notifyChanged();
      });
      observer.observe(el, {
        attributes: true,
        attributeFilter: ['style', 'class']
      });
      record.geometryObserver = observer;
    } catch (err) {
      /* 观察失败不影响窗口功能 */
    }
  }

  /**
   * 新窗口的默认位置：居中 + 层叠偏移。
   *
   * 抽出来是为了让「还原上次布局」和「手动打开」两条路径共用同一套夹取规则
   * （都经过 clampGeometry），不至于一边留 6px 边距、另一边留 40px。
   */
  cascadeBox(wantW, wantH) {
    const availW = Math.max(320, window.innerWidth);
    const availH = Math.max(240, window.innerHeight - TASKBAR_HEIGHT);

    const width = Math.min(wantW || 940, Math.max(360, availW - 40));
    const height = Math.min(wantH || 620, Math.max(240, availH - 40));

    const step = 26;
    const n = this.cascade % 6;
    this.cascade += 1;

    const x = Math.round((availW - width) / 2) + Math.round((n - 2.5) * step);
    const y = Math.round((availH - height) / 2) + Math.round((n - 2.5) * step);

    return clampGeometry({ x: x, y: y, width: width, height: height });
  }

  /** 取窗口记录 */
  get(id) {
    return this.windows.get(id) || null;
  }

  /** 关闭窗口（会走 onBeforeClose 钩子） */
  close(id) {
    const record = this.windows.get(id);
    if (record && record.win) {
      record.win.close();
    }
  }

  /** 窗口数量 */
  count() {
    return this.windows.size;
  }

  /** 遍历所有窗口 */
  each(fn) {
    this.windows.forEach(function (record) {
      fn(record);
    });
  }

  /* -------------------------------------------------------------------------
     内部事件
     ------------------------------------------------------------------------- */

  handleFocus(id) {
    const record = this.windows.get(id);
    if (record && record.win && record.win.max) {
      record.wasMax = true;
    }
    this.syncTaskbar();
    this.notify();
    notifyChanged();
  }

  handleBlur(id) {
    this.syncTaskbar();
  }

  handleStateChange(id) {
    this.syncTaskbar();
    this.notify();
    notifyChanged();
  }

  /** 返回 true 表示阻止关闭 */
  handleClose(id) {
    const record = this.windows.get(id);
    if (!record) {
      return false;
    }

    if (typeof record.onBeforeClose === 'function') {
      let blocked = false;
      try {
        blocked = record.onBeforeClose() === true;
      } catch (err) {
        blocked = false;
      }
      if (blocked) {
        return true;
      }
    }

    this.destroy(id);
    return false;
  }

  destroy(id) {
    const record = this.windows.get(id);
    if (!record) {
      return;
    }
    this.windows.delete(id);

    // 断开几何观察，避免已关闭窗口的观察器继续持有 DOM
    if (record.geometryObserver) {
      try {
        record.geometryObserver.disconnect();
      } catch (err) {
        /* 忽略 */
      }
      record.geometryObserver = null;
    }

    if (record.taskBtn && record.taskBtn.parentNode) {
      record.taskBtn.remove();
    }

    if (typeof record.onClosed === 'function') {
      try {
        record.onClosed();
      } catch (err) {
        /* 忽略清理异常 */
      }
    }

    this.syncTaskbar();
    this.notify();
    notifyChanged();
  }

  /** 任务栏按钮点击：最小化 / 还原 / 聚焦 */
  taskButtonClick(id) {
    const record = this.windows.get(id);
    if (!record || !record.win) {
      return;
    }
    const win = record.win;

    if (win.min) {
      // 从最小化恢复；如果最小化前是最大化状态，一并恢复
      win.restore();
      if (record.wasMax) {
        win.maximize();
        record.wasMax = false;
      }
      win.focus();
    } else if (win.focused) {
      // 已经是当前窗口 -> 最小化，并记住最大化状态
      record.wasMax = !!win.max;
      win.minimize();
    } else {
      win.focus();
    }

    this.syncTaskbar();
  }

  /** 把所有窗口最小化（点「显示桌面」时用） */
  minimizeAll() {
    const self = this;
    this.windows.forEach(function (record) {
      if (record.win && !record.win.min) {
        record.wasMax = !!record.win.max;
        record.win.minimize();
      }
    });
    setTimeout(function () {
      self.syncTaskbar();
    }, 20);
  }

  /** 同步任务栏按钮的激活状态 */
  syncTaskbar() {
    this.windows.forEach(function (record) {
      if (!record.taskBtn || !record.win) {
        return;
      }
      const active = !record.win.min && !!record.win.focused;
      record.taskBtn.classList.toggle('active', active);
    });
  }
}

/** 简单的文本转义（避免为了一个函数去依赖 ui.js 造成循环引用） */
function escapeText(text) {
  return String(text === null || text === undefined ? '' : text)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

/** 全局单例 */
export const wm = new WindowManager();

// 浏览器窗口尺寸变化后，winbox 会让已最大化/已限制在可视区内的窗口重新贴合，
// 位置因此可能变化，所以要顺手记一笔。不监听就不监听也不会丢数据，
// 只是下次打开浏览器时窗口位置可能略旧。
try {
  window.addEventListener('resize', function () {
    notifyChanged();
  });
} catch (err) {
  /* 非浏览器环境或极端情况：忽略 */
}

export default wm;

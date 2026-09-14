/* ==========================================================================
   桌面布局持久化
   --------------------------------------------------------------------------
   目标：关掉浏览器标签页再打开，还是原来那个桌面 —— 同样的窗口、同样的位置
   和大小、同样的目录和视图模式、后退/前进历史也还在。

   分工：
     * 本模块只做「怎么存、什么时候存、怎么还原」这三件事；
     * 窗口长什么样、怎么创建，仍然由 wins.js / explorer.js 决定 ——
       还原走的是和手动打开**完全同一条**代码路径，不在这里复制一份窗口构造，
       否则两边迟早会长歪（改一处忘一处）。

   服务端（fileweb/userstate.py）只当仓库：整份覆盖保存、不做合并、不解释内容，
   上限 256 KB。所以这里必须做到「每次提交的都是完整布局」，并且控制体积
   （见 HISTORY_LIMIT）。

   失败处理的原则：**保存失败绝不打扰用户**。
   布局记录属于锦上添花，坏掉了顶多是下次打开回到默认布局；
   为它弹一个报错对话框，反而是把小事闹大。所以出错了只记一次日志。

   ★ 终端窗口（terminal.js）
   --------------------------------------------------------------------------
   终端和别的窗口有个本质区别：**窗口里的东西活在服务端**。
   后端支持「WebSocket 断开不结束会话」，所以关掉窗口 / 关掉标签页之后，
   那个 cmd.exe 还在跑、输出还在攒，只要凭 sid 连回去就能接上。

   于是本模块对终端窗口的定义是：
     * 落盘时保存 **sid**（外加窗口几何与一点展示用信息）；
     * 还原时**接回那个 sid**，而不是新建一个会话；
     * 关闭终端窗口（标题栏 ✕）**不算结束会话** —— 会话留在服务端，
       窗口留在布局里（见 registerTerminalStash），重新打开页面它就回来了。

   这份「离开」语义要和用户能看见的行为对上：真正结束会话是工具栏
   「结束会话」那个按钮（terminal.js 发 {"type":"close"}），两者不能混。
   ========================================================================== */

import * as api from './api.js';
import { wm, applyGeometry, getWindowOwner, onWindowsChanged } from './wins.js';
import { openExplorer } from './explorer.js';
import { openPreview } from './preview.js';
import { openEditor } from './editor.js';
import { restoreTerminal } from './terminal.js';
import { openMusic } from './music.js';

/** 状态文档的版本号。格式一旦不兼容就 +1，老文档会被整体丢弃。 */
export const STATE_VERSION = 1;

/** 每个窗口最多带回多少条历史。防止有人把历史翻到几百条把请求撑爆。 */
const HISTORY_LIMIT = 30;

/** 防抖窗口：拖窗口时 winbox 会连续改 style，等用户停下来再发请求。 */
const DEBOUNCE_MS = 600;

/**
 * 关掉的终端窗口在布局里保留多久（毫秒）。
 *
 * 「关掉窗口 = 离开会话」这条语义要求窗口被关掉之后仍然留在布局里，
 * 否则用户关掉浏览器就再也回不到那个会话了。但也不能永久保留：
 * 关了几天之后那个会话一定早就被回收了，留着它只会让每次打开页面都拿一个
 * 死 sid 去撞一次服务端（虽然 terminal.js 能兜住，但没必要）。
 * 一小时的量与服务端的空闲回收尺度（分钟级）相比足够长，够覆盖
 * 「关掉窗口，干点别的，再回来」这种真实用法。
 */
const DETACHED_TERMINAL_TTL_MS = 60 * 60 * 1000;

/** 同一次会话里只抱怨一次，避免刷满控制台。 */
let loggedFailure = false;

/** 防抖计时器 / 正在提交 / 有改动待提交 */
let saveTimer = null;
let saving = false;
let pending = false;

/** 还原过程中把自动保存关掉，否则每创建一个窗口就会往回写一次。 */
let suspended = 0;

/** 最近一次成功保存的内容，用来避免「什么都没变也发请求」。 */
let lastSavedText = '';

/**
 * 服务端是否已经存过一份布局（本次启动时读到的非空文档）。
 *
 * 用来兜住这个场景：用户之前存好了布局 → 后来打开页面但一个窗口都没开
 * → 关掉。如果没有这个标记，第二次启动就会用一份「空布局」把原来的内容
 * 冲掉 —— 而用户什么都没做。
 */
let hasBaseline = false;

/** 桌面实例，initSessionState() 时记下来，保存/还原都要用。 */
let desktop = null;

/* ---------------------------------------------------------------------------
   测试钩子
   --------------------------------------------------------------------------- */

/**
 * 仅用于自动化测试：把模块内部状态清成「刚打开页面」的样子。
 *
 * 为什么需要它：行为测试要在同一个模块实例上模拟多次「关掉页面再打开」。
 * 真实浏览器里模块状态是随页面重建的，而 Node 里 import 只会求值一次，
 * 所以只能显式重置 —— 注意 terminal.js 是用 import('./sessionstate.js')
 * 推快照的，因此测试**不能**换个带 query 的路径另起一个实例，否则两边
 * 就是两个模块、两份状态，测出来的东西没有意义。
 *
 * 正式代码不要调用它。
 */
export function __resetForTest() {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  saving = false;
  pending = false;
  dirty = false;
  suspended = 0;
  lastSavedText = '';
  hasBaseline = false;
  // 只 +1 不删：上一页的登记自然会被 pageToken 过滤掉
  pageToken += 1;
}

/** 调试用：当前登记了多少条终端快照（正式代码不要用） */
export function debugStashKeys() {
  return Array.from(terminalStashes.keys());
}

/* ---------------------------------------------------------------------------
   小工具
   --------------------------------------------------------------------------- */

function isFiniteNumber(value) {
  return typeof value === 'number' && isFinite(value);
}

/** 四舍五入成整数，避免存一堆 940.0000000001 这样的浮点噪声 */
function roundInt(value) {
  const num = Number(value);
  return isFinite(num) ? Math.round(num) : null;
}

/** 从行内样式的 z-index 里取层叠顺序（winbox 用 z-index 表达前后关系） */
function zIndexOf(win) {
  const el = win && win.g;
  if (!el) {
    return 0;
  }
  const fromStyle = /z-index:\s*(-?\d+)/.exec(el.getAttribute('style') || '');
  if (fromStyle) {
    return parseInt(fromStyle[1], 10) || 0;
  }
  return (typeof win.index === 'number' && isFinite(win.index)) ? win.index : 0;
}

/* ---------------------------------------------------------------------------
   读取 / 保存
   --------------------------------------------------------------------------- */

/**
 * 拉取上次保存的状态。
 *
 * 任何异常（网络断了、后端 500、返回的不是对象）都退化成 {}，
 * 让调用方走默认布局 —— 这里**绝不抛异常**，否则整个桌面启动都会被拖下水。
 *
 * @returns {Promise<object>} 保存的状态；没有/不可用时返回 {}
 */
export async function loadState() {
  try {
    const res = await api.getDesktopState();
    const state = (res && res.state) || {};

    if (!state || typeof state !== 'object' || Array.isArray(state)) {
      return {};
    }
    // 空文档 = 「从来没存过」，这是**首跑的正常状态**，不是版本不兼容。
    // 不先判空的话，首跑会打一条「忽略不兼容的布局版本: undefined」，
    // 让排查首跑问题的人以为版本对不上（浏览器验证时确实误导过一次）。
    if (Object.keys(state).length === 0) {
      return {};
    }
    if (state.v !== STATE_VERSION) {
      // 老版本（或者被人手改过的）文档：宁可回到默认布局，
      // 也不要按错误的形状去还原出一堆乱七八糟的窗口
      console.info('[sessionstate] 忽略不兼容的布局版本:', JSON.stringify(state.v));
      return {};
    }
    return state;
  } catch (err) {
    if (err && err.status === 401) {
      return {}; // 会话过期，api 层会自己跳登录页
    }
    logFailure('读取布局失败', err);
    return {};
  }
}

function logFailure(what, err) {
  if (loggedFailure) {
    return;
  }
  loggedFailure = true;
  console.warn('[sessionstate] ' + what + '（不影响使用）:', (err && err.message) || err);
}

/** 是否为空文档（没有任何窗口） */
function isEmptyState(state) {
  return !state || !Array.isArray(state.windows) || state.windows.length === 0;
}

/** 保存状态（立即执行，不防抖）。失败只记日志。 */
export async function saveState(state, keepalive) {
  if (saving) {
    // 上一次还在路上：标记一下，等它回来再补一次，避免并发整份覆盖
    pending = true;
    return;
  }

  const doc = state || collectState();
  const payload = { v: STATE_VERSION, windows: Array.isArray(doc.windows) ? doc.windows : [] };

  // 「本来就存过，现在却一个窗口都没有」—— 这几乎总是「用户只是打开看了一眼
  // 就关了」，而不是「用户把窗口全关掉想清空布局」。宁可漏存一次，
  // 也不能把别人的布局悄悄清掉。
  if (hasBaseline && payload.windows.length === 0) {
    return;
  }

  const text = JSON.stringify(payload);

  // 内容没变就别浪费一次请求（拖动时防抖之后这种情况很常见：
  // 拖了一圈又拖回原位、或者只是点了下窗口标题栏）
  if (text === lastSavedText) {
    return;
  }

  saving = true;
  try {
    await api.putDesktopState(payload, keepalive);
    lastSavedText = text;
  } catch (err) {
    if (err && err.status === 401) {
      return; // 已跳登录页
    }
    logFailure('保存布局失败', err);
  } finally {
    saving = false;
    if (pending) {
      pending = false;
      saveState(null, false);
    }
  }
}

/** 立刻提交（页面即将卸载时用，绕过防抖） */
export function flushSave(keepalive) {
  if (saveTimer) {
    clearTimeout(saveTimer);
    saveTimer = null;
  }
  if (!dirty) {
    return Promise.resolve();
  }
  dirty = false;
  return saveState(null, keepalive);
}

/* ---------------------------------------------------------------------------
   触发保存
   --------------------------------------------------------------------------- */

/** 布局有改动（还没提交） */
let dirty = false;

/**
 * 登记一次「布局变了」，稍后合并成一次提交。
 *
 * 拖动窗口时 winbox 每一帧都在改 style，直接保存会把服务器打爆，
 * 所以这里统一走防抖。
 */
export function scheduleSave() {
  if (suspended > 0) {
    return;
  }
  if (!desktop) {
    return; // 桌面还没起来
  }
  if (!dirty && saveTimer) {
    return;
  }

  dirty = true;
  if (saveTimer) {
    clearTimeout(saveTimer);
  }
  saveTimer = setTimeout(function () {
    saveTimer = null;
    dirty = false;
    saveState(null, false);
  }, DEBOUNCE_MS);
}

/* ---------------------------------------------------------------------------
   采集当前布局
   --------------------------------------------------------------------------- */

/**
 * 判断窗口种类；返回 'unknown' 表示「不认识的窗口，既不入库也不还原」。
 *
 * 为什么按内容元素的 class 判断，而不是看窗口记录上的自定义标记：
 * 那种标记只有「还原出来的窗口」才会有，用户手动打开的窗口身上没有，
 * 结果就是「新开的窗口存不下来」—— 恰好把最该保存的场景漏掉了。
 * explorer.js / preview.js / terminal.js 各自会给自己挂
 * 'explorer' / 'preview' / 'terminal' 类名，这里以它为准，
 * 两条路径（手动打开 / 还原）判断结果完全一致。
 */
function windowKind(record) {
  const classes = record && record.content && record.content.classList;

  if (classes && classes.contains('explorer')) {
    return 'explorer';
  }
  // 终端窗口：由 terminal.js 在创建窗口时把自己登记进 wins.js 的登记表，
  // 所以用户手动打开的终端窗口同样能取到 sid（见 collectTerminal）
  if (classes && classes.contains('terminal')) {
    return 'terminal';
  }
  // 编辑器窗口（editor.js 会给自己挂 .editor 类名）。
  // 必须排在 preview 前面判断：两者都以 editorWin / previewCtx 这类字段区分，
  // 但 .editor 这个类名是编辑器独有的，先判它不会误伤预览窗口。
  if (classes && classes.contains('editor')) {
    return 'editor';
  }
  if (record && (record.previewCtx || (classes && classes.contains('preview')))) {
    return 'preview';
  }
  // 音乐播放器（music.js 会给自己挂 .music 类名）。
  // 它只需要几何信息：正在播的那首歌、音量、播放模式都由服务端的
  // 播放偏好（/api/music/prefs）按用户记着，还原窗口时它会自己接上 ——
  // 同一件事不存两份，免得两边对不上。
  if (classes && classes.contains('music')) {
    return 'music';
  }
  return 'unknown';
}

/** 把一个窗口拍成可以落盘的一条记录 */
function collectWindow(record) {
  const win = record.win;
  const kind = windowKind(record);
  const item = {
    kind: kind,
    // z 用来记录前后层叠顺序：还原时按它排序，让上次在最前面的窗口还在最前面
    z: zIndexOf(win),
    x: roundInt(win.x),
    y: roundInt(win.y),
    width: roundInt(win.width),
    height: roundInt(win.height),
    max: !!(win.g && win.g.classList.contains('max')),
    min: !!win.min
  };

  if (kind === 'explorer') {
    // 位置控制器由 explorer.js 在创建窗口时登记（见 wins.registerWindowOwner），
    // 所以用户手动打开的窗口同样能取到目录 / 视图 / 历史
    const explorer = getWindowOwner(record);
    if (explorer) {
      Object.assign(item, collectExplorer(explorer));
    }
  } else if (kind === 'preview' && record.previewCtx) {
    item.root = record.previewCtx.rootId || '';
    item.path = record.previewCtx.rel || '';
  } else if (kind === 'editor') {
    // ★ 编辑器窗口只存「窗口几何 + 文件路径」，**从不存缓冲区**。
    //   正在编辑的全文属于用户的草稿：塞进这份布局既会撑爆服务端的 256KB
    //   配额，也等于在服务端悄悄留了一份他以为只在自己浏览器里的内容。
    //   代价是「带着未保存改动关页面」会在下次还原时丢掉那些改动 ——
    //   这是刻意的取舍，editor.js 的 serialize() 里也写了同一句话。
    //   控制器的 serialize() 返回的就是 {root, path, title}，没有别的字段。
    const editorWindow = getWindowOwner(record);
    item.root = '';
    item.path = '';
    if (editorWindow && typeof editorWindow.serialize === 'function') {
      let data = null;
      try {
        data = editorWindow.serialize();
      } catch (err) {
        /* 单个窗口序列化失败不该影响其它窗口；下面兜住 */
        data = null;
      }
      data = data || {};
      item.root = data.root || '';
      item.path = data.path || '';
    }
  } else if (kind === 'terminal') {
    // 会话信息（sid / 是否已结束）在 terminal.js 那边，走同一张登记表拿；
    // 拿不到就只留几何 —— 还原出来是个「会话已结束」的窗口，不是空壳
    const terminal = getWindowOwner(record);
    if (terminal && typeof terminal.serialize === 'function') {
      Object.assign(item, collectTerminal(terminal));
    }
  }

  // 音乐播放器刻意**不在上面加任何字段**：它只需要几何信息。
  // 正在播的那首歌、音量、播放模式都由服务端的播放偏好
  // （/api/music/prefs，按用户存）记着，窗口还原后自己会接上 ——
  // 同一件事存两份，迟早会出现「布局说在放 A、偏好说在放 B」。
  return item;
}

/**
 * 终端窗口要落盘的东西：会话标识 + 一点展示用信息。
 *
 * 关键是 sid —— 刷新页面之后就是靠它接回原来那个命令行的。
 * serialize() 内部会在窗口已经关掉时退回「关窗那一刻记住的那一份」，
 * 所以这里不必区分窗口是不是还开着。
 */
function collectTerminal(terminal) {
  let data = null;
  try {
    data = terminal.serialize(true);
  } catch (err) {
    /* 单个窗口序列化失败不该影响其它窗口；下面兜住 */
    data = null;
  }
  data = data || {};

  const sid = typeof data.sid === 'string' && data.sid ? data.sid : null;
  const takenOver = data.takenOver === true;

  const out = {
    /* ★ 会话标识。null 表示这个窗口里的会话已经确认结束了 —— 下次打开页面时
       直接显示「会话已结束」，不必再拿一个死 sid 去撞服务端。
       注意这里刻意不把 sid 丢掉：会话还活着时它必须存下来。 */
    sid: sid,
    /* 「会话已结束」= 真的没有了（被回收 / 进程退出 / 用户点了结束会话）。
       takenOver 不算：那种情况下会话在另一个连接手里活得好好的，
       所以既不能标 ended（会骗用户），也不能清 sid（会把他的会话弄丢）。 */
    ended: data.ended === true && !takenOver,
    /* 被别的连接接管：会话还活着，但下次打开页面不该再去抢它 */
    takenOver: takenOver,
    /* 这一次连接是否真的接上过。
       关窗之后要靠它区分两种情况：接上过 → 会话是用户自己结束的，
       窗口该留着；从没接上过 → 那个会话我们从来没确认过，留着只会变成
       下次打开时一个「会话已结束」的空窗口，应当丢掉。 */
    attached: data.attached === true,
    shell: typeof data.shell === 'string' ? data.shell : '',
    cwd: typeof data.cwd === 'string' ? data.cwd : '',
    title: typeof data.title === 'string' ? data.title : ''
  };

  if (!sid) {
    out.ended = true;
  }

  return out;
}

/** 资源管理器窗口的位置信息（目录 + 视图模式 + 历史栈） */
function collectExplorer(explorer) {
  let data = null;
  try {
    data = explorer.serialize();
  } catch (err) {
    /* 单个窗口序列化失败不该影响其它窗口；下面按「此电脑」兜底 */
    data = null;
  }
  data = data || {};

  const out = {
    mode: data.mode === 'computer' ? 'computer' : 'dir',
    root: data.root || '',
    path: data.path || '',
    view: data.view === 'list' ? 'list' : 'icons'
  };

  // 处于「此电脑」视图、或者位置信息读不出来（root 为空）时，统一按
  // 「此电脑」记录：还原出来至少是一个能用的窗口，而不是一个打不开的路径
  if (data.mode === 'computer' || !out.root) {
    out.mode = 'computer';
    out.root = '';
    out.path = '';
  }

  const history = Array.isArray(data.history) ? data.history : [];
  const index = roundInt(data.historyIndex);

  if (history.length) {
    // 只保留最近的一段，避免历史很长时把文档撑大（服务端上限 256KB）
    const from = Math.max(0, history.length - HISTORY_LIMIT);
    out.history = history.slice(from).map(function (item) {
      return { root: (item && item.root) || '', path: (item && item.path) || '' };
    });
    // 裁剪后 index 要跟着平移，否则还原出来的「后退」位置是错的
    const shift = from;
    const idx = (isFiniteNumber(index) ? index : out.history.length - 1) - shift;
    out.historyIndex = Math.max(0, Math.min(idx, out.history.length - 1));
  } else {
    out.history = [];
    out.historyIndex = -1;
  }

  return out;
}

/**
 * 终端窗口的存档快照（窗口记录 id -> {at, item}）。
 *
 * ★ 为什么要有这份东西，而不是保存时现去问终端窗口：
 *   「关闭终端窗口 = 离开会话」这条语义要求窗口被关掉之后，那个会话仍然留在
 *   保存出去的布局里。可窗口一旦关掉，它就从 wm.windows 里消失了，
 *   collectState() 再也遍历不到它 —— 只能靠终端在关闭前把快照推过来。
 *
 *   为什么不在「采集的那一刻」才去问（即只让 collectState 主动拉）：
 *   保存是防抖 600ms 的。用户「开终端 → 接上 → 关窗口 → 立刻关浏览器」时，
 *   窗口被关掉那一刻 stash 里还什么都没有，最后那次 flushSave 采集时窗口
 *   已经不在列表里，这条会话就彻底丢了 —— 恰好把最该保住的场景漏掉。
 *   所以 terminal.js 在会话状态一变（接上 / 断开 / 结束 / 关窗）就主动登记。
 *
 * key 用窗口记录 id（关掉之后不会被复用），条目带时间戳，过期就丢。
 */
const terminalStashes = new Map();

/**
 * 「这一页」的标识。
 *
 * 正式环境里它恒为 0：每次打开页面都会重新求值这个模块，状态天然是空的。
 * 只有自动化测试会在同一个模块实例上模拟多次「关掉页面再打开」，
 * 那时由 __resetForTest() 把它 +1，好让上一页残留的登记不会混进这一页的布局。
 * 加了标记之后不需要在重置时删除条目（删了反而可能把这一页正在用的记录弄丢）。
 */
let pageToken = 0;

/**
 * 登记（或刷新）一个终端窗口的存档快照。
 *
 * terminal.js 在会话状态一变（接上 / 断开 / 结束）以及窗口关闭时都会调这里，
 * 所以它保存的永远是最新的一份 —— 而**不依赖「刚好保存过一次布局」**。
 */
export function registerTerminalStash(recordId, snapshot) {
  if (!recordId || !snapshot) {
    return;
  }
  const item = snapshot.item || snapshot;
  const takenOver = item.takenOver === true;
  const sid = typeof item.sid === 'string' && item.sid ? item.sid : null;

  terminalStashes.set(recordId, {
    at: Date.now(),
    page: pageToken,
    item: {
      kind: 'terminal',
      z: item.z,
      x: item.x,
      y: item.y,
      width: item.width,
      height: item.height,
      max: !!item.max,
      min: !!item.min,
      // 会话已结束（且不是「被接管」）才把 sid 置空：那时候它确实没法再接了；
      // 「被接管」时会话还活着，sid 必须留着
      sid: (item.ended === true && !takenOver) ? null : sid,
      ended: (item.ended === true && !takenOver) || !sid,
      takenOver: takenOver,
      /* 这次连接有没有真的接上过。
         关窗之后靠它区分两种情况：接上过 → 会话是用户自己结束的，窗口该留着；
         从没接上过 → 那个会话我们从来没确认过，留着只会变成下次打开页面时
         一个「会话已结束」的空窗口，所以丢掉（服务端空闲回收会收拾它）。 */
      attached: item.attached === true,
      shell: item.shell || '',
      cwd: item.cwd || '',
      title: item.title || ''
    }
  });
}

/**
 * 把登记进来的终端快照合并进要保存的布局。
 *
 * @param {Array} windows       已采集到的窗口条目（会往里追加）
 * @param {Set<string>} liveIds 当前仍然活着的终端窗口记录 id
 *   —— 它们的几何信息由 collectState 现场采集（更准），这里只刷新时间戳
 */
function mergeTerminalStashes(windows, liveIds) {
  const live = liveIds || new Set();
  const now = Date.now();

  terminalStashes.forEach(function (entry, key) {
    if (!entry || !entry.item) {
      return;
    }
    if (entry.page !== pageToken) {
      // 上一页留下来的登记：这一页的布局里不该出现（见 pageToken 的说明）
      return;
    }
    if (live.has(key)) {
      // 窗口还开着：几何由 collectState 现场采（更准），这里只续命
      entry.at = now;
      return;
    }
    // 窗口已经关掉或从没被采集过：超过 TTL 的条目丢弃 —— 那时候那个会话
    // 早该被回收了，留在布局里只会变成每次打开都白连一次的死窗口
    if (now - entry.at > DETACHED_TERMINAL_TTL_MS) {
      terminalStashes.delete(key);
      return;
    }
    if (!entry.item.sid && !entry.item.attached) {
      // 从没接上过的会话：不值得在布局里留一个空壳（见 registerTerminalStash）
      return;
    }
    windows.push(Object.assign({}, entry.item));
  });
}

/**
 * 采集当前整个桌面布局。
 *
 * 返回值可以直接 PUT 给 /api/desktop/state。
 * 明确**不采集**的东西：选中项、上传队列、加载中/进度条、打开的对话框、
 * 右键菜单、拖拽状态、提示队列 —— 这些是瞬时的，还原回来只会让人莫名其妙。
 *
 * 另外把「已关掉但会话仍活着的终端窗口」补回来（见 mergeTerminalStashes）：
 * 它们在窗口列表里已经不存在了，但按「关窗口 = 离开会话」的语义，
 * 它们仍然是用户桌面的一部分。
 */
export function collectState() {
  const windows = [];
  // 活着的终端窗口记录 id：用来把 stash 里「其实还开着」的那些排除掉，
  // 否则同一个会话会在布局里出现两次（还原时接两次，后一个把前一个顶掉）
  const liveTerminals = new Set();

  if (wm && wm.windows) {
    wm.windows.forEach(function (record) {
      if (!record || !record.win) {
        return;
      }
      const item = collectWindow(record);
      if (item.kind === 'unknown') {
        // 不认识的窗口类型（将来新增的窗口）：跳过而不是瞎还原
        return;
      }
      if (item.kind === 'terminal') {
        liveTerminals.add(record.id);
      }
      windows.push(item);
    });
  }

  mergeTerminalStashes(windows, liveTerminals);

  return { v: STATE_VERSION, windows: windows };
}

/* ---------------------------------------------------------------------------
   还原
   --------------------------------------------------------------------------- */

/** 还原过程中的保护计数 */
export function suspendAutosave() {
  suspended += 1;
}

export function resumeAutosave() {
  suspended = Math.max(0, suspended - 1);
}

/**
 * 还原上次的桌面布局。
 *
 * @param {object} state  loadState() 的结果；为空则什么都不做
 * @returns {Promise<number>} 成功还原的窗口数量
 */
export async function restoreState(state) {
  if (!state || !Array.isArray(state.windows) || !state.windows.length) {
    return 0;
  }

  suspendAutosave();
  let restored = 0;

  // 刚打开页面：把「上次会话里被关掉的终端窗口」那份记忆清空。
  // 不清的话，上一次 restoreState（同一个页面里重复调用时）留下的条目
  // 会和这次还原出来的窗口重复，布局里就会出现两个一样的终端窗口。
  terminalStashes.clear();

  try {
    // 层叠顺序：z 小的先还原，最后创建的排在最后（在最前面）。
    // 这样「上次焦点在哪个窗口」也能还原出来。
    const list = state.windows
      .filter(function (item) { return item && typeof item === 'object'; })
      .slice()
      .sort(function (a, b) {
        return (Number(a.z) || 0) - (Number(b.z) || 0);
      });

    for (let i = 0; i < list.length; i += 1) {
      try {
        const ok = await restoreOne(list[i]);
        if (ok) {
          restored += 1;
        }
      } catch (err) {
        // 单条坏数据绝不能中断整批还原（目录被删、盘符拔掉都可能走到这里）
        console.warn('[sessionstate] 跳过无法还原的窗口:', err && err.message ? err.message : err);
      }
    }
  } finally {
    resumeAutosave();
  }

  // 还原完立刻回存一次：把「实际上成功还原了什么」写回去
  // （比如失效的目录已经退回「此电脑」、失效的历史条目已经被丢掉）
  const snapshot = collectState();
  if (snapshot.windows.length || restored) {
    saveState(snapshot, false);
  }

  return restored;
}

/**
 * 还原一个窗口；返回 true 表示窗口确实被重建了
 *
 * 用 async 是因为终端窗口的还原要等一次 WebSocket 接上（或确认接不上）才会
 * 有确定结果，而那条路径是异步的。
 */
async function restoreOne(item) {
  const kind = item && item.kind;

  if (kind === 'explorer') {
    return restoreExplorer(item);
  }
  if (kind === 'preview') {
    return restorePreview(item);
  }
  if (kind === 'editor') {
    return restoreEditorWindow(item);
  }
  if (kind === 'terminal') {
    const result = await restoreTerminal(desktop, item);

    if (!result || !result.record) {
      return false;
    }
    // 几何信息立刻补上：虽然 window 是先按存档尺寸建的，但 clampGeometry
    // 可能因为换了小屏幕而夹过，这里再贴合一次保证和存档一致。
    // 窗口是「会话接不上也照样存在」的，所以这一步不依赖上面那次连接的结果。
    applyGeometry(result.record, geometryOf(item), false);
    restoreWindowFlags(result.record, item);
    return true;
  }
  if (kind === 'music') {
    return restoreMusic(item);
  }

  return false;
}

/**
 * 还原音乐播放器窗口：走 openMusic()，和手动打开完全同一条路径。
 *
 * ★ 只还原窗口本身，**不会开始播放**：浏览器的自动播放策略本来就会拦掉
 * 没有用户交互的播放，而且「一打开页面就突然出声」对用户也很不友好。
 * 上次听的那首歌会被摆好（暂停状态），按一下播放键就接着听。
 */
function restoreMusic(item) {
  // 功能可能在这之后被管理员关掉了（config.json 的 music.enabled）：
  // 存档里还留着这个窗口，但打开它只会得到一个「每个接口都 403」的空壳，
  // 不如干脆不还原 —— 与「关掉后开始菜单里没有入口」保持一致。
  const features = (desktop && desktop.info && desktop.info.features) || {};
  if (features.music !== true) {
    return Promise.resolve(false);
  }

  const record = openMusic(desktop, { ...geometryOf(item), silent: true });
  if (!record) {
    return Promise.resolve(false);
  }

  // 窗口是按存档尺寸建的，但 clampGeometry 可能因为换了小屏幕而夹过，
  // 这里再贴合一次保证与存档一致（不播过渡动画，位置本就是用户熟悉的位置）
  applyGeometry(record, geometryOf(item), false);
  restoreWindowFlags(record, item);

  return Promise.resolve(!!wm.get(record.id));
}

/** 还原资源管理器窗口：走 openExplorer()，和手动打开完全同一条路径 */
function restoreExplorer(item) {
  const roots = (desktop && desktop.info && desktop.info.roots) || [];

  // 根目录可能已经不在配置里了（改了 config.json，或者 U 盘拔了）：
  // 直接退回「此电脑」，而不是让这次还原失败
  const rootOk = roots.some(function (r) { return r.id === item.root; });
  const rootId = rootOk ? item.root : '';
  const rel = rootOk ? (item.path || '') : '';

  // 注意 openExplorer() 自带「同一位置已开着就聚焦」的去重逻辑。
  // 极端情况下它会直接返回一个已存在的窗口（例如启动时地址栏还带着
  // #explorer/… 深链接，先一步开了同一个目录），此时这里拿到的不是新建窗口，
  // 保存的位置/历史就不会覆盖它 —— 目录本身仍然是对的，属于可接受的取舍。
  const explorer = openExplorer(desktop, rootId, rel, geometryOf(item));
  if (!explorer) {
    return Promise.resolve(false);
  }

  // openExplorer() 返回时窗口已经建好了（start() 里的导航是异步的），
  // 所以位置要在这一刻立刻补上 —— 晚一点就会被 .winbox 的 0.18s 过渡动画
  // 拍成一段「窗口自己滑过去」的动画。
  // （窗口控制器已由 openExplorer 内部登记进 wins.js 的登记表，这里不必再记。）
  const record = explorer.record;
  applyGeometry(record, geometryOf(item), true);

  const done = explorer.restoreFromState
    ? explorer.restoreFromState({ ...item, root: rootId, path: rel })
    : Promise.resolve();

  return done.then(function () {
    // 还原完再贴合一次：如果刚才的目录已经不存在，会退回「此电脑」，
    // 尺寸可能被内容撑过，这里顺手纠正一下（仍然不播动画）
    if (!explorer.destroyed) {
      applyGeometry(record, geometryOf(item), false);
      restoreWindowFlags(record, item);
    }
    return true;
  });
}

/** 还原预览窗口 */
function restorePreview(item) {
  const roots = (desktop && desktop.info && desktop.info.roots) || [];
  const rootOk = roots.some(function (r) { return r.id === item.root; });
  if (!rootOk) {
    return Promise.resolve(false);
  }

  const name = String(item.path || '').split('/').pop() || '';

  // 预览窗口内部没有可还原的状态；文件要是已经不在了，
  // 预览自己的错误提示会显示在窗口里（不是弹窗），不会影响别的窗口
  const record = openPreview({
    desktop: desktop,
    rootId: item.root,
    rel: item.path || '',
    name: name,
    sizeText: ''
  });
  if (!record) {
    return Promise.resolve(false);
  }

  applyGeometry(record, geometryOf(item), false);
  restoreWindowFlags(record, item);
  return Promise.resolve(true);
}

/**
 * 还原编辑器窗口。
 *
 * 走的是和手动打开**完全同一条**路径（openEditor），只是位置尺寸由调用方补上。
 *
 * ★ 两个刻意的取舍，都和「绝不让用户丢数据」这条线有关：
 *   1. **不还原未保存的缓冲区。** 布局里只存了路径和几何（见 collectWindow），
 *      所以上次带着改动关掉的编辑器，这次还原出来是磁盘上的干净内容。
 *      想保住改动只有保存一条路 —— 这也正是我们希望的。
 *   2. **文件不在了就跳过。** editor.js 在读不到文件（404）时会自己把窗口关掉
 *      并记一条日志，所以这里拿到的 record 可能很快就不存在了；下面每一步都
 *      先确认窗口还在，避免对一个已经销毁的窗口动手。
 *
 * 返回值与 restorePreview 对齐：Promise<boolean>，false 表示没有重建成功。
 */
function restoreEditorWindow(item) {
  const roots = (desktop && desktop.info && desktop.info.roots) || [];
  const rootOk = roots.some(function (r) { return r.id === item.root; });
  if (!rootOk) {
    // 根目录已经不在配置里了：和预览窗口同一条规则，直接跳过
    return Promise.resolve(false);
  }

  const rel = item.path || '';
  if (!rel) {
    return Promise.resolve(false);
  }

  const name = rel.split('/').pop() || '';

  const record = openEditor({
    desktop: desktop,
    rootId: item.root,
    rel: rel,
    name: name
  });
  if (!record) {
    return Promise.resolve(false);
  }

  applyGeometry(record, geometryOf(item), false);
  restoreWindowFlags(record, item);

  // 文件不存在时编辑器会在读完那一刻自己关掉窗口，此处不再假装成功
  return Promise.resolve(!!(wm.get(record.id)));
}

/** 取窗口的几何信息（顺带过滤掉脏数据） */
function geometryOf(item) {
  const box = {};
  if (isFiniteNumber(item.x)) { box.x = item.x; }
  if (isFiniteNumber(item.y)) { box.y = item.y; }
  if (isFiniteNumber(item.width)) { box.width = item.width; }
  if (isFiniteNumber(item.height)) { box.height = item.height; }
  return box;
}

/**
 * 还原最大化 / 最小化 / 焦点。
 *
 * 顺序有讲究：先最大化，再最小化。winbox 里「最小化时顺带取消最大化」，
 * 反过来就会把 max 状态丢掉。
 */
function restoreWindowFlags(record, item) {
  const win = record && record.win;
  if (!win || !win.g) {
    return;
  }

  try {
    // 已经最大化（applyGeometry 之前传进来的 item.max）就先恢复成最大化：
    // winbox 的 maximize() 会自己记住还原时该回到多大
    if (item.max && !win.max) {
      win.maximize();
    }
  } catch (err) {
    /* 最大化失败不影响窗口存在 */
  }

  try {
    if (item.min && !win.min) {
      win.minimize();
      return; // 最小化的窗口不该抢焦点
    }
    // 最后创建的最前面的窗口会自然拿到焦点，这里显式再聚焦一次，
    // 保证「上次用的是哪个窗口」在任务栏上看得出来
    win.focus();
  } catch (err) {
    /* 聚焦失败忽略 */
  }
}

/* ---------------------------------------------------------------------------
   启动装配
   --------------------------------------------------------------------------- */

/**
 * 启动持久化：拉取状态 →（由 desktop.js 调 restoreState）→ 订阅后续变化。
 *
 * @param {object} desktopInstance Desktop 实例
 * @returns {Promise<object>} loadState() 的结果，调用方转手交给 restoreState
 */
export async function initSessionState(desktopInstance) {
  desktop = desktopInstance;

  // wins.js 里的窗口增减 / 移动 / 缩放 / 最大化都会叫到这里。
  // 注意 onWindowsChanged 是 wins.js 的**模块级导出**，不是 wm 上的方法
  // （写成 wm.onWindowsChanged() 会直接抛 TypeError，桌面启动就没了）。
  // 「一个窗口都没开就关掉页面」的保护不在这里做，而在 saveState() 里 ——
  // 那样规则只有一处，也不会因为回调被覆盖而失效。
  onWindowsChanged(function () {
    scheduleSave();
  });

  let state = {};
  try {
    state = await loadState();
  } catch (err) {
    state = {}; // loadState 自己已经兜住了，这里只是双保险
  }

  // 远端存过东西，就说明「已有基线」，这次启动的空布局不允许覆盖它
  hasBaseline = !isEmptyState(state);

  // 页面即将卸载：防抖很可能还没到点，这里补一次
  bindUnloadHooks();

  return state;
}

let unloadBound = false;

function bindUnloadHooks() {
  if (unloadBound) {
    return;
  }
  unloadBound = true;

  function onHide() {
    // 不能用 navigator.sendBeacon：它设不了 X-CSRF-Token 头，会被中间件拒掉。
    // 用 keepalive:true 的 fetch（api.request 已经统一带上头和凭据）。
    flushSave(true);
  }

  // pagehide 比 beforeunload 可靠（移动端/往返缓存场景下 beforeunload 不一定触发）
  window.addEventListener('pagehide', onHide);
  window.addEventListener('beforeunload', onHide);
}

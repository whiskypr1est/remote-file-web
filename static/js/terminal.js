/* ==========================================================================
   命令提示符窗口（CMD / PowerShell）—— 基于 xterm.js
   --------------------------------------------------------------------------
   后端已经用 ConPTY（pywinpty）给 shell 分配了**真伪终端**，因此前端必须换成
   真正的终端模拟器：

   ★ 为什么不能再自己做行编辑：
     真 TTY 的输出里全是 VT/ANSI 转义序列，例如
        \x1b]0;标题\x1b\\    （设置窗口标题）
        \x1b[?25l / \x1b[?25h（隐藏/显示光标）
        \x1b[14;5H           （把光标移到第 14 行第 5 列）
        \x1b[?2004h          （括号粘贴模式）
     这背后是「终端只是显示器，光标定位与整屏刷新全由转义序列驱动」的模型。
     早先那种「把文本 append 进一个 div」的做法不仅会把这些序列显示成乱码，
     也无法实现 python 这类逐字符回显 + 光标回退的界面。

   ★ 为什么不能再本地回显：
     真终端下**回显由 shell/ConPTY 完成**（实测 ConPTY 会把敲进去的字符
     用光标定位序列回吐给我们）。前端若再回显一遍，屏幕上就是双份字符。
     所以现在前端只做两件事：把按键原样转发、把输出原样喂给 xterm；
     行编辑、历史、Tab 补全、Ctrl+C 全部交还给 shell 原生处理。

   ★ 附带好处：中文输入法。
     xterm 内部用一个隐藏 textarea 处理 IME composition，
     所以「用输入法打中文」不需要我们再手写一套组合逻辑。

   ★★ 会话可分离（本文件的核心行为，改之前请先读完这一段）
   --------------------------------------------------------------------------
     后端（fileweb/routers/terminal.py 的模块注释是权威协议）已经做到
     「WebSocket 断开不结束会话」：cmd.exe 继续跑，输出继续进会话缓冲，
     用同一个 sid 重连时先补发积压内容再继续交互。前端要做的是**别把这条路堵上**：

       1. 关标签页 / 刷新 / 网络抖动 → **绝不发 {"type":"close"}**。
          只让 WebSocket 自然断开即可；服务端把「连接没了」理解为「人离开了」，
          而不是「不要这个会话了」。少一个 close，用户回来就能看到
          「离开这段时间跑出来的东西」——这正是整个特性的价值。
          所以这里在 pagehide/beforeunload 上注册的钩子只做两件事：
          掐掉断线提示、把 socket 关掉，一个字节都不往服务端发。
          （会话真正结束时服务端自己会 reap，见下。）

       2. 关闭窗口（标题栏 ✕）= **detach**，不是结束会话。
          窗口消失，会话留着；布局里继续记着它的 sid，重新打开页面就接回来。
          之所以敢这么「放手」：服务端有名额自愈（terminal.evict_grace_seconds
          允许新会话顶掉没人连着且空闲的旧会话），会话不会越攒越多把名额占死。
          为此 断开 与 结束会话 是两个**不同**的按钮（见下面工具栏那段）。

       3. 会话没了怎么办（放在这里一起想清楚，否则很容易做出一个「死窗口」）：
          * closed / 关闭码 4404（会话已回收）：显示「会话已结束」+ 禁用输入 +
            一个「开始新会话」按钮（就地换一个 sid，不要求用户关掉重开窗口）。
          * replaced / 关闭码 4401（被另一个连接接管，通常是同一浏览器又开了一次）：
            显示「已被新的连接接管」+ 禁用输入，并提示刷新页面 / 关掉重复的标签页。
            刻意**不**自动重连——那会和接管者反复互抢（后端是「新连接顶掉旧连接」）。
          * 建连后没收到 attached 就被关掉（握手 403：会话不存在或已过期）：
            同样按「会话已结束」处理，并且把保存的 sid 置空，避免每次开页面都
            拿一个死 sid 去撞一次。

     ★ 通信协议（与后端约定，不要随意改名）：
       客户端 -> 服务端
         {"type":"input","data":"<utf8 文本>"}      按键/粘贴/IME 提交的原样数据
         {"type":"resize","cols":N,"rows":M}        窗口尺寸变化
         {"type":"interrupt","force":bool}          中断（true = 强杀卡住的子进程）
         {"type":"attach","sid":"<sid>"}            建连后声明要接哪个会话
         {"type":"close"}                           **结束会话**（杀进程树），谨慎使用
       服务端 -> 客户端
         {"type":"output","data":"<原始 VT 流>"}    直接 term.write()
         {"type":"exit","code":N}
         {"type":"error","message":"..."}
         {"type":"interrupted","mode":"ctrl-c"|"kill-children","killed":[...]}
         {"type":"attached","id","backend","cols","rows","replay","buffered_kb","expires_in"}
         {"type":"closed","reason":"reaped|ended|closed|nomatch|forbidden","message":"..."}
         {"type":"replaced","message":"..."}
         {"type":"truncated","dropped_bytes":N}

       连接方式：这里统一用「?sid= 建连 + 首条消息再发一次 attach」。
       多发的这一条 attach 是刻意的：后端对「建连就带 sid」的写法会在握手阶段
       直接 403 拒绝（会话不存在时），前端只能看到一个没有错误信息的
       close(1006)，既分不清「会话没了」和「网断了」，也没法给出解释；
       而走首条 attach 的路径时服务端会先 accept，再用一条明确的
       closed(4404) 告诉我们原因。指向当前会话的重复 attach 是幂等的。

     ★ 会话响应里的 backend 字段：
         "conpty" = 真 TTY（交互式程序、方向键、Tab、Ctrl+C 都可用）
         "pipe"   = 回退的管道模式（这些交互能力不可用，需要提示用户）
       协议升级期间后端可能暂时不返回该字段，此时按「未知」处理：
       既不谎称真终端，也不误报管道模式。
   ========================================================================== */

import { icon } from './icons.js';
import * as api from './api.js';
import * as ui from './ui.js';
import { wm, registerWindowOwner } from './wins.js';

/** 拿不到服务端配置时的输出上限（KB） */
const DEFAULT_OUTPUT_KB = 512;

/** xterm scrollback（回滚行数）的上下限，防止配错值把浏览器撑爆 */
const MIN_SCROLLBACK = 200;
const MAX_SCROLLBACK = 20000;

/**
 * 把「输出上限 KB」折算成「回滚行数」时假定的每行字符数。
 *
 * max_output_kb 是后端配置项（原意是浏览器侧最多保留多少 KB 输出），
 * 而 xterm 的内存上限按**行**计算，两者没有精确换算关系。
 * 这里按每行约 100 字符估算（常见 80~120 列终端的中值），
 * 只求「512KB 对应几千行」这种量级正确，不追求精确。
 */
const CHARS_PER_LINE_ESTIMATE = 100;

/** 拿不到服务端尺寸时的兜底尺寸 */
const FALLBACK_COLS = 80;
const FALLBACK_ROWS = 24;

/** 调试用：每个终端最多记录多少条已发送消息 */
const SENT_LOG_LIMIT = 100;

/**
 * 服务端主动关闭连接时使用的关闭码（见后端协议注释）。
 *
 * ★ 注意 4001/4401 的差异：任务描述里写的是 4401，而当前服务端常量是
 *   terminal.py 里的 _WS_CLOSE_REPLACED = 4001（已核对源码）。以文案/消息
 *   为主、关闭码为辅，两个码都接受，免得哪边改了就对不上。
 */
const WS_CLOSE_GONE = 4404;               // 会话已结束 / 不存在
const WS_CLOSE_REPLACED_CODES = [4001, 4401];  // 会话已被新的连接接管

/**
 * 打开一个命令提示符窗口（**新建会话**）。
 *
 * 需要「接回一个已有会话」请用 restoreTerminal()：那条路径不该顺手再创建
 * 一个 shell，否则每打开一次页面就会多出一个没人用的 cmd.exe。
 *
 * @param {object} desktop 桌面实例（desktop.js 的 Desktop）
 * @returns {Promise<object|null>} 窗口记录，失败时返回 null
 */
export async function openTerminal(desktop) {
  // 服务端关掉了这个功能时入口本来就不显示；这里再兜一层，
  // 防止通过控制台手动调用绕过限制后拿到一个没用的窗口。
  const features = (desktop && desktop.info && desktop.info.features) || {};
  if (features.terminal !== true) {
    ui.showAlert('功能已关闭', '命令提示符已在服务端配置中关闭。', 'warning');
    return null;
  }

  let session;
  try {
    session = await api.request('POST', '/api/terminal/session', { json: {} });
  } catch (err) {
    if (err && err.status === 401) {
      return null; // api 层已经跳转登录页
    }
    ui.showAlert(
      '无法打开命令提示符',
      (err && err.message) || '创建命令行会话失败，请稍后重试。',
      'error'
    );
    return null;
  }

  if (!session || !session.ok || !session.id) {
    ui.showAlert('无法打开命令提示符', '服务端没有返回有效的会话标识。', 'error');
    return null;
  }

  return mountTerminal(desktop, session, null);
}

/**
 * 还原上次保存的终端窗口（sessionstate.js 的还原路径）。
 *
 * 这里**不创建新会话**：带上存档里的 sid 去接，接得上就是「回到原来那个
 * 命令行」；接不上（空闲超时被回收了）也不能失败 —— 窗口照样出现，只是
 * 里面显示「会话已结束」并给一个「开始新会话」按钮。这样用户至少看得见
 * 自己的布局被还原了，而不是整个窗口凭空消失。
 *
 * @param {object} desktop 桌面实例
 * @param {object} saved   collectState() 落盘的那条记录
 * @returns {Promise<{record: object}|null>} 包装后的窗口记录；null 表示窗口没建起来。
 *   注意「会话已经没了」**不算失败**：窗口照样出现，里面写着「会话已结束」。
 *   几何信息由调用方拿到 record 之后自行 applyGeometry。
 */
export function restoreTerminal(desktop, saved) {
  const item = saved || {};

  // 功能被关掉时（改了配置）不该还能变出一个终端窗口 —— 与 openTerminal 同一口径
  const features = (desktop && desktop.info && desktop.info.features) || {};
  if (features.terminal !== true) {
    return Promise.resolve(null);
  }

  const sid = typeof item.sid === 'string' ? item.sid : '';

  // 上次关页面时这个会话就已经结束了：不必再拿死 sid 去撞一次服务端，
  // 直接在窗口里给出「会话已结束」和「开始新会话」。
  if (!sid || item.ended === true) {
    return mountTerminal(desktop, null, item).then(wrapRestored);
  }

  // 上次是被另一个连接接管走的：会话可能还在，但**不能自动去接** ——
  // 自动接就等于在两个标签页之间来回互抢。窗口照常出现，
  // 里面写着「已被接管」并给一个「重新连接」，由用户自己决定。
  if (item.takenOver === true) {
    return mountTerminal(desktop, null, { ...item, takenOver: true }).then(wrapRestored);
  }

  const session = {
    id: sid,
    shell: item.shell || 'cmd.exe',
    cwd: item.cwd || '',
    // 尺寸与 backend 都是「已知不了」的：真值由 attached 消息带回来
    backend: null,
    expires_in: 0
  };

  return mountTerminal(desktop, session, item).then(wrapRestored);
}

/**
 * 统一还原入口的返回值。
 *
 * restoreTerminal 是异步的（要等一次 WebSocket 接上，或者等它明确失败），
 * 所以调用方（sessionstate.restoreState）拿到的是一个对象而不是裸的窗口记录：
 *   record      窗口记录，null 表示窗口根本没建起来
 *
 * 注意「会话已经没了」**不算还原失败**：窗口仍然应该出现（里面写着
 * 「会话已结束」并给一个「开始新会话」），把用户摆好的布局保住。
 * 会话当前是否活着可以在需要时问 debugEntry.session()，不必在这里判定。
 */
function wrapRestored(record) {
  return record ? { record: record } : null;
}

/* ---------------------------------------------------------------------------
   窗口装配
   --------------------------------------------------------------------------- */

/**
 * 建好一个终端窗口并在里面接上会话。
 *
 * `session` 有 id 时按「接这个会话」处理（新建与还原共用这一条路径），
 * 为 null 时挂一个「没有会话」的窗口（等着用户点开始新会话）。
 *
 * @param {object}  desktop  桌面实例
 * @param {object}  session  新建会话的响应；只接了 id/shell/cwd/cols/rows/backend
 * @param {object}  saved    还原时的存档条目（取几何与标题；新建时为 null）
 * @returns {Promise<object|null>} 窗口记录
 */
async function mountTerminal(desktop, session, saved) {
  // ---- 依赖：都是 index.html 里用普通 <script> 引入的全局 ----
  const TerminalCtor = window.Terminal;
  if (typeof TerminalCtor !== 'function') {
    ui.showAlert(
      '终端组件未加载',
      '没有找到 xterm.js，请确认 /static/vendor/xterm/xterm.js 存在且已在 index.html 中引入。',
      'error'
    );
    return null;
  }

  // addon-fit 的 UMD 包把整个导出对象挂在全局 FitAddon 上，
  // 所以类名是 FitAddon.FitAddon；这里同时兼容「全局直接就是类」的打包方式。
  const fitNamespace = window.FitAddon;
  let FitAddonCtor = null;
  if (typeof fitNamespace === 'function') {
    FitAddonCtor = fitNamespace;
  } else if (fitNamespace && typeof fitNamespace.FitAddon === 'function') {
    FitAddonCtor = fitNamespace.FitAddon;
  }

  const savedItem = saved || null;
  const initialSession = session || null;

  const shellName = shortShellName(
    (initialSession && initialSession.shell) || (savedItem && savedItem.shell)
  );

  // ---- DOM 骨架 ----
  const container = document.createElement('div');
  container.className = 'terminal';

  const toolbar = document.createElement('div');
  toolbar.className = 'term-toolbar';

  const newBtn = makeButton('new', 'folder-plus', '新建', '再开一个命令行窗口（新会话）');
  const clearBtn = makeButton('clear', 'trash', '清屏', '清空当前显示与回滚内容（不会结束 shell）');
  const interruptBtn = makeButton('interrupt', 'warning', '中断',
    '强制结束卡住的子进程、保留 shell（温和的中断请用键盘 Ctrl+C）');

  /* ★ 断开 / 结束会话 是两个不同的动作，必须让人一眼分得清：
       断开     = 走开。会话在服务端继续跑，输出继续攒着，随时能接回来。
       结束会话 = 真的把它关掉（发 {"type":"close"}，服务端杀掉整棵进程树）。
     把「结束」做成需要多点一下，是刻意的：不可逆的操作不该和随手操作挨得太近。 */
  const detachBtn = makeButton('detach', 'power', '断开', '离开这个会话：命令行继续在服务端运行，之后还能接回来');
  const endBtn = makeButton('end', 'close', '结束会话', '真正结束会话：杀掉该会话的进程树，之后无法再接回来');
  endBtn.classList.add('danger');

  const statusEl = document.createElement('span');
  statusEl.className = 'term-status';

  // 「重新连接」「开始新会话」这类就地操作按钮：挂在工具栏最右侧。
  // 复用 .term-btn 的样式，所以不需要动 terminal.css。
  const actionBtn = document.createElement('button');
  actionBtn.type = 'button';
  actionBtn.className = 'term-btn term-action';
  actionBtn.hidden = true;

  // 二次确认条：只有点过「结束会话」才出现，避免误触不可逆操作
  const confirmEl = document.createElement('span');
  confirmEl.className = 'term-confirm';
  confirmEl.hidden = true;
  const confirmYes = document.createElement('button');
  confirmYes.type = 'button';
  confirmYes.className = 'term-btn danger';
  confirmYes.textContent = '确认结束';
  const confirmNo = document.createElement('button');
  confirmNo.type = 'button';
  confirmNo.className = 'term-btn';
  confirmNo.textContent = '取消';
  confirmEl.appendChild(document.createTextNode('结束会话会杀掉正在运行的命令：'));
  confirmEl.appendChild(confirmYes);
  confirmEl.appendChild(confirmNo);

  toolbar.appendChild(newBtn);
  toolbar.appendChild(clearBtn);
  toolbar.appendChild(interruptBtn);
  toolbar.appendChild(detachBtn);
  toolbar.appendChild(endBtn);
  toolbar.appendChild(confirmEl);
  toolbar.appendChild(actionBtn);
  toolbar.appendChild(statusEl);

  // 终端宿主：xterm 会把它的 DOM 挂进来。
  // addon-fit 是量「xterm 元素**父节点**」的尺寸来算行列的，
  // 所以这个容器必须自己有确定的宽高（见 terminal.css 的 flex 规则）。
  const host = document.createElement('div');
  host.className = 'term-host';

  container.appendChild(toolbar);
  container.appendChild(host);

  // ---- 会话状态 ----
  const scrollback = resolveScrollback(desktop);
  const idleSeconds = Number(initialSession && initialSession.expires_in) || 0;

  const state = {
    // 当前会话 id。新建会话与服务端确认前为 null —— 没有 sid 就不该发 attach，
    // 也不该往布局里存一个假的会话标识。
    sid: (initialSession && initialSession.id) || null,
    // 已经和服务端确认接上的会话 id：断开后点「重新连接」要接回它
    attachedSid: null,
    /**
     * 这份存档里记着的 sid。
     * 会话已经结束 / 被接管时 state.sid 是空的（那是「当前连接」的标识），
     * 但存档里的 sid 仍然有用：「重新连接」要接回的就是它。
     */
    savedSid: (savedItem && typeof savedItem.sid === 'string' && savedItem.sid)
      ? savedItem.sid
      : null,
    shell: (initialSession && initialSession.shell) || (savedItem && savedItem.shell) || 'cmd.exe',
    // cwd 是**创建会话时**的起始目录，用户在里面 cd 走之后前端并不知道，
    // 所以它只用来做标题/展示，不要当成「当前目录」用。
    cwd: (initialSession && initialSession.cwd) || (savedItem && savedItem.cwd) || '',
    backend: normalizeBackend(initialSession && initialSession.backend),
    scrollback: scrollback,
    idleSeconds: idleSeconds,
    // 窗口里还能不能打字。注意：窗口被叉掉 / 会话结束 / 会话没了都是 false，
    // 但它**不**代表「服务端那个 cmd.exe 死了」—— 断开之后进程照样活得好好的。
    alive: !!initialSession,
    connected: false,
    everConnected: false,
    /** 本次连接是否已经收到过 attached（用来区分握手失败与接上后掉线） */
    everAttached: false,
    /** 会话已经结束：不可能再连，只能开新的 */
    ended: false,
    /**
     * 会话被另一个连接接管了（后端是「新连接顶掉旧连接」）。
     * 与 ended 是两回事：会话还活着，只是不归这个页面管了 —— 所以保存布局时
     * **必须继续留着 sid**，否则用户下次打开页面时的布局里就没有它了，
     * 而那个 cmd.exe 其实还好好跑着。
     */
    takenOver: !!(savedItem && savedItem.takenOver === true),
    /** 用户主动断开（会话仍然活着，可以接回来） */
    detached: false,
    /** 页面正在卸载：此后的任何提示都不要写，写了也没人看 */
    pageHiding: false,
    /** 服务端关掉这条连接时的关闭码/原因：dispose 路径要据此决定提示文案 */
    lastCloseReason: null,
    pendingNewSession: false,
    socket: null,
    closed: false,
    lastCols: 0,
    lastRows: 0
  };

  // 先声明控制器：registerWindowOwner 要在建窗口之后立刻登记它，
  // 而控制器方法里又要用上面那些状态。
  const controller = createTerminalController();

  const geometry = savedItem
    ? {
        x: Number(savedItem.x),
        y: Number(savedItem.y),
        width: Number(savedItem.width) || 820,
        height: Number(savedItem.height) || 500
      }
    : null;

  const record = wm.create({
    title: savedItem && savedItem.title
      ? savedItem.title
      : shellName + (state.cwd ? ' — ' + state.cwd : ''),
    iconName: 'code',   // icons.js 没有 terminal 图标，沿用最接近的 code
    content: container,
    width: (geometry && geometry.width) || 820,
    height: (geometry && geometry.height) || 500,
    // 还原时把位置一并交给 wins.js（它内部会 clampGeometry），
    // 避免先出现在屏幕中央再被 applyGeometry 挪过去。
    x: geometry && Number.isFinite(geometry.x) ? geometry.x : undefined,
    y: geometry && Number.isFinite(geometry.y) ? geometry.y : undefined,
    silent: !!savedItem,
    minWidth: 480,
    minHeight: 260,
    windowClass: 'terminal-win',
    taskLabel: shellName,
    onClosed: function () {
      teardown();
    }
  });

  // ★ 把自己登记进 wins.js 的窗口内容登记表。
  // 布局保存（sessionstate.collectState）按内容元素的 CSS 类认窗口种类，
  // 再从这里取出「窗口里在跑哪个会话」—— 正因为走的是登记表而不是窗口记录上的
  // 自定义字段，**用户手动打开的终端窗口同样能被保存**（这正是上一版漏掉的场景）。
  registerWindowOwner(record, controller);

  // ---- 创建终端 ----
  const term = new TerminalCtor({
    // 服务端给的初始尺寸（协议新增字段，老后端可能没有，用兜底值）
    cols: toPositiveInt(initialSession && initialSession.cols, FALLBACK_COLS),
    rows: toPositiveInt(initialSession && initialSession.rows, FALLBACK_ROWS),
    cursorBlink: true,
    // 真 VT 流绝不能做换行转换：\n 与 \r\n 的语义由转义序列与 shell 决定，
    // 开了 convertEol 会让 python / cmd 的整屏刷新错行。
    convertEol: false,
    scrollback: scrollback,
    fontFamily: '"Consolas", "Lucida Console", "Courier New", monospace',
    fontSize: 14,
    lineHeight: 1.15,
    // 配色照搬 Windows 控制台的经典 16 色，观感与真实 CMD 一致
    theme: {
      background: '#0c0c0c',
      foreground: '#cccccc',
      cursor: '#cccccc',
      cursorAccent: '#0c0c0c',
      selectionBackground: '#264f78',
      black: '#0c0c0c',
      red: '#c50f1f',
      green: '#13a10e',
      yellow: '#c19c00',
      blue: '#0037da',
      magenta: '#881798',
      cyan: '#3a96dd',
      white: '#cccccc',
      brightBlack: '#767676',
      brightRed: '#e74856',
      brightGreen: '#16c60c',
      brightYellow: '#f9f1a5',
      brightBlue: '#3b78ff',
      brightMagenta: '#b4009e',
      brightCyan: '#61d6d6',
      brightWhite: '#f2f2f2'
    }
  });

  let fitAddon = null;
  if (FitAddonCtor) {
    try {
      fitAddon = new FitAddonCtor();
      term.loadAddon(fitAddon);
    } catch (err) {
      fitAddon = null;
    }
  }

  term.open(host);
  // open 之后宿主才有真实渲染尺寸，这时候先适配一次
  applyFit();

  /* -------------------------------------------------------------------------
     尺寸同步
     ------------------------------------------------------------------------- */

  let resizeFrame = 0;

  /** 合并同一帧内的多次尺寸变化，避免拖拽抖动时反复 fit + 发消息 */
  function scheduleFit() {
    if (state.closed || resizeFrame) {
      return;
    }
    resizeFrame = requestAnimationFrame(function () {
      resizeFrame = 0;
      applyFit();
    });
  }

  /**
   * 按当前宿主尺寸适配终端，并把新的行列告诉后端。
   *
   * 尺寸过小（窗口被最小化、或布局还没完成）时直接跳过：
   * 那是无意义的 2x1，既会把远端 pty 缩成垃圾尺寸，
   * 也会在还原窗口时诱发一串来回抖动的 resize。
   */
  function applyFit() {
    if (state.closed) {
      return;
    }
    const rect = host.getBoundingClientRect();
    if (rect.width < 60 || rect.height < 40) {
      return;
    }
    if (fitAddon) {
      try {
        fitAddon.fit();
      } catch (err) {
        return;
      }
    }
    syncSize(false);
  }

  /**
   * 行列有变化时通知后端（force=true 用于每次建连之后，确保服务端拿到真实尺寸）。
   *
   * 重建连接之后必须 force 一次：这个会话在别的页面里可能是按另一个窗口尺寸
   * 跑的，我们这边的行列它并不知道。
   */
  function syncSize(force) {
    const cols = term.cols;
    const rows = term.rows;
    if (!cols || !rows) {
      return;
    }
    if (!force && cols === state.lastCols && rows === state.lastRows) {
      return;
    }
    state.lastCols = cols;
    state.lastRows = rows;
    send({ type: 'resize', cols: cols, rows: rows });
  }

  // ResizeObserver 同时覆盖「拖拽边框」「最大化/还原」「显示桌面后还原」
  // 这几条路径——它们本质上都只是改变了宿主元素的实际尺寸。
  let resizeObserver = null;
  if (typeof ResizeObserver === 'function') {
    resizeObserver = new ResizeObserver(function () {
      scheduleFit();
    });
    try {
      resizeObserver.observe(host);
    } catch (err) {
      resizeObserver = null;
    }
  }
  // 兜底：某些浏览器在整窗缩放时对 ResizeObserver 的派发有延迟
  window.addEventListener('resize', scheduleFit);

  /* -------------------------------------------------------------------------
     状态栏
     ------------------------------------------------------------------------- */

  function renderStatus() {
    const parts = [];

    if (state.ended) {
      parts.push('会话已结束');
    } else if (state.detached) {
      // 断开之后「服务端那个 cmd.exe 还在跑」是用户最需要知道的一件事，
      // 否则他完全没法判断自己会不会把跑了一半的东西弄丢
      parts.push('已断开（会话仍在服务端运行）');
    } else if (!state.connected) {
      parts.push(state.everConnected ? '连接中断' : '正在连接…');
    } else if (!state.everAttached) {
      parts.push('正在接入会话…');
    } else {
      parts.push('已连接');
    }

    if (state.connected && state.everAttached && state.backend === 'conpty') {
      parts.push('真终端');
    }
    if (state.connected && state.everAttached && state.backend === 'pipe') {
      // 管道模式下方向键/Tab/Ctrl+C 都不生效，必须在界面上说清楚，
      // 但只占状态栏一行，不弹窗打扰。
      parts.push('管道模式：交互式程序不可用');
    }

    if (!state.ended && state.idleSeconds > 0) {
      parts.push('空闲 ' + Math.round(state.idleSeconds / 60) + ' 分钟后自动回收');
    }
    parts.push('回滚 ' + state.scrollback + ' 行');

    let cls = 'term-status';
    if (state.ended) {
      cls += ' warn';
    } else if (state.connected && state.backend === 'pipe') {
      cls += ' warn';
    } else if (state.connected && state.everAttached) {
      cls += ' ok';
    }

    statusEl.textContent = parts.join(' · ');
    if (statusEl.className !== cls) {
      statusEl.className = cls;
    }

    // 管道模式的完整说明放在 tooltip 里，悬停才展开
    statusEl.title = state.backend === 'pipe'
      ? '服务端未能分配伪终端（未安装 pywinpty 或创建失败）。\n' +
        '此时 python、vim 等交互式程序，以及方向键、Tab 补全、Ctrl+C 都不可用，\n' +
        '但 dir / cd / echo 等普通命令仍可正常使用。'
      : '';

    renderActions();
  }

  /**
   * 工具栏上的「就地操作」按钮 + 各按钮的可用状态。
   *
   * 一个按钮承担多种文案（重新连接 / 开始新会话 / 立即重试）而不是摆三个，
   * 是因为同一时刻真正有意义的动作只有一个：
   *   * 会话已结束 -> 只能开新的
   *   * 用户主动断开 / 掉线 -> 只能接回来
   * 另外「结束会话」的二次确认条也在这里收起（会话已结束时它没有意义了）。
   */
  function renderActions() {
    endBtn.disabled = state.ended;
    detachBtn.disabled = state.ended || !state.connected;
    interruptBtn.disabled = state.ended || !state.connected;

    if (state.ended) {
      openAction('start', 'refresh', state.pendingNewSession ? '正在创建…' : '开始新会话');
    } else if (state.detached || state.takenOver) {
      // 接管之后只能由用户自己决定要不要抢回来（自动抢会两个标签页互踢）
      openAction('reconnect', 'refresh', '重新连接');
    } else if (!state.connected && state.everAttached) {
      // 掉线（服务重启、网络抖动）：会话多半还在，先给「立即重试」
      openAction('reconnect', 'refresh', '重新连接');
    } else {
      closeAction();
    }
  }

  function confirmShown() {
    return confirmEl.hidden === false;
  }

  function openAction(action, iconName, label) {
    actionBtn.hidden = false;
    actionBtn.dataset.act = action;
    actionBtn.disabled = state.pendingNewSession;
    setButtonLabel(actionBtn, iconName, label);
  }

  function closeAction() {
    actionBtn.hidden = true;
  }

  function showConfirm(show) {
    confirmEl.hidden = !show;
  }

  /* -------------------------------------------------------------------------
     输出与提示
     ------------------------------------------------------------------------- */

  /**
   * 写一条「我们自己」的提示信息。
   *
   * 必须与 shell 的原始输出区别对待：shell 的 VT 流要原样交给 xterm 解释，
   * 而错误消息里可能夹带服务端回显的、由客户端提供的字符串
   * （例如「未知的消息类型：xxx」）。万一里面混入 ESC，
   * 会被 xterm 当成终端指令执行（清屏、改标题……），所以先剥掉控制字符。
   * 注意 xterm 只按文本渲染，不存在 HTML 注入问题。
   *
   * 页面正在卸载时直接丢弃：那时候写进去的内容用户根本看不到，
   * 而且 term 可能已经被 dispose 了。
   */
  function writeNotice(text) {
    if (state.closed || state.pageHiding) {
      return;
    }
    term.write(sanitizeNotice(text));
  }

  /** 进程结束 / 会话没了之后禁止再输入 */
  function disableInput() {
    state.alive = false;
    try {
      term.options.disableStdin = true;
    } catch (err) {
      /* 个别版本不支持该选项时，onData 里的 alive 判断仍然拦得住 */
    }
  }

  /** 重新允许输入（接上一个新会话时用；disableStdin 是可以撤销的） */
  function enableInput() {
    state.alive = true;
    try {
      term.options.disableStdin = false;
    } catch (err) {
      /* 忽略 */
    }
  }

  /* -------------------------------------------------------------------------
     发送
     ------------------------------------------------------------------------- */

  // 提前声明：send() 需要往里记录已发送的消息，而这个对象在下面才组装。
  // 现在先占位，避免「send() 早于它初始化被调用」导致 TDZ 报错。
  let debugEntry = null;

  function send(message) {
    const socket = state.socket;
    if (!socket || socket.readyState !== WebSocket.OPEN) {
      return false;
    }
    try {
      socket.send(JSON.stringify(message));
    } catch (err) {
      return false;
    }
    // 记录发送情况。注意 sent 是有界的：真终端下每一次按键都是一条 input 消息，
    // 很快就能把日志冲掉，所以像「resize 到底发过几次」这种要长期可靠的问题
    // 必须用独立的计数器，不能靠翻日志。
    if (debugEntry) {
      if (message.type === 'resize') {
        debugEntry.resizeCount += 1;
        debugEntry.lastResizeValue = { cols: message.cols, rows: message.rows };
      } else if (message.type === 'input') {
        debugEntry.lastInput = message.data;
      } else {
        debugEntry.controlCount += 1;
      }
      debugEntry.sent.push(message);
      if (debugEntry.sent.length > SENT_LOG_LIMIT) {
        debugEntry.sent.shift();
      }
    }
    return true;
  }

  /* -------------------------------------------------------------------------
     WebSocket
     ------------------------------------------------------------------------- */

  /**
   * 建连并接上 state.sid 指向的会话。
   *
   * 每次调用都是「从零开始的一条新连接」：旧 socket 的 handler 先摘掉，
   * 所有 per-connection 的标志复位。重连、掉线重试、接管后重连都走这里。
   */
  function connect() {
    if (state.closed || state.ended || !state.sid) {
      renderStatus();
      return;
    }

    // 把上一条连接彻底作废：留着它的 handler 会让「旧连接的 onclose」
    // 覆盖掉新连接刚建立起来的状态
    const previous = state.socket;
    if (previous) {
      detachSocketHandlers(previous);
      try {
        previous.close(1000, 'reconnect');
      } catch (err) {
        /* 忽略 */
      }
    }

    state.everAttached = false;
    state.lastCloseReason = null;
    // pendingNewSession 刻意不在这里复位：它要一直压着「开始新会话」按钮，
    // 直到 attached（或失败）为止，否则按钮会在握手期间闪回可点状态。

    // https 页面必须用 wss，否则浏览器按混合内容拦掉。
    // Cookie 由浏览器在握手时自动带上（HttpOnly，脚本读不到也不需要读）。
    const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const url = scheme + location.host + '/api/terminal/ws?sid=' + encodeURIComponent(state.sid);

    let socket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      writeNotice('\r\n[无法建立连接：' + ((err && err.message) || err) + ']\r\n');
      state.connected = false;
      renderStatus();
      return;
    }

    state.socket = socket;
    state.everConnected = false;
    renderStatus();

    socket.onopen = function () {
      if (state.closed || state.socket !== socket) {
        return;
      }
      state.everConnected = true;
      // 首条消息再声明一次要接哪个会话。指向当前会话的重复 attach 是幂等的，
      // 换来的是「会话不存在」时能收到明确的 closed(4404) 而不是一个
      // 光秃秃的握手失败（原因见文件头）。
      const attached = send({ type: 'attach', sid: state.sid });
      if (!attached) {
        writeNotice('\r\n[无法发送 attach：连接已关闭]\r\n');
      }
      renderStatus();
      // 建连后立刻把真实尺寸同步给服务端（服务端给的初始值可能不准，
      // 会话也可能是按另一个窗口尺寸跑着的）
      applyFit();
      syncSize(true);
      focusTerm();
    };

    socket.onmessage = function (event) {
      if (state.closed || state.socket !== socket) {
        return;
      }
      let message = null;
      try {
        message = JSON.parse(event.data);
      } catch (err) {
        return; // 协议外的数据一律忽略
      }
      if (!message || typeof message !== 'object') {
        return;
      }
      handleMessage(message);
    };

    socket.onerror = function () {
      // onerror 不提供细节，真正的原因通常在 onclose 里
      renderStatus();
    };

    socket.onclose = function (event) {
      detachSocketHandlers(socket);
      if (state.closed) {
        return;
      }

      state.connected = false;
      const code = event ? event.code : 0;
      const lastCloseReason = state.lastCloseReason;

      // 服务端已经明说过原因（closed / replaced）：那些分支自己已经把界面
      // 收拾好了，这里绝不能再用一句笼统的「会话已结束」盖过去
      if (state.ended || lastCloseReason === 'replaced') {
        renderStatus();
        return;
      }

      if (WS_CLOSE_REPLACED_CODES.indexOf(code) >= 0 || lastCloseReason === 'replaced') {
        // 被接管：既不该自动重连（会和接管者无限互抢），也不该说「已结束」
        markReplaced();
        renderStatus();
        return;
      }

      if (
        code === WS_CLOSE_GONE ||
        lastCloseReason === 'gone' ||
        lastCloseReason === 'nomatch' ||
        lastCloseReason === 'forbidden'
      ) {
        markEnded(lastSessionEndedMessage(lastCloseReason));
        renderStatus();
        return;
      }

      // 接上过又断了：会话**多半还在**（服务重启、网络抖动、休眠唤醒），
      // 所以只说「连接中断」并给「重新连接」，不谎报「会话已结束」
      if (state.everAttached) {
        state.detached = true;
        disableInput();
        if (!state.pageHiding) {
          writeNotice('\r\n[连接中断] 会话可能仍在服务端运行，点工具栏「重新连接」或刷新页面即可接回。\r\n');
        }
        // 会话 id 不变，但「现在是断开状态」值得尽快落盘
        controller.remember(true, true);
        renderStatus();
        return;
      }

      // 一次都没接上就被关掉：服务端没用 4404（通常是握手阶段 403 —— 会话不存在
      // 或已过期），也可能压根没连上。这里按「会话已结束」处理，
      // 同时把保存的 sid 清掉，避免每次开页面都拿一个死 sid 去撞服务端。
      markEnded(
        code === 1006
          ? '无法连接到该会话（可能已空闲超时被服务端回收，或网络不可达）。'
          : '会话不存在或已结束。'
      );
      renderStatus();
    };

    // 连接一建立就把状态刷新一遍（「正在连接…」）
    state.connected = true;
    state.detached = false;
    state.ended = false;
    renderStatus();
  }

  /** 摘掉 handler，避免已经作废的连接再回来改状态 */
  function detachSocketHandlers(socket) {
    if (!socket) {
      return;
    }
    socket.onopen = null;
    socket.onmessage = null;
    socket.onerror = null;
    socket.onclose = null;
  }

  /** 服务端消息分发 */
  function handleMessage(message) {
    if (message.type === 'output') {
      // 原样交给 xterm：这里**必须**包含 VT 转义序列，绝不能做转义或过滤。
      // 重连时这些就是离开期间积压的输出（服务端先补发、再续传）。 */
      term.write(String(message.data == null ? '' : message.data));
      return;
    }

    if (message.type === 'attached') {
      // 服务端确认接上了：这条消息是「连的到底是哪个会话」的唯一权威来源
      state.everAttached = true;
      state.attachedSid = state.sid;

      if (message.id && message.id !== state.sid) {
        // 理论上不该发生（attach 只允许指向当前会话），真发生了就以服务端为准，
        // 否则我们会拿着错误的 sid 去保存布局
        state.sid = String(message.id);
        debugEntry && (debugEntry.sid = state.sid);
      }

      const backend = normalizeBackend(message.backend);
      if (backend) {
        state.backend = backend;
        if (debugEntry) {
          debugEntry.backend = backend;
        }
      }
      const expires = Number(message.expires_in);
      if (isFinite(expires) && expires > 0) {
        state.idleSeconds = expires;
      }
      if (message.shell) {
        state.shell = String(message.shell);
      }

      /* ★ replay=true 表示这是一次重连（会话之前就存在）——这正是本特性的
         目标场景，必须明确告诉用户「你回到的是原来那个会话」，否则他会以为
         屏幕上那些「自己没敲过」的输出是见了鬼。 */
      if (message.replay === true) {
        const buffered = Number(message.buffered_kb);
        let text = '\r\n[已恢复到之前的会话';
        if (isFinite(buffered) && buffered > 0) {
          text += '，补发离开期间积压的输出 ' + formatKb(buffered) + ']';
        } else {
          text += ']';
        }
        writeNotice(text + '\r\n');
      }

      // 接上了才算「新会话开好了」，这时才放开动作按钮
      state.pendingNewSession = false;
      renderStatus();
      return;
    }

    if (message.type === 'truncated') {
      /* 离开期间产生的输出超过了服务端缓冲上限，最旧的部分已被丢掉。
         绝不能假装输出是连续的 —— 用户会照着「看起来完整」的日志做判断。 */
      const dropped = Number(message.dropped_bytes);
      writeNotice(
        '\r\n[提示] 离开期间输出过多，最早的一小段已被丢弃' +
        (isFinite(dropped) && dropped > 0 ? '（约 ' + formatBytes(dropped) + '）' : '') +
        '\r\n'
      );
      return;
    }

    if (message.type === 'replaced') {
      markReplaced(message.message);
      return;
    }

    if (message.type === 'closed') {
      state.lastCloseReason = String(message.reason || 'gone');
      markEnded(String(message.message || lastSessionEndedMessage(state.lastCloseReason)));
      // 服务端随后会发 4404 并关闭连接，这里先把 socket 收掉，
      // 免得输入循环还挂在那条已经没用的连接上
      const socket = state.socket;
      state.socket = null;
      if (socket) {
        detachSocketHandlers(socket);
        try {
          socket.close(1000, 'session gone');
        } catch (err) {
          /* 忽略 */
        }
      }
      renderStatus();
      return;
    }

    if (message.type === 'exit') {
      const code = (message.code === undefined || message.code === null) ? 0 : message.code;
      // exit 之后服务端还会发 closed(ended)，那条会说清楚「没法再接回来了」，
      // 这里只报退出码。两者都写会让用户以为出了两次事。
      writeNotice('\r\n[进程已结束，退出码 ' + code + ']\r\n');
      disableInput();
      state.ended = true;
      renderStatus();
      return;
    }

    if (message.type === 'interrupted') {
      // 给「中断」按钮一个明确反馈，否则用户不知道到底有没有生效
      let text = '\r\n[中断] ';
      text += message.mode === 'kill-children'
        ? '已强制结束卡住的子进程（shell 保留）'
        : '已发送中断（相当于 Ctrl+C）';
      const killed = message.killed;
      if (Array.isArray(killed) && killed.length) {
        text += '，共 ' + killed.length + ' 个进程';
      }
      text += '\r\n';
      writeNotice(text);
      return;
    }

    if (message.type === 'error') {
      writeNotice('\r\n[错误] ' + String(message.message || '未知错误') + '\r\n');
      return;
    }

    // ping 之类的保活消息：刻意的空分支，说明「知道它、且不需要处理」
  }

  /** 会话被新的连接接管：明确说明原因，并把输入关掉（绝不自动抢回来） */
  function markReplaced(message) {
    if (state.closed) {
      return;
    }
    state.lastCloseReason = 'replaced';
    state.connected = false;
    state.takenOver = true;
    state.detached = false;
    // 刻意**不**设 ended：会话还活着（在另一个连接手里），
    // 设了 ended 就会把 sid 从布局里抹掉，等于把用户还开着的会话弄丢
    disableInput();
    writeNotice(
      '\r\n[' + String(message || '该终端会话已被新的连接接管') + ']\r\n' +
      '[本窗口已停止接收输出。请刷新本页，或关掉另一个正在使用该会话的标签页。]\r\n'
    );
    renderStatus();
    controller.remember(true, true);
  }

  /**
   * 会话彻底结束了（被回收 / 进程结束 / 服务端要求关闭）。
   *
   * 这里做三件事：禁用输入、把 sid 从要保存的布局里摘掉、给一个「开始新会话」。
   * 摘掉 sid 很重要：不摘的话每次打开页面都会拿这个死 sid 去连一次，
   * 白等一次握手失败。
   */
  function markEnded(message) {
    if (state.closed || state.ended) {
      return;
    }
    state.lastCloseReason = state.lastCloseReason || 'gone';
    state.ended = true;
    state.detached = false;
    state.connected = false;
    disableInput();

    writeNotice('\r\n[会话已结束] ' + sanitizeNotice(message || '会话已结束（空闲超时被回收）') + '\r\n');
    writeNotice('[这个窗口里的命令行不会再回来了，点工具栏「开始新会话」可以就地再开一个。]\r\n');
    showConfirm(false);
    renderStatus();
    // 立刻同步一次布局，避免用户在刷新前关掉页面，导致死 sid 又被存回去
    controller.remember(true, true);
  }

  /** 把键盘焦点交给终端（xterm 的按键监听挂在它的隐藏 textarea 上） */
  function focusTerm() {
    try {
      term.focus();
    } catch (err) {
      /* 忽略 */
    }
  }

  /* -------------------------------------------------------------------------
     交互
     ------------------------------------------------------------------------- */

  // 按键、右键粘贴、以及输入法提交的文本都由 xterm 统一处理，
  // 这里只负责原样转发：Ctrl+C 会变成 \x03、Tab 变成 \t、
  // 方向键变成 CSI 序列，全部由 shell 自己解释——这才是真终端的行为。
  term.onData(function (data) {
    if (!state.alive || !state.connected) {
      return;
    }
    send({ type: 'input', data: data });
  });

  newBtn.addEventListener('click', function () {
    openTerminal(desktop);
  });

  clearBtn.addEventListener('click', function () {
    // 只清本地显示与回滚（xterm 的 clear 会连带清掉 scrollback）。
    // 刻意不给 shell 发任何东西：真终端下「清屏」本来就是本地行为，
    // 发 cls 反而会在远端多留下一行命令痕迹。
    try {
      term.clear();
    } catch (err) {
      /* 忽略 */
    }
    focusTerm();
  });

  interruptBtn.addEventListener('click', function () {
    if (state.ended || !state.connected) {
      writeNotice('\r\n[中断] 当前没有连着的会话，无法发送中断请求\r\n');
      return;
    }
    // force:true = 强杀卡住的子进程但保留 shell。
    // 想要「温和的中断」用键盘 Ctrl+C（那才是真正的 ^C）。
    if (!send({ type: 'interrupt', force: true })) {
      writeNotice('\r\n[中断] 连接已断开，无法发送中断请求\r\n');
      return;
    }
    // 后端处理需要时间，先给个视觉反馈
    interruptBtn.classList.add('pending');
    setTimeout(function () {
      interruptBtn.classList.remove('pending');
    }, 1200);
    focusTerm();
  });

  /* ---- 断开：只是走开，会话继续在服务端跑 ---- */
  detachBtn.addEventListener('click', function () {
    if (state.ended || state.detached) {
      return;
    }
    detachConnection('user');
    writeNotice(
      '\r\n[已断开] 命令行仍在服务端运行（可以继续接收输出）。\r\n' +
      '[点工具栏「重新连接」或刷新页面即可接回；要真正结束它请点「结束会话」。]\r\n'
    );
    renderStatus();
  });

  /* ---- 结束会话：不可逆，先确认 ---- */
  endBtn.addEventListener('click', function () {
    if (state.ended) {
      return;
    }
    const show = !confirmShown();
    showConfirm(show);
    if (show) {
      // 断开状态下「结束」是唯一能做掉这件事的入口，别让二次确认把它藏起来
      openAction('end', 'close', '结束会话');
    } else {
      renderActions();
    }
  });

  confirmNo.addEventListener('click', function () {
    showConfirm(false);
    renderActions();
  });

  confirmYes.addEventListener('click', function () {
    showConfirm(false);
    endSessionNow();
  });

  actionBtn.addEventListener('click', function () {
    const action = actionBtn.dataset.act;
    if (action === 'reconnect') {
      reconnect();
      return;
    }
    if (action === 'start') {
      startNewSession();
      return;
    }
    if (action === 'end') {
      endSessionNow();
    }
  });

  // 点窗口任意位置都把焦点交给终端：xterm 的键盘事件挂在隐藏 textarea 上，
  // 不聚焦的话用户按键会落到窗口外层，看起来像「打字没反应」。
  container.addEventListener('mousedown', function () {
    setTimeout(function () {
      if (state.alive) {
        focusTerm();
      }
    }, 0);
  });

  // 点标题栏把窗口切到前台时，winbox 只会给根元素加/去 focus 类，
  // 不会触发 DOM 的 focus 事件，所以这里观察 class 变化，
  // 让「切回这个窗口就能直接打字」成立。
  let focusObserver = null;
  const winRoot = record && record.win ? record.win.g : null;
  if (winRoot && typeof MutationObserver === 'function') {
    let hadFocus = winRoot.classList.contains('focus');
    focusObserver = new MutationObserver(function () {
      const hasFocus = winRoot.classList.contains('focus');
      if (hasFocus && !hadFocus && state.alive) {
        focusTerm();
      }
      hadFocus = hasFocus;
    });
    try {
      focusObserver.observe(winRoot, { attributes: true, attributeFilter: ['class'] });
    } catch (err) {
      focusObserver = null;
    }
  }

  /* -------------------------------------------------------------------------
     ★ 页面离开：只放手，不杀会话
     ------------------------------------------------------------------------- */

  /**
   * ★ 这是整个特性的关键，改动前请务必理解：
   *
   * 页面卸载时**一个字节都不往服务端发**（尤其不发 {"type":"close"}）。
   * 只要 WebSocket 断掉，服务端就把这次断开理解为「人离开了」：
   * cmd.exe 继续跑，输出继续进缓冲，下次带同一个 sid 连上来就能接回现场。
   *
   * 反过来说，任何人往这里加一句 send({type:'close'})，都会让「关掉浏览器
   * 再打开」变成「命令行全没了」—— 那正是这个功能要解决的问题，
   * 加回去等于让整个特性失效，所以这里只做本地清理。
   *
   * 也不需要在这里写额外的「保存布局」逻辑：会话 id / 是否已结束这类
   * 关键信息在每次状态变化时（attach 成功、结束、断开）就已经同步进
   * 布局快照了，pagehide 上的 flushSave 拿到的就是最新的一份。
   */
  function onPageHide() {
    if (state.closed) {
      return;
    }
    state.pageHiding = true;
    // 断线提示没意义了（页面都要走了），socket 直接收掉：不发 close
    const socket = state.socket;
    state.socket = null;
    state.connected = false;
    if (socket) {
      detachSocketHandlers(socket);
      try {
        socket.close(1000, 'page hide');
      } catch (err) {
        /* 忽略 */
      }
    }
  }

  // pagehide 比 beforeunload 可靠（bfcache、移动端、进程被杀的场景下
  // beforeunload 不一定触发）。两个都注册，重复调用 onPageHide 是幂等的。
  window.addEventListener('pagehide', onPageHide);
  window.addEventListener('beforeunload', onPageHide);

  /* -------------------------------------------------------------------------
     会话动作
     ------------------------------------------------------------------------- */

  /** 断开连接但保留会话（state.detached = 可以接回来） */
  function detachConnection(reason) {
    if (state.ended) {
      return;
    }
    state.detached = true;
    state.lastCloseReason = reason === 'user' ? 'user' : 'detached';
    state.connected = false;
    disableInput();

    const socket = state.socket;
    state.socket = null;
    if (socket) {
      detachSocketHandlers(socket);   // 主动断开不该再触发「意外断开」提示
      try {
        socket.close(1000, 'detach');
      } catch (err) {
        /* 忽略 */
      }
    }
    // 断开这一刻就把「会话还活着、可以接回来」写进布局快照，
    // 这样即使用户直接关掉浏览器，下次打开也知道要接回哪个会话
    controller.remember(true, true);
  }

  /** 重新连接（接回同一个 sid） */
  function reconnect() {
    if (state.closed) {
      return;
    }
    // 三处可能的来源：当前连接 / 已确认接上的 / 存档里记着的（
    // 「会话已结束或被接管」时前两个都是空的，但存档里那个可能还活着）
    const target = state.sid || state.attachedSid || state.savedSid;
    if (!target) {
      startNewSession();
      return;
    }
    if (state.socket && state.socket.readyState === WebSocket.OPEN) {
      return; // 已经连着了，不要重复建连（会把自己顶掉）
    }
    if (state.socket && state.socket.readyState === WebSocket.CONNECTING) {
      return;
    }

    state.sid = target;
    // 从「已结束 / 被接管」往回走：这是用户明确要求的重新连接，
    // 所以要把那些拦着连接的标志清掉
    state.ended = false;
    state.takenOver = false;
    state.detached = false;
    state.everConnected = false;
    state.everAttached = false;
    enableInput();
    showConfirm(false);
    writeNotice('\r\n[正在重新连接会话…]\r\n');
    connect();
    renderStatus();
    focusTerm();
  }

  /**
   * 就地换一个新会话（会话已结束时用）。
   *
   * 刻意不新开窗口：用户的窗口位置、尺寸是他自己摆的，会话没了不该让他重摆一次。
   * 一个窗口自始至终只跑一个 shell，换了会话就把旧的那本「账」翻过去。
   */
  function startNewSession() {
    if (state.closed || state.pendingNewSession) {
      return;
    }
    state.pendingNewSession = true;
    openAction('start', 'refresh', '正在创建…');

    api.request('POST', '/api/terminal/session', { json: {} })
      .then(function (session) {
        if (state.closed) {
          return;
        }
        if (!session || !session.ok || !session.id) {
          throw new Error('服务端没有返回有效的会话标识');
        }

        state.sid = session.id;
        state.attachedSid = null;
        state.shell = session.shell || 'cmd.exe';
        state.cwd = session.cwd || '';
        state.backend = normalizeBackend(session.backend);
        state.ended = false;
        state.detached = false;
        state.everConnected = false;
        state.everAttached = false;
        state.idleSeconds = Number(session.expires_in) || 0;
        if (debugEntry) {
          debugEntry.sid = state.sid;
          debugEntry.backend = state.backend;
        }

        // 上一本账翻过去了：清屏 + 复位键盘/光标模式。
        // 送一串 RIS（终端全复位）是必要的 —— 上一个 shell 可能中途退出时
        // 留下了「应用光标模式」「备用屏」这类状态，不清掉新 shell 的界面会错位。
        try {
          term.reset();
        } catch (err) {
          try {
            term.clear();
          } catch (err2) {
            /* 忽略 */
          }
        }
        enableInput();
        writeNotice('[已开始新的会话]\r\n');
        // 窗口标题跟着新会话走（旧标题里可能还写着上一个 shell 的起始目录）
        try {
          record.win.setTitle(shortShellName(state.shell) + (state.cwd ? ' — ' + state.cwd : ''));
        } catch (err) {
          /* 忽略 */
        }
        scheduleSaveSoon();
        connect();
      })
      .catch(function (err) {
        if (state.closed) {
          return;
        }
        writeNotice('\r\n[开始新会话失败] ' + ((err && err.message) || '未知错误') + '\r\n');
        renderStatus();
      })
      .then(function () {
        state.pendingNewSession = false;
        if (!state.closed) {
          renderStatus();
        }
      });
  }

  /**
   * 真正结束会话：发 {"type":"close"}（服务端会杀掉整棵进程树），
   * 然后回落成「已结束」状态。
   *
   * ★ 断开状态下 socket 是空的，直接 send() 会静默失败 —— 那样用户点了
   * 「结束会话」，服务端那个 cmd.exe 却还活着，要等到空闲超时才被回收，
   * 而界面上却写着「已结束」。所以这里先把连接接回来再发 close。
   */
  function endSessionNow() {
    if (state.closed || state.ended) {
      return;
    }

    disableInput();
    state.connected = false;
    state.ended = true;
    state.detached = false;
    showConfirm(false);

    // ★ 先把「这个窗口原来接的是哪个会话」记下来：下面要把它从保存用的
    // 状态里清掉（会话已经不存在了，留着只会让下次打开拿一个死 sid 去连），
    // 但如果此刻是「断开」状态，还得靠它先接回来把 close 发出去。
    const target = state.attachedSid || state.sid;
    const socket = state.socket;
    state.socket = null;

    if (socket && socket.readyState === WebSocket.OPEN) {
      // 最常见的路径：连着呢，直接把 close 发出去
      try {
        socket.send(JSON.stringify({ type: 'close' }));
        if (debugEntry) {
          debugEntry.controlCount += 1;
          debugEntry.sent.push({ type: 'close' });
        }
      } catch (err) {
        /* 发不出去就只能走下面的兜底 */
      }
      detachSocketHandlers(socket);
      try {
        socket.close(1000, 'session ended by user');
      } catch (err) {
        /* 忽略 */
      }
      state.sid = null;
      state.attachedSid = null;
      state.lastCloseReason = 'ended';
      finishEndSession(true);
      return;
    }

    if (socket) {
      detachSocketHandlers(socket);
      try {
        socket.close(1000, 'session ended by user');
      } catch (err) {
        /* 忽略 */
      }
    }

    state.sid = null;
    state.attachedSid = null;
    state.lastCloseReason = 'ended';

    if (!target) {
      // 从来没接上过（连 sid 都没确认）：没什么可结束的
      finishEndSession(false);
      return;
    }

    // 断开状态：先接回来再发 close，否则服务端那个 cmd.exe 会一直跑到空闲超时
    writeNotice('\r\n[正在结束会话…]\r\n');
    reconnectForClose(target, function (sent) {
      finishEndSession(sent);
    });
  }

  /**
   * 只为了「把 close 发出去」而临时接一次连接。
   *
   * 刻意不复用 connect()：那条路径会改一堆状态（everAttached、pending、
   * 渲染「已连接」等），而这里的目的只有一个 —— 把 close 送到服务端。
   */
  function reconnectForClose(target, done) {
    const scheme = location.protocol === 'https:' ? 'wss://' : 'ws://';
    const url = scheme + location.host + '/api/terminal/ws?sid=' + encodeURIComponent(target);

    let socket;
    try {
      socket = new WebSocket(url);
    } catch (err) {
      done(false);
      return;
    }

    let settled = false;
    function finish(sent) {
      if (settled) {
        return;
      }
      settled = true;
      done(sent);
    }

    socket.onopen = function () {
      let sent = false;
      try {
        socket.send(JSON.stringify({ type: 'close' }));
        sent = true;
      } catch (err) {
        sent = false;
      }
      try {
        socket.close(1000, 'session ended by user');
      } catch (err) {
        /* 忽略 */
      }
      finish(sent);
    };
    socket.onerror = function () { /* 交给 onclose 兜底 */ };
    socket.onclose = function () { finish(false); };
  }

  /** 结束会话的收尾（发没发出去都要让界面落在「已结束」这个确定状态上） */
  function finishEndSession(sent) {
    writeNotice(
      sent
        ? '\r\n[会话已结束] 已通知服务端结束该会话。\r\n[点工具栏「开始新会话」可以再开一个。]\r\n'
        : '\r\n[会话已结束] 没能联系上服务端，本地按已结束处理。\r\n[点工具栏「开始新会话」可以再开一个。]\r\n'
    );
    controller.remember(true, true);
    renderStatus();
  }

  /** 布局要重算时叫一声，让 sid / ended 这类关键变化尽快落盘 */
  function scheduleSaveSoon() {
    if (state.closed) {
      return;
    }
    // 动态 import：终端不依赖布局模块就能工作（终端只是「顺便被保存」），
    // 也让 terminal.js 不必静态依赖 sessionstate.js 而形成往返依赖。
    // 用 import() 而不是顶层 import 还有一个好处：布局模块没加载成功时，
    // 终端照样能用。
    Promise.resolve()
      .then(function () {
        return import('./sessionstate.js');
      })
      .then(function (mod) {
        if (mod && typeof mod.scheduleSave === 'function') {
          mod.scheduleSave();
        }
      })
      .catch(function () {
        /* 布局模块不可用：终端功能不受影响，静默忽略 */
      });
  }

  /* -------------------------------------------------------------------------
     清理
     ------------------------------------------------------------------------- */

  /**
   * 窗口被关掉（wins.js 的 onClosed 钩子）。
   *
   * ★ 这里**不发 {"type":"close"}**：关闭窗口的语义是「离开」而不是「结束」。
   * 会话继续在服务端跑、输出继续攒着，布局里也继续留着它，
   * 重新打开页面就会连回来 —— 这才是「持久会话」该有的样子。
   * 要真正结束请点工具栏的「结束会话」（endSessionNow）。
   */
  function teardown() {
    if (state.closed) {
      return;
    }
    state.closed = true;
    state.alive = false;

    // 摘监听之前先把「这个窗口关掉时会话还活着」记下来：
    // 控制器还挂在 WeakMap 上，sessionstate 仍能从它取到存档条目。
    // 传 false：这一刻窗口已经不算「在桌面上」了，几何信息要用上一份
    // （winbox 关窗后 x/y 不能再代表用户摆的位置）。
    controller.remember(false, false);

    window.removeEventListener('resize', scheduleFit);
    window.removeEventListener('pagehide', onPageHide);
    window.removeEventListener('beforeunload', onPageHide);
    if (resizeObserver) {
      try {
        resizeObserver.disconnect();
      } catch (err) {
        /* 忽略 */
      }
      resizeObserver = null;
    }
    if (focusObserver) {
      try {
        focusObserver.disconnect();
      } catch (err) {
        /* 忽略 */
      }
      focusObserver = null;
    }
    if (resizeFrame) {
      cancelAnimationFrame(resizeFrame);
      resizeFrame = 0;
    }

    const socket = state.socket;
    state.socket = null;
    state.connected = false;
    if (socket) {
      detachSocketHandlers(socket);
      try {
        socket.close(1000, 'window closed');
      } catch (err) {
        /* 忽略 */
      }
    }

    try {
      term.dispose();
    } catch (err) {
      /* 忽略 */
    }

    // 从调试列表里摘掉，避免页面长期开着时越积越多
    if (window.__terminals) {
      const index = window.__terminals.indexOf(debugEntry);
      if (index >= 0) {
        window.__terminals.splice(index, 1);
      }
    }
  }

  /* -------------------------------------------------------------------------
     给 sessionstate.js 的接口（窗口内容控制器）
     ------------------------------------------------------------------------- */

  function createTerminalController() {
    /** 最近一次序列化出来的存档条目；窗口被关掉之后仍然能被 collectState 取到 */
    let stash = null;

    /**
     * 拍一份当前的存档条目。
     *
     * `visible` 由 sessionstate 传进来（窗口还在桌面上为 true）。
     * 窗口已经关掉时不能去读 winbox 的几何信息（那时 win.x/y 已经不再
     * 代表用户摆的位置），所以关窗时记住的那一份必须留着。
     */
    function serialize(visible) {
      if (!(visible && record && record.win)) {
        // 窗口已经关掉：几何信息不能再用（winbox 的 x/y 这时已经不代表
        // 用户摆的位置），所以退回「关窗那一刻记住的那一份」。
        // 连那一份都还没有（极端时序）时至少给出 kind，
        // 免得它被当成「不认识的窗口」而从布局里消失。
        return stash ? Object.assign({}, stash) : { kind: 'terminal' };
      }
      const win = record.win;
      const item = {
        kind: 'terminal',
        z: zIndexOf(win),
        x: roundInt(win.x),
        y: roundInt(win.y),
        width: roundInt(win.width),
        height: roundInt(win.height),
        max: !!(win.g && win.g.classList.contains('max')),
        min: !!win.min,
        /* ★ 会话标识：这是「刷新之后还能回到原来那个命令行」的全部依据。
           会话已经结束时写 null：那时它既不能重连，也不该占着服务端的名额，
           下次打开页面直接显示「会话已结束」+ 开始新会话即可。
           注意 takenOver 不算结束：会话还在（在别的连接手里），sid 要留着。 */
        sid: (state.ended && !state.takenOver)
          ? null
          : (state.sid || state.attachedSid || null),
        /** 这一次连接有没有真的接上过（没收过 attached 就说明会话可能压根不存在） */
        attached: !!state.everAttached,
        ended: !!state.ended,
        /** 会话被别的连接接管了：会话还活着，但本页不该再去抢 */
        takenOver: !!state.takenOver,
        shell: state.shell,
        cwd: state.cwd,
        title: (record.title || '')
      };
      stash = item;
      return Object.assign({}, item);
    }

    return {
      serialize: serialize,

      /**
       * 记忆一份存档条目，并把它推给布局模块。
       *
       * @param {boolean} visible 窗口是否还在桌面上。
       *   传 true 表示「现在就拍一份新的」—— 用在「会话结束 / 断开」这类
       *   状态刚变了、必须在刷新前落盘的时刻。
       *   传 false 表示窗口已经关掉了：这时读不到几何信息（winbox 的 x/y
       *   已经不代表用户摆的位置），所以退回上一份记住的内容。
       * @param {boolean} needsSave 顺带催一次保存（状态变化时要，纯粹刷新几何
       *   快照时不要 —— 那种情况本来就会紧跟着一次 collectState）
       */
      remember: function (visible, needsSave) {
        serialize(!!visible);
        publish();
        if (needsSave) {
          scheduleSaveSoon();
        }
      }
    };
  }

  /**
   * 把当前快照推给 sessionstate 的登记表。
   *
   * ★ 为什么不能只靠 sessionstate 自己来「拉」：
   *   它的 collectState() 只在保存布局时跑（防抖 600ms）。而窗口一旦被关掉，
   *   就从 wm.windows 里消失了，再也没人遍历得到它 —— 那种
   *   「接上 → 关窗口 → 立刻关浏览器」的路径上，最后一次采集时窗口已经不在，
   *   会话就彻底丢了。所以这里在状态一变就主动推。
   *
   * 用动态 import 是为了避免 terminal ⇄ sessionstate 的静态往返依赖
   * （sessionstate 要静态 import 本模块的 restoreTerminal）。
   * 这个 registerTerminalStash 调用点都在用户操作路径上（接上 / 断开 / 结束 /
   * 关窗），import 已经解析完成，不会踩到「页面卸载时来不及跑」的问题。
   */
  function publish() {
    const snapshot = controller && typeof controller.serialize === 'function'
      ? controller.serialize(!state.closed)
      : null;
    if (!snapshot) {
      return;
    }
    Promise.resolve()
      .then(function () {
        return import('./sessionstate.js');
      })
      .then(function (mod) {
        if (mod && typeof mod.registerTerminalStash === 'function') {
          mod.registerTerminalStash(record.id, { item: snapshot });
        }
      })
      .catch(function () {
        /* 布局模块不可用：终端功能不受影响，静默忽略 */
      });
  }

  /* -------------------------------------------------------------------------
     调试与自动化测试钩子
     ------------------------------------------------------------------------- */

  // 与 main.js 里 window.__desktop 同样用途：便于在控制台排查问题。
  // 另外 xterm 把内容画在 canvas 上，DOM 里读不到文本，
  // 所以额外提供 buffer() 把终端缓冲区取成纯文本，供自动化断言使用。
  debugEntry = {
    sid: state.sid,
    term: term,
    fitAddon: fitAddon,
    backend: state.backend,
    sent: [],
    // 下面几个是「不受日志上限影响」的长期计数，排查问题时比翻 sent 可靠
    resizeCount: 0,
    lastResizeValue: null,
    lastInput: '',
    controlCount: 0,
    /** 会话生命周期标志，便于自动化断言「关掉窗口后会话没被杀」等行为 */
    session: function () {
      return {
        sid: state.attachedSid || state.sid,
        connected: state.connected,
        everAttached: state.everAttached,
        detached: state.detached,
        ended: state.ended,
        takenOver: state.takenOver
      };
    },
    /** 当前终端缓冲区的纯文本（含回滚） */
    buffer: function () {
      try {
        const buf = term.buffer.active;
        const lines = [];
        for (let i = 0; i < buf.length; i += 1) {
          const line = buf.getLine(i);
          lines.push(line ? line.translateToString(true) : '');
        }
        return lines.join('\n');
      } catch (err) {
        return '';
      }
    },
    /** 最近一次已发出的 resize 内容（没发过则为 null） */
    lastResize: function () {
      return this.lastResizeValue;
    }
  };
  window.__terminals = window.__terminals || [];
  window.__terminals.push(debugEntry);

  /* -------------------------------------------------------------------------
     启动
     ------------------------------------------------------------------------- */

  if (state.sid) {
    renderStatus();
    connect();
  } else if (state.takenOver) {
    // 还原出来的窗口，上次是被另一个连接接管走的：会话可能还在，
    // 但**不自动去接**（两边会自动互抢），只把状态和出路摆出来
    writeNotice(
      '\r\n[该终端会话已被新的连接接管] 它可能还在另一个标签页里运行。\r\n' +
      '[要把它接回这个窗口，点工具栏「重新连接」；要开一个新的，点「开始新会话」。]\r\n'
    );
    disableInput();
    renderStatus();
  } else {
    // 还原出来的窗口，但那个会话早就没了：直接把「已结束」的界面摆出来，
    // 而不是连一个明知不存在的会话
    state.ended = true;
    writeNotice(
      '\r\n[会话已结束] 上次的命令行已经不在服务端了（可能已空闲超时被回收，或服务端重启过）。\r\n' +
      '[点工具栏「开始新会话」可以就地再开一个。]\r\n'
    );
    disableInput();
    renderStatus();
  }

  // open() 之后字体与布局可能还需一个微任务才稳定，补一次适配
  setTimeout(function () {
    if (!state.closed) {
      applyFit();
    }
  }, 60);

  return record;
}

/* ---------------------------------------------------------------------------
   小工具
   --------------------------------------------------------------------------- */

/** 归一化 backend 字段：只认协议里定义的两个值，其余（含缺失）返回 null */
function normalizeBackend(value) {
  const text = String(value == null ? '' : value).trim().toLowerCase();
  if (text === 'conpty' || text === 'pipe') {
    return text;
  }
  return null;
}

/** 取正整数，非法值走兜底 */
function toPositiveInt(value, fallback) {
  const number = Number(value);
  if (!isFinite(number) || number <= 0) {
    return fallback;
  }
  return Math.floor(number);
}

/** 四舍五入成整数（与 sessionstate 记录几何信息的口径一致） */
function roundInt(value) {
  const num = Number(value);
  return isFinite(num) ? Math.round(num) : null;
}

/** 和 sessionstate.js 取 z-index 的方式保持一致（winbox 用 z-index 表达前后关系） */
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

/** 会话结束时给一句能解释清楚原因的话（服务端已经给了 message 时以它为准） */
function lastSessionEndedMessage(reason) {
  if (reason === 'nomatch' || reason === 'forbidden') {
    return '无法接入该会话（会话不存在、已结束，或不属于当前登录身份）。';
  }
  if (reason === 'ended') {
    return '命令行进程已经结束。';
  }
  if (reason === 'reaped') {
    return '会话已结束（长时间没人连接，被空闲超时回收）。';
  }
  return '会话已结束（可能已空闲超时被回收）。';
}

/** KB 数字的人类可读写法（用于 replay 提示） */
function formatKb(kb) {
  if (kb >= 1024) {
    return (kb / 1024).toFixed(1) + ' MB';
  }
  return Math.round(kb) + ' KB';
}

/** 字节数的人类可读写法（用于 truncated 提示） */
function formatBytes(bytes) {
  if (bytes >= 1024 * 1024) {
    return (bytes / 1024 / 1024).toFixed(1) + ' MB';
  }
  if (bytes >= 1024) {
    return Math.round(bytes / 1024) + ' KB';
  }
  return Math.round(bytes) + ' 字节';
}

/** 输出上限（KB）—— 优先用服务端 /api/system/info 的配置 */
function resolveOutputKb(desktop) {
  const limits = desktop && desktop.info && desktop.info.limits;
  const value = limits && Number(limits.terminal_output_kb);
  if (value && value > 0) {
    return value;
  }
  return DEFAULT_OUTPUT_KB;
}

/** 把「输出上限 KB」折算成 xterm 的 scrollback 行数（见常量处的说明） */
function resolveScrollback(desktop) {
  const kb = resolveOutputKb(desktop);
  const lines = Math.round((kb * 1024) / CHARS_PER_LINE_ESTIMATE);
  return Math.max(MIN_SCROLLBACK, Math.min(MAX_SCROLLBACK, lines));
}

/**
 * 剥掉我们自己的提示文本里可能出现的控制字符。
 *
 * 保留 \t \n \r（排版需要），去掉 ESC 与其余 C0 控制符：
 * 否则一条「未知的消息类型：xxx」里只要混入 ESC，就会被 xterm 当指令执行。
 */
function sanitizeNotice(text) {
  return String(text == null ? '' : text)
    .replace(/\u001b/g, '')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]/g, '');
}

/** 把 shell 完整路径压成短名字，作为窗口标题与任务栏文字 */
function shortShellName(shell) {
  const name = String(shell || 'cmd.exe').split(/[\\/]/).pop() || 'cmd.exe';
  const lower = name.toLowerCase();
  if (lower === 'cmd.exe' || lower === 'cmd') {
    return '命令提示符';
  }
  if (lower.indexOf('powershell') >= 0 || lower === 'pwsh' || lower === 'pwsh.exe') {
    return 'PowerShell';
  }
  return name;
}

/** 工具栏按钮（图标 + 文字），风格与预览窗口保持一致 */
function makeButton(act, iconName, title, tooltip) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'term-btn';
  button.dataset.act = act;
  button.title = tooltip || title;

  setButtonLabel(button, iconName, title);
  return button;
}

/** 改写按钮的图标与文字（「开始新会话」的按钮文案会变，所以抽出来复用） */
function setButtonLabel(button, iconName, title) {
  // 图标来自本地 icons.js 的固定 SVG 常量（不含用户数据），用 innerHTML 注入是安全的；
  // 终端输出这类不可信内容一律走 xterm 的 write，不经过 DOM。
  button.innerHTML = '';
  const iconEl = document.createElement('span');
  iconEl.className = 'term-btn-ico';
  iconEl.innerHTML = icon(iconName);
  button.appendChild(iconEl);

  const label = document.createElement('span');
  label.textContent = title;
  button.appendChild(label);

  button.dataset.act = button.dataset.act || '';
}

export default { openTerminal, restoreTerminal };

/* ==========================================================================
   用户管理窗口（仅管理员）
   --------------------------------------------------------------------------
   三个标签页，对应管理员需要的三件事：

     1. **用户**：谁能登录、什么角色、看得到哪些目录、几个命令行窗口、
        几个进程、几个后台任务；并且能在这里建账号 / 改显示名 / 改角色 /
        改可见目录 / 重设口令 / 停用 / 强制下线。
     2. **在线**：谁现在在线、从哪个 IP、什么时候登录的、最后活动是多久以前。
     3. **审计日志**：账号层面的动作留痕（登录成功/失败、登出、改密码、
        建号/改号/停用/重设口令/强制下线）。

   三个刻意的取舍
   --------------
   1. **不做「删除用户」，只做停用。**
      用户已确认。删账号不影响他的文件夹，却会让「这个人是谁」的审计线索
      断掉；而停用是立刻生效且可逆的。真要彻底删除请手工编辑 users.json。

   2. **不显示终端输出内容。**
      这里只给「某人开着几个命令行窗口」这个**计数**（用户决定）。
      接口形状也配合了这一点：服务端的 owner_counts() 只返回数量。
      一个管理员随手看到别人终端里在跑什么，比这个功能本身带来的便利危险得多。

   3. **不参与布局持久化。**
      根元素挂 .usermgr，sessionstate.js 的 windowKind() 认不出它 → 返回
      'unknown' → 不入库不还原（与任务管理器同样的处理）。
      管理窗口每次刷新页面都自动弹出来并不是用户想要的。

   还有一个「不做」值得写下来：**不能停用自己 / 改自己的角色**。
   服务端会拒绝（并说明原因），界面上也把这两个入口对自己那一行禁掉 ——
   两处都做，是因为这个误操作的代价是「把自己锁在管理界面外面」。
   ========================================================================== */

import * as api from './api.js';
import * as ui from './ui.js';
import { icon } from './icons.js';
import { wm } from './wins.js';

/** 自动刷新间隔（秒），0 = 不自动刷新 */
const REFRESH_CHOICES = [0, 5, 10, 30];
const DEFAULT_REFRESH = 10;

/** 审计日志一次取多少条 */
const AUDIT_LIMIT = 200;

const ROLE_LABELS = {
  admin: '管理员',
  user: '普通用户'
};

/** 审计事件名 -> 中文（与 fileweb/audit.py 的 EVENT_* 常量一一对应） */
const EVENT_LABELS = {
  login_ok: '登录成功',
  login_fail: '登录失败',
  logout: '注销',
  password_change: '修改密码',
  user_create: '新建用户',
  user_update: '修改用户',
  user_disable: '停用用户',
  user_enable: '启用用户',
  user_kick: '强制下线',
  user_password_reset: '重设口令'
};

function eventLabel(name) {
  const key = String(name || '');
  return EVENT_LABELS[key] || key || '—';
}

function roleLabel(role) {
  return ROLE_LABELS[String(role || '')] || String(role || '—');
}

/** 「多久以前」——比绝对时间更能一眼看出谁还活着 */
function idleText(seconds) {
  const n = Number(seconds);
  if (!isFinite(n) || n < 0) {
    return '—';
  }
  if (n < 45) {
    return '刚刚';
  }
  if (n < 3600) {
    return Math.round(n / 60) + ' 分钟前';
  }
  if (n < 86400) {
    return Math.round(n / 3600) + ' 小时前';
  }
  return Math.round(n / 86400) + ' 天前';
}

/* ---------------------------------------------------------------------------
   可见目录
   ---------------------------------------------------------------------------
   格式由**服务端**定义并解析（见 fileweb/users.py 的 parse_roots_text）：
   每行一个目录，`路径 | 名称 | 只读`，后两段可省略。

   这里刻意不做任何解析 —— 前端只负责把文本框里的原文发给服务端、
   再把服务端给的 roots_text 显示出来。理由：格式只有一处定义，
   而且那段逻辑能在 Python 测试里覆盖（前端解析出错就是「把权限分错人」，
   偏偏又最难测）。
   --------------------------------------------------------------------------- */

/* ---------------------------------------------------------------------------
   窗口内的控制器
   --------------------------------------------------------------------------- */

class UserManager {

  constructor() {
    this.root = document.createElement('div');
    // ★ .usermgr 这个类名是「不参与布局持久化」的依据（见文件头第 3 点）
    this.root.className = 'um-root usermgr';
    this.root.innerHTML = this.template();

    this.tabsEl = this.root.querySelector('.um-tabs');
    this.statusEl = this.root.querySelector('[data-role="status"]');
    this.countEl = this.root.querySelector('[data-role="count"]');
    this.panes = {};
    ['users', 'online', 'audit'].forEach(function (name) {
      this.panes[name] = this.root.querySelector('[data-pane="' + name + '"]');
    }, this);

    this.formEl = this.root.querySelector('.um-form');
    this.formTitleEl = this.root.querySelector('[data-role="form-title"]');
    this.formErrorEl = this.root.querySelector('[data-role="form-error"]');

    this.tab = 'users';
    this.interval = DEFAULT_REFRESH;
    this.timer = null;
    this.closed = false;
    this.busy = false;
    this.users = [];
    this.editing = '';          // 正在编辑的用户名（空 = 新建）
    this.loadedAudit = false;

    this.bind();

    const self = this;
    this.onVisibility = function () {
      if (!self.closed && !document.hidden) {
        self.tick();
      }
    };
    document.addEventListener('visibilitychange', this.onVisibility);
  }

  template() {
    let options = '';
    REFRESH_CHOICES.forEach(function (sec) {
      options += '<option value="' + sec + '"' +
        (sec === DEFAULT_REFRESH ? ' selected' : '') + '>' +
        (sec === 0 ? '不自动刷新' : sec + ' 秒') + '</option>';
    });

    return [
      '<div class="um-toolbar">',
      '  <button type="button" class="um-btn primary" data-act="new">', icon('user'), '<span>新建用户</span></button>',
      '  <button type="button" class="um-btn" data-act="refresh">', icon('refresh'), '<span>刷新</span></button>',
      '  <span class="um-spacer"></span>',
      '  <span class="um-status" data-role="status"></span>',
      '  <select class="um-select" data-role="interval" title="刷新间隔">', options, '</select>',
      '</div>',

      '<div class="um-tabs">',
      '  <div class="um-tab active" data-tab="users">用户 <span class="um-badge" data-role="count"></span></div>',
      '  <div class="um-tab" data-tab="online">在线</div>',
      '  <div class="um-tab" data-tab="audit">审计日志</div>',
      '</div>',

      '<div class="um-body">',
      '  <div class="um-pane active" data-pane="users">',
      '    <div class="um-table-wrap"><table class="um-table">',
      '      <thead><tr>',
      '        <th style="width:130px">用户名</th>',
      '        <th style="width:120px">显示名</th>',
      '        <th style="width:80px">角色</th>',
      '        <th style="width:70px">状态</th>',
      '        <th style="width:130px">在线</th>',
      '        <th style="width:64px" title="正在开着的命令行窗口数">命令行</th>',
      '        <th style="width:56px" title="该用户正在运行的进程数">进程</th>',
      '        <th style="width:56px" title="后台任务数">任务</th>',
      '        <th style="width:64px" title="分配了几个可见目录">目录</th>',
      '        <th>操作</th>',
      '      </tr></thead>',
      '      <tbody data-role="users"></tbody>',
      '    </table></div>',
      '  </div>',

      '  <div class="um-pane" data-pane="online">',
      '    <div class="um-table-wrap"><table class="um-table">',
      '      <thead><tr>',
      '        <th style="width:140px">用户名</th>',
      '        <th style="width:140px">显示名</th>',
      '        <th style="width:130px">IP</th>',
      '        <th style="width:150px">登录时间</th>',
      '        <th style="width:150px">最后活动</th>',
      '        <th>状态</th>',
      '      </tr></thead>',
      '      <tbody data-role="online"></tbody>',
      '    </table></div>',
      '  </div>',

      '  <div class="um-pane" data-pane="audit">',
      '    <div class="um-table-wrap"><table class="um-table">',
      '      <thead><tr>',
      '        <th style="width:150px">时间</th>',
      '        <th style="width:110px">事件</th>',
      '        <th style="width:120px">用户</th>',
      '        <th style="width:130px">IP</th>',
      '        <th style="width:60px">结果</th>',
      '        <th>说明</th>',
      '      </tr></thead>',
      '      <tbody data-role="audit"></tbody>',
      '    </table></div>',
      '  </div>',
      '</div>',

      '<div class="um-form" hidden>',
      '  <div class="um-form-head" data-role="form-title">新建用户</div>',
      '  <div class="um-form-body">',
      '    <label class="um-field"><span>用户名</span>',
      '      <input type="text" data-field="username" autocomplete="off" spellcheck="false" placeholder="字母、数字、下划线、点、连字符">',
      '    </label>',
      '    <label class="um-field"><span>显示名</span>',
      '      <input type="text" data-field="display_name" autocomplete="off" placeholder="留空则与用户名相同">',
      '    </label>',
      '    <label class="um-field"><span>角色</span>',
      '      <select data-field="role">',
      '        <option value="user">普通用户</option>',
      '        <option value="admin">管理员</option>',
      '      </select>',
      '    </label>',
      '    <label class="um-field"><span>命令行窗口上限</span>',
      '      <input type="number" data-field="max_terminal_sessions" min="1" max="64" value="5">',
      '    </label>',
      '    <label class="um-field" data-role="password-field"><span>初始密码</span>',
      '      <input type="password" data-field="password" autocomplete="new-password" placeholder="至少 8 位">',
      '    </label>',
      '    <label class="um-field um-field-wide"><span>备注</span>',
      '      <input type="text" data-field="note" autocomplete="off" placeholder="例如：2023 级 张三（可留空）">',
      '    </label>',
      '    <label class="um-field um-field-wide"><span>可见目录</span>',
      '      <textarea data-field="roots" rows="4" spellcheck="false" ',
      '        placeholder="每行一个目录，例如：&#10;D:\\students\\zhangsan&#10;D:\\public | 公共资料 | 只读"></textarea>',
      '    </label>',
      '    <label class="um-field um-field-check"><span>启用</span>',
      '      <input type="checkbox" data-field="enabled" checked>',
      '    </label>',
      '    <div class="um-hint">',
      '      可见目录就是该用户「此电脑」里的全部内容 —— 他只能看到这里列出的目录，',
      '      不会看到任何磁盘或别人的目录。格式：<code>路径 | 名称 | 只读</code>，',
      '      后两段可省略；写 <code>只读</code> 时该目录只能读、不能改。',
      '      一个都不填 = 他看得到空桌面（也打不开任何文件）。',
      '    </div>',
      '    <div class="um-form-error" data-role="form-error"></div>',
      '  </div>',
      '  <div class="um-form-foot">',
      '    <button type="button" class="um-btn primary" data-act="save">保存</button>',
      '    <button type="button" class="um-btn" data-act="cancel">取消</button>',
      '  </div>',
      '</div>'
    ].join('');
  }

  bind() {
    const self = this;

    this.root.querySelector('[data-act="new"]').addEventListener('click', function () {
      self.openForm('');
    });
    this.root.querySelector('[data-act="refresh"]').addEventListener('click', function () {
      self.loadedAudit = false;
      self.tick();
    });
    this.root.querySelector('[data-act="save"]').addEventListener('click', function () {
      self.submitForm();
    });
    this.root.querySelector('[data-act="cancel"]').addEventListener('click', function () {
      self.closeForm();
    });

    this.root.querySelector('[data-role="interval"]').addEventListener('change', function (ev) {
      self.interval = Number(ev.target.value) || 0;
      self.schedule();
    });

    this.tabsEl.addEventListener('click', function (ev) {
      const tab = ev.target.closest ? ev.target.closest('.um-tab') : null;
      if (tab) {
        self.selectTab(tab.dataset.tab);
      }
    });

    // 表格里的按钮是渲染出来的，用委托避免每次重绘都重新绑一遍
    this.root.querySelector('.um-body').addEventListener('click', function (ev) {
      const btn = ev.target.closest ? ev.target.closest('[data-row-act]') : null;
      if (!btn) {
        return;
      }
      self.rowAction(btn.dataset.rowAct, btn.dataset.user);
    });
  }

  /* -- 数据 --------------------------------------------------------------- */

  start() {
    this.tick();
  }

  schedule() {
    this.cancelTimer();
    if (this.closed || !this.interval) {
      return;
    }
    const self = this;
    this.timer = setTimeout(function () {
      self.tick();
    }, this.interval * 1000);
  }

  cancelTimer() {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  tick() {
    if (this.closed || this.busy) {
      return;
    }
    this.busy = true;
    const self = this;

    const jobs = [api.listUsers(), api.listOnline()];
    // 审计日志是历史记录，只在切到那个标签页时取（外加手动刷新时）
    if (this.tab === 'audit' && !this.loadedAudit) {
      jobs.push(api.readAudit(AUDIT_LIMIT));
    }

    Promise.all(jobs).then(function (results) {
      self.busy = false;
      if (self.closed) {
        return;
      }
      self.renderUsers(results[0]);
      self.renderOnline(results[1]);
      if (results[2]) {
        self.renderAudit(results[2]);
        self.loadedAudit = true;
      }
      self.setStatus('更新于 ' + new Date().toLocaleTimeString());
      self.schedule();
    }).catch(function (err) {
      self.busy = false;
      if (self.closed) {
        return;
      }
      // 403 = 不是管理员（例如权限刚被改掉），这时候再轮询也没有意义
      self.setStatus(err && err.message ? err.message : '读取失败', true);
      if (!err || err.status !== 403) {
        self.schedule();
      }
    });
  }

  setStatus(text, isError) {
    if (!this.statusEl) {
      return;
    }
    this.statusEl.textContent = text || '';
    this.statusEl.classList.toggle('error', !!isError);
  }

  selectTab(name) {
    if (!this.panes[name]) {
      return;
    }
    this.tab = name;
    const self = this;
    this.tabsEl.querySelectorAll('.um-tab').forEach(function (el) {
      el.classList.toggle('active', el.dataset.tab === name);
    });
    Object.keys(this.panes).forEach(function (key) {
      self.panes[key].classList.toggle('active', key === name);
    });

    if (name === 'audit') {
      this.loadedAudit = false;
      this.tick();
    }
  }

  /* -- 渲染 --------------------------------------------------------------- */

  renderUsers(data) {
    const rows = (data && data.users) || [];
    this.users = rows;
    this.countEl.textContent = String(rows.length);
    this.countEl.hidden = false;

    const self = this;
    let html = '';

    rows.forEach(function (row) {
      const online = row.online;
      const role = String(row.role || 'user');

      // ★ 自己那一行禁用「停用 / 改角色 / 下线」：服务端也会拒绝，
      //   但界面上先挡住更省事，而且能顺带给出原因（tooltip）。
      const selfRow = !!row.is_self;
      const guardTitle = selfRow
        ? '不能对自己执行这项操作（会把自己锁在管理界面外面）' : '';

      html += '<tr' + (row.enabled ? '' : ' class="disabled"') + '>' +
        '<td class="um-name">' + ui.escapeHtml(row.username) +
          (selfRow ? '<span class="um-me">我</span>' : '') + '</td>' +
        '<td>' + ui.escapeHtml(row.display_name || row.username) + '</td>' +
        '<td>' + ui.escapeHtml(roleLabel(role)) + '</td>' +
        '<td>' + (row.enabled
          ? '<span class="um-pill ok">已启用</span>'
          : '<span class="um-pill off">已停用</span>') + '</td>' +
        '<td>' + (online
          ? '<span class="um-pill online">在线</span><span class="um-sub">' +
            ui.escapeHtml(row.ip || '') + ' · ' + ui.escapeHtml(idleText(row.idle_seconds)) + '</span>'
          : '<span class="um-sub">' + ui.escapeHtml(idleText(row.idle_seconds)) + '</span>') + '</td>' +
        '<td class="num">' + (row.terminal_sessions || 0) + '</td>' +
        '<td class="num">' + (row.processes || 0) + '</td>' +
        '<td class="num">' + (row.jobs || 0) + '</td>' +
        '<td class="num">' + ((row.roots || []).length) + '</td>' +
        '<td class="um-acts">' +
          '<button type="button" class="um-mini" data-row-act="edit" data-user="' +
            ui.escapeHtml(row.username) + '">编辑</button>' +
          '<button type="button" class="um-mini" data-row-act="password" data-user="' +
            ui.escapeHtml(row.username) + '">重设口令</button>' +
          '<button type="button" class="um-mini' + (row.enabled ? ' danger' : '') +
            '" data-row-act="toggle" data-user="' + ui.escapeHtml(row.username) + '"' +
            (selfRow ? ' disabled title="' + guardTitle + '"' : '') + '>' +
            (row.enabled ? '停用' : '启用') + '</button>' +
          '<button type="button" class="um-mini" data-row-act="kick" data-user="' +
            ui.escapeHtml(row.username) + '"' + (selfRow ? ' disabled title="' + guardTitle + '"' : '') +
            '>下线</button>' +
        '</td>' +
        '</tr>';
    });

    if (!html) {
      html = '<tr><td colspan="10" class="um-empty">没有取到用户列表</td></tr>';
    }
    this.panes.users.querySelector('[data-role="users"]').innerHTML = html;
  }

  renderOnline(data) {
    const rows = (data && data.online) || [];
    let html = '';

    rows.forEach(function (row) {
      html += '<tr>' +
        '<td class="um-name">' + ui.escapeHtml(row.username) + '</td>' +
        '<td>' + ui.escapeHtml(row.display_name || row.username) + '</td>' +
        '<td>' + ui.escapeHtml(row.ip || '—') + '</td>' +
        '<td>' + ui.escapeHtml(row.login_at ? ui.formatTime(row.login_at) : '—') + '</td>' +
        '<td>' + ui.escapeHtml(row.last_seen ? ui.formatTime(row.last_seen) : '—') + '</td>' +
        '<td>' + (row.online
          ? '<span class="um-pill online">在线</span>'
          : '<span class="um-pill off">离线</span>') +
          '<span class="um-sub">' + ui.escapeHtml(idleText(row.idle_seconds)) + '</span></td>' +
        '</tr>';
    });

    if (!html) {
      html = '<tr><td colspan="6" class="um-empty">还没有人登录过</td></tr>';
    }
    this.panes.online.querySelector('[data-role="online"]').innerHTML = html;
  }

  renderAudit(data) {
    const rows = (data && data.records) || [];
    // 最新的一条排在最前面：查问题时看的是「刚才发生了什么」
    const ordered = rows.slice().reverse();
    let html = '';

    ordered.forEach(function (row) {
      const fail = String(row.result || '') !== 'ok';
      html += '<tr>' +
        '<td class="um-time">' + ui.escapeHtml(row.time || '') + '</td>' +
        '<td>' + ui.escapeHtml(eventLabel(row.event)) + '</td>' +
        '<td>' + ui.escapeHtml(row.username || '—') + '</td>' +
        '<td>' + ui.escapeHtml(row.ip || '—') + '</td>' +
        '<td>' + (fail ? '<span class="um-pill off">失败</span>'
                       : '<span class="um-pill ok">成功</span>') + '</td>' +
        '<td class="um-detail">' + ui.escapeHtml(row.detail || '') + '</td>' +
        '</tr>';
    });

    if (!html) {
      html = '<tr><td colspan="6" class="um-empty">还没有审计记录</td></tr>';
    }
    this.panes.audit.querySelector('[data-role="audit"]').innerHTML = html;
  }

  /* -- 表单（新建 / 编辑）------------------------------------------------- */

  openForm(username) {
    this.editing = username || '';
    const user = this.findUser(username);

    this.formTitleEl.textContent = user
      ? '编辑用户：' + user.username
      : '新建用户';

    this.field('username').value = user ? user.username : '';
    this.field('username').disabled = !!user;      // 用户名不可改（它是归属标记）
    this.field('display_name').value = user ? (user.display_name || '') : '';
    this.field('note').value = user ? (user.note || '') : '';
    this.field('role').value = user ? (user.role || 'user') : 'user';
    this.field('max_terminal_sessions').value =
      String(user ? (user.max_terminal_sessions || 5) : 5);
    this.field('roots').value = user ? (user.roots_text || '') : '';
    this.field('enabled').checked = user ? !!user.enabled : true;
    this.field('password').value = '';

    // 口令只在新建时填：改口令是单独的动作（它会把对方所有会话踢下线，
    // 混在「保存」里会让人以为只是改了个显示名，实际却把人家踢了）
    this.root.querySelector('[data-role="password-field"]').hidden = !!user;

    // 自己那一行的角色不可改（服务端也会拒绝）
    const selfRow = !!user && !!user.is_self;
    this.field('role').disabled = selfRow;
    this.field('enabled').disabled = selfRow;

    this.setFormError('');
    this.formEl.hidden = false;
    this.field('username').focus();
  }

  closeForm() {
    this.formEl.hidden = true;
    this.editing = '';
    this.setFormError('');
  }

  field(name) {
    return this.root.querySelector('[data-field="' + name + '"]');
  }

  setFormError(text) {
    this.formErrorEl.textContent = text || '';
    this.formErrorEl.hidden = !text;
  }

  findUser(username) {
    const needle = String(username || '').toLowerCase();
    let found = null;
    this.users.forEach(function (row) {
      if (String(row.username).toLowerCase() === needle) {
        found = row;
      }
    });
    return found;
  }

  submitForm() {
    const username = this.field('username').value.trim();
    const payload = {
      display_name: this.field('display_name').value.trim(),
      note: this.field('note').value.trim(),
      role: this.field('role').value,
      max_terminal_sessions: Number(this.field('max_terminal_sessions').value) || 5,
      // 原文发给服务端解析（格式定义在 fileweb/users.py）
      roots_text: this.field('roots').value,
      enabled: !!this.field('enabled').checked
    };

    const self = this;
    let promise;

    if (this.editing) {
      promise = api.updateUser(this.editing, payload);
    } else {
      payload.username = username;
      payload.password = this.field('password').value;
      promise = api.createUser(payload);
    }

    this.setFormError('');
    promise.then(function (data) {
      if (self.closed) {
        return;
      }
      ui.toast(data && data.message ? data.message : '已保存', 'success');
      self.closeForm();
      self.loadedAudit = false;
      self.tick();
    }).catch(function (err) {
      // 错误留在表单里而不是弹提示：用户要照着这句话改输入
      self.setFormError(err && err.message ? err.message : '保存失败');
    });
  }

  /* -- 行内操作 ----------------------------------------------------------- */

  rowAction(action, username) {
    const user = this.findUser(username);
    if (!user) {
      return;
    }
    const self = this;

    if (action === 'edit') {
      this.openForm(username);
      return;
    }

    if (action === 'password') {
      ui.showPrompt('重设口令',
        '为「' + user.username + '」设置新口令（至少 8 位）。' +
        '重设后他当前的登录会立即失效，需要用新口令重新登录。',
        '', { okText: '重设', placeholder: '新口令' }).then(function (value) {
        if (value === null || value === undefined) {
          return;
        }
        const password = String(value);
        if (password.length < 8) {
          ui.toast('口令至少需要 8 位', 'error');
          return;
        }
        api.resetUserPassword(user.username, password).then(function (data) {
          ui.toast(data && data.message ? data.message : '已重设口令', 'success');
          self.loadedAudit = false;
          self.tick();
        }).catch(function (err) {
          ui.toast(err && err.message ? err.message : '重设失败', 'error');
        });
      });
      return;
    }

    if (action === 'toggle') {
      const next = !user.enabled;
      const message = next
        ? '要重新启用「' + user.username + '」吗？启用后他就能再次登录了。'
        : '要停用「' + user.username + '」吗？他的登录会**立即**失效，' +
          '在重新启用之前无法登录。（他的文件不会被删除。）';
      ui.showConfirm(next ? '启用用户' : '停用用户', message,
        { danger: !next, okText: next ? '启用' : '停用' }).then(function (ok) {
        if (!ok) {
          return;
        }
        api.updateUser(user.username, { enabled: next }).then(function (data) {
          ui.toast(data && data.message ? data.message : '已更新', 'success');
          self.loadedAudit = false;
          self.tick();
        }).catch(function (err) {
          ui.toast(err && err.message ? err.message : '操作失败', 'error');
        });
      });
      return;
    }

    if (action === 'kick') {
      ui.showConfirm('强制下线',
        '要强制「' + user.username + '」下线吗？他所有已登录的浏览器都会失效，' +
        '需要重新输入口令。账号本身不受影响，仍可正常登录。',
        { okText: '强制下线' }).then(function (ok) {
        if (!ok) {
          return;
        }
        api.kickUser(user.username).then(function (data) {
          ui.toast(data && data.message ? data.message : '已强制下线', 'success');
          self.loadedAudit = false;
          self.tick();
        }).catch(function (err) {
          ui.toast(err && err.message ? err.message : '操作失败', 'error');
        });
      });
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
 * 打开用户管理窗口（仅管理员）。
 *
 * 允许开多个，与其它窗口一致 —— 不做单例复用。
 */
export function openUserManager(desktop) {
  const manager = new UserManager();

  const record = wm.create({
    title: '用户管理',
    iconName: 'user',
    content: manager.root,
    width: 980,
    height: 620,
    minWidth: 640,
    minHeight: 400,
    windowClass: 'usermgr-win',
    taskLabel: '用户管理',
    onClosed: function () {
      manager.destroy();
    }
  });

  manager.start();
  return record;
}

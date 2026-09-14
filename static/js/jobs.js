/* ==========================================================================
   后台任务进度面板
   --------------------------------------------------------------------------
   复制 / 移动 / 解压这些「可能要跑很久」的操作现在走后台队列
   （请求里带 background: true），本模块负责三件事：

     * 在右下角显示进度面板：进度条 + 已处理条目/体积 + 当前文件名；
     * 提供「取消」按钮；
     * 把终态结果交回调用方 —— trackJob() 返回的 Promise 就是原来那个
       同步接口的返回值，调用方拿到之后不用改任何后续逻辑。

   为什么不做成模态对话框：
       模态框会把整个界面锁住，而「同时复制两批文件」是完全正常的用法。
       角落面板能同时显示多个任务，也不挡着用户继续浏览文件 ——
       这正是当初「转圈遮罩」最让人难受的地方。

   为什么用轮询而不是 WebSocket：
       任务进度是低频、短生命周期的数据，轮询实现简单、断线自愈，
       也和本项目其它地方的风格一致（终端之外的交互都是请求/响应式）。
       没有活跃任务时轮询会**自动停止**，不空转。

   与终端会话的区别（别混）：
       任务活在**服务进程**里，关掉浏览器也会继续跑完；重新打开页面时
       面板会从 /api/jobs 重新列出来。所以「关页面」不等于「取消任务」，
       要停就得点取消。
   ========================================================================== */

import * as api from './api.js';
import * as ui from './ui.js';
import { icon } from './icons.js';

const POLL_MS = 800;

/** 终态：不会再变，可以结算等待者 */
const TERMINAL = { done: true, failed: true, cancelled: true };

/** 全部结束后面板还留多久（让用户看得见「完成了」再收起来） */
const KEEP_VISIBLE_MS = 4500;

/** 连续多少次轮询都没见到某个任务，就认定它已经不在队列里了 */
const MAX_MISSES = 8;

let panel = null;
let listEl = null;
let timer = null;
let pollBusy = false;
let hideTimer = null;

/** jobId -> {resolve, reject, misses} */
const waiters = new Map();

/** 已经点过取消、但任务还没真的停下来的那些（用来显示「正在取消…」） */
const cancelling = new Set();

function ensurePanel() {
  if (panel) {
    return panel;
  }
  panel = document.createElement('div');
  panel.className = 'job-panel';
  panel.hidden = true;
  panel.innerHTML =
    '<div class="job-head">' +
    '<span class="job-head-ico">' + icon('activity') + '</span>' +
    '<span class="job-head-text">后台任务</span>' +
    '</div>' +
    '<div class="job-list"></div>';
  document.body.appendChild(panel);
  listEl = panel.querySelector('.job-list');
  return panel;
}

/** 「已处理 3/128 项 · 12.4 MB / 900 MB · 正在复制 xxx」这一行 */
function detailText(job) {
  if (job.status === 'failed') {
    return '失败：' + (job.error || '未知错误');
  }
  if (job.status === 'cancelled') {
    return '已取消';
  }
  if (cancelling.has(job.id)) {
    return '正在取消…';
  }
  if (job.status === 'pending') {
    return '排队中…';
  }
  if (job.status === 'done') {
    return job.message || '已完成';
  }

  const bits = [];
  if (job.total_items) {
    bits.push(job.done_items + '/' + job.total_items + ' 项');
  }
  if (job.total_bytes) {
    bits.push(ui.formatSize(job.done_bytes) + ' / ' + ui.formatSize(job.total_bytes));
  }
  if (job.current) {
    bits.push(job.current);
  }
  return bits.length ? bits.join(' · ') : '处理中…';
}

function rowHtml(job) {
  const percent = Math.max(0, Math.min(100, Number(job.percent) || 0));
  const active = !TERMINAL[job.status];

  let cls = 'job-row' + (active ? ' is-active' : '');
  if (job.status === 'done') {
    cls += ' is-done';
  } else if (job.status === 'failed') {
    cls += ' is-failed';
  } else if (job.status === 'cancelled') {
    cls += ' is-cancelled';
  }

  const label = job.status === 'done' ? '完成'
    : job.status === 'cancelled' ? '已取消'
      : job.status === 'failed' ? '失败'
        : Math.round(percent) + '%';

  const canCancel = !!job.cancellable && !cancelling.has(job.id);
  const detail = detailText(job);

  return '<div class="' + cls + '" data-job="' + ui.escapeHtml(job.id) + '">' +
    '<div class="job-row-top">' +
    '<span class="job-name" title="' + ui.escapeHtml(job.title || job.kind) + '">' +
    ui.escapeHtml(job.title || job.kind) + '</span>' +
    '<span class="job-pct">' + ui.escapeHtml(label) + '</span>' +
    (canCancel
      ? '<button type="button" class="job-cancel" data-cancel="' +
        ui.escapeHtml(job.id) + '" title="取消">' + icon('close') + '</button>'
      : '') +
    '</div>' +
    '<div class="job-bar"><i style="width:' + percent + '%"></i></div>' +
    '<div class="job-detail" title="' + ui.escapeHtml(detail) + '">' +
    ui.escapeHtml(detail) + '</div>' +
    '</div>';
}

function render(jobs, hasActive) {
  ensurePanel();
  listEl.innerHTML = jobs.map(rowHtml).join('');
  panel.hidden = false;

  listEl.querySelectorAll('[data-cancel]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const jobId = btn.dataset.cancel;
      cancelling.add(jobId);
      btn.disabled = true;
      api.cancelJob(jobId).catch(function () {
        // 取消失败（任务刚好结束了之类）：把标记撤掉，让面板回到真实状态
        cancelling.delete(jobId);
      });
      render(jobs, hasActive);   // 立刻重画，让「正在取消…」马上可见
      schedule(150);
    });
  });

  if (hideTimer) {
    clearTimeout(hideTimer);
    hideTimer = null;
  }
  if (!hasActive && !waiters.size) {
    hideTimer = setTimeout(function () {
      // 期间若又有新任务起来，下一次 render 会把这个定时器清掉
      if (panel && !listEl.querySelector('.job-row.is-active')) {
        panel.hidden = true;
      }
    }, KEEP_VISIBLE_MS);
  }
}

/** 把已经到达终态的任务结算掉（兑现或拒绝 trackJob 的 Promise） */
function settle(jobs) {
  if (!waiters.size) {
    return;
  }

  const byId = {};
  jobs.forEach(function (job) {
    byId[job.id] = job;
  });

  waiters.forEach(function (waiter, jobId) {
    const job = byId[jobId];

    if (!job) {
      // 任务不在列表里：可能被回收了，也可能只是这一轮没列出来。
      // 连续多次都没见到才判定丢失，避免误伤。
      waiter.misses = (waiter.misses || 0) + 1;
      if (waiter.misses >= MAX_MISSES) {
        waiters.delete(jobId);
        cancelling.delete(jobId);
        const missing = new Error('任务已不在队列中（可能已被回收）');
        missing.status = 0;
        waiter.reject(missing);
      }
      return;
    }

    waiter.misses = 0;
    if (!TERMINAL[job.status]) {
      return;
    }

    waiters.delete(jobId);
    cancelling.delete(jobId);

    if (job.status === 'done') {
      waiter.resolve(job.result || {});
      return;
    }

    const err = new Error(job.status === 'cancelled'
      ? '已取消' : (job.error || '任务失败'));
    err.cancelled = (job.status === 'cancelled');
    err.status = 0;
    waiter.reject(err);
  });
}

function schedule(delay) {
  if (timer) {
    clearTimeout(timer);
  }
  timer = setTimeout(poll, delay === undefined ? POLL_MS : delay);
}

function stop() {
  if (timer) {
    clearTimeout(timer);
    timer = null;
  }
}

async function poll() {
  timer = null;

  if (pollBusy) {
    schedule();
    return;
  }
  pollBusy = true;

  let data = null;
  try {
    data = await api.listJobs();
  } catch (err) {
    pollBusy = false;
    if (err && err.status === 401) {
      stop();          // 会话没了，轮询没有意义
      return;
    }
    schedule(2000);    // 网络抖动就放慢重试，不要刷屏
    return;
  }
  pollBusy = false;

  const jobs = (data && data.jobs) || [];
  const hasActive = jobs.some(function (job) {
    return !TERMINAL[job.status];
  });

  render(jobs, hasActive);
  settle(jobs);

  if (hasActive || waiters.size) {
    schedule();
  } else {
    stop();
  }
}

/**
 * 跟踪一个后台任务，并在它结束时把结果交回来。
 *
 * @param {string} jobId 后台接口返回的 job_id
 * @returns {Promise<object>} 成功时 resolve 任务的 result
 *          （形状与对应的同步接口完全一致）；
 *          失败或取消时 reject，取消的情况带 err.cancelled = true。
 */
export function trackJob(jobId) {
  if (!jobId) {
    return Promise.reject(new Error('缺少任务号'));
  }

  ensurePanel();

  return new Promise(function (resolve, reject) {
    waiters.set(jobId, { resolve: resolve, reject: reject, misses: 0 });
    // 立刻刷一次：让面板马上出现，而不是干等一个轮询周期
    poll();
  });
}

/** 是否还有正在跟踪的任务（调试/测试用） */
export function hasPending() {
  return waiters.size > 0;
}

/* ==========================================================================
   预览窗口
   --------------------------------------------------------------------------
   按文件类型自动选择查看器：
     * 图片      —— 自绘查看器，支持缩放 / 适应窗口 / 原始大小 / 旋转 / 拖动
     * PDF       —— 本地 pdf.js，支持翻页、缩放、适应宽度（懒加载渲染）
     * 文本/代码 —— 等宽字体，后端已做编码识别（UTF-8 / GB18030 / Big5 …）
     * 音视频    —— HTML5 播放器，依赖后端 Range 支持以便拖动进度条
     * Office    —— 优先用后端转出的 PDF；没装 LibreOffice 时用后端解析出的 HTML
     * 压缩包等  —— 给出信息与下载入口，不做内容预览
   ========================================================================== */

import { icon } from './icons.js';
import * as api from './api.js';
import * as ui from './ui.js';
import { wm } from './wins.js';
import { openEditor } from './editor.js';

/* ---------------------------------------------------------------------------
   类型判定
   --------------------------------------------------------------------------- */

const KIND_BY_EXT = {};

function reg(kind, list) {
  list.split(' ').forEach(function (ext) {
    // 注意：extensionOf() 返回的是「带点」的扩展名（如 .jpg），
    // 所以这里的键必须补上前导点，否则永远匹配不上。
    KIND_BY_EXT['.' + ext] = kind;
  });
}

reg('image', 'jpg jpeg jpe jfif png gif bmp webp ico avif tif tiff heic heif');
reg('video', 'mp4 webm ogv mov m4v mkv avi wmv flv mpg mpeg 3gp ts rmvb');
reg('audio', 'mp3 wav ogg oga flac m4a aac opus ape wma aiff mid midi');
reg('pdf', 'pdf');
reg('text', 'txt md markdown log json csv tsv xml html htm yml yaml ini conf cfg toml ' +
  'properties env py pyw js mjs cjs ts jsx tsx vue css scss less ' +
  'java c h cpp hpp cs go rs rb php sh bash bat cmd ps1 sql lua pl swift kt scala ' +
  'gradle gitignore editorconfig dockerfile makefile tex');
reg('office', 'doc docx docm dot dotx xls xlsx xlsm xlt xltx ppt pptx pptm pot potx odt ods odp rtf wps et dps');
reg('archive', 'zip rar 7z tar gz bz2 xz tgz cab iso jar war');

function extensionOf(name) {
  const index = String(name || '').lastIndexOf('.');
  return index > 0 ? name.slice(index).toLowerCase() : '';
}

/** 判定预览种类 */
export function detectKind(name) {
  const ext = extensionOf(name);
  return KIND_BY_EXT[ext] || 'other';
}

/* ---------------------------------------------------------------------------
   小工具
   --------------------------------------------------------------------------- */

function toolBtn(act, iconName, title, text) {
  return '<button type="button" class="pv-btn" data-act="' + act + '" title="' +
    ui.escapeHtml(title || '') + '">' + icon(iconName) +
    (text ? '<span>' + ui.escapeHtml(text) + '</span>' : '') + '</button>';
}

function separator() {
  return '<span style="width:1px;height:18px;background:#dfe3e8;margin:0 3px"></span>';
}

function downloadButton(ctx) {
  return toolBtn('download', 'download', '下载到本地', '下载');
}

/** 下载当前文件 */
function bindDownload(root, button) {
  if (button) {
    button.addEventListener('click', function () {
      api.triggerDownload(api.downloadUrl(root.rootId, root.rel));
    });
  }
}

function messageBox(bodyEl, iconName, text, actions) {
  const box = document.createElement('div');
  box.className = 'pv-message';
  let html = icon(iconName) + '<div class="pv-msg-text">' + ui.escapeHtml(text) + '</div>';
  if (actions && actions.length) {
    html += '<div class="pv-actions">' + actions.map(function (a, i) {
      return '<button type="button" class="pv-btn" data-idx="' + i + '">' +
        icon(a.iconName || 'download') + '<span>' + ui.escapeHtml(a.label) + '</span></button>';
    }).join('') + '</div>';
  }
  box.innerHTML = html;
  bodyEl.appendChild(box);

  if (actions && actions.length) {
    box.querySelectorAll('.pv-actions .pv-btn').forEach(function (btn) {
      btn.addEventListener('click', function () {
        const action = actions[Number(btn.dataset.idx)];
        if (action && typeof action.onClick === 'function') {
          action.onClick();
        }
      });
    });
  }
  return box;
}

/* ===========================================================================
   图片查看器
   =========================================================================== */

function buildImage(container, ctx) {
  container.innerHTML =
    '<div class="preview-toolbar">' +
    toolBtn('zoom-out', 'zoom-out', '缩小') +
    toolBtn('zoom-in', 'zoom-in', '放大') +
    toolBtn('fit', 'fit', '适应窗口') +
    toolBtn('actual', 'actual-size', '原始大小 (100%)') +
    separator() +
    toolBtn('rotate-left', 'rotate-left', '向左旋转 90°') +
    toolBtn('rotate-right', 'rotate-right', '向右旋转 90°') +
    '<span class="pv-label zoom-label">100%</span>' +
    '<span class="spacer"></span>' +
    '<span class="pv-label dim-label"></span>' +
    separator() +
    downloadButton(ctx) +
    '</div>' +
    '<div class="pv-body"><div class="image-stage">' +
    '<div class="image-rotor"><img alt="" draggable="false"></div>' +
    '</div></div>';

  const stage = container.querySelector('.image-stage');
  // ★ 旋转用的外层占位容器：图片元素本身只负责「画」，旋转后的**占位尺寸**
  //   由它承担（详见 apply() 的说明）。
  const rotor = stage.querySelector('.image-rotor');
  const img = stage.querySelector('img');
  const zoomLabel = container.querySelector('.zoom-label');
  const dimLabel = container.querySelector('.dim-label');

  let scale = 1;
  let rotation = 0;
  let fitMode = true;
  let naturalW = 0;
  let naturalH = 0;

  /** 旋转 90°/270° 之后，图片在屏幕上占据的宽高（即「显示尺寸」） */
  function displaySize() {
    const swap = Math.abs(rotation % 180) === 90;
    return {
      w: swap ? naturalH : naturalW,
      h: swap ? naturalW : naturalH
    };
  }

  function updateLabels() {
    if (!naturalW || !naturalH) {
      return;
    }
    const swapped = Math.abs(rotation % 180) === 90;
    dimLabel.textContent = swapped
      // ★ 旋转后把「显示尺寸」也写出来：只写文件本身的尺寸会让人以为
      //   旋转没生效（400×200 的图转 90° 后文件仍是 400×200，
      //   但屏幕上应该是 200×400）。
      ? naturalW + ' × ' + naturalH + ' 像素（显示 ' + naturalH + ' × ' + naturalW + '）'
      : naturalW + ' × ' + naturalH + ' 像素';
    zoomLabel.textContent = Math.round(scale * 100) + '%';
  }

  /**
   * 把当前 scale / rotation 反映到 DOM 上。
   *
   * ★ 这里曾经有一个会让图片「严重畸变」的 bug，改法值得记下来：
   *
   *   原实现把**旋转后的尺寸**（400×200 转 90° 就是 200×400）直接写成了
   *   `<img>` 的 width/height，同时又给它加 `transform: rotate(90deg)`。
   *   于是同一件事被做了两遍：
   *     1. img 元素被强行拉成 200×400 —— 而图片内容的原始比例是 2:1，
   *        填进 1:2 的框里就是**横向压扁、纵向拉长**（用户看到的「畸变严重」）；
   *     2. 这个已经变形的框再被 transform 转 90°，屏幕上又回到 400×200。
   *   所以用户看到的现象正是「点了旋转，还是长 400 宽 200，而且严重变形」。
   *
   *   正确做法是把两件事分开：
   *     * `<img>` 永远保持**原始**宽高 × scale（内容绝不被拉伸），
   *       旋转只由 transform 负责（transform 不改变布局盒子）；
   *     * 旋转后的占位尺寸交给外层 .image-rotor —— 它决定滚动区域与居中，
   *       所以大图旋转后仍然能滚到每一个角。
   */
  function apply() {
    const size = displaySize();
    if (!size.w || !size.h) {
      return;
    }
    const w = Math.max(1, Math.round(naturalW * scale));
    const h = Math.max(1, Math.round(naturalH * scale));

    img.style.width = w + 'px';
    img.style.height = h + 'px';
    // translate 先把图片中心挪到 rotor 中心，再绕自身中心旋转
    img.style.transform = 'translate(-50%, -50%) rotate(' + rotation + 'deg)';

    rotor.style.width = Math.max(1, Math.round(size.w * scale)) + 'px';
    rotor.style.height = Math.max(1, Math.round(size.h * scale)) + 'px';

    updateLabels();
  }

  function fit() {
    const size = displaySize();
    if (!size.w || !size.h) {
      return;
    }
    const boxW = Math.max(60, stage.clientWidth - 36);
    const boxH = Math.max(60, stage.clientHeight - 36);
    scale = Math.min(boxW / size.w, boxH / size.h);
    // 小图不要放大，保持 100%
    scale = Math.min(scale, 1);
    if (scale < 0.02) {
      scale = 0.02;
    }
    fitMode = true;
    apply();
  }

  function zoomBy(factor) {
    fitMode = false;
    scale = Math.max(0.02, Math.min(16, scale * factor));
    apply();
  }

  function setActual() {
    fitMode = false;
    scale = 1;
    apply();
  }

  container.querySelector('[data-act="zoom-in"]').addEventListener('click', function () { zoomBy(1.25); });
  container.querySelector('[data-act="zoom-out"]').addEventListener('click', function () { zoomBy(1 / 1.25); });
  container.querySelector('[data-act="fit"]').addEventListener('click', fit);
  container.querySelector('[data-act="actual"]').addEventListener('click', setActual);
  container.querySelector('[data-act="rotate-left"]').addEventListener('click', function () {
    rotation = (rotation + 270) % 360;
    if (fitMode) { fit(); } else { apply(); }
  });
  container.querySelector('[data-act="rotate-right"]').addEventListener('click', function () {
    rotation = (rotation + 90) % 360;
    if (fitMode) { fit(); } else { apply(); }
  });
  bindDownload(ctx, container.querySelector('[data-act="download"]'));

  // 滚轮缩放
  stage.addEventListener('wheel', function (e) {
    if (!e.ctrlKey && !e.metaKey && Math.abs(e.deltaY) < 2) {
      return;
    }
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? 1.15 : 1 / 1.15);
  }, { passive: false });

  // 双击在「适应窗口」与「100%」之间切换
  stage.addEventListener('dblclick', function () {
    if (fitMode) { setActual(); } else { fit(); }
  });

  // 按住拖动平移（利用滚动容器）
  let dragging = false;
  let startX = 0;
  let startY = 0;
  let startScrollLeft = 0;
  let startScrollTop = 0;

  stage.addEventListener('mousedown', function (e) {
    if (e.button !== 0) {
      return;
    }
    dragging = true;
    startX = e.clientX;
    startY = e.clientY;
    startScrollLeft = stage.scrollLeft;
    startScrollTop = stage.scrollTop;
    stage.classList.add('grabbing');
    e.preventDefault();
  });

  window.addEventListener('mousemove', function (e) {
    if (!dragging) {
      return;
    }
    stage.scrollLeft = startScrollLeft - (e.clientX - startX);
    stage.scrollTop = startScrollTop - (e.clientY - startY);
  });

  window.addEventListener('mouseup', function () {
    if (dragging) {
      dragging = false;
      stage.classList.remove('grabbing');
    }
  });

  img.addEventListener('load', function () {
    naturalW = img.naturalWidth;
    naturalH = img.naturalHeight;
    // 尺寸标签由 apply() -> updateLabels() 统一维护（旋转时要一起变），
    // 这里不再单独写，免得两处不一致
    fit();
  });

  img.addEventListener('error', function () {
    container.querySelector('.pv-body').innerHTML = '';
    messageBox(container.querySelector('.pv-body'), 'error',
      '图片加载失败，文件可能已损坏或格式不被浏览器支持。',
      [{ label: '下载文件', iconName: 'download', onClick: function () { api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel)); } }]);
  });

  img.src = api.rawUrl(ctx.rootId, ctx.rel);
}

/* ===========================================================================
   PDF 阅读器（pdf.js）
   =========================================================================== */

let pdfLibPromise = null;

const PDF_VENDOR = '/static/vendor/pdf/';

/** 用 <script> 注入传统脚本（UMD 版 pdf.js 走这条路） */
function loadScriptTag(src) {
  return new Promise(function (resolve, reject) {
    const el = document.createElement('script');
    el.src = src;
    el.onload = function () { resolve(); };
    el.onerror = function () { reject(new Error('无法加载 ' + src)); };
    document.head.appendChild(el);
  });
}

/**
 * 这个环境有没有「浏览器内置 PDF 阅读器」？
 *
 * ★ 桌面 Chrome/Edge/Firefox/Safari 有；**安卓一律没有** ——
 *   Android WebView 与安卓版 Chrome 都不内嵌 PDF 渲染，
 *   所以 <iframe src="xxx.pdf"> 在平板上永远是**一块黑屏**。
 *   这正是「平板打开 PDF 黑屏」的直接原因，所以这条兜底路在安卓上必须关掉。
 */
function hasNativePdfViewer() {
  const ua = (navigator && navigator.userAgent) || '';
  return !/Android/i.test(ua);
}

/**
 * 懒加载本地 pdf.js（首次打开 PDF 时才下载，避免拖慢桌面启动）。
 *
 * ★ 为什么优先用 UMD 的 pdf.min.js、而不是 ESM 的 pdf.min.mjs：
 *   pdf.js 6.x 用到了 `Promise.withResolvers()`（Chrome/WebView 119+）这类
 *   很新的 API，而安卓平板的系统 WebView 通常远低于此（Android 13 出厂约 108）。
 *   这种情况下 .mjs **仍然能 import 成功**（下面只检查 getDocument 在不在），
 *   真正的失败发生在 getDocument 那一刻：
 *       TypeError: Promise.withResolvers is not a function
 *   → 掉进 catch → 退回「浏览器内置阅读器」iframe → 安卓没有内置 PDF 渲染器
 *   → 用户看到的就是一块黑。很难查，因为控制台只留一行 console.info。
 *
 *   v3.11.174 是纯 JS（不需要 wasm，也不用那些新 API），在很老的 WebView 上
 *   照样能跑；实测在**人为删掉那些新 API** 的环境里依旧渲染成功，
 *   而在同样环境下 6.3.289 必抛 `Promise.withResolvers is not a function`。
 *
 *   .mjs 分支保留：将来若有人重新装回 v4+，这里仍然能用。
 */
function loadPdfLib() {
  if (!pdfLibPromise) {
    pdfLibPromise = (async function () {
      try {
        await loadScriptTag(PDF_VENDOR + 'pdf.min.js');
        const lib = window.pdfjsLib;
        if (!lib || typeof lib.getDocument !== 'function') {
          throw new Error('pdf.min.js 没有导出 pdfjsLib');
        }
        lib.GlobalWorkerOptions.workerSrc = PDF_VENDOR + 'pdf.worker.min.js';
        return lib;
      } catch (err) {
        // 没有 .js 就退回 ESM 版（pdf.js v4+ 的文件命名）
        const mod = await import(PDF_VENDOR + 'pdf.min.mjs');
        const lib = (mod && typeof mod.getDocument === 'function') ? mod : (mod.default || mod);
        if (!lib || typeof lib.getDocument !== 'function') {
          throw new Error('pdf.js 模块导出异常');
        }
        lib.GlobalWorkerOptions.workerSrc = PDF_VENDOR + 'pdf.worker.min.mjs';
        return lib;
      }
    })();
  }
  return pdfLibPromise;
}

/**
 * 构建 PDF 阅读器。
 *
 * @param {HTMLElement} container 预览容器
 * @param {object} ctx  上下文
 * @param {object} options {url, title}
 */
function buildPdf(container, ctx, options) {
  const opts = options || {};
  const url = opts.url || api.rawUrl(ctx.rootId, ctx.rel);

  container.innerHTML =
    '<div class="preview-toolbar">' +
    toolBtn('prev', 'chevron-up', '上一页') +
    '<input type="text" class="pv-btn page-input" style="width:52px;text-align:center;padding:0 4px" value="1">' +
    '<span class="pv-label">/ <span class="page-total">-</span></span>' +
    toolBtn('next', 'chevron-down', '下一页') +
    separator() +
    toolBtn('zoom-out', 'zoom-out', '缩小') +
    '<span class="pv-label zoom-label">100%</span>' +
    toolBtn('zoom-in', 'zoom-in', '放大') +
    toolBtn('fit-width', 'fit', '适应宽度') +
    '<span class="spacer"></span>' +
    downloadButton(ctx) +
    '</div>' +
    '<div class="pv-body"><div class="pdf-scroll"></div>' +
    '<div class="pdf-loading"><div class="spinner"></div><div class="pdf-load-text">正在加载 PDF…</div>' +
    '<div class="pdf-progress"><i></i></div></div></div>';

  const bodyEl = container.querySelector('.pv-body');
  const scrollEl = container.querySelector('.pdf-scroll');
  const loadingEl = container.querySelector('.pdf-loading');
  const loadText = container.querySelector('.pdf-load-text');
  const progressBar = container.querySelector('.pdf-progress i');
  const pageInput = container.querySelector('.page-input');
  const pageTotal = container.querySelector('.page-total');
  const zoomLabel = container.querySelector('.zoom-label');

  bindDownload(ctx, container.querySelector('[data-act="download"]'));

  let doc = null;
  let pages = [];
  let scale = 1;
  let fitWidth = true;
  let observer = null;
  let nativeUsed = false;
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  // pdf.js 迟迟没就绪就切到浏览器内置阅读器的等待上限（毫秒）
  const WATCHDOG_MS = 8000;

  function showError(text) {
    loadingEl.style.display = 'none';
    scrollEl.innerHTML = '';
    messageBox(scrollEl, 'error', text, [
      { label: '下载文件', iconName: 'download', onClick: function () { api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel)); } },
      { label: '用浏览器内置阅读器打开', iconName: 'external', onClick: function () { window.open(url, '_blank', 'noopener'); } }
    ]);
  }

  /**
   * 兜底方案：改用浏览器内置的 PDF 阅读器（iframe 内嵌）。
   *
   * 为什么需要它：pdf.js 依赖 Worker 往返与 requestAnimationFrame，
   * 在个别环境（浏览器版本差异、Worker 受限、渲染时序异常）下可能迟迟不返回。
   * 与其让用户对着"正在加载"发呆，不如果断切到浏览器自带引擎 ——
   * Chrome/Edge 内置的阅读器同样支持翻页与缩放，且几乎不可能失败。
   */
  function useNativeViewer(reason) {
    if (nativeUsed) {
      return;
    }
    // 安卓没有内置阅读器：塞 iframe 只会得到一块黑屏。
    // 与其让用户对着黑屏猜，不如直接把原因和出路讲清楚。
    if (!hasNativePdfViewer()) {
      useUnavailableFallback(reason);
      return;
    }
    nativeUsed = true;

    container.innerHTML =
      '<div class="preview-toolbar">' +
      '<span class="pv-label">浏览器内置 PDF 阅读器</span>' +
      '<span class="spacer"></span>' +
      toolBtn('newtab', 'external', '在新标签页打开') +
      downloadButton(ctx) +
      '</div>' +
      '<div class="pv-body" style="background:#525659">' +
      '<iframe class="pdf-native" title="PDF 预览" src="' +
      ui.escapeHtml(url) + '#view=FitH"></iframe>' +
      '</div>';

    bindDownload(ctx, container.querySelector('[data-act="download"]'));

    const newTab = container.querySelector('[data-act="newtab"]');
    if (newTab) {
      newTab.addEventListener('click', function () {
        window.open(url, '_blank', 'noopener');
      });
    }

    if (reason) {
      console.info('[pdf] 已切换到浏览器内置阅读器：' + reason);
    }
  }

  /**
   * 没有内置阅读器时的兜底：明确说明原因 + 给出出路，而不是留一块黑屏。
   *
   * 走到这里意味着同时满足两件事：
   *   1. 页面内置的渲染器（pdf.js）失败了；
   *   2. 这台设备没有浏览器内置的 PDF 阅读器 —— 典型就是安卓 WebView。
   * 此前这种情况会塞一个 iframe，结果是一块黑屏，用户完全不知道发生了什么
   * （原因只写在 console.info 里，看不到）。
   */
  function useUnavailableFallback(reason) {
    if (nativeUsed) {
      return;
    }
    nativeUsed = true;
    loadingEl.style.display = 'none';
    scrollEl.innerHTML = '';
    messageBox(
      scrollEl,
      'error',
      '这台设备没有内置的 PDF 阅读器（安卓的 WebView 与浏览器都不带），' +
      '而页面内置的渲染器也没能启动。\n\n' +
      '可以先把文件下载下来，用别的应用打开。' +
      (reason ? '\n\n技术原因：' + reason : ''),
      [
        { label: '下载文件', iconName: 'download', onClick: function () {
            api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel));
        } }
      ]
    );
  }

  async function renderPage(info) {
    if (info.rendering || info.renderedScale === scale) {
      return;
    }
    info.rendering = true;
    try {
      const viewport = info.page.getViewport({ scale: scale * dpr });
      info.canvas.width = Math.floor(viewport.width);
      info.canvas.height = Math.floor(viewport.height);
      info.canvas.style.width = Math.floor(viewport.width / dpr) + 'px';
      info.canvas.style.height = Math.floor(viewport.height / dpr) + 'px';
      const context = info.canvas.getContext('2d', { alpha: false });

      // 同时传 canvas 与 canvasContext：
      //   * pdf.js v5 起改用 canvas 参数，旧版本用 canvasContext；
      //   * 两个都传可以同时兼容两种版本。
      // 之前只用 canvasContext 并靠同步 try/catch 兜底是行不通的 ——
      // 新版不会同步抛错，而是让返回的 task.promise 异步 reject，
      // 结果就是「页面框排版正常、但画布一片空白」。
      await info.page.render({
        canvas: info.canvas,
        canvasContext: context,
        viewport: viewport
      }).promise;
      info.renderedScale = scale;
    } catch (err) {
      // 不要静默吞掉：单页渲染失败要留下可排查的日志
      console.error('[pdf] 第 ' + info.num + ' 页渲染失败:', err);
    } finally {
      info.rendering = false;
    }
  }

  async function rebuild(keepScrollRatio) {
    const ratio = keepScrollRatio && scrollEl.scrollHeight
      ? scrollEl.scrollTop / scrollEl.scrollHeight
      : 0;

    if (observer) {
      observer.disconnect();
      observer = null;
    }
    scrollEl.innerHTML = '';
    pages = [];

    for (let i = 1; i <= doc.numPages; i++) {
      const page = await doc.getPage(i);
      const viewport = page.getViewport({ scale: scale });
      const wrap = document.createElement('div');
      wrap.className = 'pdf-page';
      wrap.dataset.page = String(i);
      wrap.style.width = Math.floor(viewport.width) + 'px';
      wrap.style.height = Math.floor(viewport.height) + 'px';
      const canvas = document.createElement('canvas');
      wrap.appendChild(canvas);
      scrollEl.appendChild(wrap);
      pages.push({ num: i, page: page, wrap: wrap, canvas: canvas, renderedScale: 0, rendering: false });
    }

    observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (entry.isIntersecting) {
          const info = pages[Number(entry.target.dataset.page) - 1];
          if (info) {
            renderPage(info);
          }
        }
      });
    }, { root: scrollEl, rootMargin: '500px 0px' });

    pages.forEach(function (info) {
      observer.observe(info.wrap);
    });

    if (ratio) {
      requestAnimationFrame(function () {
        scrollEl.scrollTop = ratio * scrollEl.scrollHeight;
      });
    }

    updateCurrentPage();
  }

  function updateCurrentPage() {
    if (!pages.length) {
      return;
    }
    const mid = scrollEl.scrollTop + scrollEl.clientHeight / 2;
    let current = 1;
    for (let i = 0; i < pages.length; i++) {
      if (pages[i].wrap.offsetTop <= mid) {
        current = i + 1;
      } else {
        break;
      }
    }
    pageInput.value = String(current);
  }

  function applyScale(newScale, refit) {
    const clamped = Math.max(0.2, Math.min(6, newScale));
    if (Math.abs(clamped - scale) < 0.001 && !refit) {
      return;
    }
    scale = clamped;
    zoomLabel.textContent = Math.round(scale * 100) + '%';
    rebuild(true);
  }

  function computeFitWidthScale() {
    if (!pages.length) {
      return 1;
    }
    const pageWidth = pages[0].page.getViewport({ scale: 1 }).width;
    const available = Math.max(200, scrollEl.clientWidth - 36);
    return available / pageWidth;
  }

  // 工具栏
  container.querySelector('[data-act="prev"]').addEventListener('click', function () {
    const index = Math.max(1, Number(pageInput.value) - 1);
    scrollToPage(index);
  });
  container.querySelector('[data-act="next"]').addEventListener('click', function () {
    const index = Math.min(pages.length, Number(pageInput.value) + 1);
    scrollToPage(index);
  });
  container.querySelector('[data-act="zoom-in"]').addEventListener('click', function () {
    fitWidth = false;
    applyScale(scale * 1.2);
  });
  container.querySelector('[data-act="zoom-out"]').addEventListener('click', function () {
    fitWidth = false;
    applyScale(scale / 1.2);
  });
  container.querySelector('[data-act="fit-width"]').addEventListener('click', function () {
    fitWidth = true;
    applyScale(computeFitWidthScale(), true);
  });

  pageInput.addEventListener('keydown', function (e) {
    e.stopPropagation();
    if (e.key === 'Enter') {
      e.preventDefault();
      scrollToPage(Number(pageInput.value) || 1);
      pageInput.blur();
    }
  });

  function scrollToPage(num) {
    const index = Math.max(1, Math.min(pages.length, num));
    const info = pages[index - 1];
    if (info) {
      scrollEl.scrollTop = info.wrap.offsetTop - 8;
      pageInput.value = String(index);
      renderPage(info);
    }
  }

  scrollEl.addEventListener('scroll', function () {
    updateCurrentPage();
  });

  // 窗口尺寸变化时，如果处于「适应宽度」模式则重新排版
  let resizeTimer = null;
  window.addEventListener('resize', function () {
    if (!fitWidth || !doc) {
      return;
    }
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function () {
      applyScale(computeFitWidthScale(), true);
    }, 220);
  });

  // ---- 加载 ----
  // 看门狗：pdf.js 迟迟没就绪就改用浏览器内置阅读器，保证用户一定看得到内容。
  // 下载过程中每有进度就重新计时，这样大文件不会被误判成"卡死"。
  let watchdog = null;
  function armWatchdog() {
    clearTimeout(watchdog);
    watchdog = setTimeout(function () {
      useNativeViewer('pdf.js 未能在 ' + (WATCHDOG_MS / 1000) + ' 秒内就绪');
    }, WATCHDOG_MS);
  }
  armWatchdog();

  loadPdfLib().then(function (lib) {
    loadText.textContent = '正在下载 PDF 数据…';
    const task = lib.getDocument({
      url: url,
      withCredentials: true,
      // 中文 PDF 若使用未内嵌的 CID 字体，需要 cmaps 才能正确显示
      cMapUrl: '/static/vendor/pdf/cmaps/',
      cMapPacked: true,
      standardFontDataUrl: '/static/vendor/pdf/standard_fonts/'
    });

    task.onProgress = function (data) {
      if (data && data.total) {
        progressBar.style.width = Math.round((data.loaded / data.total) * 100) + '%';
      }
      armWatchdog();   // 只要有进展就重新计时
    };

    return task.promise;
  }).then(function (loaded) {
    clearTimeout(watchdog);
    if (nativeUsed) {
      return; // 看门狗已经切走了，这里不再插手
    }
    doc = loaded;
    pageTotal.textContent = String(doc.numPages);
    loadingEl.style.display = 'none';
    scale = computeFitWidthScale();
    zoomLabel.textContent = Math.round(scale * 100) + '%';
    return rebuild(false);
  }).catch(function (err) {
    clearTimeout(watchdog);
    // pdf.js 失败不再直接报错，而是退回浏览器内置阅读器，用户照样能看
    useNativeViewer('pdf.js 加载失败：' + ((err && err.message) || err));
  });
}

/* ===========================================================================
   文本查看器
   =========================================================================== */

/**
 * 已经打开的文本预览窗口：'root|rel' -> 重新读取的函数。
 *
 * 用途：编辑器保存成功后要让同样文件的那扇预览窗口刷一下。
 * 方向是 **editor -> preview**（preview 不 import editor），
 * 免得 preview ⇄ editor 相互 import 变成循环依赖。
 */
const openTextPreviews = new Map();

/**
 * 让某个文件已经开着的文本预览重新读一遍磁盘。
 *
 * 编辑器保存后调这里：不刷新的话用户会看到「编辑器里是新的、预览里是旧的」，
 * 很容易误以为保存没生效，然后再存一次。
 *
 * @param {string} rootId 根目录 id
 * @param {string} rel    相对路径
 * @returns {number} 实际刷新了几个预览窗口
 */
export function refreshTextPreviews(rootId, rel) {
  const key = String(rootId || '') + '|' + String(rel || '');
  const reload = openTextPreviews.get(key);
  if (typeof reload !== 'function') {
    return 0;
  }
  try {
    reload();
  } catch (err) {
    console.warn('[preview] 刷新文本预览失败:', (err && err.message) || err);
    return 0;
  }
  return 1;
}

/**
 * 构建文本预览。
 *
 * @param {object} container 预览容器
 * @param {object} ctx       上下文
 * @param {object} options   { readOnly } —— readOnly 为真时不显示「编辑」按钮
 */
function buildText(container, ctx, options) {
  const opts = options || {};
  // 只读根目录下的文件不给出编辑入口：保存会被后端 403 挡掉，
  // 与其让用户写完才被拒，不如一开始就不摆这个按钮
  const canEdit = opts.readOnly !== true;

  container.innerHTML =
    '<div class="preview-toolbar">' +
    toolBtn('wrap', 'wrap', '自动换行', '换行') +
    toolBtn('copy', 'copy', '复制全部内容', '复制') +
    (canEdit ? toolBtn('edit', 'pencil', '在编辑器窗口中打开', '编辑') : '') +
    '<span class="pv-label info-label">正在读取…</span>' +
    '<span class="spacer"></span>' +
    downloadButton(ctx) +
    '</div>' +
    '<div class="pv-body"><div class="text-view"><pre></pre></div></div>';

  const bodyEl = container.querySelector('.pv-body');
  const viewEl = container.querySelector('.text-view');
  const preEl = container.querySelector('pre');
  const infoLabel = container.querySelector('.info-label');

  bindDownload(ctx, container.querySelector('[data-act="download"]'));

  container.querySelector('[data-act="wrap"]').addEventListener('click', function () {
    viewEl.classList.toggle('wrap');
  });
  container.querySelector('[data-act="copy"]').addEventListener('click', function () {
    ui.copyText(preEl.textContent || '').then(function () {
      ui.toast('已复制全部内容', 'success');
    }).catch(function () {
      ui.toast('复制失败，请手动选择复制', 'error');
    });
  });

  const editBtn = container.querySelector('[data-act="edit"]');
  if (editBtn) {
    editBtn.addEventListener('click', function () {
      openEditor({
        desktop: ctx.desktop,
        rootId: ctx.rootId,
        rel: ctx.rel,
        name: ctx.name
      });
    });
  }

  /**
   * 读取并把结果画到界面上。
   *
   * @param {boolean} silent 重新读取时置真：失败就不弹错误块，
   *   只留在后台记一笔 —— 用户并没有主动要求刷新，不该因为
   *   一次后台刷新失败就把已经看得好好的内容换成一屏报错。
   */
  function reloadText(silent) {
    return api.textPreview(ctx.rootId, ctx.rel).then(function (res) {
      if (res.binary) {
        bodyEl.innerHTML = '';
        messageBox(bodyEl, 'file',
          res.message || '这看起来是二进制文件，无法以文本方式预览。',
          [{ label: '下载文件', iconName: 'download', onClick: function () { api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel)); } }]);
        infoLabel.textContent = '';
        return;
      }

      preEl.textContent = res.content || '';

      let info = res.encoding + ' · ' + res.line_count + ' 行 · ' + res.size_text;
      if (res.truncated) {
        info += ' · 已截断（仅显示前 ' + res.max_kb + 'KB）';
      }
      infoLabel.textContent = info;
    }).catch(function (err) {
      if (err && err.status === 401) {
        return;
      }
      if (silent) {
        console.warn('[preview] 后台刷新文本失败:', (err && err.message) || err);
        return;
      }
      bodyEl.innerHTML = '';
      messageBox(bodyEl, 'error', '读取文本失败：' + ((err && err.message) || '未知错误'),
        [{ label: '下载文件', iconName: 'download', onClick: function () { api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel)); } }]);
    });
  }

  // 登记「这扇窗口正在看这个文件」，编辑器保存后能顺着它找回来刷新
  openTextPreviews.set(ctx.rootId + '|' + ctx.rel, function () {
    reloadText(true);
  });

  reloadText(false);
}

/* ===========================================================================
   音视频播放器
   =========================================================================== */

function buildMedia(container, ctx, isVideo) {
  const url = api.rawUrl(ctx.rootId, ctx.rel);

  container.innerHTML =
    '<div class="preview-toolbar">' +
    '<span class="pv-label">' + (isVideo ? '视频播放' : '音频播放') + '</span>' +
    '<span class="spacer"></span>' +
    downloadButton(ctx) +
    '</div>' +
    '<div class="pv-body" style="background:#101214">' +
    '<div class="media-stage">' +
    (isVideo
      ? '<video controls preload="metadata" playsinline></video>'
      : '<audio controls preload="metadata"></audio>') +
    '<div class="media-info">' +
    '<div class="mi-name">' + ui.escapeHtml(ctx.name) + '</div>' +
    '<div>' + ui.escapeHtml(ctx.sizeText || '') + ' · ' + ui.escapeHtml(ctx.rel) + '</div>' +
    '</div>' +
    '</div></div>';

  bindDownload(ctx, container.querySelector('[data-act="download"]'));

  const player = container.querySelector('video, audio');
  const infoEl = container.querySelector('.media-info');

  player.src = url;

  player.addEventListener('loadedmetadata', function () {
    const duration = isFinite(player.duration) ? formatDuration(player.duration) : '未知';
    const extra = document.createElement('div');
    extra.textContent = '时长 ' + duration +
      (player.videoWidth ? ' · ' + player.videoWidth + ' × ' + player.videoHeight : '');
    infoEl.appendChild(extra);
  });

  player.addEventListener('error', function () {
    const stage = container.querySelector('.media-stage');
    player.remove();
    const fallback = document.createElement('div');
    fallback.className = 'media-fallback';
    fallback.innerHTML =
      '<div style="font-size:32px;margin-bottom:10px">⚠️</div>' +
      '<div>浏览器无法播放这个文件。</div>' +
      '<div style="margin-top:8px;opacity:.8">常见原因是编码不被支持（例如 MKV 容器、H.265 编码），' +
      '或者文件本身不是真正的媒体文件。<br>可以下载到本地用专业播放器打开。</div>';
    stage.insertBefore(fallback, stage.firstChild);

    const actions = document.createElement('div');
    actions.className = 'ar-actions';
    const btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'pv-btn';
    btn.innerHTML = icon('download') + '<span>下载文件</span>';
    btn.addEventListener('click', function () {
      api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel));
    });
    actions.appendChild(btn);
    stage.appendChild(actions);
  });
}

function formatDuration(seconds) {
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = function (n) { return (n < 10 ? '0' : '') + n; };
  return h > 0 ? (h + ':' + pad(m) + ':' + pad(s)) : (m + ':' + pad(s));
}

/* ===========================================================================
   Office 文档
   =========================================================================== */

function buildOffice(container, ctx) {
  container.innerHTML = '<div class="pv-body"><div class="pv-message">' +
    icon('file-text') + '<div class="pv-msg-text">正在准备预览…</div></div></div>';

  const bodyEl = container.querySelector('.pv-body');

  api.officePreview(ctx.rootId, ctx.rel).then(function (res) {
    if (res.mode === 'pdf' && res.pdf_url) {
      // 有 LibreOffice：直接复用 PDF 阅读器
      buildPdf(container, ctx, { url: res.pdf_url });
      return;
    }

    if (res.mode === 'html' && res.html) {
      container.innerHTML =
        '<div class="preview-toolbar">' +
        '<span class="pv-label">内容预览</span>' +
        '<span class="spacer"></span>' +
        downloadButton(ctx) +
        '</div>' +
        '<div class="pv-body"><div class="office-view">' +
        (res.message ? '<div class="office-note">' + ui.escapeHtml(res.message) + '</div>' : '') +
        // 注意：这段 HTML 由后端 office.py 生成，其中所有文档文字都已做转义
        res.html +
        '</div></div>';
      bindDownload(ctx, container.querySelector('[data-act="download"]'));
      return;
    }

    // 不支持预览：给出友好提示
    container.innerHTML =
      '<div class="preview-toolbar">' +
      '<span class="pv-label">无法预览</span>' +
      '<span class="spacer"></span>' +
      downloadButton(ctx) +
      '</div>' +
      '<div class="pv-body"></div>';

    const actions = [
      { label: '下载文件', iconName: 'download', onClick: function () { api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel)); } }
    ];

    if (res.libreoffice === false) {
      actions.push({
        label: '重新检测 LibreOffice',
        iconName: 'refresh',
        onClick: function () {
          ui.setBusy(true, '正在重新检测…');
          api.rescanSoffice().then(function (r) {
            ui.setBusy(false);
            ui.toast(r.message, r.libreoffice ? 'success' : 'warn');
          }).catch(function (err) {
            ui.setBusy(false);
            ui.toast('检测失败：' + ((err && err.message) || ''), 'error');
          });
        }
      });
    }

    messageBox(container.querySelector('.pv-body'), 'warning',
      res.message || '暂不支持预览该文档。', actions);
    bindDownload(ctx, container.querySelector('[data-act="download"]'));
  }).catch(function (err) {
    if (err && err.status === 401) {
      return;
    }
    bodyEl.innerHTML = '';
    messageBox(bodyEl, 'error', '预览失败：' + ((err && err.message) || '未知错误'),
      [{ label: '下载文件', iconName: 'download', onClick: function () { api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel)); } }]);
  });
}

/* ===========================================================================
   压缩包 / 其它类型
   =========================================================================== */

function buildSimple(container, ctx, title, text, iconName) {
  container.innerHTML =
    '<div class="preview-toolbar">' +
    '<span class="pv-label">' + ui.escapeHtml(title) + '</span>' +
    '<span class="spacer"></span>' +
    downloadButton(ctx) +
    '</div>' +
    '<div class="pv-body" style="background:#fff">' +
    '<div class="archive-view">' +
    '<div class="ar-icon">' + icon(iconName) + '</div>' +
    '<div class="ar-name">' + ui.escapeHtml(ctx.name) + '</div>' +
    '<div class="ar-meta">' + ui.escapeHtml(text) + '<br>' +
    ui.escapeHtml(ctx.rel) + '</div>' +
    '<div class="ar-actions"></div>' +
    '</div></div>';

  bindDownload(ctx, container.querySelector('[data-act="download"]'));

  const actions = container.querySelector('.ar-actions');
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'pv-btn';
  btn.innerHTML = icon('download') + '<span>下载文件</span>';
  btn.addEventListener('click', function () {
    api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel));
  });
  actions.appendChild(btn);
}

/**
 * 压缩包内容浏览。
 *
 * 早先这里只有一句「压缩包不支持在线解压预览」，但「看里面有什么」这件事
 * 本来就不需要解压：服务端的 archive.list_entries 是解压流程的第一道安全
 * 校验（逐条目查名字、识别加密包、修 GBK 乱码），把它的结果直接给前端就够了。
 * 所以现在打开压缩包能看到目录表，不必「先解压到磁盘、看完再删掉」。
 *
 * 仍然是**只读**：解压动作留在资源管理器的右键菜单里，这里只浏览 + 下载。
 */
function buildArchive(container, ctx) {
  container.innerHTML =
    '<div class="preview-toolbar">' +
    '<span class="pv-label">' + ui.escapeHtml(ctx.name) + '</span>' +
    '<span class="pv-meta" data-role="summary"></span>' +
    '<span class="spacer"></span>' +
    downloadButton(ctx) +
    '</div>' +
    '<div class="pv-body arc-body">' +
    '<div class="arc-empty">正在读取压缩包内容…</div>' +
    '</div>';

  bindDownload(ctx, container.querySelector('[data-act="download"]'));

  const body = container.querySelector('.pv-body');
  const summary = container.querySelector('[data-role="summary"]');

  api.archiveListing(ctx.rootId, ctx.rel).then(function (data) {
    const entries = (data && data.entries) || [];

    summary.textContent = String(data.format || '').toUpperCase() +
      ' · ' + (data.entry_count || 0) + ' 项' +
      (data.total_bytes ? ' · 展开后 ' + (data.total_text || '') : '');

    let html = '';

    // 解压遇到同名顶层项是**整体中止**的（见服务端 extract），
    // 所以这里提前把冲突摆出来，别等用户点了「解压」才被拒。
    if (data.conflict_count) {
      const names = (data.conflicts || []).slice(0, 5).join('、');
      html += '<div class="arc-warn">当前文件夹里已有同名项（' +
        ui.escapeHtml(names) +
        (data.conflict_count > 5 ? ' 等 ' + data.conflict_count + ' 项' : '') +
        '），直接解压会被整体拒绝 —— 请先移走/改名，或解压到新建的子文件夹。</div>';
    }

    if (!entries.length) {
      html += '<div class="arc-empty">这个压缩包里没有条目。</div>';
    } else {
      html += '<table class="arc-table"><thead><tr>' +
        '<th>名称</th><th class="num">原始大小</th><th>类型</th>' +
        '</tr></thead><tbody>';

      entries.forEach(function (item) {
        const kindText = item.is_dir
          ? '文件夹'
          : (item.is_link ? '链接（解压时跳过）' : '文件');

        html += '<tr' + (item.is_link ? ' class="is-link"' : '') + '>' +
          '<td class="arc-name" title="' + ui.escapeHtml(item.name) + '">' +
          icon(item.is_dir ? 'folder' : 'file', 'arc-ico') +
          '<span>' + ui.escapeHtml(item.name) + '</span></td>' +
          '<td class="num">' +
          (item.is_dir ? '' : ui.formatSize(item.size || 0)) + '</td>' +
          '<td>' + ui.escapeHtml(kindText) + '</td>' +
          '</tr>';
      });

      html += '</tbody></table>';

      if (data.entry_count > entries.length) {
        html += '<div class="arc-empty">共 ' + data.entry_count +
          ' 项，这里只显示了前 ' + entries.length + ' 项。</div>';
      }
    }

    body.innerHTML = html;
  }).catch(function (err) {
    // 加密包 / 损坏包 / 没装 UnRAR 都会落到这里，服务端给的是可读原因
    messageBox(body, 'error', (err && err.message) || '读取压缩包失败',
      [{
        label: '下载文件', iconName: 'download',
        onClick: function () {
          api.triggerDownload(api.downloadUrl(ctx.rootId, ctx.rel));
        }
      }]);
  });
}

/* ===========================================================================
   对外入口
   =========================================================================== */
const WINDOW_SPECS = {
  image: { width: 1000, height: 700, iconName: 'image' },
  pdf: { width: 1020, height: 780, iconName: 'pdf' },
  text: { width: 940, height: 660, iconName: 'code' },
  video: { width: 960, height: 660, iconName: 'video' },
  audio: { width: 720, height: 480, iconName: 'audio' },
  office: { width: 1020, height: 780, iconName: 'document' },
  archive: { width: 940, height: 640, iconName: 'archive' },
  other: { width: 620, height: 460, iconName: 'file' }
};

/**
 * 打开预览窗口。
 *
 * @param {object} ctx {rootId, rel, name, sizeText, entry, desktop, explorer}
 * @returns {object} 窗口记录
 */
export function openPreview(ctx) {
  const kind = detectKind(ctx.name);
  const spec = WINDOW_SPECS[kind] || WINDOW_SPECS.other;

  const container = document.createElement('div');
  // 关键：必须给容器加上 .preview（flex 纵向布局 + 撑满窗口高度），
  // 内部的 .pv-body 才能拿到确定高度、内容超出时才会出现滚动条。
  // 少这一行会导致长文本 / 长 PDF 被窗口裁掉、滚轮也滚不动。
  container.className = 'preview';
  const record = wm.create({
    title: ctx.name,
    iconName: spec.iconName,
    content: container,
    width: spec.width,
    height: spec.height,
    minWidth: 420,
    minHeight: 300,
    taskLabel: ctx.name
  });

  const localCtx = {
    rootId: ctx.rootId,
    rel: ctx.rel,
    name: ctx.name,
    sizeText: ctx.sizeText || '',
    // desktop 透传给编辑器：从预览里点「编辑」时要用它保持调用签名一致
    desktop: ctx.desktop || null
  };

  try {
    if (kind === 'image') {
      buildImage(container, localCtx);
    } else if (kind === 'pdf') {
      buildPdf(container, localCtx, {});
    } else if (kind === 'text') {
      // 只读根目录下的文本不给「编辑」入口，所以要把只读状态告诉文本查看器
      const readOnly = !!(ctx.explorer && ctx.explorer.readonly);
      buildText(container, localCtx, { readOnly: readOnly });
    } else if (kind === 'video') {
      buildMedia(container, localCtx, true);
    } else if (kind === 'audio') {
      buildMedia(container, localCtx, false);
    } else if (kind === 'office') {
      buildOffice(container, localCtx);
    } else if (kind === 'archive') {
      buildArchive(container, localCtx);
    } else {
      buildSimple(container, localCtx, '文件信息',
        '该类型文件不支持在线预览。' +
        (localCtx.sizeText ? '\n文件大小：' + localCtx.sizeText : ''), 'file');
    }
  } catch (err) {
    container.innerHTML = '<div class="pv-body"></div>';
    messageBox(container.querySelector('.pv-body'), 'error',
      '打开预览时出错：' + ((err && err.message) || err));
  }

  // 记下「这个窗口在看哪个文件」，供 sessionstate.js 在下次打开浏览器时重建。
  // 预览窗口本身没有可序列化的内部状态（滚动位置、缩放比例都不还原），
  // 所以只需要这份定位信息。
  if (record) {
    record.previewCtx = { rootId: localCtx.rootId, rel: localCtx.rel };

    // 窗口关掉后要把文本预览的登记摘掉，否则编辑器保存时会去刷新一扇
    // 已经不存在的窗口（回调本身是安全的，但没必要留着一份死引用）
    if (kind === 'text') {
      const key = localCtx.rootId + '|' + localCtx.rel;
      record.onClosed = function () {
        if (openTextPreviews.get(key)) {
          openTextPreviews.delete(key);
        }
      };
    }
  }

  return record;
}

export default openPreview;

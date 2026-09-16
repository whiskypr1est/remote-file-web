/* ==========================================================================
   照片（时间轴相册）
   --------------------------------------------------------------------------
   这个窗口解决的是「一堆照片按拍摄时间摊在时间轴上」这件事，以及随之而来的
   整理动作：改时间、改地点、打标签、按时间段归类。

   三个设计上的要点，值得先说清楚：

   1. **时间的三态必须在界面上体现出来。** 有效时间 = 用户改过 ?? EXIF 拍摄
      时间 ?? 文件修改时间。最后那一档是**下载/保存时间**，不是拍摄时间 ——
      微信图片、截图、从网上下载的图全部落在这一档。如果不把它们标出来，
      时间轴会看着挺满、其实一半的位置是错的。所以：
        * 低可信的照片在网格里带一个「待整理」角标；
        * 侧栏有专门的「待整理」入口，让这件事有明确的终点。

   2. **改的是相册里的记录，不是照片文件。** 编辑只写服务端的
      photos_state.json，原图一个字节都不动（写回 EXIF 要重编码 JPEG，
      画质会掉、还会丢厂商信息）。这一点在编辑面板里对用户明说，
      否则他会以为换了台电脑也能看到自己的修改。

   3. **批量操作要能撤销。** 把 137 张照片整体 +8 小时改错了，一张张改回来
      是不可接受的，所以工具栏常驻一个「撤销」。
   ========================================================================== */

import * as api from './api.js';
import * as ui from './ui.js';
import { icon } from './icons.js';
import { wm } from './wins.js';
/* 时间轴的纯逻辑（分组 / 排序 / 筛选）单独放在 phototime.js 里：
   那边不碰 DOM，所以能被 node 直接跑用例；这一层只管画界面与发请求。 */
import {
  LEVELS, SORTS, SOURCE_TEXT,
  groupPhotos, sortPhotos, filterPhotos, rangeOf, prettyTime
} from './phototime.js';

let openRecord = null;

/* --------------------------------------------------------------------------
   小工具
   -------------------------------------------------------------------------- */

/** 文件名去掉扩展名（黑框里当标题用） */
function stemOf(name) {
  const index = String(name || '').lastIndexOf('.');
  return index > 0 ? name.slice(0, index) : name;
}

/* --------------------------------------------------------------------------
   主应用
   -------------------------------------------------------------------------- */

class PhotosApp {
  constructor(desktop) {
    this.desktop = desktop;

    this.photos = [];
    this.albums = [];
    this.sources = [];
    this.prefs = { level: 'day', sort: 'taken_desc' };
    this.stats = {};
    this.limits = { thumb_size: 320 };

    /* 筛选状态：kind 决定用哪一套规则，value 是附加值（相册 id / 来源键） */
    this.filter = { kind: 'all', value: '' };
    this.query = '';

    this.selection = new Set();
    this.lastClicked = '';

    this.loading = false;

    this.root = document.createElement('div');
    this.root.className = 'ph-root photos';

    this._observer = null;
    this._onKeyDown = null;
    this._viewer = null;
    this._viewerIndex = -1;

    this._build();
  }

  /* ======================================================================
     骨架
     ====================================================================== */

  _build() {
    this.root.innerHTML =
      '<div class="ph-toolbar">' +
        '<button class="ph-btn primary" data-act="import">' + icon('upload') + '<span>导入照片</span></button>' +
        '<button class="ph-btn" data-act="rescan">' + icon('refresh') + '<span>重新扫描</span></button>' +
        '<button class="ph-btn" data-act="undo" title="撤销最近一次编辑（批量改时间改错了可以回退）">' + icon('repeat') + '<span>撤销</span></button>' +
        '<span class="ph-sep"></span>' +
        '<div class="ph-seg" data-role="level">' +
          LEVELS.map(function (item) {
            return '<button data-level="' + item.value + '">' + item.label + '</button>';
          }).join('') +
        '</div>' +
        '<select class="ph-select" data-role="sort">' +
          SORTS.map(function (item) {
            return '<option value="' + item.value + '">' + ui.escapeHtml(item.label) + '</option>';
          }).join('') +
        '</select>' +
        '<span class="ph-grow"></span>' +
        '<label class="ph-search">' + icon('search') +
          '<input type="text" data-role="search" placeholder="搜索文件名 / 地点 / 标签">' +
        '</label>' +
      '</div>' +
      '<div class="ph-body">' +
        '<div class="ph-side" data-role="side"></div>' +
        '<div class="ph-main" data-role="main"></div>' +
      '</div>' +
      '<div class="ph-batch" data-role="batch" hidden></div>' +
      '<div class="ph-modal-host" data-role="modal" hidden></div>';

    this.$toolbar = this.root.querySelector('.ph-toolbar');
    this.$side = this.root.querySelector('[data-role="side"]');
    this.$main = this.root.querySelector('[data-role="main"]');
    this.$batch = this.root.querySelector('[data-role="batch"]');
    this.$modal = this.root.querySelector('[data-role="modal"]');
    this.$search = this.root.querySelector('[data-role="search"]');
    this.$sort = this.root.querySelector('[data-role="sort"]');
    this.$level = this.root.querySelector('[data-role="level"]');

    this._bindToolbar();
  }

  _bindToolbar() {
    const self = this;

    this.$toolbar.addEventListener('click', function (event) {
      const button = event.target.closest('button');
      if (!button) {
        return;
      }
      const act = button.dataset.act;
      if (act === 'import') {
        self.openImportDialog();
      } else if (act === 'rescan') {
        self.doRescan();
      } else if (act === 'undo') {
        self.doUndo();
      } else if (button.dataset.level) {
        self.setLevel(button.dataset.level);
      }
    });

    this.$sort.addEventListener('change', function () {
      self.prefs.sort = self.$sort.value;
      self.render();
      api.photosSavePrefs({ sort: self.prefs.sort }).catch(function () { /* 偏好存不上不影响浏览 */ });
    });

    let timer = null;
    this.$search.addEventListener('input', function () {
      clearTimeout(timer);
      timer = setTimeout(function () {
        self.query = self.$search.value.trim().toLowerCase();
        self.render();
      }, 180);
    });

    /* 侧栏与主区的点击都用事件委托，重绘时不用重新绑定 */
    this.$side.addEventListener('click', function (event) {
      const item = event.target.closest('[data-nav]');
      if (item) {
        self.setFilter(item.dataset.nav, item.dataset.value || '');
        return;
      }
      const button = event.target.closest('button');
      if (button && button.dataset.act === 'new-album') {
        self.openAlbumDialog();
      } else if (button && button.dataset.act === 'drop-source') {
        self.dropSource(button.dataset.root, button.dataset.path);
      }
    });

    this.$main.addEventListener('click', this._onGridClick.bind(this));
    this.$main.addEventListener('dblclick', this._onGridDoubleClick.bind(this));
    this.$main.addEventListener('contextmenu', this._onGridContextMenu.bind(this));

    this.$batch.addEventListener('click', function (event) {
      const button = event.target.closest('button');
      if (!button) {
        return;
      }
      if (button.dataset.act === 'clear') {
        self.selection.clear();
        self.render();
      } else if (button.dataset.act === 'edit') {
        self.openBatchEdit();
      } else if (button.dataset.act === 'album') {
        self.addSelectionToAlbum();
      } else if (button.dataset.act === 'select-all') {
        self.visible.forEach(function (photo) { self.selection.add(photo.id); });
        self.render();
      }
    });
  }

  /* ======================================================================
     数据
     ====================================================================== */

  async load() {
    this.loading = true;
    this.renderStatus('正在读取照片库…');
    try {
      const data = await api.photosLibrary();
      this.photos = data.photos || [];
      this.albums = data.albums || [];
      this.sources = (data.library && data.library.sources) || [];
      this.stats = data.stats || {};
      this.limits = (data.library && data.library.limits) || { thumb_size: 320 };
      if (data.prefs) {
        this.prefs = {
          level: data.prefs.level || 'day',
          sort: data.prefs.sort || 'taken_desc'
        };
      }
      this.truncated = !!data.truncated;
      this.inaccessible = (data.library && data.library.inaccessible) || 0;

      /* 已经不在库里的 id 要从选择集里清掉，否则批量操作会带上幽灵 */
      const alive = new Set(this.photos.map(function (p) { return p.id; }));
      const self = this;
      Array.from(this.selection).forEach(function (id) {
        if (!alive.has(id)) {
          self.selection.delete(id);
        }
      });

      this.$sort.value = this.prefs.sort;

      /* ★ 必须先收掉 loading 再渲染。
         renderGrid 里有一道「加载中就先不画」的闸门，而这个 render() 是在
         同一个 try 里调的 —— 反过来写的话，网格永远是空的：
         侧栏数字、统计全都对，主区却一直停在「正在读取照片库…」。
         这个 bug 骗过了接口测试、静态闸门和纯逻辑用例，只有真的在浏览器里
         打开一次才看得见（第一版就是这样）。 */
      this.loading = false;
      this.render();
    } catch (err) {
      this.loading = false;
      this.renderStatus('读取照片库失败：' + ((err && err.message) || err), true);
    }
  }

  /* ======================================================================
     筛选 / 排序
     ====================================================================== */

  setFilter(kind, value) {
    this.filter = { kind: kind, value: value || '' };
    this.render();
  }

  setLevel(level) {
    if (this.prefs.level === level) {
      return;
    }
    this.prefs.level = level;
    this.render();
    api.photosSavePrefs({ level: level }).catch(function () { /* 同上 */ });
  }

  /** 按当前筛选条件算出要显示的照片（同时缓存到 this.visible，供范围选择用） */
  _computeVisible() {
    const list = filterPhotos(this.photos, {
      kind: this.filter.kind,
      value: this.filter.value,
      query: this.query,
      albums: this.albums
    });
    this.visible = sortPhotos(list, this.prefs.sort);
    return this.visible;
  }

  _computeGroups() {
    return groupPhotos(this.visible, this.prefs.level);
  }

  /* ======================================================================
     渲染
     ====================================================================== */

  render() {
    this._computeVisible();
    this.renderSide();
    this.renderGrid();
    this.renderBatchBar();
    this.renderLevelButtons();
  }

  renderLevelButtons() {
    const level = this.prefs.level;
    this.$level.querySelectorAll('button').forEach(function (button) {
      button.classList.toggle('on', button.dataset.level === level);
    });
  }

  renderStatus(text, isError) {
    this.$main.innerHTML = '<div class="ph-empty' + (isError ? ' error' : '') + '">' +
      ui.escapeHtml(text) + '</div>';
  }

  renderSide() {
    const self = this;
    const stats = this.stats || {};
    const unsorted = stats.file || 0;
    const missing = stats.missing || 0;

    function nav(key, value, label, count, extraClass) {
      const on = self.filter.kind === key && String(self.filter.value || '') === String(value || '');
      return '<div class="ph-nav' + (on ? ' on' : '') + (extraClass ? ' ' + extraClass : '') +
        '" data-nav="' + ui.escapeHtml(key) + '" data-value="' + ui.escapeHtml(value || '') + '">' +
        '<span class="ph-nav-label">' + ui.escapeHtml(label) + '</span>' +
        (count == null ? '' : '<span class="ph-nav-count">' + count + '</span>') +
        '</div>';
    }

    let html = '';
    html += '<div class="ph-side-group">';
    html += nav('all', '', '全部照片', stats.total || 0);
    html += nav('unsorted', '', '待整理（时间不可靠）', unsorted, unsorted ? 'warn' : '');
    if (missing) {
      html += nav('missing', '', '找不到文件', missing, 'warn');
    }
    html += '</div>';

    html += '<div class="ph-side-group">';
    html += '<div class="ph-side-title">相册' +
      '<button class="ph-mini" data-act="new-album" title="新建相册">' + icon('plus') + '</button></div>';
    if (!this.albums.length) {
      html += '<div class="ph-side-hint">还没有相册。可以选中照片后右键归类，或者按时间段自动归类。</div>';
    }
    this.albums.forEach(function (album) {
      const label = (album.kind === 'smart' ? '⏱ ' : '') + album.name;
      html += nav('album', album.id, label, album.kind === 'smart' ? null : (album.items || []).length);
    });
    html += '</div>';

    if (this.sources.length) {
      html += '<div class="ph-side-group">';
      html += '<div class="ph-side-title">已纳入的目录</div>';
      this.sources.forEach(function (source) {
        const name = source.path || '（根目录）';
        html += '<div class="ph-source">' +
          nav('source', source.root + '\u0000' + source.path, name, null) +
          '<button class="ph-mini danger" data-act="drop-source" data-root="' +
            ui.escapeHtml(source.root) + '" data-path="' + ui.escapeHtml(source.path) +
            '" title="从相册移出（不会删除照片文件）">' + icon('close') + '</button>' +
          '</div>';
      });
      html += '</div>';
    }

    this.$side.innerHTML = html;
  }

  renderGrid() {
    if (this.loading) {
      return;
    }
    if (!this.photos.length) {
      this.renderStatus('照片库还是空的。点左上角的「导入照片」，挑一个文件夹开始 —— ' +
        '照片不会被复制，只是就地记进相册。');
      return;
    }
    if (!this.visible.length) {
      this.renderStatus('当前筛选下没有照片。');
      return;
    }

    const self = this;
    const groups = this._computeGroups();
    const box = this.limits.thumb_size || 320;

    let html = '<div class="ph-timeline">';
    groups.forEach(function (group) {
      html += '<section class="ph-group" data-group="' + ui.escapeHtml(group.key) + '">';
      html += '<header class="ph-group-head">' +
        '<span class="ph-group-title">' + ui.escapeHtml(group.title) + '</span>' +
        (group.sub ? '<span class="ph-group-sub">' + ui.escapeHtml(group.sub) + '</span>' : '') +
        '<span class="ph-group-count">' + group.photos.length + ' 张</span>' +
        '</header>';
      html += '<div class="ph-grid">';
      group.photos.forEach(function (photo) {
        html += self._cellHtml(photo, box);
      });
      html += '</div></section>';
    });
    html += '</div>';

    this.$main.innerHTML = html;
    this._observeImages();
    this._refreshSelectionClasses();
  }

  _cellHtml(photo, box) {
    const classes = ['ph-cell'];
    if (photo.missing) {
      classes.push('missing');
    }
    if (photo.source === 'file' && !photo.missing) {
      classes.push('unsorted');
    }
    if (photo.edited) {
      classes.push('edited');
    }

    /* ★ 低可信时间的角标：不标出来的话，时间轴看着挺满、其实一半位置是错的 */
    let badges = '';
    if (photo.missing) {
      badges += '<span class="ph-badge missing" title="文件已经不在了（移动硬盘没插？）">找不到</span>';
    } else if (photo.source === 'file') {
      badges += '<span class="ph-badge warn" title="时间取自文件修改时间，不是拍摄时间 —— 点开可以改">待整理</span>';
    }
    if (photo.edited) {
      badges += '<span class="ph-badge edited" title="这张的时间或地点被改过">已改</span>';
    }

    const place = (photo.place && photo.place.name) || '';
    const title = photo.name + '\n' + prettyTime(photo.taken_at) + '\n' + (place || photo.relpath);

    return '<figure class="' + classes.join(' ') + '" data-id="' + photo.id + '" title="' +
      ui.escapeHtml(title) + '">' +
      '<div class="ph-thumb">' +
        '<img alt="" data-src="' + ui.escapeHtml(api.photoThumbUrl(photo.id, box, Math.round(photo.mtime || 0))) + '">' +
        badges +
      '</div>' +
      '<figcaption class="ph-cap">' +
        '<span class="ph-cap-time">' + ui.escapeHtml(photo.taken_at ? photo.taken_at.slice(11, 16) : '--:--') + '</span>' +
        '<span class="ph-cap-name">' + ui.escapeHtml(stemOf(photo.name)) + '</span>' +
      '</figcaption>' +
      '</figure>';
  }

  /**
   * 缩略图懒加载。
   *
   * 为什么必须懒加载：几千张照片一次性发几千个请求，浏览器会排队排到天荒地老，
   * 服务端也要同时对几千个文件做解码。只请求视口附近的那些，滚动时才补。
   */
  _observeImages() {
    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }

    const images = this.$main.querySelectorAll('img[data-src]');
    if (!images.length) {
      return;
    }

    if (typeof IntersectionObserver !== 'function') {
      /* 老浏览器没有 IntersectionObserver：直接全部加载，功能不受影响 */
      images.forEach(function (img) {
        img.src = img.dataset.src;
        delete img.dataset.src;
      });
      return;
    }

    const self = this;
    this._observer = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        if (!entry.isIntersecting) {
          return;
        }
        const img = entry.target;
        img.src = img.dataset.src;
        delete img.dataset.src;
        self._observer.unobserve(img);
      });
    }, { root: this.$main, rootMargin: '400px 0px' });

    images.forEach(function (img) {
      self._observer.observe(img);
    });
  }

  _refreshSelectionClasses() {
    const selection = this.selection;
    this.$main.querySelectorAll('.ph-cell').forEach(function (cell) {
      cell.classList.toggle('selected', selection.has(cell.dataset.id));
    });
  }

  renderBatchBar() {
    const count = this.selection.size;
    if (!count) {
      this.$batch.hidden = true;
      this.$batch.innerHTML = '';
      return;
    }

    this.$batch.hidden = false;
    this.$batch.innerHTML =
      '<span class="ph-batch-count">已选 ' + count + ' 张</span>' +
      '<button class="ph-btn" data-act="edit">' + icon('pencil') + '<span>编辑时间 / 地点</span></button>' +
      '<button class="ph-btn" data-act="album">' + icon('plus') + '<span>加入相册</span></button>' +
      '<button class="ph-btn" data-act="select-all">' + icon('check') + '<span>全选当前筛选</span></button>' +
      '<button class="ph-btn" data-act="clear">' + icon('close') + '<span>取消选择</span></button>';
  }

  /* ======================================================================
     选择
     ====================================================================== */

  _onGridClick(event) {
    const cell = event.target.closest('.ph-cell');
    if (!cell) {
      return;
    }
    const id = cell.dataset.id;

    if (event.shiftKey && this.lastClicked) {
      const ids = this.visible.map(function (p) { return p.id; });
      const from = ids.indexOf(this.lastClicked);
      const to = ids.indexOf(id);
      if (from >= 0 && to >= 0) {
        if (!event.ctrlKey && !event.metaKey) {
          this.selection.clear();
        }
        const low = Math.min(from, to);
        const high = Math.max(from, to);
        for (let i = low; i <= high; i++) {
          this.selection.add(ids[i]);
        }
      }
    } else if (event.ctrlKey || event.metaKey) {
      if (this.selection.has(id)) {
        this.selection.delete(id);
      } else {
        this.selection.add(id);
      }
      this.lastClicked = id;
    } else {
      this.selection.clear();
      this.selection.add(id);
      this.lastClicked = id;
    }

    this._refreshSelectionClasses();
    this.renderBatchBar();
  }

  _onGridDoubleClick(event) {
    const cell = event.target.closest('.ph-cell');
    if (cell) {
      this.openViewer(cell.dataset.id);
    }
  }

  _onGridContextMenu(event) {
    const cell = event.target.closest('.ph-cell');
    if (!cell) {
      return;
    }
    event.preventDefault();

    const id = cell.dataset.id;
    if (!this.selection.has(id)) {
      this.selection.clear();
      this.selection.add(id);
      this.lastClicked = id;
      this._refreshSelectionClasses();
      this.renderBatchBar();
    }

    const self = this;
    const count = this.selection.size;
    const many = count > 1;
    const ids = Array.from(this.selection);

    ui.showContextMenu(event.clientX, event.clientY, [
      { label: many ? '查看（' + count + ' 张里的一张）' : '查看', iconName: 'eye',
        onClick: function () { self.openViewer(id); } },
      { label: many ? '编辑选中的 ' + count + ' 张…' : '编辑时间 / 地点…', iconName: 'pencil',
        onClick: function () { self.openBatchEdit(); } },
      { label: '下载原图' + (many ? '（逐张）' : ''), iconName: 'download',
        onClick: function () {
          ids.forEach(function (photoId, index) {
            setTimeout(function () {
              api.triggerDownload(api.photoRawUrl(photoId, true));
            }, index * 400);
          });
        } },
      'separator',
      { label: '加入相册…', iconName: 'plus', onClick: function () { self.addSelectionToAlbum(); } },
      { label: '把当前筛选存为新相册…', iconName: 'photos', onClick: function () { self.openAlbumDialog(); } },
      'separator',
      { label: '清除这张的时间修改', iconName: 'repeat', disabled: many,
        onClick: function () { self.clearTimeOverride(id); } }
    ]);
  }

  /* ======================================================================
     看大图（全屏灯箱）
     ----------------------------------------------------------------------
     为什么做成**铺满整屏**，而不是窗口里的一个面板：

       原来那块地方就在窗口内部，右侧的编辑面板还要切掉一截，于是竖拍的照片
       只剩很小一块，而且完全不能放大。现在灯箱挂在 document.body 上
       （不在窗口里），所以：
         * 图片在**整屏**正中，先按「适应窗口」最大化显示；
         * 滚轮缩放、按住拖动平移、双击在「适应 / 原始大小」之间切换；
         * 右侧的信息与编辑面板可以整个收起来，图片就占满整屏。

     层级刻意用 2000：高于窗口层（100）与任务栏（1000），但低于对话框
     （7000）—— 这样查看器里弹「确认清除时间」仍然显示在最上面。
     ====================================================================== */

  openViewer(id) {
    const list = this.visible.length ? this.visible : this.photos;
    let index = -1;
    for (let i = 0; i < list.length; i++) {
      if (list[i].id === id) {
        index = i;
        break;
      }
    }
    if (index < 0) {
      return;
    }

    this._viewerList = list;
    this._viewerIndex = index;
    this._ensureViewer();
    this._renderViewer();
  }

  _imgEl() {
    return this._viewer ? this._viewer.querySelector('[data-role="img"]') : null;
  }

  _stageEl() {
    return this._viewer ? this._viewer.querySelector('.ph-lb-stage') : null;
  }

  /** 「适应窗口」的缩放比（按图片原始像素与舞台尺寸算） */
  _fitScale() {
    const img = this._imgEl();
    const stage = this._stageEl();
    if (!img || !stage) {
      return 1;
    }
    const width = img.naturalWidth || 0;
    const height = img.naturalHeight || 0;
    if (!width || !height || !stage.clientWidth || !stage.clientHeight) {
      return 1;
    }
    return Math.min(stage.clientWidth / width, stage.clientHeight / height);
  }

  /**
   * 把当前的缩放与平移写到 img 上。
   *
   * ★ 这里**不用** max-width/max-height 来「适应」：那样浏览器会先把图片
   *   缩到容器大小，我们再对它做 scale 就是在**已经缩过的结果**上乘，
   *   尺寸就全乱了。所以图片始终按原始像素布局，尺寸完全由 transform 决定。
   */
  _applyZoom() {
    const img = this._imgEl();
    const stage = this._stageEl();
    if (!img || !stage) {
      return;
    }

    if (this._fitMode) {
      this._zoom = this._fitScale();
      this._panX = 0;
      this._panY = 0;
    }

    const scale = Math.max(0.02, Math.min(20, this._zoom || 1));
    this._zoom = scale;

    const dispW = (img.naturalWidth || 0) * scale;
    const dispH = (img.naturalHeight || 0) * scale;

    /* 图比舞台小就强制居中；比舞台大才允许拖动，而且不能把图拖出舞台外 */
    const maxX = Math.max(0, (dispW - stage.clientWidth) / 2);
    const maxY = Math.max(0, (dispH - stage.clientHeight) / 2);
    this._panX = Math.max(-maxX, Math.min(maxX, this._panX || 0));
    this._panY = Math.max(-maxY, Math.min(maxY, this._panY || 0));

    img.style.visibility = 'visible';
    img.style.transform = 'translate(' + this._panX + 'px,' + this._panY + 'px) scale(' + scale + ')';
    img.style.cursor = (maxX > 0 || maxY > 0) ? 'grab' : 'default';

    const label = this._viewer.querySelector('[data-role="zoom"]');
    if (label) {
      label.textContent = Math.round(scale * 100) + '%';
    }
  }

  /** 缩放并把 (originX, originY) 这个点钉在原地（滚轮缩放的手感靠它） */
  _zoomTo(newScale, originX, originY) {
    const stage = this._stageEl();
    if (!stage) {
      return;
    }
    const rect = stage.getBoundingClientRect();
    const centerX = rect.left + rect.width / 2;
    const centerY = rect.top + rect.height / 2;

    const target = Math.max(0.02, Math.min(20, newScale));
    const ratio = target / (this._zoom || 1);

    const dx = (originX == null) ? 0 : (originX - centerX);
    const dy = (originY == null) ? 0 : (originY - centerY);

    this._panX = dx - (dx - (this._panX || 0)) * ratio;
    this._panY = dy - (dy - (this._panY || 0)) * ratio;
    this._zoom = target;
    this._fitMode = false;
    this._applyZoom();
  }

  /** 回到「适应窗口」（true）或「原始大小」（false） */
  _resetZoom(fit) {
    this._fitMode = !!fit;
    if (!fit) {
      this._zoom = 1;
      this._panX = 0;
      this._panY = 0;
    }
    this._applyZoom();
  }

  _togglePanel() {
    if (!this._viewer) {
      return;
    }
    this._viewer.classList.toggle('no-panel');
    /* 面板收起来之后舞台变宽，正在「适应窗口」时要重新算一次 */
    this._applyZoom();
  }

  /** 建一次灯箱的 DOM 并把事件挂好（挂在 document.body 上，不在窗口里） */
  _ensureViewer() {
    if (this._viewer) {
      return;
    }
    const self = this;

    this._viewer = document.createElement('div');
    this._viewer.className = 'ph-lightbox';
    this._viewer.innerHTML =
      '<div class="ph-lb-top">' +
        '<div class="ph-lb-title" data-role="vt"></div>' +
        '<div class="ph-lb-tools">' +
          '<button data-act="zoom-out" title="缩小（-）">−</button>' +
          '<span class="ph-lb-zoom" data-role="zoom">100%</span>' +
          '<button data-act="zoom-in" title="放大（+）">+</button>' +
          '<button data-act="fit" title="适应窗口（0）">适应</button>' +
          '<button data-act="actual" title="原始大小（1）">1:1</button>' +
          '<span class="ph-lb-sep"></span>' +
          '<button data-act="info" title="显示 / 隐藏信息面板（I）">' + icon('info') + '</button>' +
          '<button data-act="dl" title="下载原图">' + icon('download') + '</button>' +
          '<button data-act="close" title="关闭（Esc）">' + icon('close') + '</button>' +
        '</div>' +
      '</div>' +
      '<div class="ph-lb-body">' +
        '<div class="ph-lb-stage">' +
          '<button class="ph-lb-nav prev" data-act="prev" title="上一张（←）">‹</button>' +
          '<img class="ph-lb-img" data-role="img" alt="">' +
          '<button class="ph-lb-nav next" data-act="next" title="下一张（→）">›</button>' +
          '<div class="ph-lb-hint">滚轮缩放 · 按住拖动平移 · 双击切换「适应 / 原始大小」</div>' +
        '</div>' +
        '<aside class="ph-lb-side" data-role="side-panel"></aside>' +
      '</div>';

    this._viewer.addEventListener('click', function (event) {
      const button = event.target.closest('button');
      if (!button) {
        return;
      }
      /* ★ 一律通过 self._viewerList / _viewerIndex 取当前这张，
         不要闭包捕获 list —— 换一批照片再看时会指向旧数组。 */
      const current = (self._viewerList || [])[self._viewerIndex];
      if (!current) {
        return;
      }
      const act = button.dataset.act;
      if (act === 'close') {
        self._closeViewer();
      } else if (act === 'prev') {
        self._stepViewer(-1);
      } else if (act === 'next') {
        self._stepViewer(1);
      } else if (act === 'zoom-in') {
        self._zoomTo((self._zoom || 1) * 1.25);
      } else if (act === 'zoom-out') {
        self._zoomTo((self._zoom || 1) / 1.25);
      } else if (act === 'fit') {
        self._resetZoom(true);
      } else if (act === 'actual') {
        self._resetZoom(false);
      } else if (act === 'info') {
        self._togglePanel();
      } else if (act === 'dl') {
        api.triggerDownload(api.photoRawUrl(current.id, true));
      } else if (act === 'save') {
        self.saveViewerEdit(current);
      } else if (act === 'revert') {
        self.clearTimeOverride(current.id);
      } else if (act === 'same-place') {
        self.applyPlaceToSameGps(current);
      }
    });

    const stage = this._stageEl();

    /* 滚轮缩放：以**鼠标所在的那个点**为中心，而不是以图片中心 ——
       后者在放大看细节时会让人觉得「图跑掉了」 */
    stage.addEventListener('wheel', function (event) {
      if (!self._viewer) {
        return;
      }
      event.preventDefault();
      const factor = event.deltaY < 0 ? 1.15 : (1 / 1.15);
      self._zoomTo((self._zoom || 1) * factor, event.clientX, event.clientY);
    }, { passive: false });

    /* 拖动平移。move / up 绑在 document 上：拖到灯箱外也能跟手，
       而且松手一定收得到（否则会一直粘着鼠标） */
    let dragging = false;
    let moved = 0;
    let startX = 0;
    let startY = 0;
    let baseX = 0;
    let baseY = 0;

    stage.addEventListener('mousedown', function (event) {
      if (event.button !== 0) {
        return;
      }
      dragging = true;
      moved = 0;
      startX = event.clientX;
      startY = event.clientY;
      baseX = self._panX || 0;
      baseY = self._panY || 0;
      event.preventDefault();
    });

    this._onDragMove = function (event) {
      if (!dragging) {
        return;
      }
      moved = Math.abs(event.clientX - startX) + Math.abs(event.clientY - startY);
      self._panX = baseX + (event.clientX - startX);
      self._panY = baseY + (event.clientY - startY);
      self._applyZoom();
    };
    this._onDragUp = function () {
      if (!dragging) {
        return;
      }
      dragging = false;
      self._applyZoom();
    };
    document.addEventListener('mousemove', this._onDragMove);
    document.addEventListener('mouseup', this._onDragUp);

    /* 双击在「适应窗口」与「原始大小」之间切换（和常见看图工具一致） */
    stage.addEventListener('dblclick', function (event) {
      if (event.target.closest('button')) {
        return;
      }
      if (self._fitMode) {
        self._resetZoom(false);
      } else {
        self._resetZoom(true);
      }
    });

    /* 点空白处关闭。★ 只在「没拖动过」时关：拖完图松手那一下也会产生
       click，不加这个判断的话平移一次就把查看器关了。 */
    stage.addEventListener('click', function (event) {
      if (event.target !== stage && !event.target.classList.contains('ph-lb-img')) {
        return;
      }
      if (moved > 4) {
        return;
      }
      self._closeViewer();
    });

    /* 键盘：翻页、缩放、开关面板、关闭。
       绑在 document 上，这样不用先点一下灯箱 */
    this._onKeyDown = function (event) {
      if (!self._viewer) {
        return;
      }
      const key = event.key;
      if (key === 'Escape') {
        self._closeViewer();
      } else if (key === 'ArrowLeft') {
        self._stepViewer(-1);
        event.preventDefault();
      } else if (key === 'ArrowRight') {
        self._stepViewer(1);
        event.preventDefault();
      } else if (key === '+' || key === '=') {
        self._zoomTo((self._zoom || 1) * 1.25);
        event.preventDefault();
      } else if (key === '-' || key === '_') {
        self._zoomTo((self._zoom || 1) / 1.25);
        event.preventDefault();
      } else if (key === '0' || key === 'f' || key === 'F') {
        self._resetZoom(true);
      } else if (key === '1') {
        self._resetZoom(false);
      } else if (key === 'i' || key === 'I') {
        self._togglePanel();
      }
    };
    document.addEventListener('keydown', this._onKeyDown);

    /* 浏览器窗口尺寸变了要重算「适应窗口」的比例，否则会留白或溢出 */
    this._onResize = function () {
      if (self._viewer && self._fitMode) {
        self._applyZoom();
      }
    };
    window.addEventListener('resize', this._onResize);

    document.body.appendChild(this._viewer);
  }

  _renderViewer() {
    const self = this;
    const list = this._viewerList || [];
    const photo = list[this._viewerIndex];
    if (!photo) {
      this._closeViewer();
      return;
    }

    this._viewer.querySelector('[data-role="vt"]').textContent =
      photo.name + '   (' + (this._viewerIndex + 1) + ' / ' + list.length + ')';

    const img = this._imgEl();
    /* 换一张就回到「适应窗口」，并且先藏起来 —— 否则大图会先以原始尺寸
       闪一下（那一下看起来就像图错位了） */
    img.style.visibility = 'hidden';
    this._fitMode = true;
    this._panX = 0;
    this._panY = 0;

    if (photo.missing) {
      img.removeAttribute('src');
      img.alt = '文件已经不在了';
      this._renderViewerPanel(photo);
      return;
    }

    img.alt = photo.name;
    img.onload = function () {
      /* 尺寸要等加载完才知道，所以「适应窗口」必须在 onload 里再算一次 */
      self._resetZoom(true);
    };
    img.src = api.photoRawUrl(photo.id, false);

    /* 缓存命中的图片不会再触发 onload，这里补一次 */
    if (img.complete && img.naturalWidth) {
      this._resetZoom(true);
    }

    this._renderViewerPanel(photo);
  }

  _renderViewerPanel(photo) {
    const self = this;
    const panel = this._viewer.querySelector('[data-role="side-panel"]');
    const place = photo.place || {};
    const gps = photo.gps || {};

    /* 同坐标的照片有几张？多的话值得一次给整批命名（同一次拍摄的坐标通常一模一样） */
    let samePlace = 0;
    if (gps.lat != null && gps.lon != null) {
      this.photos.forEach(function (other) {
        if (other.gps && other.gps.lat === gps.lat && other.gps.lon === gps.lon) {
          samePlace += 1;
        }
      });
    }

    let html = '';
    html += '<div class="ph-info">';
    html += '<div class="ph-info-row"><span>时间</span><b>' +
      (photo.taken_at ? ui.escapeHtml(prettyTime(photo.taken_at)) : '（没有可用时间）') + '</b></div>';
    html += '<div class="ph-info-note' + (photo.source === 'file' ? ' warn' : '') + '">' +
      ui.escapeHtml(SOURCE_TEXT[photo.source] || '') + '</div>';

    html += '<div class="ph-info-row"><span>文件</span><b>' + ui.escapeHtml(photo.relpath) + '</b></div>';
    if (photo.w && photo.h) {
      html += '<div class="ph-info-row"><span>尺寸</span><b>' + photo.w + ' × ' + photo.h + '</b></div>';
    }
    const exifBits = [];
    if (photo.camera) {
      exifBits.push(photo.camera);
    }
    if (photo.lens) {
      exifBits.push(photo.lens);
    }
    if (photo.fnum) {
      exifBits.push('f/' + photo.fnum);
    }
    if (photo.exposure) {
      exifBits.push(photo.exposure);
    }
    if (photo.iso) {
      exifBits.push('ISO ' + photo.iso);
    }
    if (exifBits.length) {
      html += '<div class="ph-info-row"><span>相机</span><b>' + ui.escapeHtml(exifBits.join(' · ')) + '</b></div>';
    }
    if (photo.missing) {
      html += '<div class="ph-info-note warn">文件当前找不到。相册里的记录与你的修改都还在，' +
        '把设备接回来再点「重新扫描」就会恢复。</div>';
    }
    html += '</div>';

    html += '<div class="ph-edit">';
    html += '<label class="ph-field"><span>拍摄时间</span>' +
      '<input type="text" data-field="taken_at" placeholder="2023-08-15 12:34:56" value="' +
      ui.escapeHtml(photo.taken_at ? photo.taken_at.replace('T', ' ') : '') + '"></label>';

    html += '<label class="ph-field"><span>地点名称</span>' +
      '<input type="text" data-field="place" placeholder="例如：青岛·栈桥" value="' +
      ui.escapeHtml(place.name || '') + '"></label>';

    html += '<div class="ph-field-row">' +
      '<label class="ph-field"><span>纬度</span><input type="text" data-field="lat" value="' +
        (place.lat != null ? place.lat : '') + '"></label>' +
      '<label class="ph-field"><span>经度</span><input type="text" data-field="lon" value="' +
        (place.lon != null ? place.lon : '') + '"></label>' +
      '</div>';

    if (samePlace > 1) {
      html += '<button class="ph-link" data-act="same-place">这批有 ' + samePlace +
        ' 张是同一个坐标，把地名应用到全部</button>';
    }

    html += '<label class="ph-field"><span>标签</span>' +
      '<input type="text" data-field="tags" placeholder="用逗号分隔，例如：家人, 海边" value="' +
      ui.escapeHtml((photo.tags || []).join(', ')) + '"></label>';

    html += '<label class="ph-field"><span>星级</span><select data-field="rating">' +
      [0, 1, 2, 3, 4, 5].map(function (value) {
        return '<option value="' + value + '"' + (photo.rating === value ? ' selected' : '') + '>' +
          (value ? '★'.repeat(value) : '（无）') + '</option>';
      }).join('') +
      '</select></label>';

    html += '<label class="ph-field"><span>备注</span>' +
      '<textarea data-field="caption" rows="2" placeholder="随手记一句">' +
      ui.escapeHtml(photo.caption || '') + '</textarea></label>';

    html += '<div class="ph-edit-actions">' +
      '<button class="ph-btn primary" data-act="save">保存</button>' +
      '<button class="ph-btn" data-act="revert">清除时间修改</button>' +
      '</div>';
    html += '<div class="ph-edit-hint">改动只存在相册里，<b>不会修改照片文件本身</b>。' +
      '批量改错了可以用工具栏的「撤销」回退。</div>';
    html += '</div>';

    panel.innerHTML = html;
    /* ★ 这里**不能**再给 panel 挂 click 监听：_renderViewerPanel 每翻一张
       就会跑一次，而 panel 元素本身是复用的 —— 挂上去会越积越多，
       看够 10 张之后点一次「保存」会真的发 10 个请求。
       面板里的按钮统一由上面 viewer 根节点上那一个监听器处理。 */
  }

  _stepViewer(delta) {
    const list = this._viewerList || [];
    if (!list.length) {
      return;
    }
    this._viewerIndex = (this._viewerIndex + delta + list.length) % list.length;
    this._renderViewer();
  }

  _closeViewer() {
    if (this._onKeyDown) {
      document.removeEventListener('keydown', this._onKeyDown);
      this._onKeyDown = null;
    }
    if (this._onDragMove) {
      document.removeEventListener('mousemove', this._onDragMove);
      this._onDragMove = null;
    }
    if (this._onDragUp) {
      document.removeEventListener('mouseup', this._onDragUp);
      this._onDragUp = null;
    }
    if (this._onResize) {
      window.removeEventListener('resize', this._onResize);
      this._onResize = null;
    }
    if (this._viewer) {
      this._viewer.remove();
      this._viewer = null;
    }
    this._viewerList = null;
    this._viewerIndex = -1;
    this._zoom = 1;
    this._panX = 0;
    this._panY = 0;
    this._fitMode = true;
  }

  /* ======================================================================
     编辑
     ====================================================================== */

  /** 从一段 DOM 里收集编辑表单的值 */
  _readForm(container) {
    const patch = {};
    const takenAt = container.querySelector('[data-field="taken_at"]');
    const place = container.querySelector('[data-field="place"]');
    const lat = container.querySelector('[data-field="lat"]');
    const lon = container.querySelector('[data-field="lon"]');
    const tags = container.querySelector('[data-field="tags"]');
    const rating = container.querySelector('[data-field="rating"]');
    const caption = container.querySelector('[data-field="caption"]');

    if (takenAt) {
      patch.taken_at = takenAt.value.trim();
    }
    if (place || lat || lon) {
      const entry = { name: place ? place.value.trim() : '' };
      if (lat && lat.value.trim()) {
        entry.lat = lat.value.trim();
      }
      if (lon && lon.value.trim()) {
        entry.lon = lon.value.trim();
      }
      /* 名称与坐标都空 = 清掉地点 */
      patch.place = (entry.name || entry.lat != null || entry.lon != null) ? entry : null;
    }
    if (tags) {
      patch.tags = tags.value.trim();
    }
    if (rating) {
      patch.rating = Number(rating.value) || 0;
    }
    if (caption) {
      patch.caption = caption.value.trim();
    }
    return patch;
  }

  async saveViewerEdit(photo) {
    const panel = this._viewer.querySelector('[data-role="side-panel"]');
    const patch = this._readForm(panel);

    /* 表单里带 taken_at 空值 = 用户把时间清空了；这是合法的（退回 EXIF），
       所以不做「必须有时间」的校验，交给服务端解释。 */

    try {
      await api.photosEdit([photo.id], patch);
      ui.toast('已保存', 'success');
      await this.load();
      /* 重载之后 visible 会重建，按 id 找回当前位置继续看这一张 */
      this.openViewer(photo.id);
    } catch (err) {
      ui.showAlert('保存失败', (err && err.message) || String(err), 'error');
    }
  }

  async clearTimeOverride(id) {
    const ok = await ui.showConfirm('清除时间修改',
      '把这张的拍摄时间恢复成照片自带的 EXIF（没有 EXIF 就回到文件时间）。',
      { okText: '清除', iconName: 'question' });
    if (!ok) {
      return;
    }
    try {
      await api.photosEdit([id], { taken_at: '' });
      ui.toast('已恢复', 'success');
      await this.load();
    } catch (err) {
      ui.showAlert('操作失败', (err && err.message) || String(err), 'error');
    }
  }

  /**
   * 把地名应用到所有同坐标的照片。
   *
   * 同一次拍摄的照片 GPS 坐标通常**完全一致**，所以一次输入就能解决几百张的
   * 「这是哪儿」—— 这是不做地图（离线环境下拿不到反查）之后最实用的补偿。
   */
  async applyPlaceToSameGps(photo) {
    const panel = this._viewer.querySelector('[data-role="side-panel"]');
    const name = (panel.querySelector('[data-field="place"]').value || '').trim();
    if (!name) {
      ui.showAlert('先填地名', '请先在上面填一个地点名称，再应用到同坐标的照片。', 'warning');
      return;
    }
    const gps = photo.gps || {};
    const ids = this.photos.filter(function (other) {
      return other.gps && other.gps.lat === gps.lat && other.gps.lon === gps.lon;
    }).map(function (other) { return other.id; });

    const ok = await ui.showConfirm('应用到同坐标的照片',
      '将把「' + name + '」写到这 ' + ids.length + ' 张照片上。',
      { okText: '应用', iconName: 'question' });
    if (!ok) {
      return;
    }

    try {
      await api.photosEdit(ids, { place: { name: name, lat: gps.lat, lon: gps.lon } });
      ui.toast('已更新 ' + ids.length + ' 张', 'success');
      await this.load();
      this.openViewer(photo.id);
    } catch (err) {
      ui.showAlert('操作失败', (err && err.message) || String(err), 'error');
    }
  }

  /* ---- 批量编辑 ---- */

  openBatchEdit() {
    if (!this.selection.size) {
      return;
    }
    const self = this;
    const ids = Array.from(this.selection);

    const body =
      '<div class="ph-batch-note">将对选中的 <b>' + ids.length + '</b> 张照片生效。' +
      '留空的项不会被改动。</div>' +

      '<div class="ph-section">' +
        '<div class="ph-section-title">时间</div>' +
        '<div class="ph-field-row">' +
          '<label class="ph-field"><span>整体平移</span>' +
            '<input type="number" step="0.5" data-field="shift" placeholder="例如 8 或 -8"></label>' +
          '<label class="ph-field"><span>单位</span><select data-field="shift_unit">' +
            '<option value="hours">小时</option><option value="days">天</option>' +
          '</select></label>' +
        '</div>' +
        '<div class="ph-section-note">平移会保留照片彼此的先后顺序 —— ' +
          '相机的时区设错了、一次旅行全部偏 8 小时，就是这种情况。</div>' +
        '<label class="ph-field"><span>或统一设为</span>' +
          '<input type="text" data-field="set_to" placeholder="2023-08-15 12:34:56（留空则不改）"></label>' +
      '</div>' +

      '<div class="ph-section">' +
        '<div class="ph-section-title">地点</div>' +
        '<div class="ph-field-row">' +
          '<label class="ph-field"><span>名称</span><input type="text" data-field="place" placeholder="留空则不改"></label>' +
        '</div>' +
        '<div class="ph-field-row">' +
          '<label class="ph-field"><span>纬度</span><input type="text" data-field="lat"></label>' +
          '<label class="ph-field"><span>经度</span><input type="text" data-field="lon"></label>' +
        '</div>' +
      '</div>' +

      '<div class="ph-section">' +
        '<div class="ph-section-title">标签与备注</div>' +
        '<label class="ph-field"><span>替换标签</span>' +
          '<input type="text" data-field="tags" placeholder="用逗号分隔，留空则不改"></label>' +
        '<div class="ph-field-row">' +
          '<label class="ph-field"><span>星级</span><select data-field="rating">' +
            '<option value="">（不改）</option>' +
            [0, 1, 2, 3, 4, 5].map(function (v) {
              return '<option value="' + v + '">' + (v ? '★'.repeat(v) : '清除') + '</option>';
            }).join('') +
          '</select></label>' +
          '<label class="ph-field"><span>备注</span><input type="text" data-field="caption" placeholder="留空则不改"></label>' +
        '</div>' +
      '</div>';

    this._openModal('批量编辑', body, '应用', function (form) {
      return self._runBatchEdit(ids, form);
    });
  }

  async _runBatchEdit(ids, form) {
    const shift = form.querySelector('[data-field="shift"]').value.trim();
    const unit = form.querySelector('[data-field="shift_unit"]').value;
    const setTo = form.querySelector('[data-field="set_to"]').value.trim();
    const place = form.querySelector('[data-field="place"]').value.trim();
    const lat = form.querySelector('[data-field="lat"]').value.trim();
    const lon = form.querySelector('[data-field="lon"]').value.trim();
    const tags = form.querySelector('[data-field="tags"]').value.trim();
    const rating = form.querySelector('[data-field="rating"]').value;
    const caption = form.querySelector('[data-field="caption"]').value.trim();

    if (!shift && !setTo && !place && !lat && !lon && !tags && rating === '' && !caption) {
      ui.showAlert('没有要修改的内容', '至少填一项再点应用。', 'warning');
      return false;
    }

    try {
      /* 时间：平移与「统一设为」是互斥的两种语义，这里让「统一设为」优先 */
      if (setTo) {
        await api.photosBatchTime(ids, { setTo: setTo });
      } else if (shift) {
        const amount = Number(shift);
        if (!isFinite(amount) || amount === 0) {
          ui.showAlert('平移量不合法', '请填一个非 0 的数字。', 'warning');
          return false;
        }
        const hours = unit === 'days' ? amount * 24 : amount;
        await api.photosBatchTime(ids, { deltaHours: hours });
      }

      const patch = {};
      if (place || lat || lon) {
        patch.place = { name: place };
        if (lat) {
          patch.place.lat = lat;
        }
        if (lon) {
          patch.place.lon = lon;
        }
      }
      if (tags) {
        patch.tags = tags;
      }
      if (rating !== '') {
        patch.rating = Number(rating);
      }
      if (caption) {
        patch.caption = caption;
      }
      if (Object.keys(patch).length) {
        await api.photosEdit(ids, patch);
      }

      ui.toast('已更新 ' + ids.length + ' 张（可以撤销）', 'success');
      await this.load();
      return true;
    } catch (err) {
      ui.showAlert('批量编辑失败', (err && err.message) || String(err), 'error');
      return false;
    }
  }

  /* ======================================================================
     导入 / 重扫 / 撤销
     ====================================================================== */

  async openImportDialog() {
    const self = this;

    let roots = [];
    try {
      const data = await api.listRoots();
      roots = data.roots || [];
    } catch (err) {
      ui.showAlert('读取根目录失败', (err && err.message) || String(err), 'error');
      return;
    }

    /* 两个来源：服务器上已有的目录（默认）／从浏览器所在的电脑上传。
       ★ 上传那条路**不需要**用户有任何可见目录，所以即使一个根都没分配到，
         也不该把整个对话框挡掉 —— 只把服务器那一栏说明白就行。 */
    const serverHtml = roots.length
      ? ('<div class="ph-field-row">' +
           '<label class="ph-field"><span>位置</span><select data-field="root">' +
             roots.map(function (root) {
               return '<option value="' + ui.escapeHtml(root.id) + '">' +
                 ui.escapeHtml(root.name) + '　' + ui.escapeHtml(root.path) + '</option>';
             }).join('') +
           '</select></label>' +
         '</div>' +
         '<div class="ph-field"><span>当前目录</span>' +
           '<div class="ph-crumbs" data-role="crumbs"></div></div>' +
         '<div class="ph-dirlist" data-role="dirs"></div>' +
         '<div class="ph-section-note">进入某个文件夹后点「索引这个文件夹」，' +
           '它会连同子文件夹一起收录。<b>照片不会被复制</b>，原文件留在原处。</div>')
      : '<div class="ph-section-note">你的账号还没有被分配任何目录，' +
        '所以看不到服务器上的文件。<br>可以用上面的「从我的电脑上传」把照片传上来。</div>';

    const body =
      '<div class="ph-seg ph-seg-wide" data-role="mode">' +
        '<button data-mode="server" class="on">服务器上的文件夹</button>' +
        '<button data-mode="upload">从我的电脑上传</button>' +
      '</div>' +

      '<div data-role="server-pane">' + serverHtml + '</div>' +

      '<div data-role="upload-pane" hidden>' +
        '<div class="ph-drop" data-role="drop">' +
          '<div class="ph-drop-ico">' + icon('upload') + '</div>' +
          '<div class="ph-drop-text">把照片拖到这里，或者' +
            '<button type="button" class="ph-link" data-act="pick">选择照片…</button></div>' +
          '<div class="ph-drop-hint">可以一次选多张。照片会上传到服务器，' +
            '然后立刻出现在时间轴上。</div>' +
        '</div>' +
        '<input type="file" accept="image/*" multiple hidden data-role="files">' +
        '<div class="ph-upload-list" data-role="list"></div>' +
        '<div class="ph-upload-progress" data-role="progress" hidden>' +
          '<div class="ph-upload-bar"><i data-role="bar"></i></div>' +
          '<div class="ph-upload-text" data-role="ptext"></div>' +
        '</div>' +
      '</div>';

    const modal = this._openModal('导入照片', body, '索引这个文件夹', function (form) {
      if (form.dataset.mode === 'upload') {
        return self._doUpload(form);
      }
      const rootField = form.querySelector('[data-field="root"]');
      if (!rootField) {
        return false;
      }
      return self._doImport(rootField.value, form.dataset.path || '');
    }, { keepOpen: true });

    const form = modal.querySelector('.ph-modal-body');
    const modeBox = form.querySelector('[data-role="mode"]');
    const okButton = modal.querySelector('.ph-modal-foot [data-act="ok"]');
    form.dataset.mode = 'server';
    form.dataset.path = '';

    /* ---- 两种来源之间切换 ---- */
    const serverPane = form.querySelector('[data-role="server-pane"]');
    const uploadPane = form.querySelector('[data-role="upload-pane"]');
    const fileInput = form.querySelector('[data-role="files"]');
    const dropZone = form.querySelector('[data-role="drop"]');
    const listBox = form.querySelector('[data-role="list"]');
    const progressBox = form.querySelector('[data-role="progress"]');
    const progressBar = form.querySelector('[data-role="bar"]');
    const progressText = form.querySelector('[data-role="ptext"]');
    let picked = [];

    /* ★ _doUpload 是在**模态框的提交回调**里被调用的，那里拿不到这里的闭包，
       所以把这份清单放到实例上。picked 始终是同一个数组引用，
       后面的 push / splice 都会同步过去。 */
    this._pendingUpload = picked;

    function setMode(mode) {
      form.dataset.mode = mode;
      serverPane.hidden = mode !== 'server';
      uploadPane.hidden = mode !== 'upload';
      modeBox.querySelectorAll('button').forEach(function (button) {
        button.classList.toggle('on', button.dataset.mode === mode);
      });
      if (okButton) {
        okButton.textContent = (mode === 'upload') ? '开始上传' : '索引这个文件夹';
        okButton.disabled = (mode === 'upload') && !picked.length;
      }
    }

    modeBox.addEventListener('click', function (event) {
      const button = event.target.closest('button');
      if (button && button.dataset.mode) {
        setMode(button.dataset.mode);
      }
    });

    /* ---- 选文件 / 拖放 ---- */
    function isImage(file) {
      return /\.(jpe?g|jfif|png|gif|bmp|webp|tiff?|avif)$/i.test(file.name || '');
    }

    function acceptFiles(fileList) {
      const incoming = Array.prototype.slice.call(fileList || []);
      const skipped = [];
      incoming.forEach(function (file) {
        if (!isImage(file)) {
          skipped.push(file.name + '（不是图片）');
          return;
        }
        const dup = picked.some(function (item) {
          return item.name === file.name && item.size === file.size;
        });
        if (!dup) {
          picked.push(file);
        }
      });

      if (skipped.length) {
        ui.toast('跳过了 ' + skipped.length + ' 个非图片文件：' + skipped.slice(0, 3).join('、'),
          'warn', '提示', 6000);
      }
      renderList();
    }

    function renderList() {
      if (!picked.length) {
        listBox.innerHTML = '';
        if (okButton) {
          okButton.disabled = (form.dataset.mode === 'upload');
        }
        return;
      }
      const total = picked.reduce(function (sum, f) { return sum + f.size; }, 0);
      let html = '<div class="ph-upload-head">已选 ' + picked.length + ' 张，共 ' +
        ui.formatSize(total) + '</div>';
      picked.forEach(function (file, index) {
        html += '<div class="ph-upload-item">' +
          '<span class="ph-upload-name">' + ui.escapeHtml(file.name) + '</span>' +
          '<span class="ph-upload-size">' + ui.formatSize(file.size) + '</span>' +
          '<button type="button" class="ph-mini" data-remove="' + index + '" title="移除">' +
            icon('close') + '</button>' +
          '</div>';
      });
      listBox.innerHTML = html;
      if (okButton) {
        okButton.disabled = false;
      }
    }

    dropZone.addEventListener('click', function (event) {
      if (event.target.closest('[data-act="pick"]') || event.target === dropZone) {
        fileInput.click();
      }
    });

    fileInput.addEventListener('change', function () {
      acceptFiles(fileInput.files);
      fileInput.value = '';
    });

    listBox.addEventListener('click', function (event) {
      const button = event.target.closest('[data-remove]');
      if (!button) {
        return;
      }
      picked.splice(Number(button.dataset.remove), 1);
      renderList();
    });

    /* 拖放：dragover 必须 preventDefault，否则浏览器会直接打开这个文件 */
    ['dragenter', 'dragover'].forEach(function (name) {
      dropZone.addEventListener(name, function (event) {
        event.preventDefault();
        event.stopPropagation();
        dropZone.classList.add('over');
      });
    });
    ['dragleave', 'drop'].forEach(function (name) {
      dropZone.addEventListener(name, function (event) {
        event.preventDefault();
        event.stopPropagation();
        dropZone.classList.remove('over');
      });
    });
    dropZone.addEventListener('drop', function (event) {
      const dt = event.dataTransfer;
      if (dt && dt.files && dt.files.length) {
        acceptFiles(dt.files);
      }
    });

    /* 上传时把进度写进对话框里的进度条 */
    form.__photoProgress = function (done, total, label) {
      progressBox.hidden = false;
      progressBar.style.width = (total ? Math.round(done * 100 / total) : 0) + '%';
      progressText.textContent = label || '';
    };

    if (roots.length) {
      const rootSelect = form.querySelector('[data-field="root"]');
      const crumbs = form.querySelector('[data-role="crumbs"]');
      const dirs = form.querySelector('[data-role="dirs"]');

      async function browse(rootId, path) {
        form.dataset.path = path;
        crumbs.textContent = (path || '（根目录）');
        dirs.innerHTML = '<div class="ph-side-hint">正在读取…</div>';
        try {
          const data = await api.listDir({ root: rootId, path: path, sort: 'name', order: 'asc' });
          const folders = (data.entries || []).filter(function (entry) { return entry.is_dir; });
          let html = '';
          if (path) {
            html += '<div class="ph-dir up" data-dir="..">↰ 上一层</div>';
          }
          if (!folders.length) {
            html += '<div class="ph-side-hint">这个文件夹里没有子文件夹，可以直接索引它。</div>';
          }
          folders.forEach(function (entry) {
            html += '<div class="ph-dir" data-dir="' + ui.escapeHtml(entry.name) + '">' +
              icon('folder') + '<span>' + ui.escapeHtml(entry.name) + '</span></div>';
          });
          dirs.innerHTML = html;
        } catch (err) {
          dirs.innerHTML = '<div class="ph-side-hint error">' +
            ui.escapeHtml((err && err.message) || String(err)) + '</div>';
        }
      }

      rootSelect.addEventListener('change', function () {
        browse(rootSelect.value, '');
      });

      dirs.addEventListener('click', function (event) {
        const item = event.target.closest('.ph-dir');
        if (!item) {
          return;
        }
        const name = item.dataset.dir;
        let path = form.dataset.path || '';
        if (name === '..') {
          path = path.split('/').slice(0, -1).join('/');
        } else {
          path = path ? (path + '/' + name) : name;
        }
        browse(rootSelect.value, path);
      });

      browse(roots[0].id, '');
    }

    /* 一个根都没有时直接落到「上传」那一栏，免得用户看着一屏说明发愣 */
    setMode(roots.length ? 'server' : 'upload');
  }

  /**
   * 把选中的文件逐张上传（**串行**）。
   *
   * 为什么串行：一次几十张同时发会把连接数和磁盘 IO 都打满，进度也就没法
   * 说清是第几张了 —— 与音乐批量导入、时长探测是同一个取舍。
   */
  async _doUpload(form) {
    const files = this._pendingUpload || [];
    if (!files.length) {
      ui.showAlert('还没有选择照片', '先把照片拖进上面的框，或者点「选择照片…」。', 'warning');
      return false;
    }

    const okButton = this.$modal.querySelector('.ph-modal-foot [data-act="ok"]');
    const report = form.__photoProgress || function () {};

    let done = 0;
    const failed = [];
    try {
      for (let i = 0; i < files.length; i++) {
        const file = files[i];
        const label = '正在上传 ' + (i + 1) + '/' + files.length + '：' + file.name;
        report(0, 1, label);
        try {
          await api.photosUpload(file, function (loaded, total) {
            report(loaded, total, label);
          });
          done += 1;
        } catch (err) {
          failed.push(file.name + '：' + ((err && err.message) || err));
        }
        report(i + 1, files.length, '已完成 ' + (i + 1) + '/' + files.length);
      }
    } finally {
      if (okButton) {
        okButton.disabled = false;
      }
      this._pendingUpload = null;
    }

    if (done) {
      ui.toast('已上传 ' + done + ' 张照片', 'success');
    }
    if (failed.length) {
      ui.showAlert('有 ' + failed.length + ' 张没能上传',
        failed.slice(0, 8).join('\n') + (failed.length > 8 ? '\n…' : ''), 'warning');
    }

    if (done) {
      this._closeModal();
      await this.load();
    }
    return done > 0;
  }

  async _doImport(root, path) {
    try {
      ui.setBusy(true, '正在索引照片…');
      const data = await api.photosImport(root, [path], false);
      ui.toast(data.message || '已导入', 'success');
      this._closeModal();
      await this.load();
      return true;
    } catch (err) {
      ui.showAlert('导入失败', (err && err.message) || String(err), 'error');
      return false;
    } finally {
      ui.setBusy(false);
    }
  }

  async doRescan() {
    const ok = await ui.showConfirm('重新扫描',
      '会核对每张照片是否还在原处、按内容找回被移动或改名的文件、并收录目录里新增的图片。' +
      '照片比较多时可能要等一会儿。',
      { okText: '开始扫描', iconName: 'refresh' });
    if (!ok) {
      return;
    }
    try {
      ui.setBusy(true, '正在扫描…');
      const data = await api.photosRescan();
      ui.toast(data.message || '扫描完成', 'success');
      await this.load();
    } catch (err) {
      ui.showAlert('扫描失败', (err && err.message) || String(err), 'error');
    } finally {
      ui.setBusy(false);
    }
  }

  async doUndo() {
    try {
      const data = await api.photosUndo();
      ui.toast(data.message || '已撤销', 'success');
      await this.load();
    } catch (err) {
      ui.showAlert('撤销失败', (err && err.message) || String(err), 'warning');
    }
  }

  async dropSource(root, path) {
    const ok = await ui.showConfirm('从相册移出',
      '将把「' + (path || '根目录') + '」里的照片从相册中移除，连同你对它们做的修改。' +
      '**照片文件本身不会被删除。**',
      { okText: '移出相册', danger: true, iconName: 'warning' });
    if (!ok) {
      return;
    }
    try {
      const data = await api.photosForget(root, path);
      ui.toast(data.message || '已移出', 'success');
      this.setFilter('all', '');
      await this.load();
    } catch (err) {
      ui.showAlert('操作失败', (err && err.message) || String(err), 'error');
    }
  }

  /* ======================================================================
     相册
     ====================================================================== */

  openAlbumDialog() {
    const self = this;
    const selected = this.selection.size;

    /* 用当前筛选出来的时间范围做默认值 —— 「把这段时间的存成一个相册」
       正是「按时间轴归类」最自然的下一步。 */
    const range = this._currentRange();

    const body =
      '<label class="ph-field"><span>相册名</span>' +
        '<input type="text" data-field="name" placeholder="例如：2023 暑假"></label>' +
      '<label class="ph-field"><span>类型</span><select data-field="kind">' +
        '<option value="manual">手动相册（自己挑照片）</option>' +
        '<option value="smart">按时间段自动归类</option>' +
      '</select></label>' +
      '<div class="ph-section" data-role="smart">' +
        '<div class="ph-field-row">' +
          '<label class="ph-field"><span>开始</span><input type="text" data-field="start" value="' +
            ui.escapeHtml(range.start || '') + '" placeholder="2023-07-01"></label>' +
          '<label class="ph-field"><span>结束</span><input type="text" data-field="end" value="' +
            ui.escapeHtml(range.end || '') + '" placeholder="2023-08-31"></label>' +
        '</div>' +
        '<div class="ph-section-note">时间范围内的照片会自动出现在这个相册里，' +
          '以后新导入的也会自动归类进来。</div>' +
      '</div>' +
      '<div class="ph-section-note" data-role="manual">' +
        (selected ? '会把当前选中的 ' + selected + ' 张放进这个相册。'
                  : '还没有选中照片，可以先建一个空相册，之后再往里放。') +
      '</div>';

    this._openModal('新建相册', body, '创建', function (form) {
      return self._doCreateAlbum(form);
    });
  }

  /** 当前筛选结果的时间范围（用来给「按时间段」相册做默认值） */
  _currentRange() {
    return rangeOf(this.visible);
  }

  async _doCreateAlbum(form) {
    const name = form.querySelector('[data-field="name"]').value.trim();
    const kind = form.querySelector('[data-field="kind"]').value;
    if (!name) {
      ui.showAlert('相册名不能为空', '给相册起个名字。', 'warning');
      return false;
    }

    const payload = { name: name, kind: kind };
    if (kind === 'smart') {
      payload.start = form.querySelector('[data-field="start"]').value.trim();
      payload.end = form.querySelector('[data-field="end"]').value.trim();
      if (!payload.start && !payload.end) {
        ui.showAlert('需要时间范围', '按时间段归类时，至少要给一个开始或结束时间。', 'warning');
        return false;
      }
    } else {
      payload.items = Array.from(this.selection);
    }

    try {
      const data = await api.photosCreateAlbum(payload);
      ui.toast(data.message || '已创建', 'success');
      await this.load();
      if (data.album) {
        this.setFilter('album', data.album.id);
      }
      return true;
    } catch (err) {
      ui.showAlert('创建失败', (err && err.message) || String(err), 'error');
      return false;
    }
  }

  async addSelectionToAlbum() {
    if (!this.selection.size) {
      return;
    }
    const manual = this.albums.filter(function (album) { return album.kind !== 'smart'; });
    if (!manual.length) {
      ui.showAlert('还没有手动相册', '先用侧栏「相册」旁边的 + 建一个吧。', 'warning');
      return;
    }

    const self = this;
    const ids = Array.from(this.selection);
    const body = '<label class="ph-field"><span>目标相册</span><select data-field="album">' +
      manual.map(function (album) {
        return '<option value="' + ui.escapeHtml(album.id) + '">' + ui.escapeHtml(album.name) + '</option>';
      }).join('') +
      '</select></label><div class="ph-section-note">把选中的 ' + ids.length + ' 张加进去。</div>';

    this._openModal('加入相册', body, '加入', async function (form) {
      const albumId = form.querySelector('[data-field="album"]').value;
      try {
        await api.photosAlbumItems(albumId, ids, 'add');
        ui.toast('已加入相册', 'success');
        await self.load();
        return true;
      } catch (err) {
        ui.showAlert('加入失败', (err && err.message) || String(err), 'error');
        return false;
      }
    });
  }

  /* ======================================================================
     模态框（窗口内浮层）
     ====================================================================== */

  _openModal(title, bodyHtml, okText, onSubmit, opts) {
    const self = this;
    const options = opts || {};

    /* ★ 监听器只在**第一次**打开时挂一次。每次打开都挂的话，
       用几次之后点一下「确定」就会把回调跑好几遍
       （表现是同一个操作提交多次），而且这种 bug 只会随着使用次数变严重。 */
    if (!this._modalWired) {
      this._modalWired = true;
      this.$modal.addEventListener('click', async function (event) {
        const button = event.target.closest('button');
        if (!button) {
          if (event.target.classList.contains('ph-modal-backdrop')) {
            self._closeModal();
          }
          return;
        }
        if (button.dataset.act === 'cancel') {
          self._closeModal();
          return;
        }
        if (button.dataset.act !== 'ok' || !self._modalSubmit || self._modalBusy) {
          return;
        }
        /* 连点两下不该提交两次（导入、建相册这类操作重复执行代价不小） */
        self._modalBusy = true;
        button.disabled = true;
        try {
          const result = await self._modalSubmit(self.$modal.querySelector('.ph-modal-body'));
          if (result !== false && !self._modalKeepOpen) {
            self._closeModal();
          }
        } finally {
          self._modalBusy = false;
          button.disabled = false;
        }
      });
    }

    this._modalSubmit = onSubmit;
    this._modalKeepOpen = !!options.keepOpen;

    this.$modal.hidden = false;
    this.$modal.innerHTML =
      '<div class="ph-modal-backdrop"></div>' +
      '<div class="ph-modal">' +
        '<div class="ph-modal-head">' + ui.escapeHtml(title) + '</div>' +
        '<div class="ph-modal-body">' + bodyHtml + '</div>' +
        '<div class="ph-modal-foot">' +
          '<button class="ph-btn" data-act="cancel">取消</button>' +
          '<button class="ph-btn primary" data-act="ok">' + ui.escapeHtml(okText || '确定') + '</button>' +
        '</div>' +
      '</div>';

    const form = this.$modal.querySelector('.ph-modal-body');

    /* 「按时间段」的那些输入框在选「手动」时应当隐藏 —— 留着会让人以为
       手动相册也要填时间范围 */
    const kindSelect = form.querySelector('[data-field="kind"]');
    if (kindSelect) {
      const sync = function () {
        const smart = kindSelect.value === 'smart';
        const smartBox = form.querySelector('[data-role="smart"]');
        const manualBox = form.querySelector('[data-role="manual"]');
        if (smartBox) {
          smartBox.hidden = !smart;
        }
        if (manualBox) {
          manualBox.hidden = smart;
        }
      };
      kindSelect.addEventListener('change', sync);
      sync();
    }

    return this.$modal;
  }

  _closeModal() {
    this.$modal.hidden = true;
    this.$modal.innerHTML = '';
    this._modalSubmit = null;
    this._modalKeepOpen = false;
  }

  /* ======================================================================
     生命周期
     ====================================================================== */

  destroy() {
    if (this._observer) {
      this._observer.disconnect();
      this._observer = null;
    }
    this._closeViewer();
    this._closeModal();
  }
}

/* --------------------------------------------------------------------------
   窗口入口（单例）
   -------------------------------------------------------------------------- */

/**
 * 打开照片窗口。
 *
 * @param {object} desktop 桌面实例
 * @param {object} geom    可选几何信息 {x, y, width, height, silent}，
 *                         由 sessionstate.js 还原布局时传入（与 openMusic 同款）。
 *                         单例：还原时若窗口已经在，就只把它拉到前面。
 */
export function openPhotos(desktop, geom) {
  if (openRecord && wm.get(openRecord.id)) {
    const win = openRecord.win;
    if (win) {
      if (win.min) {
        win.restore();
      }
      win.focus();
    }
    return openRecord;
  }

  const box = geom || {};
  const app = new PhotosApp(desktop);

  const record = wm.create({
    title: '照片',
    iconName: 'photos',
    content: app.root,
    width: Number.isFinite(Number(box.width)) ? Number(box.width) : 1180,
    height: Number.isFinite(Number(box.height)) ? Number(box.height) : 720,
    x: Number.isFinite(Number(box.x)) ? Number(box.x) : undefined,
    y: Number.isFinite(Number(box.y)) ? Number(box.y) : undefined,
    silent: !!box.silent,
    minWidth: 720,
    minHeight: 460,
    windowClass: 'photos-win',
    taskLabel: '照片',
    onClosed: function () {
      app.destroy();
      if (openRecord && openRecord.id === record.id) {
        openRecord = null;
      }
    }
  });

  openRecord = record;
  app.load().catch(function () { /* 错误状态已经显示在主区里了 */ });
  return record;
}

export default openPhotos;

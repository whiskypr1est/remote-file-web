/* ==========================================================================
   音乐播放器窗口
   --------------------------------------------------------------------------
   仿 QQ 音乐的三栏布局：左边是我的歌单，中间是歌曲列表，右边是可收起的歌词
   （带逐句高亮），底部是播放条（封面 / 歌名 / 传输控制 / 播放模式 / 进度 /
   音量）。工具栏负责「导入 / 上传 / 新建歌单 / 搜索」。

   几个刻意的取舍
   --------------
   1. **单例窗口**：播放器开着两个会同时出声，而且「当前播放」这份状态没有
      地方安放。所以再次点入口时只把已有窗口拉到前面，而不是再开一个
      （与本项目其它窗口「允许开多个」的风格不同，这里是有意的）。

   2. **不自动播放**：恢复上次的歌只把它**摆好**，不替你按下播放键。
      浏览器本来也会拦掉没有用户交互的自动播放，硬试只会换来一个报错的
      Promise 和用户的一脸问号。

   3. **时长按需探测**：服务端不解析音频（那要引入一个音频元数据库），
      列表里的时长由浏览器读 `<audio>` 元数据得到。只探测**当前视图的前 N 首**，
      而且是串行的 —— 一个几百首的曲库如果一上来就并发几十个请求，
      启动瞬间会把服务端打得很难看。

   4. **封面用生成的占位图**：不解析 ID3 里的专辑封面（那要自己写 ID3/FLAC
      元数据解析），改成按歌名散列出一个渐变色 + 首字。视觉上够用，
      而且零解析风险。

   5. **导入走内置文件选择器**：不弹系统文件框（那选的是**服务器**上的路径，
      浏览器根本看不到），而是列「我能看到的根目录」让用户点选 ——
      与文件管理器同一套可见性规则。
   ========================================================================== */

import * as api from './api.js';
import * as ui from './ui.js';
import { icon } from './icons.js';
import { wm } from './wins.js';

/** 能播的扩展名（与服务端 music.PLAYABLE_EXTENSIONS 保持一致） */
const AUDIO_EXTENSIONS = ['mp3', 'flac', 'm4a', 'aac', 'ogg', 'oga', 'opus', 'wav', 'webm'];

/** 四种播放模式：列表循环 / 单曲循环 / 顺序播放 / 随机播放 */
const PLAY_MODES = [
  { key: 'list', label: '列表循环', iconName: 'repeat' },
  { key: 'single', label: '单曲循环', iconName: 'repeat-one' },
  { key: 'order', label: '顺序播放', iconName: 'arrow-right' },
  { key: 'shuffle', label: '随机播放', iconName: 'shuffle' }
];

/** 列表里最多探测几首的时长（见文件头第 3 点） */
const DURATION_PROBE_LIMIT = 40;

/** 播放偏好回写服务的防抖间隔（拖动音量条时不必每动一下发一次） */
const PREFS_SAVE_DELAY = 900;

const AUDIO_ACCEPT = AUDIO_EXTENSIONS.map(function (ext) { return '.' + ext; }).join(',');

/* ---------------------------------------------------------------------------
   纯函数
   --------------------------------------------------------------------------- */

/** 秒 -> mm:ss（超过一小时给 h:mm:ss） */
function formatDuration(seconds) {
  const total = Math.round(Number(seconds) || 0);
  if (!isFinite(total) || total <= 0) {
    return '--:--';
  }
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = function (n) { return (n < 10 ? '0' : '') + n; };
  return h > 0 ? (h + ':' + pad(m) + ':' + pad(s)) : (m + ':' + pad(s));
}

/**
 * 解析 LRC 歌词。
 *
 * 返回 {synced, lines}：synced 为真表示有时间标签，可以逐句高亮；
 * 否则就是一段纯文本，只做静态展示。
 *
 * ★ 一行可能有**多个**时间标签（副歌复用同一句词时很常见，例如
 *   `[00:12.00][01:20.00]同一句`）。所以要按标签逐个展开，而不是只取第一个 ——
 *   只取第一个的话，第二段副歌就不会亮。
 */
export function parseLrc(text) {
  const raw = String(text || '');
  const tagRe = /\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]/g;
  const lines = [];
  let synced = false;

  raw.split(/\r?\n/).forEach(function (line) {
    const tags = [];
    let match;
    tagRe.lastIndex = 0;
    while ((match = tagRe.exec(line)) !== null) {
      const minutes = Number(match[1]) || 0;
      const seconds = Number(match[2]) || 0;
      let frac = match[3] || '0';
      // `.5` 是半秒、`.50` 是 0.5 秒、`.500` 是 0.5 秒 —— 按位数补齐
      const millis = Number(frac) / Math.pow(10, frac.length);
      tags.push(minutes * 60 + seconds + millis);
    }

    // 去掉所有时间标签之后剩下的才是歌词正文
    const body = line.replace(tagRe, '').trim();

    if (tags.length) {
      synced = true;
      tags.forEach(function (time) {
        lines.push({ time: time, text: body });
      });
    } else if (body) {
      lines.push({ time: null, text: body });
    }
  });

  if (synced) {
    lines.sort(function (a, b) { return (a.time || 0) - (b.time || 0); });
  }
  return { synced: synced, lines: lines };
}

/** 按歌名散列出一个稳定的色相，给占位封面用 */
function coverHue(seed) {
  const text = String(seed || '?');
  let hash = 0;
  for (let i = 0; i < text.length; i++) {
    hash = (hash * 31 + text.charCodeAt(i)) % 360;
  }
  return hash;
}

function isAudioName(name) {
  const ext = String(name || '').split('.').pop().toLowerCase();
  return AUDIO_EXTENSIONS.indexOf(ext) >= 0;
}

function extOf(name) {
  const parts = String(name || '').split('.');
  return parts.length > 1 ? parts.pop().toLowerCase() : '';
}

/* ---------------------------------------------------------------------------
   控制器
   --------------------------------------------------------------------------- */

class MusicPlayer {

  constructor(desktop) {
    this.desktop = desktop || null;
    this.root = document.createElement('div');
    this.root.className = 'ms-root music';
    this.root.innerHTML = this.template();

    this.$ = {
      sidebar: this.root.querySelector('.ms-sidebar'),
      list: this.root.querySelector('.ms-list-body'),
      listHead: this.root.querySelector('.ms-list-title'),
      listMeta: this.root.querySelector('.ms-list-meta'),
      lyrics: this.root.querySelector('.ms-lyrics-body'),
      lyricsTitle: this.root.querySelector('.ms-lyrics-name'),
      search: this.root.querySelector('.ms-search input'),
      status: this.root.querySelector('.ms-status'),
      audio: this.root.querySelector('audio'),
      cover: this.root.querySelector('.ms-cover'),
      coverArt: this.root.querySelector('.ms-cover-art'),
      title: this.root.querySelector('.ms-now-title'),
      artist: this.root.querySelector('.ms-now-artist'),
      playBtn: this.root.querySelector('.ms-play'),
      modeBtn: this.root.querySelector('.ms-mode'),
      position: this.root.querySelector('.ms-position'),
      duration: this.root.querySelector('.ms-duration'),
      progress: this.root.querySelector('.ms-progress'),
      volume: this.root.querySelector('.ms-volume'),
      volumeBtn: this.root.querySelector('.ms-volume-btn'),
      picker: this.root.querySelector('.ms-picker'),
      pickerCrumb: this.root.querySelector('.ms-picker-crumb'),
      pickerList: this.root.querySelector('.ms-picker-list'),
      pickerFoot: this.root.querySelector('.ms-picker-foot'),
      lyricsEdit: this.root.querySelector('.ms-lyrics-edit'),
      lyricsArea: this.root.querySelector('.ms-lyrics-area'),
      fileInput: this.root.querySelector('.ms-file-input'),
      lrcInput: this.root.querySelector('.ms-lrc-input')
    };

    this.songs = [];
    this.playlists = [];
    this.library = {};
    this.prefs = { volume: 0.8, mode: 'list', muted: false, last: '', source: 'all' };
    this.view = { type: 'all', id: '' };
    this.queue = [];            // 当前视图对应的有序歌曲数组
    this.currentId = '';
    this.lyrics = { synced: false, lines: [], id: '' };
    this.activeLine = -1;
    this.durations = {};        // id -> 秒（浏览器探测出来的）
    this.probing = false;
    this.closed = false;
    this.seeking = false;
    this.saveTimer = null;
    this.shuffleHistory = [];
    this.picker = { root: '', path: '', selected: [], entries: [], loading: false };

    this.bind();
    // 首帧就把音量摆到位，避免「先以 100% 响一下再跳到设定值」
    this.applyVolume();
    this.applyMode();
  }

  /* -- 模板 --------------------------------------------------------------- */

  template() {
    return [
      '<div class="ms-toolbar">',
      '  <button type="button" class="ms-btn primary" data-act="pick">', icon('plus'), '<span>导入歌曲</span></button>',
      '  <button type="button" class="ms-btn" data-act="upload">', icon('upload'), '<span>上传</span></button>',
      '  <button type="button" class="ms-btn" data-act="new-playlist">', icon('list'), '<span>新建歌单</span></button>',
      '  <span class="ms-spacer"></span>',
      '  <span class="ms-status"></span>',
      '  <label class="ms-search">', icon('search'),
      '    <input type="text" placeholder="搜索歌曲 / 歌手" autocomplete="off" spellcheck="false">',
      '  </label>',
      '</div>',

      '<div class="ms-main">',
      '  <div class="ms-sidebar"></div>',

      '  <div class="ms-list">',
      '    <div class="ms-list-head">',
      '      <span class="ms-list-title">全部歌曲</span>',
      '      <span class="ms-list-meta"></span>',
      '    </div>',
      '    <div class="ms-table-wrap"><table class="ms-table">',
      '      <thead><tr>',
      '        <th style="width:44px">#</th>',
      '        <th>标题</th>',
      '        <th style="width:150px">歌手</th>',
      '        <th style="width:74px">时长</th>',
      '        <th style="width:56px" title="有歌词">词</th>',
      '        <th style="width:132px">操作</th>',
      '      </tr></thead>',
      '      <tbody class="ms-list-body"></tbody>',
      '    </table></div>',
      '  </div>',

      '  <div class="ms-lyrics">',
      '    <div class="ms-lyrics-head">',
      '      <span class="ms-lyrics-name">歌词</span>',
      '      <span class="ms-spacer"></span>',
      '      <button type="button" class="ms-mini" data-act="lyrics-upload" title="上传 .lrc 文件">上传</button>',
      '      <button type="button" class="ms-mini" data-act="lyrics-edit" title="直接粘贴 / 编辑歌词">编辑</button>',
      '    </div>',
      '    <div class="ms-lyrics-body"><div class="ms-lyrics-empty">播放一首歌就会显示歌词</div></div>',
      '  </div>',
      '</div>',

      '<div class="ms-player">',
      '  <div class="ms-cover"><div class="ms-cover-art"></div></div>',
      '  <div class="ms-now">',
      '    <div class="ms-now-title">未在播放</div>',
      '    <div class="ms-now-artist"></div>',
      '  </div>',
      '  <div class="ms-transport">',
      '    <button type="button" class="ms-icon-btn" data-act="prev" title="上一首">', icon('prev'), '</button>',
      '    <button type="button" class="ms-icon-btn ms-play" data-act="toggle" title="播放 / 暂停">', icon('play'), '</button>',
      '    <button type="button" class="ms-icon-btn" data-act="next" title="下一首">', icon('next'), '</button>',
      '  </div>',
      '  <div class="ms-progress-wrap">',
      '    <span class="ms-time ms-position">00:00</span>',
      '    <input type="range" class="ms-progress" min="0" max="1000" value="0" step="1" aria-label="播放进度">',
      '    <span class="ms-time ms-duration">--:--</span>',
      '  </div>',
      '  <div class="ms-right">',
      '    <button type="button" class="ms-mode" data-act="mode" title="播放模式">',
      icon('repeat'), '<span>列表</span></button>',
      '    <button type="button" class="ms-icon-btn ms-volume-btn" data-act="mute" title="静音 / 取消静音">',
      icon('volume'), '</button>',
      '    <input type="range" class="ms-volume" min="0" max="100" value="80" step="1" aria-label="音量">',
      '  </div>',
      '</div>',

      '<audio preload="metadata"></audio>',
      '<input type="file" class="ms-file-input" accept="' + AUDIO_ACCEPT + '" multiple hidden>',
      '<input type="file" class="ms-lrc-input" accept=".lrc,.txt,text/plain" hidden>',

      /* 导入用的文件选择器（覆盖在列表区域上） */
      '<div class="ms-picker" hidden>',
      '  <div class="ms-picker-head">',
      '    <span class="ms-picker-title">从文件里选歌</span>',
      '    <span class="ms-spacer"></span>',
      '    <button type="button" class="ms-mini" data-act="picker-close">', icon('close'), '</button>',
      '  </div>',
      '  <div class="ms-picker-crumb"></div>',
      '  <div class="ms-picker-list"></div>',
      '  <div class="ms-picker-foot">',
      '    <span class="ms-picker-count">未选择</span>',
      '    <span class="ms-spacer"></span>',
      '    <button type="button" class="ms-btn" data-act="picker-cancel">取消</button>',
      '    <button type="button" class="ms-btn primary" data-act="picker-import">导入</button>',
      '  </div>',
      '</div>',

      /* 歌词编辑（粘贴 / 修改） */
      '<div class="ms-lyrics-edit" hidden>',
      '  <div class="ms-picker-head">',
      '    <span class="ms-picker-title">编辑歌词（支持 LRC 时间标签）</span>',
      '    <span class="ms-spacer"></span>',
      '    <button type="button" class="ms-mini" data-act="lyrics-close">', icon('close'), '</button>',
      '  </div>',
      '  <textarea class="ms-lyrics-area" spellcheck="false" ',
      '    placeholder="[00:01.00]第一句&#10;[00:05.50]第二句"></textarea>',
      '  <div class="ms-picker-foot">',
      '    <span class="ms-spacer"></span>',
      '    <button type="button" class="ms-btn" data-act="lyrics-cancel">取消</button>',
      '    <button type="button" class="ms-btn primary" data-act="lyrics-save">保存</button>',
      '  </div>',
      '</div>'
    ].join('');
  }

  /* -- 事件绑定 ----------------------------------------------------------- */

  bind() {
    const self = this;

    this.root.addEventListener('click', function (ev) {
      const target = ev.target.closest ? ev.target.closest('[data-act]') : null;
      if (!target) {
        return;
      }
      self.action(target.dataset.act, target);
    });

    // 列表 / 歌单 / 歌词都靠委托，重绘时不必重新绑
    this.$.list.addEventListener('click', function (ev) {
      const row = ev.target.closest ? ev.target.closest('[data-row-act]') : null;
      if (row) {
        ev.stopPropagation();
        self.rowAction(row.dataset.rowAct, row.dataset.song, row.dataset.playlist, ev);
        return;
      }
      const playRow = ev.target.closest ? ev.target.closest('tr[data-song]') : null;
      if (playRow) {
        self.playSong(playRow.dataset.song);
      }
    });
    this.$.list.addEventListener('dblclick', function (ev) {
      const playRow = ev.target.closest ? ev.target.closest('tr[data-song]') : null;
      if (playRow) {
        self.playSong(playRow.dataset.song);
      }
    });

    this.$.sidebar.addEventListener('click', function (ev) {
      const item = ev.target.closest ? ev.target.closest('[data-view]') : null;
      if (item) {
        self.switchView(item.dataset.view, item.dataset.id || '');
        return;
      }
      const menu = ev.target.closest ? ev.target.closest('[data-pl-act]') : null;
      if (menu) {
        ev.stopPropagation();
        self.playlistAction(menu.dataset.plAct, menu.dataset.id);
      }
    });

    this.$.lyrics.addEventListener('click', function (ev) {
      const line = ev.target.closest ? ev.target.closest('[data-time]') : null;
      if (line && self.currentId) {
        // 点歌词跳转 —— QQ 音乐同款行为，成本极低但很好用
        try {
          self.$.audio.currentTime = Number(line.dataset.time) || 0;
        } catch (err) { /* 还没有可用的媒体时忽略 */ }
      }
    });

    this.$.search.addEventListener('input', function () { self.renderList(); });

    this.$.audio.addEventListener('loadedmetadata', function () {
      const seconds = self.$.audio.duration;
      if (isFinite(seconds) && seconds > 0 && self.currentId) {
        self.durations[self.currentId] = seconds;
        self.renderList();
      }
      self.renderProgress();
    });
    this.$.audio.addEventListener('timeupdate', function () {
      self.renderProgress();
      self.highlightLyric();
    });
    this.$.audio.addEventListener('ended', function () { self.onEnded(); });
    this.$.audio.addEventListener('play', function () { self.renderTransport(); });
    this.$.audio.addEventListener('pause', function () { self.renderTransport(); });
    this.$.audio.addEventListener('error', function () {
      if (!self.currentId) {
        return;
      }
      ui.toast('这首歌播放失败（文件可能已损坏，或浏览器不支持该格式）', 'error');
      self.renderTransport();
    });

    this.$.progress.addEventListener('input', function () {
      self.seeking = true;
      self.$.position.textContent = formatDuration(
        (Number(self.$.progress.value) / 1000) * (self.$.audio.duration || 0));
    });
    this.$.progress.addEventListener('change', function () {
      const total = self.$.audio.duration || 0;
      if (total > 0) {
        self.$.audio.currentTime = (Number(self.$.progress.value) / 1000) * total;
      }
      self.seeking = false;
    });

    this.$.volume.addEventListener('input', function () {
      self.prefs.volume = Number(self.$.volume.value) / 100;
      self.prefs.muted = false;
      self.applyVolume();
      self.savePrefsSoon();
    });

    this.$.fileInput.addEventListener('change', function () {
      self.uploadFiles(Array.prototype.slice.call(self.$.fileInput.files || []));
      self.$.fileInput.value = '';
    });
    this.$.lrcInput.addEventListener('change', function () {
      const file = (self.$.lrcInput.files || [])[0];
      self.$.lrcInput.value = '';
      if (file) {
        self.readLyricsFile(file);
      }
    });

    this.$.pickerList.addEventListener('click', function (ev) {
      self.onPickerClick(ev);
    });

    // 键盘：空格播放/暂停、左右切歌（与常见播放器一致）
    this.onKeyDown = function (ev) {
      if (self.closed || !self.root.contains(document.activeElement)) {
        return;
      }
      const tag = (document.activeElement && document.activeElement.tagName) || '';
      if (tag === 'INPUT' || tag === 'TEXTAREA') {
        return;
      }
      if (ev.code === 'Space') {
        ev.preventDefault();
        self.toggle();
      } else if (ev.code === 'ArrowRight' && ev.ctrlKey) {
        self.next(false);
      } else if (ev.code === 'ArrowLeft' && ev.ctrlKey) {
        self.prev();
      }
    };
    document.addEventListener('keydown', this.onKeyDown);
  }

  action(act, el) {
    switch (act) {
      case 'pick': this.openPicker(); break;
      case 'upload': this.$.fileInput.click(); break;
      case 'new-playlist': this.newPlaylist(); break;
      case 'prev': this.prev(); break;
      case 'next': this.next(false); break;
      case 'toggle': this.toggle(); break;
      case 'mode': this.cycleMode(); break;
      case 'mute': this.toggleMute(); break;
      case 'lyrics-upload': this.$.lrcInput.click(); break;
      case 'lyrics-edit': this.openLyricsEditor(); break;
      case 'lyrics-close':
      case 'lyrics-cancel': this.$.lyricsEdit.hidden = true; break;
      case 'lyrics-save': this.saveLyrics(); break;
      case 'picker-close':
      case 'picker-cancel': this.closePicker(); break;
      case 'picker-import': this.doImport(); break;
      default: break;
    }
    void el;
  }

  /* -- 数据 --------------------------------------------------------------- */

  load(keepStatus) {
    const self = this;
    return api.musicLibrary().then(function (data) {
      self.songs = data.songs || [];
      self.playlists = data.playlists || [];
      self.library = data.library || {};
      self.limits = data.limits || {};
      self.prefs = Object.assign(self.prefs, data.prefs || {});

      self.$.volume.value = String(Math.round((self.prefs.volume || 0) * 100));
      self.applyVolume();
      self.applyMode();
      self.renderSidebar();
      self.renderList();
      self.renderLyricsPanel();

      // 恢复上次看的是哪个歌单（偏好里存了 source）—— QQ 音乐同款「记住上次」
      if (!self.viewRestored) {
        self.viewRestored = true;
        const source = String(self.prefs.source || '');
        if (source && source !== 'all' &&
            self.playlists.some(function (pl) { return pl.id === source; })) {
          self.switchView('playlist', source);
        }
      }

      if (!keepStatus) {
        self.setStatus(self.library.count + ' 首 · ' + (self.library.total_text || ''));
      }
      // 把上次那首摆好（不自动播放，见文件头第 2 点）
      if (!self.currentId && self.prefs.last &&
          self.songs.some(function (s) { return s.id === self.prefs.last; })) {
        self.prepareSong(self.prefs.last);
      }
      self.probeDurations();
      return data;
    }).catch(function (err) {
      self.setStatus((err && err.message) || '读取曲库失败', true);
      throw err;
    });
  }

  setStatus(text, isError) {
    this.$.status.textContent = text || '';
    this.$.status.classList.toggle('error', !!isError);
  }

  songById(id) {
    const needle = String(id || '');
    let found = null;
    this.songs.forEach(function (song) {
      if (song.id === needle) {
        found = song;
      }
    });
    return found;
  }

  /** 当前视图（全部 / 某个歌单）对应的歌曲列表，按搜索词过滤 */
  visibleSongs() {
    let list = this.songs;

    if (this.view.type === 'playlist') {
      const playlist = this.playlists.filter(function (pl) {
        return pl.id === this.view.id;
      }, this)[0];
      const ids = playlist ? playlist.songs : [];
      // 按歌单里的顺序排（歌单本身就是一种排序），而不是按文件名
      list = ids.map(function (id) {
        return this.songById(id);
      }, this).filter(Boolean);
    }

    const query = String(this.$.search.value || '').trim().toLowerCase();
    if (query) {
      list = list.filter(function (song) {
        return (song.title + ' ' + song.artist + ' ' + song.id)
          .toLowerCase().indexOf(query) >= 0;
      });
    }
    return list.slice();
  }

  /* -- 渲染：侧栏 --------------------------------------------------------- */

  renderSidebar() {
    const self = this;
    const current = this.view;
    let html = '';

    html += '<div class="ms-side-title">音乐库</div>';
    html += this.sideItem('all', '', 'list', '全部歌曲', this.songs.length);

    const withLyrics = this.songs.filter(function (s) { return s.has_lyrics; }).length;
    html += '<div class="ms-side-sub">' + withLyrics + ' 首带歌词</div>';

    html += '<div class="ms-side-title">我的歌单</div>';
    if (!this.playlists.length) {
      html += '<div class="ms-side-empty">还没有歌单</div>';
    }
    this.playlists.forEach(function (playlist) {
      html += self.sideItem('playlist', playlist.id, 'music', playlist.name,
                            playlist.songs.length);
    });

    html += '<div class="ms-side-title">曲库位置</div>';
    html += '<div class="ms-side-path" title="' +
      ui.escapeHtml(this.library.dir || '') + '">' +
      ui.escapeHtml(this.library.dir || '') + '</div>';
    if (this.library.per_user) {
      html += '<div class="ms-side-sub">每人一个独立曲库</div>';
    } else {
      html += '<div class="ms-side-sub">全机共用同一个曲库</div>';
    }

    this.$.sidebar.innerHTML = html;
    void current;
  }

  sideItem(type, id, iconName, label, count) {
    const active = this.view.type === type && String(this.view.id) === String(id);
    let html = '<div class="ms-side-item' + (active ? ' active' : '') +
      '" data-view="' + type + '" data-id="' + ui.escapeHtml(id) + '">' +
      icon(iconName) +
      '<span class="ms-side-label">' + ui.escapeHtml(label) + '</span>' +
      '<span class="ms-side-count">' + (count || 0) + '</span>';

    if (type === 'playlist') {
      html += '<span class="ms-side-acts">' +
        '<button type="button" class="ms-side-act" data-pl-act="rename" data-id="' +
        ui.escapeHtml(id) + '" title="重命名">' + icon('pencil') + '</button>' +
        '<button type="button" class="ms-side-act" data-pl-act="delete" data-id="' +
        ui.escapeHtml(id) + '" title="删除歌单">' + icon('trash') + '</button>' +
        '</span>';
    }
    return html + '</div>';
  }

  /* -- 渲染：列表 --------------------------------------------------------- */

  renderList() {
    const list = this.visibleSongs();
    this.queue = list;
    const self = this;
    const query = String(this.$.search.value || '').trim();

    this.$.listTitle.textContent = this.view.type === 'playlist'
      ? this.playlistName(this.view.id)
      : '全部歌曲';
    this.$.listMeta.textContent = query
      ? '匹配 ' + list.length + ' / ' + this.songs.length + ' 首'
      : list.length + ' 首';

    let html = '';
    list.forEach(function (song, index) {
      const playing = song.id === self.currentId;
      const duration = self.durations[song.id];
      html += '<tr' + (playing ? ' class="playing"' : '') +
        ' data-song="' + ui.escapeHtml(song.id) + '">' +
        '<td class="ms-num">' + (playing
          ? '<span class="ms-playing">' + icon('volume') + '</span>'
          : (index + 1)) + '</td>' +
        '<td class="ms-title" title="' + ui.escapeHtml(song.id) + '">' +
          ui.escapeHtml(song.title || song.id) +
          (song.has_lyrics ? '' : '') + '</td>' +
        '<td class="ms-artist">' + ui.escapeHtml(song.artist || '—') + '</td>' +
        '<td class="ms-dur">' + formatDuration(duration) + '</td>' +
        '<td class="ms-lyric-flag">' + (song.has_lyrics
          ? '<span class="ms-dot" title="有歌词"></span>' : '') + '</td>' +
        '<td class="ms-acts">' +
          '<button type="button" class="ms-mini" data-row-act="add" data-song="' +
            ui.escapeHtml(song.id) + '">加入歌单</button>' +
          (self.view.type === 'playlist'
            ? '<button type="button" class="ms-mini" data-row-act="remove" data-song="' +
              ui.escapeHtml(song.id) + '" data-playlist="' + ui.escapeHtml(self.view.id) +
              '">移出</button>'
            : '<button type="button" class="ms-mini danger" data-row-act="delete" data-song="' +
              ui.escapeHtml(song.id) + '">移除</button>') +
        '</td>' +
        '</tr>';
    });

    if (!html) {
      html = '<tr><td colspan="6" class="ms-empty">' +
        (query ? '没有匹配的歌曲'
               : (this.view.type === 'playlist'
                  ? '这个歌单还是空的：在「全部歌曲」里点「加入歌单」'
                  : '曲库是空的：点左上角「导入歌曲」把歌放进来')) +
        '</td></tr>';
    }

    this.$.list.innerHTML = html;
  }

  playlistName(id) {
    let name = '歌单';
    this.playlists.forEach(function (playlist) {
      if (playlist.id === id) {
        name = playlist.name;
      }
    });
    return name;
  }

  /* -- 渲染：播放条 ------------------------------------------------------- */

  renderTransport() {
    const paused = this.$.audio.paused;
    this.$.playBtn.innerHTML = icon(paused ? 'play' : 'pause');
    this.$.playBtn.title = paused ? '播放' : '暂停';
  }

  renderProgress() {
    const total = this.$.audio.duration || 0;
    const now = this.$.audio.currentTime || 0;
    this.$.duration.textContent = total > 0 ? formatDuration(total) : '--:--';
    if (!this.seeking) {
      this.$.position.textContent = formatDuration(now);
      this.$.progress.value = total > 0 ? String(Math.round((now / total) * 1000)) : '0';
    }
  }

  renderNowPlaying() {
    const song = this.songById(this.currentId);
    if (!song) {
      this.$.title.textContent = '未在播放';
      this.$.artist.textContent = '';
      this.$.coverArt.textContent = '';
      this.$.coverArt.style.background = '';
      return;
    }
    this.$.title.textContent = song.title || song.id;
    this.$.artist.textContent = song.artist || extOf(song.id).toUpperCase();

    const hue = coverHue(song.title || song.id);
    this.$.coverArt.style.background =
      'linear-gradient(135deg, hsl(' + hue + ',72%,62%), hsl(' +
      ((hue + 48) % 360) + ',68%,48%))';
    this.$.coverArt.textContent = (song.title || song.id).trim().charAt(0) || '♪';
  }

  renderMode() {
    const mode = this.prefs.mode || 'list';
    let spec = PLAY_MODES[0];
    PLAY_MODES.forEach(function (item) {
      if (item.key === mode) {
        spec = item;
      }
    });
    this.$.modeBtn.innerHTML = icon(spec.iconName) + '<span>' +
      ui.escapeHtml(spec.label.slice(0, 2)) + '</span>';
    this.$.modeBtn.title = '播放模式：' + spec.label + '（点击切换）';
  }

  /* -- 播放 --------------------------------------------------------------- */

  prepareSong(id) {
    const song = this.songById(id);
    if (!song) {
      return;
    }
    this.currentId = id;
    this.$.audio.src = api.musicStreamUrl(id);
    this.renderNowPlaying();
    this.renderList();
    this.loadLyrics(id);
  }

  playSong(id) {
    const song = this.songById(id);
    if (!song) {
      return;
    }
    this.prepareSong(id);
    const audio = this.$.audio;
    const promise = audio.play();
    if (promise && typeof promise.catch === 'function') {
      promise.catch(function (err) {
        // 浏览器拦自动播放时不必弹错（用户点一下就好），但要说明白
        ui.toast('浏览器拦下了自动播放，请再点一次播放按钮', 'warn');
        void err;
      });
    }
    this.savePrefsSoon({ last: id });
    this.setStatus('正在播放：' + (song.title || song.id));
  }

  toggle() {
    const audio = this.$.audio;
    if (!this.currentId) {
      // 还没选歌：从当前视图的第一首开始
      const first = this.queue[0] || this.songs[0];
      if (first) {
        this.playSong(first.id);
      } else {
        ui.toast('曲库是空的，先导入几首歌吧', 'warn');
      }
      return;
    }
    if (audio.paused) {
      const promise = audio.play();
      if (promise && typeof promise.catch === 'function') {
        promise.catch(function () { /* 同上，交给用户再点一次 */ });
      }
    } else {
      audio.pause();
    }
  }

  /** 当前播放位置在队列里的下标（-1 表示当前歌不在当前视图里） */
  queueIndex() {
    const self = this;
    let index = -1;
    this.queue.forEach(function (song, i) {
      if (song.id === self.currentId) {
        index = i;
      }
    });
    return index;
  }

  next(auto) {
    if (!this.queue.length) {
      return;
    }
    const mode = this.prefs.mode || 'list';
    const index = this.queueIndex();

    if (mode === 'single') {
      // 单曲循环：手动点「下一首」仍然要换歌（否则按钮看起来坏了）；
      // 只有**自然播完**才重播同一首。
      if (auto) {
        this.$.audio.currentTime = 0;
        const promise = this.$.audio.play();
        if (promise && promise.catch) {
          promise.catch(function () { /* 忽略 */ });
        }
        return;
      }
    }

    if (mode === 'shuffle') {
      const picked = this.pickShuffle();
      if (picked) {
        this.playSong(picked.id);
      }
      return;
    }

    const last = index === this.queue.length - 1 || index < 0;
    if (last) {
      if (mode === 'order') {
        // 顺序播放：到头就停，不绕回去
        this.$.audio.pause();
        this.$.audio.currentTime = 0;
        this.setStatus('已到列表末尾');
        return;
      }
      this.playSong(this.queue[0].id);
      return;
    }
    this.playSong(this.queue[index + 1].id);
  }

  prev() {
    if (!this.queue.length) {
      return;
    }
    const mode = this.prefs.mode || 'list';
    if (mode === 'shuffle') {
      const picked = this.pickShuffle();
      if (picked) {
        this.playSong(picked.id);
      }
      return;
    }

    const index = this.queueIndex();
    // 播了 3 秒以上时，「上一首」先回到本曲开头（与常见播放器一致）
    if (index >= 0 && this.$.audio.currentTime > 3) {
      this.$.audio.currentTime = 0;
      return;
    }
    if (index <= 0) {
      if (mode === 'order') {
        this.$.audio.currentTime = 0;
        return;
      }
      this.playSong(this.queue[this.queue.length - 1].id);
      return;
    }
    this.playSong(this.queue[index - 1].id);
  }

  pickShuffle() {
    if (this.queue.length <= 1) {
      return this.queue[0] || null;
    }
    // 记一个短历史，避免随机播放老是来回跳同一对歌
    this.shuffleHistory.push(this.currentId);
    if (this.shuffleHistory.length > 5) {
      this.shuffleHistory.shift();
    }
    const pool = this.queue.filter(function (song) {
      return song.id !== this.currentId &&
        this.shuffleHistory.indexOf(song.id) < 0;
    }, this);
    const from = pool.length ? pool : this.queue;
    return from[Math.floor(Math.random() * from.length)];
  }

  onEnded() {
    const mode = this.prefs.mode || 'list';
    if (mode === 'order' && this.queueIndex() === this.queue.length - 1) {
      this.setStatus('播放完毕');
      this.renderTransport();
      return;
    }
    this.next(true);
  }

  cycleMode() {
    const keys = PLAY_MODES.map(function (item) { return item.key; });
    const index = keys.indexOf(this.prefs.mode || 'list');
    this.prefs.mode = keys[(index + 1) % keys.length];
    this.applyMode();
    this.savePrefsSoon({ mode: this.prefs.mode });
    this.renderMode();
    ui.toast('播放模式：' + this.modeLabel(), 'info');
  }

  modeLabel() {
    let label = '列表循环';
    PLAY_MODES.forEach(function (item) {
      if (item.key === this.prefs.mode) {
        label = item.label;
      }
    }, this);
    return label;
  }

  applyMode() {
    // 单曲循环交给 ended 事件处理，这里不改 audio.loop：
    // 两种做法混用会出现「单曲循环下按下一首没反应」这种怪现象
    this.$.audio.loop = false;
    this.renderMode();
  }

  applyVolume() {
    this.$.audio.volume = this.prefs.muted ? 0 : Math.min(1, Math.max(0, this.prefs.volume));
    this.$.volume.value = String(Math.round((this.prefs.volume || 0) * 100));
    this.$.volumeBtn.innerHTML = icon(this.prefs.muted || !this.prefs.volume
      ? 'volume-mute' : 'volume');
  }

  toggleMute() {
    this.prefs.muted = !this.prefs.muted;
    this.applyVolume();
    this.savePrefsSoon({ muted: this.prefs.muted });
  }

  savePrefsSoon(patch) {
    if (patch) {
      Object.assign(this.prefs, patch);
    }
    const self = this;
    if (this.saveTimer) {
      clearTimeout(this.saveTimer);
    }
    this.saveTimer = setTimeout(function () {
      self.saveTimer = null;
      api.musicSavePrefs({
        volume: self.prefs.volume,
        mode: self.prefs.mode,
        muted: self.prefs.muted,
        last: self.currentId || self.prefs.last,
        source: self.view.type === 'playlist' ? self.view.id : 'all'
      }).catch(function () { /* 偏好存不上不影响播放，静默即可 */ });
    }, PREFS_SAVE_DELAY);
  }

  /* -- 歌词 --------------------------------------------------------------- */

  loadLyrics(id) {
    const self = this;
    this.lyrics = { synced: false, lines: [], id: id };
    this.activeLine = -1;
    this.renderLyricsPanel();

    return api.musicLyrics(id).then(function (data) {
      if (self.currentId !== id) {
        return;          // 期间已经切歌了，这份结果作废
      }
      const parsed = parseLrc(data.text || '');
      self.lyrics = {
        synced: (data.found && parsed.synced),
        lines: parsed.lines,
        id: id,
        found: !!data.found,
        source: data.source || '',
        plain: !parsed.synced
      };
      self.renderLyricsPanel();
      self.highlightLyric();
    }).catch(function () {
      self.lyrics = { synced: false, lines: [], id: id };
      self.renderLyricsPanel();
    });
  }

  renderLyricsPanel() {
    const song = this.songById(this.currentId);
    this.$.lyricsTitle.textContent = song
      ? (song.title || song.id) + (this.lyrics.source ? '· ' + this.lyrics.source : '')
      : '歌词';

    const lines = this.lyrics.lines || [];
    if (!this.currentId) {
      this.$.lyrics.innerHTML =
        '<div class="ms-lyrics-empty">播放一首歌就会显示歌词</div>';
      return;
    }
    if (!lines.length) {
      this.$.lyrics.innerHTML =
        '<div class="ms-lyrics-empty">' +
        (this.lyrics.found ? '歌词是空的' : '暂无歌词') +
        '<div class="ms-lyrics-tip">把同名 .lrc 放在歌旁边（导入时会自动带进来），' +
        '或点右上角「上传 / 编辑」手动加一份。</div></div>';
      return;
    }

    let html = '';
    lines.forEach(function (line, index) {
      const time = line.time === null ? '' : ' data-time="' + line.time + '"';
      html += '<div class="ms-lyric-line" data-index="' + index + '"' + time + '>' +
        (ui.escapeHtml(line.text) || '&nbsp;') + '</div>';
    });
    this.$.lyrics.innerHTML = html;
    this.activeLine = -1;
  }

  highlightLyric() {
    if (!this.lyrics.synced || !this.lyrics.lines.length) {
      return;
    }
    const now = this.$.audio.currentTime || 0;
    const lines = this.lyrics.lines;

    // 找最后一句「时间 <= 当前」的歌词（线性扫即可，歌词最多几百行）
    let index = -1;
    for (let i = 0; i < lines.length; i++) {
      if ((lines[i].time || 0) <= now + 0.25) {
        index = i;
      } else {
        break;
      }
    }
    if (index === this.activeLine) {
      return;
    }
    this.activeLine = index;

    const nodes = this.$.lyrics.querySelectorAll('.ms-lyric-line');
    nodes.forEach(function (node, i) {
      const on = i === index;
      node.classList.toggle('active', on);
      if (on && node.scrollIntoView) {
        node.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
    });
  }

  openLyricsEditor() {
    if (!this.currentId) {
      ui.toast('先选一首歌', 'warn');
      return;
    }
    const text = (this.lyrics.lines || []).map(function (line) {
      if (line.time === null) {
        return line.text;
      }
      const minutes = Math.floor(line.time / 60);
      const seconds = line.time - minutes * 60;
      const pad = function (n) { return (n < 10 ? '0' : '') + n; };
      return '[' + pad(minutes) + ':' + pad(Math.floor(seconds)) + '.' +
        pad(Math.round((seconds % 1) * 100)) + ']' + line.text;
    }).join('\n');

    this.$.lyricsArea.value = text;
    this.$.lyricsEdit.hidden = false;
    this.$.lyricsArea.focus();
  }

  saveLyrics() {
    const self = this;
    if (!this.currentId) {
      return;
    }
    const text = this.$.lyricsArea.value;
    api.musicSaveLyrics(this.currentId, text).then(function () {
      ui.toast('歌词已保存', 'success');
      self.$.lyricsEdit.hidden = true;
      self.loadLyrics(self.currentId);
      // 列表里的「有词」标记要跟着变
      const song = self.songById(self.currentId);
      if (song) {
        song.has_lyrics = !!(text || '').trim();
        self.renderList();
      }
    }).catch(function (err) {
      ui.toast((err && err.message) || '歌词保存失败', 'error');
    });
  }

  readLyricsFile(file) {
    const self = this;
    if (!this.currentId) {
      ui.toast('先选一首歌，再上传它的歌词', 'warn');
      return;
    }
    const reader = new FileReader();
    reader.onload = function () {
      self.$.lyricsArea.value = String(reader.result || '');
      self.$.lyricsEdit.hidden = false;
      // 不直接保存：让用户先看一眼（选错文件是很常见的）
      ui.toast('已读入「' + file.name + '」，确认后点保存', 'info');
    };
    reader.onerror = function () {
      ui.toast('读取歌词文件失败', 'error');
    };
    reader.readAsText(file, 'utf-8');
  }

  /* -- 上传 --------------------------------------------------------------- */

  uploadFiles(files) {
    const self = this;
    if (!files.length) {
      return;
    }
    const maxMb = (this.limits && this.limits.max_upload_mb) || 0;

    // ★ 先按大小自查：服务端是按 Content-Length 提前拒绝的，那时连接会被
    //   直接重置，浏览器只能报「网络错误」—— 用户看到的提示会很难懂。
    const tooBig = files.filter(function (file) {
      return maxMb && file.size > maxMb * 1024 * 1024;
    });
    const ok = files.filter(function (file) {
      return !maxMb || file.size <= maxMb * 1024 * 1024;
    });
    if (tooBig.length) {
      ui.toast('有 ' + tooBig.length + ' 个文件超过单曲上限 ' + maxMb + 'MB，已跳过', 'warn');
    }
    if (!ok.length) {
      return;
    }

    let done = 0;
    let failed = 0;

    const step = function (index) {
      if (index >= ok.length || self.closed) {
        self.setStatus('上传完成：成功 ' + done + ' 首' +
          (failed ? '，失败 ' + failed + ' 首' : ''));
        self.load(true).catch(function () { /* 已经提示过了 */ });
        return;
      }
      const file = ok[index];
      self.setStatus('正在上传 ' + (index + 1) + '/' + ok.length + '：' + file.name);

      api.musicUpload(file, function (loaded, total) {
        const percent = total ? Math.round((loaded / total) * 100) : 0;
        self.setStatus('正在上传 ' + file.name + ' ' + percent + '%');
      }).then(function () {
        done++;
      }).catch(function (err) {
        failed++;
        ui.toast('「' + file.name + '」上传失败：' +
          ((err && err.message) || '未知错误'), 'error');
      }).then(function () {
        step(index + 1);
      });
    };

    // 串行上传：同时传几首大文件只会互相抢带宽，进度也看不清
    step(0);
  }

  /* -- 导入（内置文件选择器）---------------------------------------------- */

  openPicker() {
    const roots = (this.desktop && this.desktop.info && this.desktop.info.roots) || [];
    if (!roots.length) {
      ui.toast('你没有任何可访问的目录，先把目录分配给这个账号', 'warn');
      return;
    }
    this.picker = { root: roots[0].id, path: '', selected: [], entries: [], loading: false };
    this.$.picker.hidden = false;
    this.loadPickerDir();
  }

  closePicker() {
    this.$.picker.hidden = true;
  }

  loadPickerDir() {
    const self = this;
    this.picker.loading = true;
    this.renderPicker();

    api.listDir({ root: this.picker.root, path: this.picker.path }).then(function (data) {
      if (self.$.picker.hidden) {
        return;
      }
      const entries = (data.entries || []).slice();
      // 目录在前、音频在后，其余不显示（省得在一堆文档里找歌）
      entries.sort(function (a, b) {
        if (a.is_dir !== b.is_dir) {
          return a.is_dir ? -1 : 1;
        }
        return a.name.localeCompare(b.name, 'zh');
      });
      self.picker.entries = entries;
      self.picker.loading = false;
      self.renderPicker();
    }).catch(function (err) {
      self.picker.loading = false;
      self.renderPicker();
      ui.toast((err && err.message) || '读取目录失败', 'error');
    });
  }

  renderPicker() {
    const roots = (this.desktop && this.desktop.info && this.desktop.info.roots) || [];
    const self = this;

    // 面包屑：根目录下拉 + 当前路径
    let crumb = '<select class="ms-picker-root">';
    roots.forEach(function (root) {
      crumb += '<option value="' + ui.escapeHtml(root.id) + '"' +
        (root.id === self.picker.root ? ' selected' : '') + '>' +
        ui.escapeHtml(root.name) + '</option>';
    });
    crumb += '</select>';
    crumb += '<span class="ms-crumb-path">' +
      (this.picker.path ? ui.escapeHtml(this.picker.path) : '（顶层）') + '</span>';

    this.$.pickerCrumb.innerHTML = crumb;
    const select = this.$.pickerCrumb.querySelector('select');
    if (select) {
      select.addEventListener('change', function () {
        self.picker.root = select.value;
        self.picker.path = '';
        self.picker.selected = [];
        self.loadPickerDir();
      });
    }

    let html = '';
    if (this.picker.loading) {
      html = '<div class="ms-picker-empty">正在读取…</div>';
    } else {
      if (this.picker.path) {
        html += '<div class="ms-picker-row dir" data-dir="..">' + icon('folder-open') +
          '<span>..（上一级）</span></div>';
      }
      const shown = this.picker.entries.filter(function (entry) {
        return entry.is_dir || isAudioName(entry.name);
      });
      shown.forEach(function (entry) {
        if (entry.is_dir) {
          html += '<div class="ms-picker-row dir" data-dir="' +
            ui.escapeHtml(entry.name) + '">' + icon('folder') +
            '<span class="ms-picker-name">' + ui.escapeHtml(entry.name) + '</span></div>';
        } else {
          const picked = self.picker.selected.indexOf(entry.name) >= 0;
          html += '<label class="ms-picker-row file' + (picked ? ' picked' : '') + '">' +
            '<input type="checkbox" data-file="' + ui.escapeHtml(entry.name) + '"' +
            (picked ? ' checked' : '') + '>' +
            icon('audio') +
            '<span class="ms-picker-name">' + ui.escapeHtml(entry.name) + '</span>' +
            '<span class="ms-picker-size">' + ui.escapeHtml(entry.size_text || '') + '</span>' +
            '</label>';
        }
      });
      if (!html) {
        html = '<div class="ms-picker-empty">这个目录里没有子目录，也没有可播放的音频</div>';
      }
    }
    this.$.pickerList.innerHTML = html;
    this.$.pickerFoot.querySelector('.ms-picker-count').textContent =
      this.picker.selected.length ? ('已选 ' + this.picker.selected.length + ' 首') : '未选择';
  }

  onPickerClick(ev) {
    const row = ev.target.closest ? ev.target.closest('.ms-picker-row') : null;
    if (row && row.dataset.dir !== undefined && row.dataset.dir !== null) {
      const dir = row.dataset.dir;
      if (dir === '..') {
        const parts = this.picker.path.split('/').filter(Boolean);
        parts.pop();
        this.picker.path = parts.join('/');
      } else {
        this.picker.path = this.picker.path
          ? (this.picker.path + '/' + dir) : dir;
      }
      this.picker.selected = [];
      this.loadPickerDir();
      return;
    }

    const box = ev.target.closest ? ev.target.closest('input[data-file]') : null;
    if (box) {
      const name = box.dataset.file;
      const at = this.picker.selected.indexOf(name);
      if (box.checked && at < 0) {
        this.picker.selected.push(name);
      } else if (!box.checked && at >= 0) {
        this.picker.selected.splice(at, 1);
      }
      const line = box.closest('.ms-picker-row');
      if (line) {
        line.classList.toggle('picked', box.checked);
      }
      this.$.pickerFoot.querySelector('.ms-picker-count').textContent =
        this.picker.selected.length ? ('已选 ' + this.picker.selected.length + ' 首') : '未选择';
    }
  }

  doImport() {
    const self = this;
    const names = this.picker.selected.slice();
    if (!names.length) {
      ui.toast('先勾选要导入的歌', 'warn');
      return;
    }

    const base = this.picker.path;
    const paths = names.map(function (name) {
      return base ? (base + '/' + name) : name;
    });

    this.setStatus('正在导入 ' + names.length + ' 首…');
    api.musicImport(this.picker.root, paths).then(function (data) {
      ui.toast(data.message || '导入完成', 'success');
      if (data.skipped && data.skipped.length) {
        // 跳过的一般是「不是音频」「太大」这类，逐条说清楚
        data.skipped.slice(0, 3).forEach(function (item) {
          ui.toast(item.name + '：' + item.reason, 'warn');
        });
      }
      self.closePicker();
      self.load(true).catch(function () { /* 已经提示过了 */ });
    }).catch(function (err) {
      ui.toast((err && err.message) || '导入失败', 'error');
      self.setStatus('导入失败', true);
    });
  }

  /* -- 歌单 --------------------------------------------------------------- */

  newPlaylist() {
    const self = this;
    ui.showPrompt('新建歌单', '给歌单起个名字', '', { okText: '创建' })
      .then(function (name) {
        if (name === null || name === undefined) {
          return;
        }
        const clean = String(name).trim();
        if (!clean) {
          ui.toast('歌单名不能为空', 'warn');
          return;
        }
        api.musicCreatePlaylist(clean).then(function (data) {
          ui.toast(data.message || '已创建', 'success');
          return self.load(true);
        }).catch(function (err) {
          ui.toast((err && err.message) || '创建失败', 'error');
        });
      });
  }

  playlistAction(act, id) {
    const self = this;
    if (act === 'rename') {
      ui.showPrompt('重命名歌单', '新名字', this.playlistName(id), { okText: '保存' })
        .then(function (name) {
          if (name === null || name === undefined) {
            return;
          }
          api.musicRenamePlaylist(id, String(name).trim()).then(function () {
            return self.load(true);
          }).catch(function (err) {
            ui.toast((err && err.message) || '重命名失败', 'error');
          });
        });
      return;
    }
    if (act === 'delete') {
      ui.showConfirm('删除歌单',
        '要删除歌单「' + this.playlistName(id) + '」吗？' +
        '歌单里的歌**不会**从曲库里删掉。', { danger: true, okText: '删除' })
        .then(function (ok) {
          if (!ok) {
            return;
          }
          api.musicDeletePlaylist(id).then(function () {
            if (self.view.type === 'playlist' && self.view.id === id) {
              self.view = { type: 'all', id: '' };
            }
            return self.load(true);
          }).catch(function (err) {
            ui.toast((err && err.message) || '删除失败', 'error');
          });
        });
    }
  }

  rowAction(act, songId, playlistId, ev) {
    const self = this;

    if (act === 'delete') {
      ui.showConfirm('从曲库移除',
        '要把「' + songId + '」从音乐库移除吗？（连它的歌词一起删；' +
        '导入时复制进来的源文件不受影响）', { danger: true, okText: '移除' })
        .then(function (ok) {
          if (!ok) {
            return;
          }
          api.musicDelete(songId).then(function (data) {
            ui.toast(data.message || '已移除', 'success');
            if (self.currentId === songId) {
              self.$.audio.pause();
              self.$.audio.removeAttribute('src');
              self.currentId = '';
              self.renderNowPlaying();
              self.renderLyricsPanel();
            }
            return self.load(true);
          }).catch(function (err) {
            ui.toast((err && err.message) || '移除失败', 'error');
          });
        });
      return;
    }

    if (act === 'remove') {
      api.musicPlaylistSong(playlistId, songId, 'remove').then(function () {
        return self.load(true);
      }).catch(function (err) {
        ui.toast((err && err.message) || '移出失败', 'error');
      });
      return;
    }

    if (act === 'add') {
      if (!this.playlists.length) {
        ui.showConfirm('还没有歌单', '你还没有创建歌单，现在创建一个吗？', { okText: '创建' })
          .then(function (ok) {
            if (ok) {
              self.newPlaylist();
            }
          });
        return;
      }
      // 用一个轻量菜单挑歌单（项目里已有现成的右键菜单组件）
      const x = ev ? ev.clientX : 240;
      const y = ev ? ev.clientY : 240;
      const items = this.playlists.map(function (playlist) {
        return {
          label: playlist.name,
          iconName: 'music',
          onClick: function () {
            api.musicPlaylistSong(playlist.id, songId, 'add').then(function (data) {
              ui.toast(data.message || '已加入歌单', 'success');
              return self.load(true);
            }).catch(function (err) {
              ui.toast((err && err.message) || '加入失败', 'error');
            });
          }
        };
      });
      ui.showContextMenu(x, y, items);
    }
  }

  switchView(type, id) {
    this.view = { type: type, id: id };
    this.renderSidebar();
    this.renderList();
  }

  /* -- 时长探测 ----------------------------------------------------------- */

  probeDurations() {
    if (this.probing || this.closed) {
      return;
    }
    const self = this;
    const pending = this.visibleSongs()
      .filter(function (song) { return !self.durations[song.id]; })
      .slice(0, DURATION_PROBE_LIMIT);

    if (!pending.length) {
      return;
    }
    this.probing = true;

    // 串行探测：一次只读一首的元数据（preload=metadata 只会取到文件头，
    // 服务端有 Range 支持，不会整首下载）
    const step = function (index) {
      if (index >= pending.length || self.closed) {
        self.probing = false;
        return;
      }
      const song = pending[index];
      const probe = new Audio();
      probe.preload = 'metadata';
      let settled = false;

      const finish = function () {
        if (settled) {
          return;
        }
        settled = true;
        probe.src = '';
        step(index + 1);
      };

      probe.addEventListener('loadedmetadata', function () {
        if (isFinite(probe.duration) && probe.duration > 0) {
          self.durations[song.id] = probe.duration;
          // 只更新那一格，不整表重绘（重绘会把滚动位置和 hover 状态打断）
          const row = self.$.list.querySelector('tr[data-song="' +
            (window.CSS && CSS.escape ? CSS.escape(song.id) : song.id) + '"]');
          if (row) {
            const cell = row.querySelector('.ms-dur');
            if (cell) {
              cell.textContent = formatDuration(probe.duration);
            }
          }
        }
        finish();
      });
      probe.addEventListener('error', finish);
      setTimeout(finish, 8000);      // 兜底，别让一首坏文件卡住整条链
      probe.src = api.musicStreamUrl(song.id);
    };

    step(0);
  }

  destroy() {
    this.closed = true;
    if (this.saveTimer) {
      clearTimeout(this.saveTimer);
      this.saveTimer = null;
    }
    if (this.onKeyDown) {
      document.removeEventListener('keydown', this.onKeyDown);
      this.onKeyDown = null;
    }
    try {
      // 关窗口时必须停掉声音，否则「窗口关了歌还在响」会很吓人
      this.$.audio.pause();
      this.$.audio.removeAttribute('src');
    } catch (err) { /* 忽略 */ }
  }
}

/* ---------------------------------------------------------------------------
   入口（单例）
   --------------------------------------------------------------------------- */

let openRecord = null;

/**
 * 打开音乐播放器。
 *
 * ★ 刻意做成**单例**：两个播放器会同时出声，而且「当前播放」这份状态无处安放。
 *   已经开着就把它拉到前面（最小化了就先还原），而不是再开一个。
 */
export function openMusic(desktop) {
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

  const player = new MusicPlayer(desktop);

  const record = wm.create({
    title: '音乐播放器',
    iconName: 'music',
    content: player.root,
    width: 1080,
    height: 660,
    minWidth: 720,
    minHeight: 460,
    windowClass: 'music-win',
    taskLabel: '音乐播放器',
    onClosed: function () {
      player.destroy();
      if (openRecord && openRecord.id === record.id) {
        openRecord = null;
      }
    }
  });

  openRecord = record;
  player.load().catch(function () { /* 错误状态已经显示在工具栏上了 */ });
  return record;
}

export default openMusic;

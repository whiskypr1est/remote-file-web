/* ==========================================================================
   照片的时间轴逻辑（纯函数，无 DOM）
   --------------------------------------------------------------------------
   为什么单独一个文件
   ------------------
   这一小段是「按时间轴归类」的全部规则：时间怎么分组、没有时间的排哪、
   筛选与排序怎么算。它出错的样子很隐蔽 —— 时间轴看着挺满，其实分组错了、
   或者一批照片被悄悄漏掉，界面上没有任何报错。

   而 photos.js 本身**没法在 node 里测**：它 import 了 wins.js，而 wins.js
   在模块级就 `new WindowManager()`（要摸 document）。所以把这套纯逻辑拆出来，
   它就能像 nowplaying.js 那样被 node 直接跑用例 —— 测的是**与线上一模一样的
   那份实现**，而不是在测试里重写一遍。

   这里的东西都不碰 DOM、不碰网络，输入输出都是普通对象，容易钉住也容易改。
   ========================================================================== */

/* 时间轴的聚合粒度 */
export const LEVELS = [
  { value: 'year', label: '年' },
  { value: 'month', label: '月' },
  { value: 'day', label: '日' }
];

export const SORTS = [
  { value: 'taken_desc', label: '时间（新 → 旧）' },
  { value: 'taken_asc', label: '时间（旧 → 新）' },
  { value: 'name_asc', label: '文件名（A → Z）' },
  { value: 'name_desc', label: '文件名（Z → A）' }
];

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

/* 时间来源 -> 界面上怎么解释它 */
export const SOURCE_TEXT = {
  user: '你手动设置的时间',
  exif: '照片 EXIF 里的拍摄时间',
  file: '文件的修改时间（这张没有拍摄信息，多半是截图 / 聊天里保存的图）'
};

/** 没有可用时间的那一批（文件时间都读不到，极少见）归到这个键 */
export const UNKNOWN_KEY = '~unknown';

/** 把 "2023-08-15T12:34:56" 变成 "2023-08-15 12:34:56" */
export function prettyTime(iso) {
  if (!iso) {
    return '';
  }
  return String(iso).slice(0, 10) + ' ' + String(iso).slice(11, 19);
}

/**
 * 算出一张照片属于时间轴的哪一组，以及这一组的标题。
 *
 * ★ 没有时间的照片单独归一组（UNKNOWN_KEY），**不并进任何年份**：
 *   把它塞进最近的一年会让时间轴看起来「完整」，但那是在编造信息。
 *
 * @param {string} takenAt "2023-08-15T12:34:56" 或空串
 * @param {string} level   year | month | day
 */
export function groupOf(takenAt, level) {
  if (!takenAt) {
    return { key: UNKNOWN_KEY, title: '时间未知', sub: '' };
  }

  const text = String(takenAt);
  const year = text.slice(0, 4);
  const month = Number(text.slice(5, 7));
  const day = Number(text.slice(8, 10));

  if (level === 'year') {
    return { key: year, title: year + ' 年', sub: '' };
  }
  if (level === 'month') {
    return { key: text.slice(0, 7), title: month + ' 月', sub: year + ' 年' };
  }

  const stamp = text.slice(0, 10);
  let weekday = '';
  const parsed = new Date(stamp + 'T00:00:00');
  if (!isNaN(parsed.getTime())) {
    weekday = WEEKDAYS[parsed.getDay()];
  }
  return { key: stamp, title: month + ' 月 ' + day + ' 日', sub: year + ' 年 ' + weekday };
}

/**
 * 把一份照片列表按当前粒度分组（保持传入的顺序）。
 *
 * 返回 [{key, title, sub, photos}]，未知时间那一组固定排在最后 ——
 * 它不属于时间轴上的任何位置，摆在末尾最不容易被误读成「最新的」。
 */
export function groupPhotos(list, level) {
  const groups = [];
  const index = {};

  (list || []).forEach(function (photo) {
    const info = groupOf(photo.taken_at, level);
    let group = index[info.key];
    if (!group) {
      group = { key: info.key, title: info.title, sub: info.sub, photos: [] };
      index[info.key] = group;
      groups.push(group);
    }
    group.photos.push(photo);
  });

  const unknown = groups.filter(function (g) { return g.key === UNKNOWN_KEY; })[0];
  if (unknown) {
    groups.splice(groups.indexOf(unknown), 1);
    groups.push(unknown);
  }
  return groups;
}

/**
 * 排序。
 *
 * ★ 没有时间的照片**无论升序降序都排在最后**：它们不属于时间轴上的任何
 *   位置，按升序把它顶到最前面（时间串是空串）只会让人以为那是最老的照片。
 */
export function sortPhotos(list, sort) {
  const items = (list || []).slice();

  items.sort(function (a, b) {
    if (sort === 'name_asc' || sort === 'name_desc') {
      const result = String(a.name || '').toLowerCase()
        .localeCompare(String(b.name || '').toLowerCase());
      return sort === 'name_asc' ? result : -result;
    }

    const left = a.taken_at || '';
    const right = b.taken_at || '';
    if (!left && !right) {
      return String(a.name || '').localeCompare(String(b.name || ''));
    }
    if (!left) {
      return 1;
    }
    if (!right) {
      return -1;
    }
    return sort === 'taken_asc' ? left.localeCompare(right) : right.localeCompare(left);
  });

  return items;
}

/** 这张照片是否落在智能相册（按时间段）的范围里 */
export function inAlbumRange(photo, album) {
  if (!photo || !photo.taken_at || !album) {
    return false;
  }
  if (album.start && photo.taken_at < album.start) {
    return false;
  }
  if (album.end && photo.taken_at > album.end) {
    return false;
  }
  return true;
}

/**
 * 筛选。
 *
 * @param {Array}  list    全部照片
 * @param {object} options {kind, value, query, albums}
 *   kind: all | unsorted（时间来自文件时间，即「待整理」）
 *         | undated | missing | source | album
 */
export function filterPhotos(list, options) {
  const opts = options || {};
  const kind = opts.kind || 'all';
  const value = opts.value || '';
  let items = (list || []).slice();

  if (kind === 'unsorted') {
    /* ★「待整理」= 时间取自文件修改时间的那一批。这是最有用的一个筛选项：
       它们的时间是下载时间而不是拍摄时间，不修好，时间轴就一直是错的。 */
    items = items.filter(function (p) { return p.source === 'file' && !p.missing; });
  } else if (kind === 'undated') {
    items = items.filter(function (p) { return !p.taken_at; });
  } else if (kind === 'missing') {
    items = items.filter(function (p) { return !!p.missing; });
  } else if (kind === 'source') {
    const parts = String(value).split('\u0000');
    const root = parts[0];
    const path = parts[1] || '';
    items = items.filter(function (p) {
      if (p.root !== root) {
        return false;
      }
      return path === '' || String(p.relpath || '').indexOf(path + '/') === 0;
    });
  } else if (kind === 'album') {
    const albums = opts.albums || [];
    const album = albums.filter(function (a) { return a.id === value; })[0];
    if (!album) {
      return [];
    }
    if (album.kind === 'smart') {
      items = items.filter(function (p) { return inAlbumRange(p, album); });
    } else {
      const members = new Set(album.items || []);
      items = items.filter(function (p) { return members.has(p.id); });
    }
  }

  const query = String(opts.query || '').trim().toLowerCase();
  if (query) {
    items = items.filter(function (p) {
      const place = (p.place && p.place.name) || '';
      const haystack = [p.name, place, (p.tags || []).join(' '), p.camera || '']
        .join(' ').toLowerCase();
      return haystack.indexOf(query) >= 0;
    });
  }

  return items;
}

/** 当前这批照片的时间范围（用来给「按时间段建相册」做默认值） */
export function rangeOf(list) {
  let min = '';
  let max = '';
  (list || []).forEach(function (photo) {
    if (!photo.taken_at) {
      return;
    }
    if (!min || photo.taken_at < min) {
      min = photo.taken_at;
    }
    if (!max || photo.taken_at > max) {
      max = photo.taken_at;
    }
  });
  return {
    start: min ? min.slice(0, 10) : '',
    end: max ? max.slice(0, 10) : ''
  };
}

export default {
  LEVELS,
  SORTS,
  SOURCE_TEXT,
  UNKNOWN_KEY,
  prettyTime,
  groupOf,
  groupPhotos,
  sortPhotos,
  inAlbumRange,
  filterPhotos,
  rangeOf
};

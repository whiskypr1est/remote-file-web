/* ==========================================================================
   照片时间轴逻辑的测试（用 node 直接跑，无第三方依赖）
   --------------------------------------------------------------------------
   为什么要专门测这一小块
   ----------------------
   「按时间轴归类」的全部规则都在 phototime.js 里：时间怎么分组、没有时间的
   照片排哪儿、筛选与排序怎么算。它出错的样子非常隐蔽 —— 时间轴看着挺满，
   其实分组错了、或者一批照片被悄悄漏掉，界面上没有任何报错，用户只会觉得
   「我那张照片怎么不见了」。

   其中最容易写错、后果也最讨厌的一条是：**没有可用时间的照片不能混进任何
   年份**。它们的时间读不到（文件被删了、时间戳是空的），塞进最近的一年会让
   时间轴看起来「完整」，但那是在编造信息。

   为什么能直接在 node 里跑
   ------------------------
   phototime.js 不碰 DOM、不碰网络，输入输出都是普通对象。
   （photos.js 本身没法这样测：它 import 了 wins.js，而 wins.js 在模块级就要
   摸 document。纯逻辑拆出来正是为了这一点。）

   跑法：node tests/js/photos-timeline.test.mjs
   ========================================================================== */

import assert from 'node:assert/strict';

const time = await import(new URL('../../static/js/phototime.js', import.meta.url));

/* ---------------------------------------------------------------------------
   造数据
   --------------------------------------------------------------------------- */

let seq = 0;

/**
 * 造一条照片记录（只带本模块会用到的字段）。
 * source: user | exif | file —— file 表示「时间取自文件修改时间」，也就是待整理。
 */
function photo(overrides) {
  seq += 1;
  return Object.assign({
    id: 'id' + seq,
    name: 'IMG_' + seq + '.jpg',
    relpath: 'trip/IMG_' + seq + '.jpg',
    root: 'share',
    taken_at: '2023-08-15T12:00:00',
    source: 'exif',
    place: null,
    tags: [],
    camera: '',
    missing: false
  }, overrides || {});
}

/* ---------------------------------------------------------------------------
   用例
   --------------------------------------------------------------------------- */

const tests = [];
const test = (name, fn) => tests.push({ name, fn });

/* ---- 分组 ---- */

test('日粒度：分组键是日期，标题带月日，副标题带年份与星期', () => {
  const info = time.groupOf('2023-08-15T12:34:56', 'day');
  assert.equal(info.key, '2023-08-15');
  assert.equal(info.title, '8 月 15 日');
  assert.equal(info.sub, '2023 年 周二', '2023-08-15 确实是周二');
});

test('月粒度：同一个月归到同一个键', () => {
  const a = time.groupOf('2023-08-15T12:34:56', 'month');
  const b = time.groupOf('2023-08-31T23:59:59', 'month');
  assert.equal(a.key, '2023-08');
  assert.equal(b.key, '2023-08');
  assert.equal(a.title, '8 月');
  assert.equal(a.sub, '2023 年');
});

test('年粒度：同一年归到同一个键', () => {
  const a = time.groupOf('2023-01-01T00:00:00', 'year');
  const b = time.groupOf('2023-12-31T23:59:59', 'year');
  assert.equal(a.key, '2023');
  assert.equal(b.key, '2023');
  assert.equal(a.title, '2023 年');
});

test('★ 没有时间的照片单独归一组，绝不混进任何年份', () => {
  const info = time.groupOf('', 'day');
  assert.equal(info.key, time.UNKNOWN_KEY);
  assert.equal(info.title, '时间未知');
  assert.equal(time.groupOf(null, 'year').key, time.UNKNOWN_KEY);
  assert.equal(time.groupOf(undefined, 'month').key, time.UNKNOWN_KEY);

  const groups = time.groupPhotos([
    photo({ taken_at: '2023-08-15T10:00:00' }),
    photo({ taken_at: '', source: 'file' }),
    photo({ taken_at: '2023-08-16T10:00:00' })
  ], 'year');

  const keys = groups.map((g) => g.key);
  assert.deepEqual(keys, ['2023', time.UNKNOWN_KEY]);
  const unknown = groups[keys.indexOf(time.UNKNOWN_KEY)];
  assert.equal(unknown.photos.length, 1);
});

test('★ 未知时间那一组固定排在最后（不论输入顺序）', () => {
  const groups = time.groupPhotos([
    photo({ taken_at: '', name: 'zzz.jpg' }),
    photo({ taken_at: '2021-01-01T00:00:00' }),
    photo({ taken_at: '2024-01-01T00:00:00' })
  ], 'year');
  assert.equal(groups[groups.length - 1].key, time.UNKNOWN_KEY);
});

test('分组保持传入的顺序，不会重排照片', () => {
  const groups = time.groupPhotos([
    photo({ taken_at: '2023-08-15T18:00:00', name: 'c.jpg' }),
    photo({ taken_at: '2023-08-15T09:00:00', name: 'a.jpg' }),
    photo({ taken_at: '2023-08-15T12:00:00', name: 'b.jpg' })
  ], 'day');

  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0].photos.map((p) => p.name), ['c.jpg', 'a.jpg', 'b.jpg'],
    '分组只负责归类，排序是 sortPhotos 的事');
});

test('年粒度下跨月同年的照片合成一组，月粒度下拆开', () => {
  const items = [
    photo({ taken_at: '2023-08-15T10:00:00' }),
    photo({ taken_at: '2023-09-01T10:00:00' })
  ];
  assert.equal(time.groupPhotos(items, 'year').length, 1);
  assert.equal(time.groupPhotos(items, 'month').length, 2);
  assert.equal(time.groupPhotos(items, 'day').length, 2);
});

test('空列表分组得到空数组（不抛异常）', () => {
  assert.deepEqual(time.groupPhotos([], 'day'), []);
  assert.deepEqual(time.groupPhotos(null, 'day'), []);
});

/* ---- 排序 ---- */

test('按时间倒序（默认）与升序', () => {
  const items = [
    photo({ taken_at: '2023-01-01T00:00:00', name: 'old.jpg' }),
    photo({ taken_at: '2024-06-01T00:00:00', name: 'new.jpg' }),
    photo({ taken_at: '2023-09-01T00:00:00', name: 'mid.jpg' })
  ];
  assert.deepEqual(time.sortPhotos(items, 'taken_desc').map((p) => p.name),
    ['new.jpg', 'mid.jpg', 'old.jpg']);
  assert.deepEqual(time.sortPhotos(items, 'taken_asc').map((p) => p.name),
    ['old.jpg', 'mid.jpg', 'new.jpg']);
});

test('按文件名排序（大小写不敏感）', () => {
  const items = [
    photo({ name: 'b.JPG' }),
    photo({ name: 'A.jpg' }),
    photo({ name: 'c.jpg' })
  ];
  assert.deepEqual(time.sortPhotos(items, 'name_asc').map((p) => p.name),
    ['A.jpg', 'b.JPG', 'c.jpg']);
  assert.deepEqual(time.sortPhotos(items, 'name_desc').map((p) => p.name),
    ['c.jpg', 'b.JPG', 'A.jpg']);
});

test('★★ 没有时间的照片在升序和降序里都排在最后', () => {
  const items = [
    photo({ taken_at: '', name: 'none.jpg', source: 'file' }),
    photo({ taken_at: '2023-01-01T00:00:00', name: 'old.jpg' }),
    photo({ taken_at: '2024-01-01T00:00:00', name: 'new.jpg' })
  ];
  for (const sort of ['taken_desc', 'taken_asc']) {
    const names = time.sortPhotos(items, sort).map((p) => p.name);
    assert.equal(names[names.length - 1], 'none.jpg',
      sort + ' 下没有时间的照片都该在最后（升序顶到最前会让人以为它是最老的）');
  }
});

test('排序不改动传入的数组（返回副本）', () => {
  const items = [
    photo({ taken_at: '2024-01-01T00:00:00', name: 'b.jpg' }),
    photo({ taken_at: '2023-01-01T00:00:00', name: 'a.jpg' })
  ];
  const before = items.map((p) => p.name);
  time.sortPhotos(items, 'taken_desc');
  assert.deepEqual(items.map((p) => p.name), before, '原数组不该被就地排序');
});

/* ---- 筛选 ---- */

test('全部：不做任何过滤', () => {
  const items = [photo(), photo({ taken_at: '', source: 'file' })];
  assert.equal(time.filterPhotos(items, { kind: 'all' }).length, 2);
});

test('★ 待整理：只收「时间取自文件时间」的那些，且排除已失效的文件', () => {
  const items = [
    photo({ source: 'exif' }),
    photo({ source: 'user' }),
    photo({ source: 'file', name: 'wechat.jpg' }),
    photo({ source: 'file', missing: true, name: 'gone.jpg' })
  ];
  const result = time.filterPhotos(items, { kind: 'unsorted' });
  assert.deepEqual(result.map((p) => p.name), ['wechat.jpg'],
    '待整理是「时间不可靠且文件还在」的那一批');
});

test('找不到文件：单独一个筛选项', () => {
  const items = [photo(), photo({ missing: true, name: 'gone.jpg' })];
  assert.deepEqual(time.filterPhotos(items, { kind: 'missing' }).map((p) => p.name),
    ['gone.jpg']);
});

test('时间完全未知：单独一个筛选项', () => {
  const items = [photo(), photo({ taken_at: '' })];
  assert.equal(time.filterPhotos(items, { kind: 'undated' }).length, 1);
});

test('来源目录：按根 + 路径前缀筛选，空路径表示整个根', () => {
  const items = [
    photo({ root: 'share', relpath: 'trip/a.jpg' }),
    photo({ root: 'share', relpath: 'trip/sub/b.jpg' }),
    photo({ root: 'share', relpath: 'other/c.jpg' }),
    photo({ root: 'private', relpath: 'trip/d.jpg' })
  ];
  const trip = time.filterPhotos(items, { kind: 'source', value: 'share\u0000trip' });
  assert.deepEqual(trip.map((p) => p.relpath), ['trip/a.jpg', 'trip/sub/b.jpg'],
    '前缀要带斜杠语义：trip 不该匹配到 tripxxx');

  const whole = time.filterPhotos(items, { kind: 'source', value: 'share\u0000' });
  assert.equal(whole.length, 3);
});

test('前缀筛选不会被同名前缀的兄弟目录骗到', () => {
  const items = [
    photo({ root: 'share', relpath: 'trip/a.jpg' }),
    photo({ root: 'share', relpath: 'tripod/b.jpg' })
  ];
  const result = time.filterPhotos(items, { kind: 'source', value: 'share\u0000trip' });
  assert.deepEqual(result.map((p) => p.relpath), ['trip/a.jpg']);
});

test('智能相册：时间范围两端都是闭区间', () => {
  const album = { id: 'al1', kind: 'smart', start: '2023-06-01T00:00:00', end: '2023-09-01T00:00:00' };
  const items = [
    photo({ taken_at: '2023-05-31T23:59:59', name: 'before.jpg' }),
    photo({ taken_at: '2023-06-01T00:00:00', name: 'start.jpg' }),
    photo({ taken_at: '2023-08-15T12:00:00', name: 'middle.jpg' }),
    photo({ taken_at: '2023-09-01T00:00:00', name: 'end.jpg' }),
    photo({ taken_at: '2023-09-01T00:00:01', name: 'after.jpg' }),
    photo({ taken_at: '', name: 'none.jpg' })
  ];
  const result = time.filterPhotos(items, { kind: 'album', value: 'al1', albums: [album] });
  assert.deepEqual(result.map((p) => p.name), ['start.jpg', 'middle.jpg', 'end.jpg'],
    '两端都算在内；没有时间的照片进不了按时间段的相册');
});

test('智能相册：只给开始时间时就只有下界', () => {
  const album = { id: 'al1', kind: 'smart', start: '2023-06-01T00:00:00', end: '' };
  const items = [
    photo({ taken_at: '2023-01-01T00:00:00', name: 'before.jpg' }),
    photo({ taken_at: '2030-01-01T00:00:00', name: 'far.jpg' })
  ];
  assert.deepEqual(
    time.filterPhotos(items, { kind: 'album', value: 'al1', albums: [album] })
      .map((p) => p.name),
    ['far.jpg']);
});

test('手动相册：按成员 id 过滤', () => {
  const a = photo({ name: 'a.jpg' });
  const b = photo({ name: 'b.jpg' });
  const album = { id: 'al2', kind: 'manual', items: [b.id] };
  const result = time.filterPhotos([a, b], { kind: 'album', value: 'al2', albums: [album] });
  assert.deepEqual(result.map((p) => p.name), ['b.jpg']);
});

test('相册被删掉之后：结果是空，而不是「不过滤」', () => {
  const items = [photo(), photo()];
  assert.deepEqual(time.filterPhotos(items, { kind: 'album', value: 'nope', albums: [] }), [],
    '★ 返回全部会让用户以为整个库都进了这个不存在的相册');
});

test('搜索：文件名 / 地点 / 标签 / 相机都能命中，且大小写不敏感', () => {
  const items = [
    photo({ name: 'Beach.JPG' }),
    photo({ name: 'x.jpg', place: { name: '青岛·栈桥' } }),
    photo({ name: 'y.jpg', tags: ['家人', '海边'] }),
    photo({ name: 'z.jpg', camera: 'iPhone 14 Pro' }),
    photo({ name: 'other.jpg' })
  ];
  assert.equal(time.filterPhotos(items, { kind: 'all', query: 'beach' }).length, 1);
  assert.equal(time.filterPhotos(items, { kind: 'all', query: '栈桥' }).length, 1);
  assert.equal(time.filterPhotos(items, { kind: 'all', query: '海边' }).length, 1);
  assert.equal(time.filterPhotos(items, { kind: 'all', query: 'iphone' }).length, 1,
    '相机型号也要能搜到（iPhone 的型号大小写不固定）');
  assert.equal(time.filterPhotos(items, { kind: 'all', query: 'nomatch' }).length, 0);
});

test('搜索会叠加在其它筛选之上', () => {
  const items = [
    photo({ source: 'file', name: 'wechat-2023.jpg' }),
    photo({ source: 'file', name: 'screenshot.png' }),
    photo({ source: 'exif', name: 'wechat-good.jpg' })
  ];
  const result = time.filterPhotos(items, { kind: 'unsorted', query: 'wechat' });
  assert.deepEqual(result.map((p) => p.name), ['wechat-2023.jpg']);
});

test('空白搜索词等于不搜索', () => {
  const items = [photo(), photo()];
  assert.equal(time.filterPhotos(items, { kind: 'all', query: '   ' }).length, 2);
});

/* ---- 范围 ---- */

test('时间范围忽略没有时间的照片', () => {
  const range = time.rangeOf([
    photo({ taken_at: '2023-08-15T12:00:00' }),
    photo({ taken_at: '2024-03-01T00:00:00' }),
    photo({ taken_at: '' })
  ]);
  assert.deepEqual(range, { start: '2023-08-15', end: '2024-03-01' });
});

test('没有一张有时间时：范围是空串而不是 NaN', () => {
  assert.deepEqual(time.rangeOf([photo({ taken_at: '' })]), { start: '', end: '' });
  assert.deepEqual(time.rangeOf([]), { start: '', end: '' });
});

/* ---- 展示辅助 ---- */

test('prettyTime 把 ISO 串变成好读的写法，空值给空串', () => {
  assert.equal(time.prettyTime('2023-08-15T12:34:56'), '2023-08-15 12:34:56');
  assert.equal(time.prettyTime(''), '');
  assert.equal(time.prettyTime(null), '');
});

test('每个时间来源都有一句给用户看的解释', () => {
  for (const key of ['user', 'exif', 'file']) {
    assert.ok(time.SOURCE_TEXT[key] && time.SOURCE_TEXT[key].length > 0,
      '来源 ' + key + ' 必须能解释给用户听');
  }
  assert.ok(time.SOURCE_TEXT.file.indexOf('修改时间') >= 0,
    'file 那一档必须点明它是文件时间，不是拍摄时间');
});

test('粒度与排序的选项表是三档 / 四种，且值不重复', () => {
  assert.deepEqual(time.LEVELS.map((l) => l.value), ['year', 'month', 'day']);
  assert.equal(new Set(time.SORTS.map((s) => s.value)).size, time.SORTS.length);
  assert.ok(time.SORTS.some((s) => s.value === 'taken_desc'));
});

/* ---------------------------------------------------------------------------
   运行
   --------------------------------------------------------------------------- */

let failed = 0;
for (const item of tests) {
  try {
    await item.fn();
    console.log('ok   ' + item.name);
  } catch (err) {
    failed += 1;
    console.log('FAIL ' + item.name);
    console.log('     ' + (err && err.message ? err.message : String(err)));
  }
}

console.log('');
console.log('Ran ' + tests.length + ' tests, ' + failed + ' failed');
process.exitCode = failed ? 1 : 0;

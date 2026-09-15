/* ==========================================================================
   API 客户端
   --------------------------------------------------------------------------
   统一处理：
     * Cookie 携带（credentials: same-origin）
     * CSRF 令牌头（所有改状态请求）
     * 401 自动跳回登录页
     * 错误信息提取（后端统一返回 {ok:false, message:"..."}）
   ========================================================================== */

/** 当前会话的 CSRF 令牌，登录后由 main.js 设置 */
let csrfToken = '';

export function setCsrfToken(token) {
  csrfToken = token || '';
}

export function getCsrfToken() {
  return csrfToken;
}

/** 业务异常：带 HTTP 状态码与后端错误码 */
export class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = 'ApiError';
    this.status = status || 0;
    this.code = code || '';
  }
}

/** 会话失效时跳转登录页（加个标记避免重复跳转） */
let redirecting = false;
export function redirectToLogin() {
  if (redirecting) {
    return;
  }
  redirecting = true;
  location.replace('/login.html');
}

/** 拼接带查询参数的 URL */
export function buildUrl(path, params) {
  const url = new URL(path, location.origin);
  if (params) {
    Object.keys(params).forEach(function (key) {
      const value = params[key];
      if (value === undefined || value === null) {
        return;
      }
      url.searchParams.set(key, String(value));
    });
  }
  return url.pathname + url.search;
}

/** 绝对 URL（少数场景需要完整地址） */
function absoluteUrl(path, params) {
  return location.origin + buildUrl(path, params);
}

/**
 * 统一请求入口。
 *
 * @param {string} method  HTTP 方法
 * @param {string} path    接口路径
 * @param {object} options {params, json, body, headers, signal, keepalive}
 *
 * keepalive 只给「页面正在卸载时也要把请求发出去」的场景用（见 sessionstate.js
 * 的 pagehide 保存）。注意这里**不能用 navigator.sendBeacon** 替代：
 * sendBeacon 无法设置 X-CSRF-Token 头，会被中间件直接拒掉。
 */
export async function request(method, path, options) {
  const opts = options || {};
  const headers = Object.assign({}, opts.headers || {});
  const init = {
    method: method,
    credentials: 'same-origin',
    headers: headers
  };

  if (opts.keepalive) {
    init.keepalive = true;
  }

  if (method !== 'GET' && method !== 'HEAD' && csrfToken) {
    headers['X-CSRF-Token'] = csrfToken;
  }

  if (opts.json !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(opts.json);
  } else if (opts.body !== undefined) {
    init.body = opts.body;
  }
  if (opts.signal) {
    init.signal = opts.signal;
  }

  let response;
  try {
    response = await fetch(buildUrl(path, opts.params), init);
  } catch (err) {
    if (err && err.name === 'AbortError') {
      throw err;
    }
    throw new ApiError('无法连接到服务器，请检查网络或服务是否已启动', 0);
  }

  if (response.status === 401) {
    redirectToLogin();
    throw new ApiError('未登录或会话已过期', 401, 'unauthorized');
  }

  // 解析响应体（后端所有错误都返回 JSON）
  const text = await response.text();
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch (err) {
      data = null;
    }
  }

  if (!response.ok) {
    let message = '';
    if (data) {
      message = data.message || data.detail || '';
    }
    if (!message) {
      message = '请求失败（HTTP ' + response.status + '）';
      if (text && text.length < 200 && text.indexOf('<') !== 0) {
        message += '：' + text;
      }
    }
    throw new ApiError(message, response.status, (data && data.code) || '');
  }

  return data === null ? { ok: true } : data;
}

export const api = {
  get: function (path, params, extra) {
    return request('GET', path, Object.assign({ params: params }, extra || {}));
  },
  post: function (path, json, params) {
    return request('POST', path, { json: json || {}, params: params });
  },
  del: function (path, json, params) {
    return request('DELETE', path, { json: json || {}, params: params });
  }
};

/* ---------------------------------------------------------------------------
   认证
   --------------------------------------------------------------------------- */

export function login(username, password) {
  return request('POST', '/api/auth/login', {
    json: { username: username, password: password }
  });
}

export function logout() {
  return request('POST', '/api/auth/logout', { json: {} });
}

/**
 * 修改登录口令。
 *
 * 服务端会校验 current_password，并在成功后**轮换 session_secret** ——
 * 也就是所有已签发的会话（含当前这个）立即失效。所以调用方拿到成功响应后
 * 必须引导用户重新登录，不能继续用旧会话发请求。
 */
export function changePassword(currentPassword, newPassword) {
  return request('POST', '/api/auth/password', {
    json: { current_password: currentPassword, new_password: newPassword }
  });
}

export function authStatus() {
  return request('GET', '/api/auth/status');
}

/* ---------------------------------------------------------------------------
   业务接口封装
   --------------------------------------------------------------------------- */

export function systemInfo() {
  return request('GET', '/api/system/info');
}

/**
 * 系统负载快照（任务管理器窗口用）。
 * @param {number} top  返回多少个进程（0 = 用服务端配置里的默认值）
 * @param {string} sort 进程排序依据：'cpu'（默认）| 'memory'
 */
export function sysmonSnapshot(top, sort) {
  return request('GET', '/api/sysmon/snapshot', {
    params: { top: top || 0, sort: sort || 'cpu' }
  });
}

/**
 * 列出压缩包里的条目（只读，不解压）。
 * @param {string} root 根标识
 * @param {string} path 压缩包在该根下的相对路径
 */
export function archiveListing(root, path) {
  return request('GET', '/api/fs/archive', { params: { root: root, path: path } });
}

/**
 * 按文件名搜索（递归，不搜内容）。
 * @param {string} query 关键词，至少 2 个字符
 * @param {object} [opts] {root, limit}
 */
export function searchFiles(query, opts) {
  const options = opts || {};
  return request('GET', '/api/fs/search', {
    params: { q: query, root: options.root || '', limit: options.limit || 0 }
  });
}

/** 列目录；path 可以是相对路径，也可以是绝对路径（地址栏场景） */
export function listDir(params) {
  return request('GET', '/api/fs/list', { params: params });
}

export function listRoots() {
  return request('GET', '/api/fs/roots');
}

export function makeDir(root, path, name) {
  return request('POST', '/api/fs/mkdir', { json: { root: root, path: path, name: name } });
}

/**
 * 新建空文件。
 * name 是**含扩展名的完整文件名**，后缀由调用方（用户自己选/填）决定。
 * 同名已存在时服务端返回 409，绝不覆盖已有文件。
 */
export function newFile(root, path, name) {
  return request('POST', '/api/fs/newfile', { json: { root: root, path: path, name: name } });
}

export function renameEntry(root, path, newName) {
  return request('POST', '/api/fs/rename', {
    json: { root: root, path: path, new_name: newName }
  });
}

/** 删除；permanent=true 时强制永久删除 */
export function deleteEntries(root, paths, permanent) {
  return request('POST', '/api/fs/delete', {
    json: { root: root, paths: paths, permanent: !!permanent }
  });
}

/**
 * 复制；目标已有同名项时由服务端自动改名（报告.docx -> 报告 (1).docx），不会覆盖。
 *
 * @param {string}   root        源根目录 id
 * @param {string[]} paths       源相对路径（entry.rel）
 * @param {string}   targetRoot  目标根目录 id（可与源不同 -> 跨盘）
 * @param {string}   targetPath  目标目录（相对目标根）
 * @param {boolean}  background  置真则后台执行：立刻返回 {job_id}，由进度面板轮询
 */
export function copyEntries(root, paths, targetRoot, targetPath, background) {
  return request('POST', '/api/fs/copy', {
    json: {
      root: root,
      paths: paths,
      target_root: targetRoot || '',
      target_path: targetPath || '',
      background: !!background
    }
  });
}

/** 移动；同盘直接改名，跨盘由服务端复制后删除，重名同样自动改名 */
export function moveEntries(root, paths, targetRoot, targetPath, background) {
  return request('POST', '/api/fs/move', {
    json: {
      root: root,
      paths: paths,
      target_root: targetRoot || '',
      target_path: targetPath || '',
      background: !!background
    }
  });
}

/* ---------------------------------------------------------------------------
   后台任务（复制 / 移动 / 解压的进度与取消）
   --------------------------------------------------------------------------- */

/** 任务列表（最近的在前）。进度面板靠它一次拿到所有活跃任务，不必逐个查。 */
export function listJobs(limit) {
  return request('GET', '/api/jobs', { params: { limit: limit || 20 } });
}

/** 单个任务的进度与结果（终态任务的 result 与对应同步接口形状一致） */
export function getJob(jobId) {
  return request('GET', '/api/jobs/' + encodeURIComponent(jobId));
}

/**
 * 请求取消任务。
 * 注意是**协作式**取消：这里成功只代表标记打上了，
 * 真正停下要等任务走到下一个检查点 —— 所以要继续轮询直到 status 变 cancelled。
 */
export function cancelJob(jobId) {
  return request('POST', '/api/jobs/' + encodeURIComponent(jobId) + '/cancel', { json: {} });
}

/**
 * 压缩：把选中的条目打成压缩包，存在服务端目录里（源文件保持不动）。
 *
 * 和 createZip 是两回事：那个是「打包给浏览器下载」，产物在临时目录用完即弃；
 * 这里是真在用户目录里生成一个压缩文件，所以要多带目标目录、格式和压缩级别。
 *
 * @param {object} payload
 *   root        源根目录 id
 *   paths       源相对路径数组（entry.rel）
 *   target_root 压缩包存放的根目录 id（与 target_path 同时留空则放在第一个源的同级目录）
 *   target_path 压缩包存放的目录（相对目标根）
 *   name        压缩包文件名（含扩展名）；留空由服务端自动起名
 *   format      zip / tar / tar.gz / 7z / rar；留空按 name 的后缀推断
 *   level       压缩级别 0-5（0 为仅存储）
 */
export function compressEntries(payload) {
  const opts = payload || {};
  return request('POST', '/api/fs/compress', {
    json: {
      root: opts.root || '',
      paths: opts.paths || [],
      target_root: opts.target_root || '',
      target_path: opts.target_path || '',
      name: opts.name || '',
      format: opts.format || '',
      level: typeof opts.level === 'number' ? opts.level : 3
    }
  });
}

/**
 * 解压压缩包到服务端目录。
 *
 * 不用传格式：服务端按扩展名自己认（zip / tar 系 / 7z / rar）。
 * overwrite 默认false —— 目标目录里已有同名顶层项时服务端会整体拒绝（409），
 * 绝不会悄悄覆盖，所以前端也保持「不覆盖」这个默认。
 *
 * @param {object} payload
 *   root        压缩包所在的根目录 id
 *   path        压缩包相对路径（entry.rel）
 *   target_root 解压到的根目录 id（与 target_path 同时留空则解到压缩包自己的目录）
 *   target_path 解压到的目录（相对目标根）
 *   overwrite   是否允许覆盖同名项，默认 false
 *   background  置真则后台执行：立刻返回 {job_id}，由进度面板轮询
 */
export function extractArchive(payload) {
  const opts = payload || {};
  return request('POST', '/api/fs/extract', {
    json: {
      root: opts.root || '',
      path: opts.path || '',
      target_root: opts.target_root || '',
      target_path: opts.target_path || '',
      overwrite: !!opts.overwrite,
      background: !!opts.background
    }
  });
}

/** 打包下载：先创建 zip 拿令牌 */
export function createZip(root, paths) {
  return request('POST', '/api/fs/zip', { json: { root: root, paths: paths } });
}

/** 文本预览 */
export function textPreview(root, path) {
  return request('GET', '/api/fs/text', { params: { root: root, path: path } });
}

/* ---------------------------------------------------------------------------
   文本编辑
   --------------------------------------------------------------------------- */

/**
 * 读取文本文件（编辑器窗口用）。
 *
 * 和 textPreview 打的是同一个接口，区别只在**用途**：这个函数的返回值要原样交给
 * 编辑器窗口，其中 mtime / bom / newline 三项是保存时回传的凭据，缺一不可
 * （服务端靠它们还原原文件的行尾与 BOM）。单独留一个入口是为了让「编辑器读文件」
 * 这件事在 api 层有新意可循，不必去猜 textPreview 的那几个字段从哪来。
 *
 * 注意返回的 content 是服务端**按原编码解码**后的文本（UTF-8 / GB18030 / Big5 …），
 * 行尾可能是 \r\n —— 编辑器内部统一按 \n 处理，保存时再由服务端按 newline 还原。
 */
export function getText(root, path) {
  return request('GET', '/api/fs/text', { params: { root: root, path: path } });
}

/**
 * 保存文本文件。
 *
 * ★ text 必须是**已归一化成 \n 的全文**：编码、BOM、行尾由服务端按
 *   encoding / bom / newline 三个字段重新套用，前端不要自己拼 \r\n。
 * ★ base_mtime / base_size 是「打开时看到的版本」。服务端会拿它们比对磁盘现状，
 *   不一致就返回 409 且**什么都不写**，以此挡住「我改的时候别人也在改」的覆盖。
 *   所以保存成功后必须用返回值里的新 mtime 刷新这份凭据，否则第二次保存会误判冲突。
 *
 * @param {object} payload
 *   root        根目录 id
 *   path        相对路径
 *   text        全文内容（LF 归一化）
 *   encoding    GET 拿到的编码，原样回传
 *   bom         GET 拿到的 BOM 标志，原样回传
 *   newline     GET 拿到的行尾符，原样回传
 *   base_mtime  GET 拿到的 mtime
 *   base_size   GET 拿到的字节数
 * @returns {Promise<object>} {ok, size, size_text, mtime}
 */
export function saveText(payload) {
  const opts = payload || {};
  return request('POST', '/api/fs/text', {
    json: {
      root: opts.root || '',
      path: opts.path || '',
      text: typeof opts.text === 'string' ? opts.text : '',
      encoding: opts.encoding || 'utf-8',
      bom: !!opts.bom,
      newline: opts.newline === '\n' || opts.newline === '\r' ? opts.newline : '\r\n',
      base_mtime: typeof opts.base_mtime === 'number' ? opts.base_mtime : null,
      base_size: typeof opts.base_size === 'number' ? opts.base_size : null
    }
  });
}

/** Office 预览（可能较慢，需要长超时） */
export function officePreview(root, path) {
  return request('GET', '/api/fs/office', { params: { root: root, path: path } });
}

/** 重新探测 LibreOffice */
export function rescanSoffice() {
  return request('POST', '/api/system/soffice/rescan', { json: {} });
}

/* ---------------------------------------------------------------------------
   虚拟桌面快捷方式
   --------------------------------------------------------------------------- */

/** 列出全部桌面快捷方式 */
export function listShortcuts() {
  return request('GET', '/api/desktop/shortcuts');
}

/**
 * 把文件/文件夹发送到虚拟桌面。
 * 只写服务端数据文件，不会往 Windows 真实桌面创建任何东西。
 */
export function createShortcut(root, path, name) {
  return request('POST', '/api/desktop/shortcuts', {
    json: { root: root, path: path, name: name || '' }
  });
}

/** 重命名快捷方式（只改显示名，不影响指向的真实路径） */
export function renameShortcut(id, name) {
  return request('POST', '/api/desktop/shortcuts/rename', {
    json: { id: id, name: name }
  });
}

/** 删除快捷方式（只删桌面图标，不删真实文件） */
export function deleteShortcut(id) {
  return request('POST', '/api/desktop/shortcuts/delete', {
    json: { id: id }
  });
}

/* ---------------------------------------------------------------------------
   界面状态（窗口布局 / 当前目录 / 视图模式）
   --------------------------------------------------------------------------- */

/**
 * 读取上次保存的界面状态。
 *
 * 后端永远返回 200：没存过就是 {ok:true, state:{}}，所以调用方要自己把
 * 空对象当成「使用默认布局」，而不是当成失败。
 */
export function getDesktopState() {
  return request('GET', '/api/desktop/state');
}

/**
 * 保存界面状态（整份覆盖，服务端不做合并）。
 *
 * 因此每次都必须传完整布局：漏掉的键就等于被删掉了。
 *
 * @param {object} state    完整状态文档
 * @param {boolean} keepalive 页面正在卸载时置 true
 */
export function putDesktopState(state, keepalive) {
  return request('PUT', '/api/desktop/state', {
    json: { state: state },
    keepalive: !!keepalive
  });
}

/* ---------------------------------------------------------------------------
   用户管理（仅管理员，服务端 require_admin 会把普通用户挡在 403）
   --------------------------------------------------------------------------- */

/** 用户列表：含在线状态、命令行窗口数、进程数、后台任务数 */
export function listUsers() {
  return request('GET', '/api/users');
}

/** 在线情况（比用户列表轻，适合较频繁地刷新） */
export function listOnline() {
  return request('GET', '/api/users/online');
}

/** 最近的审计日志（时间正序，最新在最后） */
export function readAudit(limit) {
  return request('GET', '/api/users/audit', {
    params: { limit: limit || 200 }
  });
}

/** 新建用户（初始口令由管理员设定） */
export function createUser(payload) {
  return request('POST', '/api/users', { json: payload || {} });
}

/**
 * 修改用户（部分更新：只传要改的字段）。
 * 停用与启用也走这里：updateUser(name, { enabled: false })。
 */
export function updateUser(username, patch) {
  return request('POST', '/api/users/' + encodeURIComponent(username) + '/update', {
    json: patch || {}
  });
}

/** 管理员重设某个用户的口令（他当前的登录会立即失效） */
export function resetUserPassword(username, password) {
  return request('POST', '/api/users/' + encodeURIComponent(username) + '/password', {
    json: { password: password }
  });
}

/** 强制某人下线（不改口令、不停用） */
export function kickUser(username) {
  return request('POST', '/api/users/' + encodeURIComponent(username) + '/kick', {
    json: {}
  });
}

/* ---------------------------------------------------------------------------
   音乐播放器
   --------------------------------------------------------------------------- */

/** 曲库：库信息 + 全部歌曲 + 歌单 + 播放偏好（一次取全，前端自己排/搜） */
export function musicLibrary() {
  return request('GET', '/api/music/library');
}

/**
 * 从「我能看到的文件」里导入歌曲。
 * @param {string} root  根标识
 * @param {string[]} paths 该根下的相对路径列表
 */
export function musicImport(root, paths) {
  return request('POST', '/api/music/import', {
    json: { root: root, paths: paths || [] }
  });
}

/** 直接把本机文件上传进曲库（原始体上传，带进度） */
export function musicUpload(file, onProgress) {
  return uploadBlob('/api/music/upload', file, file.name, onProgress);
}

/** 从曲库删除一首歌（连同它的歌词；导入的源文件不受影响） */
export function musicDelete(id) {
  return request('POST', '/api/music/delete', { json: { id: id } });
}

/** 读歌词（服务端会自动找同名 .lrc） */
export function musicLyrics(id) {
  return request('GET', '/api/music/lyrics', { params: { id: id } });
}

/** 保存歌词（上传 .lrc 的文本，或直接粘贴） */
export function musicSaveLyrics(id, text) {
  return request('POST', '/api/music/lyrics', { json: { id: id, text: text } });
}

export function musicCreatePlaylist(name) {
  return request('POST', '/api/music/playlists', { json: { name: name } });
}

export function musicRenamePlaylist(id, name) {
  return request('POST', '/api/music/playlists/rename', { json: { id: id, name: name } });
}

export function musicDeletePlaylist(id) {
  return request('POST', '/api/music/playlists/delete', { json: { id: id } });
}

/**
 * 歌单增删歌曲。
 * @param {string} id     歌单 id
 * @param {string} song   歌曲标识（曲库里的文件名）
 * @param {string} action 'add' | 'remove'
 */
export function musicPlaylistSong(id, song, action) {
  return request('POST', '/api/music/playlists/songs', {
    json: { id: id, song: song, action: action || 'add' }
  });
}

/** 保存播放偏好（音量 / 模式 / 上次播到哪） */
export function musicSavePrefs(patch) {
  return request('POST', '/api/music/prefs', { json: patch || {} });
}

/** 音频流地址（交给 <audio src>，无法带自定义头） */
export function musicStreamUrl(id) {
  return buildUrl('/api/music/stream', { id: id });
}

/* ---------------------------------------------------------------------------
   照片（时间轴相册）
   --------------------------------------------------------------------------- */

/** 整库一次取全：照片 + 统计 + 相册 + 浏览偏好 */
export function photosLibrary() {
  return request('GET', '/api/photos/library');
}

/** 单张详情（EXIF 原值 + 用户覆盖值 + 最终有效值） */
export function photoItem(id) {
  return request('GET', '/api/photos/item', { params: { id: id } });
}

/**
 * 就地索引选中的文件/文件夹。
 * ★ 与音乐不同：这里**不复制**文件，只记录位置与元数据。
 * @param {string} root  根标识
 * @param {string[]} paths 该根下的相对路径（空串表示根目录本身）
 * @param {boolean} background true = 立刻返回 job_id，进度在任务面板里看
 */
export function photosImport(root, paths, background) {
  return request('POST', '/api/photos/import', {
    json: { root: root, paths: paths || [], background: !!background }
  });
}

/** 重扫：核对、按指纹找回被移动的文件、发现新增 */
export function photosRescan() {
  return request('POST', '/api/photos/rescan', { json: {} });
}

/** 已纳入索引的目录清单 */
export function photosSources() {
  return request('GET', '/api/photos/sources');
}

/** 把一个目录移出相册（不会删除任何照片文件） */
export function photosForget(root, path) {
  return request('POST', '/api/photos/forget', { json: { root: root, path: path } });
}

/**
 * 改一批照片。
 * patch 里出现的键才会被改：taken_at / place / tags / rating / caption
 */
export function photosEdit(ids, patch) {
  return request('POST', '/api/photos/edit', {
    json: { ids: ids || [], patch: patch || {} }
  });
}

/** 批量平移时间（保留相对先后）；setTo 给了就是「统一设为同一个时间」 */
export function photosBatchTime(ids, opts) {
  const body = { ids: ids || [] };
  if (opts && opts.setTo) {
    body.set_to = opts.setTo;
  } else if (opts && opts.deltaSeconds != null) {
    body.delta_seconds = opts.deltaSeconds;
  } else if (opts && opts.deltaHours != null) {
    body.delta_hours = opts.deltaHours;
  }
  return request('POST', '/api/photos/batch/time', { json: body });
}

/** 撤销最近一次编辑 */
export function photosUndo() {
  return request('POST', '/api/photos/undo', { json: {} });
}

export function photosCreateAlbum(payload) {
  return request('POST', '/api/photos/albums', { json: payload || {} });
}

export function photosRenameAlbum(id, name) {
  return request('POST', '/api/photos/albums/rename', { json: { id: id, name: name } });
}

export function photosDeleteAlbum(id) {
  return request('POST', '/api/photos/albums/delete', { json: { id: id } });
}

/** 往手动相册里加照片 / 移出照片 */
export function photosAlbumItems(id, ids, action) {
  return request('POST', '/api/photos/albums/items', {
    json: { id: id, ids: ids || [], action: action || 'add' }
  });
}

/** 保存浏览偏好（时间轴粒度 / 排序） */
export function photosSavePrefs(patch) {
  return request('POST', '/api/photos/prefs', { json: patch || {} });
}

/**
 * 画廊缩略图 URL。
 *
 * v 参数只是为了让浏览器在图片被换掉后重新取 —— 服务端真正的缓存失效
 * 靠「文件 mtime + 大小 + 尺寸」组成的缓存键。
 */
export function photoThumbUrl(id, box, version) {
  return buildUrl('/api/photos/thumb', { id: id, box: box, v: version });
}

/** 原图 URL（支持 Range；download=1 时作为附件下载） */
export function photoRawUrl(id, download) {
  return buildUrl('/api/photos/raw', { id: id, download: download ? '1' : undefined });
}

/* ---------------------------------------------------------------------------
   URL 构造（用于 <img>、<video>、<a download> 等无法带自定义头的场景）
   --------------------------------------------------------------------------- */

/** 原始文件 URL；download=true 时让浏览器直接下载 */
export function rawUrl(root, path, download) {
  return buildUrl('/api/fs/raw', {
    root: root,
    path: path,
    download: download ? '1' : undefined
  });
}

/** 单文件下载 URL */
export function downloadUrl(root, path) {
  return buildUrl('/api/fs/raw', { root: root, path: path, download: '1' });
}

/** 缩略图 URL（带版本参数，避免浏览器缓存旧图） */
export function thumbUrl(root, path, version) {
  return buildUrl('/api/fs/thumb', { root: root, path: path, v: version });
}

/** Office 转换后的 PDF URL */
export function officePdfUrl(root, path) {
  return buildUrl('/api/fs/office/pdf', { root: root, path: path });
}

/* ---------------------------------------------------------------------------
   下载触发
   --------------------------------------------------------------------------- */

/**
 * 触发浏览器下载。
 *
 * 用隐藏的 <a download> 而不是 fetch，是因为这样浏览器会边收边写盘，
 * 几百 MB 的文件也不会占用 JS 内存；而且能复用服务的 Range 支持。
 */
export function triggerDownload(url) {
  const link = document.createElement('a');
  link.href = url;
  link.rel = 'noopener';
  // 服务端已经带了 Content-Disposition: attachment，这里不必再指定文件名
  link.download = '';
  link.style.display = 'none';
  document.body.appendChild(link);
  link.click();
  setTimeout(function () {
    link.remove();
  }, 0);
}

/* ---------------------------------------------------------------------------
   上传（原始请求体 + 进度回调）
   --------------------------------------------------------------------------- */

/**
 * 把一段二进制原样 POST 到某个接口，带上传进度。
 *
 * 上传类接口收的是**流**而不是 JSON（服务端可以边收边写盘，大文件不会先
 * 在内存或临时目录里落一份），所以不能走 request()。用 XHR 而不是 fetch
 * 是为了拿到上传进度（fetch 至今没有可用的上传进度事件）。
 *
 * @param {string}   path       接口路径
 * @param {Blob}     blob       要发送的内容（通常是 File）
 * @param {string}   filename   ?filename= 查询参数
 * @param {function} onProgress (loaded, total) => void
 * @returns {Promise<object>}
 */
export function uploadBlob(path, blob, filename, onProgress) {
  return new Promise(function (resolve, reject) {
    const xhr = new XMLHttpRequest();
    xhr.open('POST', buildUrl(path, { filename: filename }), true);
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    if (csrfToken) {
      xhr.setRequestHeader('X-CSRF-Token', csrfToken);
    }

    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) {
          onProgress(e.loaded, e.total);
        }
      };
    }

    xhr.onload = function () {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch (err) {
        data = null;
      }

      if (xhr.status === 401) {
        redirectToLogin();
        reject(new ApiError('未登录或会话已过期', 401, 'unauthorized'));
        return;
      }

      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) {
        resolve(data);
        return;
      }

      let message = (data && (data.message || data.detail)) || '';
      if (!message) {
        message = '上传失败（HTTP ' + xhr.status + '）';
      }
      reject(new ApiError(message, xhr.status, (data && data.code) || ''));
    };

    xhr.onerror = function () {
      // 服务端按 Content-Length 提前拒绝时（例如超过单曲上限），
      // 连接可能被直接重置 —— 与其报「网络错误」，不如把可能的原因说清楚
      reject(new ApiError('上传失败：连接被中断（常见原因是文件超过大小上限）', 0));
    };

    xhr.send(blob);
  });
}

/**
 * 上传单个文件。
 *
 * 不用 FormData，而是把文件本身作为请求体：
 * 服务端可以边收边写盘，2GB 文件也不会先在服务器临时目录里落一份。
 *
 * @param {string}   root        根目录 id
 * @param {string}   relPath     目标目录（相对根目录）
 * @param {File}     file        文件对象
 * @param {function} onProgress  (loaded, total) => void
 * @param {object}   signalObj   可选 {abort: fn} 用于取消
 * @returns {Promise<object>}
 */
export function uploadFile(root, relPath, file, onProgress, signalObj) {
  return new Promise(function (resolve, reject) {
    const xhr = new XMLHttpRequest();

    if (signalObj) {
      signalObj.abort = function () {
        try {
          xhr.abort();
        } catch (err) {
          /* 忽略 */
        }
      };
    }

    xhr.open(
      'POST',
      buildUrl('/api/fs/upload', { root: root, path: relPath, filename: file.name }),
      true
    );
    xhr.withCredentials = true;
    xhr.setRequestHeader('Content-Type', 'application/octet-stream');
    if (csrfToken) {
      xhr.setRequestHeader('X-CSRF-Token', csrfToken);
    }

    if (xhr.upload && onProgress) {
      xhr.upload.onprogress = function (e) {
        if (e.lengthComputable) {
          onProgress(e.loaded, e.total);
        }
      };
    }

    xhr.onload = function () {
      let data = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch (err) {
        data = null;
      }

      if (xhr.status === 401) {
        redirectToLogin();
        reject(new ApiError('未登录或会话已过期', 401, 'unauthorized'));
        return;
      }

      if (xhr.status >= 200 && xhr.status < 300 && data && data.ok) {
        resolve(data);
        return;
      }

      let message = (data && (data.message || data.detail)) || '';
      if (!message) {
        message = '上传失败（HTTP ' + xhr.status + '）';
      }
      reject(new ApiError(message, xhr.status, (data && data.code) || ''));
    };

    xhr.onerror = function () {
      reject(new ApiError('网络错误，上传失败', 0));
    };
    xhr.onabort = function () {
      reject(new ApiError('上传已取消', 0, 'aborted'));
    };

    xhr.send(file);
  });
}

export default api;

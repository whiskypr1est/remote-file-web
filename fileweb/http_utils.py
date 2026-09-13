# -*- coding: utf-8 -*-
"""
HTTP 辅助模块
=============

主要解决两件事：

1. **Range 请求**（视频/音频拖动进度条的关键）
   自己实现 206 Partial Content，比依赖框架默认行为更可控，
   并且能覆盖 416、条件请求（If-None-Match）等边界情况。

2. **安全地内联输出文件**
   如果把用户上传的 .html/.svg 以原本的 Content-Type 内联返回，
   浏览器会在本服务同源下执行其中的脚本，形成存储型 XSS。
   因此内联预览时统一把这类危险类型降级为 text/plain，
   并附上 X-Content-Type-Options: nosniff。
"""

from __future__ import annotations

import mimetypes
import os
import re
from email.utils import formatdate
from typing import Any, Dict, Iterator, Optional, Tuple, Union
from urllib.parse import quote

from starlette.responses import Response, StreamingResponse

# 单次读取块大小：512KB 在吞吐与内存之间比较平衡
CHUNK_SIZE = 512 * 1024

# 浏览器会「同源执行」的类型。内联预览时必须降级成纯文本。
UNSAFE_INLINE_EXTENSIONS = {
    ".html", ".htm", ".xhtml", ".shtml", ".svg", ".svgz",
    ".xml", ".xsl", ".xslt", ".mhtml", ".mht", ".htc",
    ".js", ".mjs", ".cjs", ".vbs", ".vbe", ".wsf", ".hta", ".jar",
}

# 补充一些 Python 默认没登记的 MIME 类型
mimetypes.add_type("video/mp4", ".mp4")
mimetypes.add_type("video/webm", ".webm")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("video/quicktime", ".mov")
mimetypes.add_type("audio/mpeg", ".mp3")
mimetypes.add_type("audio/flac", ".flac")
mimetypes.add_type("audio/ogg", ".ogg")
mimetypes.add_type("audio/oga", ".oga")
mimetypes.add_type("audio/wav", ".wav")
mimetypes.add_type("audio/mp4", ".m4a")
mimetypes.add_type("audio/aac", ".aac")
mimetypes.add_type("text/plain", ".log")
mimetypes.add_type("text/plain", ".md")
mimetypes.add_type("text/plain", ".ini")
mimetypes.add_type("text/plain", ".conf")
mimetypes.add_type("application/pdf", ".pdf")

# Range 头解析：bytes=start-end
_RANGE_RE = re.compile(r"^bytes=(\d*)-(\d*)$", re.IGNORECASE)


# ---------------------------------------------------------------------------
# 响应头辅助
# ---------------------------------------------------------------------------

def content_disposition(filename: str, inline: bool = False) -> str:
    """
    生成兼容中文文件名的 Content-Disposition。

    HTTP 头只能是 Latin-1，所以中文名必须走 RFC 5987 的 filename* 参数，
    同时给一个 ASCII 兜底名，老客户端也能拿到一个可用的名字。
    """
    disposition = "inline" if inline else "attachment"
    fallback = "".join(
        ch for ch in (filename or "")
        if 32 <= ord(ch) < 127 and ch not in '"\\'
    ).strip()
    # 去掉可能造成头注入的字符
    fallback = fallback.replace("\r", "").replace("\n", "") or "file"
    encoded = quote(filename or "file", safe="")
    return "%s; filename=\"%s\"; filename*=UTF-8''%s" % (disposition, fallback, encoded)


def guess_media_type(path: str, inline: bool = True) -> str:
    """
    推断 MIME 类型。

    inline=True 且扩展名属于「浏览器会执行」的类型时，强制降级为 text/plain，
    避免存储型 XSS。
    """
    ext = os.path.splitext(path or "")[1].lower()

    if inline and ext in UNSAFE_INLINE_EXTENSIONS:
        return "text/plain; charset=utf-8"

    media_type, _encoding = mimetypes.guess_type(path or "")
    if not media_type:
        return "application/octet-stream"

    # 文本类补上 charset，否则浏览器按本地编码猜，中文会乱码
    if media_type.startswith("text/") and "charset" not in media_type:
        media_type += "; charset=utf-8"

    return media_type


def parse_range_header(header: Optional[str], file_size: int) -> Union[None, str, Tuple[int, int]]:
    """
    解析 Range 请求头。

    返回：
        None            —— 未提供 Range，或多段 Range（按 RFC 允许忽略，直接给完整内容）
        "invalid"       —— 请求范围不可满足，应返回 416
        (start, end)    —— 闭区间，应返回 206
    """
    if not header:
        return None

    header = header.strip()
    if "," in header:
        # 多段 Range：本地文件服务没必要支持，直接忽略返回整个文件
        return None

    match = _RANGE_RE.match(header)
    if not match:
        return None

    start_text, end_text = match.group(1), match.group(2)
    if not start_text and not end_text:
        return None

    try:
        if not start_text:
            # 后缀范围：bytes=-500 表示最后 500 字节
            suffix_length = int(end_text)
            if suffix_length <= 0:
                return "invalid"
            start = max(0, file_size - suffix_length)
            end = file_size - 1
        else:
            start = int(start_text)
            end = int(end_text) if end_text else file_size - 1
    except ValueError:
        return None

    if start > end or start >= file_size:
        return "invalid"

    return start, min(end, file_size - 1)


# ---------------------------------------------------------------------------
# 文件流响应
# ---------------------------------------------------------------------------

def _iter_file(path: str, start: int, length: int) -> Iterator[bytes]:
    """
    按块读取文件片段。

    这是个同步生成器，Starlette 会自动把每次 next() 丢到线程池执行，
    因此不会阻塞事件循环。
    """
    remaining = length
    with open(path, "rb") as fh:
        fh.seek(start)
        while remaining > 0:
            chunk = fh.read(min(CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)
            yield chunk


def file_response(
    request,
    abs_path: str,
    filename: Optional[str] = None,
    inline: bool = True,
    media_type: Optional[str] = None,
    extra_headers: Optional[Dict[str, str]] = None,
) -> Response:
    """
    输出本地文件，完整支持 Range（拖动进度条）、条件请求与中文文件名。

    abs_path 必须是**已经过安全校验**的绝对路径。
    """
    if not os.path.isfile(abs_path):
        return Response("文件不存在或已被删除", status_code=404, media_type="text/plain; charset=utf-8")

    try:
        stat = os.stat(abs_path)
    except OSError:
        return Response("无法读取文件", status_code=403, media_type="text/plain; charset=utf-8")

    file_size = int(stat.st_size)
    name = filename or os.path.basename(abs_path)

    # ETag 用「修改时间 + 大小」，文件一变就失效
    etag = '"%x-%x"' % (int(stat.st_mtime), file_size)
    last_modified = formatdate(stat.st_mtime, usegmt=True)

    headers: Dict[str, str] = {
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "Last-Modified": last_modified,
        # 私有内容，禁止中间缓存；但允许浏览器协商缓存（配合 ETag 省流量）
        "Cache-Control": "private, max-age=0, must-revalidate",
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": content_disposition(name, inline=inline),
    }
    if extra_headers:
        headers.update(extra_headers)

    # 条件请求：内容没变直接 304
    if_none_match = request.headers.get("if-none-match")
    if if_none_match and if_none_match == etag:
        return Response(
            status_code=304,
            headers={
                "ETag": etag,
                "Last-Modified": last_modified,
                "Cache-Control": headers["Cache-Control"],
            },
        )

    resolved_type = media_type or guess_media_type(abs_path, inline=inline)

    parsed = parse_range_header(request.headers.get("range"), file_size)

    if parsed == "invalid":
        headers["Content-Range"] = "bytes */%d" % file_size
        return Response(
            "请求的字节范围无法满足",
            status_code=416,
            headers=headers,
            media_type="text/plain; charset=utf-8",
        )

    if isinstance(parsed, tuple):
        start, end = parsed
        length = end - start + 1
        headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, file_size)
        headers["Content-Length"] = str(length)
        return StreamingResponse(
            _iter_file(abs_path, start, length),
            status_code=206,
            media_type=resolved_type,
            headers=headers,
        )

    headers["Content-Length"] = str(file_size)
    return StreamingResponse(
        _iter_file(abs_path, 0, file_size),
        status_code=200,
        media_type=resolved_type,
        headers=headers,
    )


def json_error(message: str, status_code: int = 400, code: str = "") -> Response:
    """统一的 JSON 错误响应。"""
    from fastapi.responses import JSONResponse

    payload: Dict[str, Any] = {"ok": False, "message": message}
    if code:
        payload["code"] = code
    return JSONResponse(payload, status_code=status_code)

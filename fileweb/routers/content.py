# -*- coding: utf-8 -*-
"""
内容与预览路由
==============

    GET /api/fs/raw         —— 输出文件原始内容（支持 Range，用于图片/音视频/下载）
    GET /api/fs/thumb       —— 图片缩略图（100x100，服务端缓存）
    GET /api/fs/text        —— 文本预览（自动识别 UTF-8 / GB18030 / Big5 等编码）
    POST /api/fs/text       —— 保存文本编辑结果（原子写入 + 编码/BOM/换行还原）
    GET /api/fs/office      —— Office 预览（soffice 转 PDF，或纯 Python 解析 HTML）
    GET /api/fs/office/pdf  —— 输出转换后的 PDF（给 pdf.js 用）

这几个接口都只接受「根标识 + 相对路径」，不接受任意绝对路径以外的信息，
转换产生的中间文件（PDF）也只能通过接口按原文件路径反查获得，
不会把缓存目录本身暴露出去。
"""

from __future__ import annotations

import codecs
import os
import tempfile
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .. import fsops, office, thumbs
from ..deps import get_state, resolver_of
from ..http_utils import file_response
from ..security import PathSecurityError
# 写权限/受保护路径的判定复用 fs.py 里那一对守卫，而不是在这里再抄一份。
# 这两个函数只依赖入参与 HTTPException，fs.py 也从不反向导入本模块，
# 因此这条导入不会成环（app.py 里两个路由的注册先后也无关紧要）。
from .fs import _ensure_not_protected, _ensure_writable

router = APIRouter(prefix="/api/fs", tags=["内容与预览"])

# 文本预览支持的编码，按优先级从高到低尝试
TEXT_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "big5", "cp932", "latin-1")

# 判定为二进制文件的采样长度
SNIFF_BYTES = 8192

# 读写文本时统一使用的换行符表示
NEWLINES = ("\r\n", "\n", "\r")

# 保存文本时允许的编码白名单。
#
# 客户端会把 GET 回报的 encoding 原样回传，所以这里必须只认我们自己产出的
# 那几个名字（外加 GBK / UTF-16 的常见别名）。**绝不能**把客户端给的字符串
# 直接丢进 str.encode()：那等于让请求方指定任意编解码器，
# 而且 "utf-8(replace)" 这种兜底名根本不是合法编码名。
TEXT_ENCODE_ENCODINGS = {
    "utf-8": "utf-8",
    "utf-8-sig": "utf-8",      # 兼容 GET 回报的名字；BOM 由 bom 字段决定
    "utf8": "utf-8",
    "gb18030": "gb18030",
    "gbk": "gb18030",          # GB18030 是 GBK 的超集，GBK 字节可无损往返
    "gb2312": "gb18030",
    "big5": "big5",
    "big5hkscs": "big5hkscs",
    "cp932": "cp932",
    "shift_jis": "shift_jis",
    "latin-1": "latin-1",
    "latin1": "latin-1",
    "iso-8859-1": "iso-8859-1",
    # UTF-16 交给 Python 的 utf-16 编解码器处理字节序与 BOM 标志位，
    # 它写出的就是「LE 字节 + FF FE」，正好是 Windows 记事本的样子。
    "utf-16": "utf-16",
}

# BOM 字节完全写死，不用 codecs.BOM_*，也不引入 "utf-8-sig" 这种
# 「编码时自动加 BOM」的编码器：一条路径只负责一件事，
# 就不会出现「bom=true 又叠一次编码器自带 BOM」的双重 BOM。
UTF8_BOM = b"\xef\xbb\xbf"
UTF16_LE_BOM = b"\xff\xfe"

# 可能带 BOM 的编码 -> 该编码的 BOM 字节。
# "utf-8-sig" 是解码器回报的名字，它和 "utf-8" 是同一族（只是带 BOM），
# 所以两个键都要有：GET 汇报的 encoding 正是 "utf-8-sig" 时也要能查到。
BOM_BYTES = {
    "utf-8": UTF8_BOM,
    "utf-8-sig": UTF8_BOM,
    "utf-16": UTF16_LE_BOM,
}

# 乐观并发校验里对 mtime 的容差（秒）。
#
# 为什么是 2ms 而不是 0：st_mtime 是 st_mtime_ns 除以 1e9 得到的 float，
# 换算本身就会丢掉一点点精度，直接 == 比较可能在「文件根本没变」时误判冲突。
# 2ms 小到什么程度：任何一次真实编辑都由编辑器的「写临时文件再替换 / 截断
# 重写」完成，新 mtime 与旧值相差都是几十毫秒到秒级，2ms 的窗口不可能把
# 一次真实修改盖过去。反过来，NTFS 的 mtime 精度是 100ns，FAT/exFAT 最粗
# 也只到 2s —— 那种粗粒度只会让冲突**更容易**被发现，不会让冲突漏掉。
MTIME_TOLERANCE_SECONDS = 0.002


class TextSavePayload(BaseModel):
    """POST /api/fs/text 的请求体：客户端把 GET 回报的元信息原样回传。"""
    root: str = ""
    path: str = ""
    text: str = ""
    encoding: str = "utf-8"
    # BOM 与否、换行符风格由客户端回传，服务端据此还原原始字节形态
    bom: bool = False
    newline: str = "\n"
    # 乐观并发令牌：GET 时拿到的 mtime / size。
    # 缺省（None）表示不校验 —— 调用方在 GET 没拿到这两个值时应当显式传 0。
    base_mtime: Optional[float] = None
    base_size: Optional[int] = None


def _resolve_file(request: Request, root: str, path: str, must_dir: bool = False):
    """
    解析并校验路径，同时确认文件确实存在。

    返回 (state, root_cfg, abs_path)
    """
    state = get_state(request)

    try:
        root_cfg, abs_path = resolver_of(request).resolve(root, path)
    except PathSecurityError as exc:
        raise HTTPException(status_code=403, detail=str(exc))

    if not os.path.exists(abs_path):
        raise HTTPException(status_code=404, detail="文件不存在或已被删除")

    if must_dir and not os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="该路径不是目录")

    return state, root_cfg, abs_path


# ---------------------------------------------------------------------------
# 原始内容
# ---------------------------------------------------------------------------

@router.get("/raw")
async def raw_content(
    request: Request,
    root: str = "",
    path: str = "",
    download: bool = False,
    name: str = "",
):
    """
    输出文件原始内容。

    * download=false（默认）：内联输出，用于图片预览、音视频播放。
      视频拖动进度条依赖本接口的 Range 支持。
    * download=true：作为附件下载。

    注意：.html/.svg 这类会被浏览器执行的类型，内联时会自动降级为 text/plain，
    避免用户上传的网页在本服务同源下执行脚本。
    """
    _state, _root_cfg, abs_path = _resolve_file(request, root, path)

    if os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="这是一个目录，请使用打包下载")

    display_name = name or os.path.basename(abs_path)

    return file_response(
        request,
        abs_path,
        filename=display_name,
        inline=not download,
    )


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------

@router.get("/thumb")
async def thumbnail(request: Request, root: str = "", path: str = "", v: str = ""):
    """
    返回图片缩略图（JPEG，默认 100x100）。

    非图片、解码失败、功能未开启时返回 404，
    前端收到 404 会改用类型图标显示，不会显示破图。

    v 参数（一般是文件修改时间）只是为了改变 URL 让浏览器刷新缓存，
    服务端并不使用它，真正的缓存失效靠文件 mtime + size 组成的缓存键。
    """
    state = get_state(request)
    cfg = state.cfg
    thumb_cfg = cfg.get("thumbs") or {}

    if not thumb_cfg.get("enabled", True):
        raise HTTPException(status_code=404, detail="缩略图功能已关闭")

    _state, _root_cfg, abs_path = _resolve_file(request, root, path)

    if os.path.isdir(abs_path):
        raise HTTPException(status_code=404, detail="目录没有缩略图")

    if not thumbs.is_image(abs_path):
        raise HTTPException(status_code=404, detail="不是支持的图片类型")

    cache_dir = thumb_cfg.get("cache_dir") or os.path.join(state.base_dir, "thumb_cache")
    box = int(thumb_cfg.get("size") or 100)
    # 缓存体积上限来自配置；此前没往下传，导致 thumbs.max_cache_mb 是死配置
    max_mb = int(thumb_cfg.get("max_cache_mb") or 512)

    try:
        ok, thumb_path = await run_in_threadpool(
            thumbs.ensure_thumb, abs_path, cache_dir, box, max_mb
        )
    except Exception:  # noqa: BLE001
        ok, thumb_path = False, None

    if not ok or not thumb_path:
        raise HTTPException(status_code=404, detail="无法生成缩略图")

    return file_response(
        request,
        thumb_path,
        filename="thumb.jpg",
        inline=True,
        media_type="image/jpeg",
        extra_headers={
            # 缩略图 URL 带版本参数，可以放心让浏览器缓存久一点
            "Cache-Control": "private, max-age=86400",
        },
    )


# ---------------------------------------------------------------------------
# 文本预览
# ---------------------------------------------------------------------------

def _decode_text(raw: bytes) -> Tuple[str, str]:
    """
    尝试用多种编码解码文本，返回 (文本, 编码名)。

    顺序很关键：
      utf-8-sig 处理带 BOM 的 UTF-8（Windows 记事本常见），
      然后严格 UTF-8，
      再退到 GB18030（覆盖 GBK/GB2312，中文 Windows 存量文件主力编码），
      最后 Big5 / cp932 / latin-1 兜底。

    重要：bytes.decode() **不做**「通用换行」折叠 —— 那是 io.TextIOWrapper
    和 open(newline=...) 的行为，不在字节解码这一层。所以这里解出来的文本
    原样保留 \\r\\n 与单独的 \\r，正好是 _detect_newline() 观测换行风格所需要的。

    ★ 这里曾经写成 raw.decode(encoding, newline)，想把 newline 透传给 decode：
      bytes.decode 的第二个位置参数其实是 errors，根本没有 newline 参数。
      后果有两个，都很隐蔽：
        * newline="" 时等于传了 errors=""，只有在**真的解码失败**时才会
          暴露成 LookupError: unknown error handler name ''；
        * newline=None（默认值）时等于传了 errors=None，直接
          TypeError: decode() argument 'errors' must be str, not None。
      既然 decode 本来就不会折叠换行，这个参数纯属画蛇添足，已删除。
    """
    for encoding in TEXT_ENCODINGS:
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        if encoding == "utf-8-sig" and not raw.startswith(UTF8_BOM):
            # utf-8-sig 不带 BOM 时其实就等价于 utf-8（编解码器会照常解出内容）。
            # 但把编码名如实报成 "utf-8" 很重要：GET 的 encoding 字段会跟
            # bom 字段配对回传给保存接口，报成 utf-8-sig 会让前端以为原文件
            # 带 BOM，进而显示错误的编码/保存选项。
            return text, "utf-8"
        return text, encoding
    # 理论上不会走到这里（latin-1 不会失败），保险起见用替换字符兜底
    return raw.decode("utf-8", errors="replace"), "utf-8(replace)"


def _detect_bom(raw: bytes, encoding: str) -> bool:
    """
    判断这段字节开头是不是该编码的 BOM。

    只对 utf-8 / utf-16 做判断：其它编码（GBK、Big5…）本来就不带 BOM，
    硬套 BOM 概念只会误报。
    """
    bom = BOM_BYTES.get(encoding)
    return bool(bom) and raw.startswith(bom)


def _detect_newline(content: str) -> str:
    """
    返回文本里「第一个出现的」换行风格，没有换行符时返回 "\\n"。

    传入的 content 必须是**换行原样保留**的文本。这一点由 bytes.decode 天然
    保证 —— 它不做「通用换行」折叠（那是 io.TextIOWrapper / open(newline=...)
    的行为），所以 _decode_text 解出来的内容里 \\r\\n 与单独的 \\r 都还在。
    （不需要、也没法给 decode 传 newline 参数：它的签名只有 encoding/errors。）

    必须是**向后看**：\\r 后面跟 \\n 才算 CRLF，否则是单独的 CR。
    写成「往前看上一个字符是否为 \\r」会漏掉 CRLF —— 因为 \\r\\n 会被切成
    两个字符各走一轮循环，那个 \\r 早已被跳过。
    """
    index = 0
    length = len(content)
    while index < length:
        ch = content[index]
        if ch == "\r":
            if index + 1 < length and content[index + 1] == "\n":
                return "\r\n"
            return "\r"
        if ch == "\n":
            return "\n"
        index += 1
    return "\n"


def _looks_binary(raw: bytes) -> bool:
    """采样判断是否为二进制文件（含 NUL 字节或大量不可打印字符）。"""
    if not raw:
        return False
    if b"\x00" in raw:
        return True
    sample = raw[:SNIFF_BYTES]
    printable = sum(1 for byte in sample if 32 <= byte < 127 or byte in (9, 10, 13) or byte >= 128)
    return (printable / max(1, len(sample))) < 0.75


def _text_looks_binary(text: str) -> bool:
    """
    按文本判断内容是不是二进制。

    专供 UTF-16：它的编码字节里**天然**含大量 NUL（"a" 就是一个 00 字节），
    拿 _looks_binary 去量必然判成二进制，那样 UTF-16 文件永远存不进去。
    所以对 UTF-16 改判「解码后的文本里有没有 NUL」——
    真正的二进制内容在解码后仍会留下 NUL，正常文本则不会有。
    """
    return "\x00" in text


@router.get("/text")
async def text_preview(request: Request, root: str = "", path: str = ""):
    """
    文本预览。

    只读取文件开头的一段（默认 2MB，由 config.json 的 preview.text_max_kb 控制），
    避免打开几个 GB 的日志文件时把服务器内存和浏览器一起拖死。
    """
    state = get_state(request)
    cfg = state.cfg
    max_kb = int((cfg.get("preview") or {}).get("text_max_kb") or 2048)
    max_bytes = max_kb * 1024

    _state, _root_cfg, abs_path = _resolve_file(request, root, path)

    if os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="这是一个目录")

    try:
        file_size = os.path.getsize(abs_path)
    except OSError as exc:
        raise HTTPException(status_code=403, detail="无法读取文件：%s" % exc)

    def _read() -> Tuple[bytes, bool]:
        with open(abs_path, "rb") as fh:
            data = fh.read(max_bytes)
        return data, file_size > len(data)

    try:
        raw, truncated = await run_in_threadpool(_read)
    except PermissionError:
        raise HTTPException(status_code=403, detail="没有权限读取该文件")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="读取文件失败：%s" % exc)

    if _looks_binary(raw):
        return {
            "ok": False,
            "binary": True,
            "message": "这看起来是二进制文件，无法以文本方式预览，请下载后用专用软件打开。",
            "size": file_size,
        }

    # bytes.decode 不会折叠换行，所以这里拿到的 content 原样保留 \r\n
    # 与单独的 \r —— _detect_newline 正是靠这一点观测文件的换行风格。
    content, encoding = _decode_text(raw)

    # 统计行数（截断时只统计已读取部分）
    line_count = content.count("\n") + (0 if content.endswith("\n") or not content else 1)

    try:
        mtime = os.path.getmtime(abs_path)
    except OSError as exc:
        raise HTTPException(status_code=403, detail="无法读取文件：%s" % exc)

    return {
        "ok": True,
        "binary": False,
        "content": content,
        "encoding": encoding,
        "size": file_size,
        "size_text": fsops.human_size(file_size),
        "truncated": truncated,
        "max_kb": max_kb,
        "line_count": line_count,
        "name": os.path.basename(abs_path),
        # mtime 给编辑器当乐观并发令牌：保存时原样回传，服务端比对不一致就拒写
        "mtime": mtime,
        # 保存时按这两个字段还原原始字节形态（BOM + 换行符风格）
        "bom": _detect_bom(raw, encoding),
        "newline": _detect_newline(content),
    }


# ---------------------------------------------------------------------------
# 文本保存（编辑器写回）
# ---------------------------------------------------------------------------

def _normalize_encoding(name: str) -> str:
    """把客户端回传的编码名收敛成白名单里的 Python 编码名，不在白名单就 400。"""
    key = (name or "").strip().lower()
    resolved = TEXT_ENCODE_ENCODINGS.get(key)
    if not resolved:
        raise HTTPException(
            status_code=400,
            detail="不支持以该编码保存：%s。请重新打开文件后再试。" % (name or "(空)"),
        )
    return resolved


def _to_lf(text: str) -> str:
    """
    把任意换行风格统一成 LF。

    先转 \\r\\n 再转单独的 \\r，顺序反了会把 CRLF 拆成两个 LF。
    客户端本该提交 LF 文本，这里再兜一次底：否则「文本里已经有 CRLF、
    又被要求写成 CRLF」就会变成 \\r\\r\\n。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _apply_newline(text_lf: str, newline: str) -> str:
    """把 LF 文本转成目标换行风格。newline 不在允许集合内时 400。"""
    if newline not in NEWLINES:
        raise HTTPException(
            status_code=400,
            detail="换行符参数不合法：%r（只接受 \\r\\n、\\n、\\r）" % (newline,),
        )
    if newline == "\n":
        return text_lf
    return text_lf.replace("\n", newline)


def _encode_text(text: str, encoding: str, bom: bool) -> bytes:
    """
    按客户端回传的编码 + BOM 选项编码文本，还原原始字节形态。

    UTF-16 交给 Python 的 utf-16 编解码器：它会自己写 BOM 标志位，
    正好对应「bom=true」；而 bom=false 的 UTF-16 根本没有合法写法
    （没有 BOM 就无从判断字节序），所以直接拒绝而不是猜一个。
    """
    if encoding == "utf-16":
        if not bom:
            raise HTTPException(
                status_code=400,
                detail="UTF-16 文件必须带 BOM，无法以「不带 BOM 的 UTF-16」保存。",
            )
        try:
            return text.encode("utf-16")   # 编解码器自己写好 BOM
        except UnicodeEncodeError:
            raise HTTPException(
                status_code=400,
                detail="内容无法用原编码（utf-16）保存，可能是新增了不支持的字符。",
            )

    if bom and encoding not in BOM_BYTES:
        # 例如 GBK / Big5：这些编码根本没有 BOM 的概念。
        # 客户端谎报 bom=true 时必须明确拒绝 —— 直接拿 encoding 去查
        # BOM_BYTES 会抛 KeyError，那是 500，不该由一次畸形请求触发。
        raise HTTPException(
            status_code=400,
            detail="编码 %s 不支持 BOM，无法按「带 BOM」保存。" % encoding,
        )

    try:
        raw = text.encode(encoding)
    except UnicodeEncodeError:
        raise HTTPException(
            status_code=400,
            detail="内容无法用原编码（%s）保存，可能是新增了该编码不支持的字符。"
                   "请另存为 UTF-8 后再试。" % encoding,
        )

    if bom:
        return BOM_BYTES[encoding] + raw
    return raw


def _reject_binary_content(data: bytes, encoding: str, text: str) -> None:
    """
    拒绝写入「看起来还是二进制」的内容。

    编码后含 NUL 的文件下次 GET 会被判成二进制，等于把用户刚保存的文本
    变成一个打不开的文件，所以在写入前就拦下。

    UTF-16 例外：它的字节里天然有 NUL，只能按解码后的文本来判断
    （见 _text_looks_binary）。
    """
    is_binary = (_text_looks_binary(text) if encoding == "utf-16"
                 else _looks_binary(data))
    if is_binary:
        raise HTTPException(
            status_code=400,
            detail="内容按该编码保存后会包含 NUL 等二进制字节，已拒绝写入。",
        )


def _save_text_sync(abs_path: str, payload: TextSavePayload, encoding: str,
                    max_bytes: int) -> Dict[str, Any]:
    """
    真正落盘的部分，整体在线程池里跑（同步文件 IO + 原子替换）。

    顺序是刻意安排的，每一步都必须在写之前完成：
        1. 确认目标仍是普通文件（不是目录/设备/失效链接）
        2. 确认磁盘上的文件本身没超过可编辑上限（防止把截断视图写回去）
        3. 编码 + BOM + 换行还原
        4. 拒绝编码后仍是二进制的内容
        5. 乐观并发校验（mtime / size 与客户端回传的基线比对）
        6. 临时文件 + os.replace 原子替换
    """
    if not os.path.isfile(abs_path):
        raise HTTPException(status_code=400, detail="该路径不是一个普通文件，无法保存")

    # ---- 1/2. 数据丢失护栏：太大就根本不读不写 --------------------------
    #
    # GET /api/fs/text 只读前 preview.text_max_kb 字节。如果文件真实大小超过
    # 这个上限，客户端手里拿到的必然是**截断过的**内容；此时若放行写入，
    # 一个 100MB 的日志会被「保存」悄悄截成 2MB —— 这是不可逆的数据丢失。
    # 所以宁可拒绝，并明确告诉用户改走「下载 → 编辑 → 上传」。
    try:
        before = os.stat(abs_path)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="文件不存在或已被删除")
    except PermissionError:
        raise HTTPException(status_code=403, detail="没有权限写入该文件")
    except OSError as exc:
        raise HTTPException(status_code=400, detail="无法读取文件状态：%s" % exc)

    if before.st_size > max_bytes:
        raise HTTPException(
            status_code=400,
            detail="文件约 %s，超过了在线编辑上限 %d KB，无法就地保存（否则会把"
                   "未读取的部分截断丢失）。请改用「下载 → 本地编辑 → 上传」的方式。"
                   % (fsops.human_size(before.st_size), max_bytes // 1024),
        )

    # ---- 3. 编码 / BOM / 换行还原 ---------------------------------------
    # 统一成 LF 后再按目标风格重排；这样重复保存也不会累积出 \r\r\n
    text_lf = _to_lf(payload.text)
    newline_applied = _apply_newline(text_lf, payload.newline)
    data = _encode_text(newline_applied, encoding, payload.bom)

    # ---- 4. 拒绝写入「看起来还是二进制」的内容 ---------------------------
    _reject_binary_content(data, encoding, newline_applied)

    # ---- 5. 乐观并发校验 -------------------------------------------------
    # 任何一个维度不一致都拒写，且不碰文件。
    if payload.base_mtime is not None and \
            abs(before.st_mtime - float(payload.base_mtime)) > MTIME_TOLERANCE_SECONDS:
        raise HTTPException(
            status_code=409,
            detail="文件已在磁盘上被其他程序修改（修改时间不一致），为避免覆盖他人的改动，"
                   "本次保存已取消。请重新打开文件查看最新内容。",
        )

    if payload.base_size is not None and int(before.st_size) != int(payload.base_size):
        raise HTTPException(
            status_code=409,
            detail="文件已在磁盘上被其他程序修改（大小不一致），为避免覆盖他人的改动，"
                   "本次保存已取消。请重新打开文件查看最新内容。",
        )

    # ---- 6. 同目录临时文件 + os.replace 原子替换 -------------------------
    #
    # 必须同目录：os.replace 只有在同一卷上才保证原子性，
    # 而系统临时目录很可能在另一个盘（Windows 上通常是 C:）。
    directory = os.path.dirname(abs_path)
    fd, tmp_path = tempfile.mkstemp(prefix=".fileweb-text-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)

        # 尽量保留原文件的权限位（Windows 上 chmod 语义有限，失败也不影响写入）
        try:
            os.chmod(tmp_path, before.st_mode)
        except OSError:
            pass

        os.replace(tmp_path, abs_path)
    except PermissionError:
        _remove_quietly(tmp_path)
        raise HTTPException(status_code=403, detail="没有权限写入该文件")
    except OSError as exc:
        _remove_quietly(tmp_path)
        raise HTTPException(status_code=500, detail="保存文件失败：%s" % exc)
    except BaseException:
        # 任何其它异常（含写入途中被打断）都不能留下临时文件
        _remove_quietly(tmp_path)
        raise

    after = os.stat(abs_path)

    return {
        "ok": True,
        "size": after.st_size,
        "size_text": fsops.human_size(after.st_size),
        "mtime": after.st_mtime,
    }


def _remove_quietly(path: str) -> None:
    """删除临时文件，失败不抛异常（清理动作不能掩盖真正的错误）。"""
    try:
        os.unlink(path)
    except OSError:
        pass


@router.post("/text")
async def text_save(request: Request, payload: TextSavePayload) -> Dict[str, Any]:
    """
    保存文本编辑结果（配合 GET /api/fs/text 使用）。

    请求体里的 encoding / bom / newline 就是 GET 回报的那三个值，
    服务端据此还原原始字节形态，做到「原样保存不改字节」。

    错误码：
        400 不是普通文件 / 编码不在白名单 / 编码后是二进制 / 文件太大不宜就地编辑
        403 只读根目录 / 受保护路径 / 无权限
        404 文件已不存在
        409 文件在磁盘上已被他人修改（乐观并发冲突），此时**不会写入任何内容**
        413 提交的文本本身超过 preview.text_max_kb 上限
    """
    state = get_state(request)
    cfg = state.cfg
    max_kb = int((cfg.get("preview") or {}).get("text_max_kb") or 2048)
    max_bytes = max_kb * 1024

    _state, root_cfg, abs_path = _resolve_file(request, payload.root, payload.path)

    # 权限类判断放前面：即使后面因为别的原因失败，也不该让不可写的位置
    # 产生「看起来像是可以写」的行为差异。
    _ensure_writable(root_cfg)
    _ensure_not_protected(cfg, abs_path)
    # 临时文件要落在目标所在目录，所以父目录同样不能是受保护路径
    # （PathResolver 返回的是 realpath，符号链接已经解析过，这里挡住
    #   「受保护目录里的文件被就地替换」这条路）。
    _ensure_not_protected(cfg, os.path.dirname(abs_path))

    # 413：提交的文本本身超上限。先做一次便宜的字符数预筛（UTF-8 下
    # 1 个字符至少 1 字节），只有超过一半时才算真实字节数，避免为了报错
    # 先把几十 MB 的文本编码一遍。
    submitted_bytes = len(payload.text)
    if submitted_bytes > max_bytes // 2:
        submitted_bytes = len(payload.text.encode("utf-8"))
    if submitted_bytes > max_bytes:
        raise HTTPException(
            status_code=413,
            detail="提交的内容约 %s，超过在线编辑上限 %d KB（preview.text_max_kb），"
                   "请改用「下载 → 编辑 → 上传」的方式。"
                   % (fsops.human_size(submitted_bytes), max_kb),
        )

    encoding = _normalize_encoding(payload.encoding)

    return await run_in_threadpool(_save_text_sync, abs_path, payload, encoding, max_bytes)


# ---------------------------------------------------------------------------
# Office 预览
# ---------------------------------------------------------------------------

def _office_pdf_path(abs_path: str, cfg: Dict[str, Any]) -> Optional[str]:
    """
    反查某个文档已缓存的转换结果（PDF 路径）。
    没有缓存或没装 LibreOffice 时返回 None。
    """
    office_cfg = cfg.get("office") or {}
    if not office_cfg.get("enabled", True):
        return None

    soffice = office.find_soffice(office_cfg.get("soffice_path") or "")
    if not soffice:
        return None

    cache_dir = office_cfg.get("cache_dir")
    if not cache_dir:
        return None

    cached = os.path.join(cache_dir, office._pdf_cache_key(abs_path) + ".pdf")
    if os.path.isfile(cached) and os.path.getsize(cached) > 0:
        return cached
    return None


@router.get("/office")
async def office_preview(request: Request, root: str = "", path: str = ""):
    """
    Office / 文档预览。

    返回结构：
        {"ok": true, "mode": "pdf",  "pdf_url": "/api/fs/office/pdf?..."}
        {"ok": true, "mode": "html", "html": "<div>...</div>", "message": "..."}
        {"ok": true, "mode": "unsupported", "message": "友好提示"}
    """
    state = get_state(request)
    cfg = state.cfg

    _state, root_cfg, abs_path = _resolve_file(request, root, path)

    if os.path.isdir(abs_path):
        raise HTTPException(status_code=400, detail="这是一个目录")

    extension = os.path.splitext(abs_path)[1].lower()
    if extension not in office.OFFICE_EXTENSIONS:
        return {
            "ok": True,
            "mode": "unsupported",
            "message": "该类型不走 Office 预览流程，请使用对应的预览窗口。",
        }

    office_cfg = cfg.get("office") or {}

    try:
        result = await run_in_threadpool(office.build_preview, abs_path, office_cfg)
    except Exception as exc:  # noqa: BLE001
        # 预览失败绝不能让接口 500，转成友好提示交给前端展示
        return {
            "ok": True,
            "mode": "unsupported",
            "message": "预览时发生意外错误：%s" % exc,
        }

    mode = result.get("mode")

    if mode == "pdf":
        rel = resolver_of(request).to_rel(root_cfg, abs_path)
        return {
            "ok": True,
            "mode": "pdf",
            "converter": result.get("converter", "libreoffice"),
            "message": result.get("message", ""),
            # 通过原文件路径反查缓存 PDF，前端无需知道缓存目录结构
            "pdf_url": "/api/fs/office/pdf?root=%s&path=%s" % (
                quote(root_cfg["id"], safe=""),
                quote(rel, safe=""),
            ),
        }

    if mode == "html":
        return {
            "ok": True,
            "mode": "html",
            "converter": result.get("converter", "python-ooxml"),
            "html": result.get("html", ""),
            "message": result.get("message", ""),
        }

    return {
        "ok": True,
        "mode": "unsupported",
        "message": result.get("message", "暂不支持预览该文档。"),
        "libreoffice": bool(office.find_soffice(office_cfg.get("soffice_path") or "")),
    }


@router.get("/office/pdf")
async def office_pdf(request: Request, root: str = "", path: str = ""):
    """
    输出 Office 文档转换后的 PDF，供前端 pdf.js 渲染。

    这里不接受「缓存文件路径」作为参数，而是用原文档路径重新计算缓存键，
    因此不存在通过该接口读取任意文件的风险。
    """
    state = get_state(request)
    cfg = state.cfg

    _state, _root_cfg, abs_path = _resolve_file(request, root, path)

    pdf_path = _office_pdf_path(abs_path, cfg)
    if not pdf_path:
        raise HTTPException(
            status_code=404,
            detail="尚未生成 PDF 预览，请先在预览窗口中触发转换。",
        )

    base_name = os.path.splitext(os.path.basename(abs_path))[0] + ".pdf"

    return file_response(
        request,
        pdf_path,
        filename=base_name,
        inline=True,
        media_type="application/pdf",
    )

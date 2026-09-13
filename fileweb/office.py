# -*- coding: utf-8 -*-
"""
Office 文档预览模块
===================

两种预览路径，按优先级自动选择：

1. **LibreOffice 无头转 PDF（首选）**
   调用本机 soffice.exe 把 doc/docx/xls/xlsx/ppt/pptx/odt… 转成 PDF，
   再由前端用 pdf.js 渲染。版式最接近原文档。

2. **纯 Python 解析 OOXML（降级）**
   没装 LibreOffice 时，docx/pptx/xlsx 本质上是 zip + XML，
   直接用标准库解压解析，抽取文字与表格生成 HTML 预览。
   注意：这只保证「内容可读」，不保证「版式一致」。
   老的二进制格式 .doc/.xls/.ppt 无法用这种方式解析，只能提示安装 LibreOffice。

并发安全：
    LibreOffice 同一时刻只允许一个实例使用同一个用户配置目录，
    因此这里用全局锁串行化转换，并为每次转换分配独立的
    -env:UserInstallation 目录，避免残留锁文件导致后续转换全部失败。
"""

from __future__ import annotations

import hashlib
import glob
import html
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量与全局状态
# ---------------------------------------------------------------------------

# 需要走 Office 预览流程的扩展名
OFFICE_EXTENSIONS = {
    ".doc", ".docx", ".docm", ".dot", ".dotx",
    ".xls", ".xlsx", ".xlsm", ".xlt", ".xltx", ".csv",
    ".ppt", ".pptx", ".pptm", ".pot", ".potx",
    ".odt", ".ods", ".odp", ".rtf",
}

# 能用纯 Python 解析的 OOXML 扩展名
OOXML_EXTENSIONS = {".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".xltx", ".pptx", ".pptm", ".potx"}

# 老的二进制格式，只有 LibreOffice 能处理
LEGACY_BINARY_EXTENSIONS = {".doc", ".xls", ".ppt", ".dot", ".xlt", ".pot"}

# 解析 OOXML 时的安全上限，防止畸形文档把内存吃光
_MAX_PART_BYTES = 64 * 1024 * 1024      # 单个 XML 部件最大 64MB
_MAX_XLSX_ROWS = 1000                    # 表格最多渲染 1000 行
_MAX_XLSX_COLS = 64                      # 最多渲染 64 列
_MAX_TEXT_CHARS = 400_000                # 生成的 HTML 文本上限

# LibreOffice 转换必须串行
_soffice_lock = threading.Lock()

# soffice 路径探测结果缓存：{配置值: 探测结果}
_soffice_cache: Dict[str, Optional[str]] = {}
_soffice_cache_lock = threading.Lock()


# ---------------------------------------------------------------------------
# LibreOffice 探测
# ---------------------------------------------------------------------------

def _version_sort_key(path: str) -> Tuple[int, ...]:
    """
    提取路径里的版本号，用于「新版本优先」排序。

    直接对完整路径做反字典序是不对的：'LibreOffice 7.6' 会排在
    'LibreOffice 24.8' 之前（字符 '7' > '2'），结果可能选中更旧的版本。
    这里把所有数字片段解析成整数元组再比较。
    """
    numbers: List[int] = []
    current = ""
    for ch in path:
        if ch.isdigit():
            current += ch
        elif current:
            numbers.append(int(current))
            current = ""
    if current:
        numbers.append(int(current))
    return tuple(numbers) if numbers else (0,)


def _soffice_candidates() -> List[str]:
    """列出所有值得尝试的 soffice.exe 位置。"""
    candidates: List[str] = []

    # 1) 先看 PATH
    which = shutil.which("soffice") or shutil.which("soffice.exe")
    if which:
        candidates.append(which)

    if sys.platform == "win32":
        # 2) 常见安装位置（含带版本号的目录，如 LibreOffice 7）
        patterns = [
            r"C:\Program Files\LibreOffice*\program\soffice.exe",
            r"C:\Program Files (x86)\LibreOffice*\program\soffice.exe",
            r"D:\Program Files\LibreOffice*\program\soffice.exe",
            r"D:\LibreOffice*\program\soffice.exe",
            r"C:\LibreOffice*\program\soffice.exe",
        ]
        for pattern in patterns:
            try:
                matches = glob.glob(pattern)
                matches.sort(key=_version_sort_key, reverse=True)
                candidates.extend(matches)
            except Exception:  # noqa: BLE001
                continue
    else:
        # Linux/macOS 常见路径，便于同一套代码跨平台跑
        candidates.extend([
            "/usr/bin/soffice",
            "/usr/local/bin/soffice",
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
        ])

    # 去重且保持顺序
    seen = set()
    ordered = []
    for item in candidates:
        key = os.path.normcase(item)
        if key not in seen:
            seen.add(key)
            ordered.append(item)
    return ordered


def find_soffice(configured_path: str = "") -> Optional[str]:
    """
    探测可用的 soffice 可执行文件。

    探测结果会缓存，避免每次预览都去扫磁盘。
    传入 configured_path 时优先使用它（但仍会校验文件是否存在）。
    """
    cache_key = configured_path or ""
    with _soffice_cache_lock:
        if cache_key in _soffice_cache:
            return _soffice_cache[cache_key]

    found: Optional[str] = None

    if configured_path:
        candidate = configured_path.strip().strip('"')
        if os.path.isfile(candidate):
            found = candidate

    if not found:
        for candidate in _soffice_candidates():
            if candidate and os.path.isfile(candidate):
                found = candidate
                break

    with _soffice_cache_lock:
        _soffice_cache[cache_key] = found
    return found


def reset_soffice_cache() -> None:
    """清空探测缓存（用户装了 LibreOffice 之后可以调用来重新探测）。"""
    with _soffice_cache_lock:
        _soffice_cache.clear()


# ---------------------------------------------------------------------------
# LibreOffice 转 PDF
# ---------------------------------------------------------------------------

def _pdf_cache_key(src_path: str) -> str:
    """转换缓存键：路径 + 修改时间 + 大小。"""
    try:
        st = os.stat(src_path)
        raw = "%s|%.6f|%d" % (os.path.normcase(os.path.abspath(src_path)), st.st_mtime, st.st_size)
    except OSError:
        raw = os.path.normcase(os.path.abspath(src_path))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:32]


def _path_to_file_url(path: str) -> str:
    """
    把本地路径转换成 file:// URL，供 -env:UserInstallation 使用。

    必须做完整的百分号转义：早先只替换了空格和 #，而 office_cache 默认就落在
    项目目录下，而项目目录名本身完全可能含中文。URL 里直接出现非 ASCII
    字符时，部分 LibreOffice 版本会解析失败或静默退回共享用户配置 ——
    那样「每次转换使用独立 profile」的设计前提就没了。
    """
    p = os.path.abspath(path).replace("\\", "/")
    if not p.startswith("/"):
        p = "/" + p

    safe = "/:.-_~"
    out = []
    for ch in p:
        if ch.isascii() and (ch.isalnum() or ch in safe):
            out.append(ch)
        else:
            # 非 ASCII 按 UTF-8 逐字节做百分号编码
            out.extend("%%%02X" % byte for byte in ch.encode("utf-8"))
    return "file://" + "".join(out)


def _kill_process_tree(proc) -> None:
    """
    结束子进程及其整棵进程树。

    LibreOffice 由 soffice.exe 启动，真正干活的是 soffice.bin：
    只结束前者会留下孤儿进程一直占着 profile 目录，导致清理临时目录时
    静默失败，在缓存目录里积攒下一堆 office-* 垃圾。
    """
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:  # noqa: BLE001
        pass

    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=15,
                check=False,
            )
            return
        except Exception:  # noqa: BLE001 - taskkill 不可用时退回直接 kill
            pass

    try:
        proc.kill()
    except Exception:  # noqa: BLE001
        pass


# 缓存淘汰的节流状态：每转换若干次、且间隔足够久才扫一次目录
_evict_state = {"last": 0.0, "count": 0}
_evict_lock = threading.Lock()
_EVICT_INTERVAL_COUNT = 20
_EVICT_MIN_INTERVAL = 120.0
# 超过这个年龄的 office-* 临时目录视为「异常退出留下的孤儿」，直接删除
_ORPHAN_DIR_MAX_AGE = 3600.0


def _maybe_evict_cache(cache_dir: str, max_mb: int = 1024) -> None:
    """
    定期清理转换缓存目录。

    做两件事：
      1. 删掉超过 1 小时的 office-* 临时工作目录（转换超时被杀后可能残留）；
      2. 缓存的 PDF 总体积超过上限时，按最旧优先删到上限的 80%。

    此前这里什么都没有：源文件每保存一次就多出一份 PDF，永不回收。
    """
    with _evict_lock:
        _evict_state["count"] += 1
        if _evict_state["count"] % _EVICT_INTERVAL_COUNT != 0:
            return
        now = time.time()
        if now - _evict_state["last"] < _EVICT_MIN_INTERVAL:
            return
        _evict_state["last"] = now

        try:
            limit_bytes = max(64, int(max_mb)) * 1024 * 1024
            entries: List[Tuple[float, int, str]] = []
            total = 0

            for name in os.listdir(cache_dir):
                full = os.path.join(cache_dir, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue

                if os.path.isdir(full):
                    if name.startswith("office-") and now - st.st_mtime > _ORPHAN_DIR_MAX_AGE:
                        shutil.rmtree(full, ignore_errors=True)
                    continue

                # 正在写入的临时文件不动它；但异常中断留下的陈旧 .tmp 要按年龄回收
                # —— 它们是完整的 PDF 副本，放着很占地方
                if name.endswith(".tmp"):
                    if now - st.st_mtime > _ORPHAN_DIR_MAX_AGE:
                        try:
                            os.unlink(full)
                        except OSError:
                            pass
                    continue

                total += st.st_size
                entries.append((st.st_mtime, st.st_size, full))

            if total <= limit_bytes:
                return

            target = int(limit_bytes * 0.8)
            entries.sort(key=lambda item: item[0])   # 旧的排前面
            for _mtime, size, full in entries:
                if total <= target:
                    break
                try:
                    os.unlink(full)
                    total -= size
                except OSError:
                    continue
        except Exception:  # noqa: BLE001 - 清理失败绝不能影响转换结果
            return


def convert_to_pdf(
    src_path: str,
    cache_dir: str,
    soffice_path: str,
    timeout: int = 120,
    max_cache_mb: int = 1024,
) -> Tuple[bool, str]:
    """
    用 LibreOffice 把文档转成 PDF（带磁盘缓存）。

    返回：
        (True, PDF 绝对路径)
        (False, 失败原因，可直接展示给用户)
    """
    key = _pdf_cache_key(src_path)
    cached_pdf = os.path.join(cache_dir, key + ".pdf")

    if os.path.isfile(cached_pdf) and os.path.getsize(cached_pdf) > 0:
        return True, cached_pdf

    if not os.path.isfile(src_path):
        return False, "源文件不存在或已被删除"

    try:
        os.makedirs(cache_dir, exist_ok=True)
    except OSError as exc:
        return False, "无法创建转换缓存目录：%s" % exc

    # 独立的工作目录 + 独立的用户配置目录。
    # 这里必须自己兜住异常：磁盘满 / 权限不足 / 路径过长都会抛 OSError，
    # 一旦让它穿透出去，build_preview 里的纯 Python 降级分支就再也没机会执行。
    try:
        work_dir = tempfile.mkdtemp(prefix="office-", dir=cache_dir)
    except OSError as exc:
        return False, "无法创建转换临时目录：%s" % exc

    profile_dir = os.path.join(work_dir, "profile")
    out_dir = os.path.join(work_dir, "out")
    try:
        os.makedirs(profile_dir, exist_ok=True)
        os.makedirs(out_dir, exist_ok=True)
    except OSError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return False, "无法创建转换临时目录：%s" % exc

    cmd = [
        soffice_path,
        "--headless",
        "--norestore",
        "--invisible",
        "--nologo",
        "--nodefault",
        "--nolockcheck",
        "--nofirststartwizard",
        "-env:UserInstallation=%s" % _path_to_file_url(profile_dir),
        "--convert-to",
        "pdf:writer_pdf_Export",
        "--outdir",
        out_dir,
        src_path,
    ]

    # Windows 下隐藏控制台窗口，避免服务器上反复弹黑框
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    timeout_seconds = max(10, int(timeout))
    stderr_data = b""

    try:
        # LibreOffice 不能并发共用配置目录，这里串行执行。
        # 这里用 Popen 而不是 subprocess.run：run() 超时时只会结束直接子进程，
        # 而且拿不到 pid，没法结束整棵进程树（见 _kill_process_tree）。
        with _soffice_lock:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=creationflags,
            )
            try:
                _stdout, stderr_data = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                _kill_process_tree(proc)
                shutil.rmtree(work_dir, ignore_errors=True)
                return False, "LibreOffice 转换超时（超过 %d 秒）。文档可能过大或已损坏。" % timeout_seconds
    except FileNotFoundError:
        shutil.rmtree(work_dir, ignore_errors=True)
        return False, "找不到 LibreOffice 可执行文件，请检查配置中的 office.soffice_path。"
    except Exception as exc:  # noqa: BLE001
        shutil.rmtree(work_dir, ignore_errors=True)
        return False, "调用 LibreOffice 失败：%s" % exc

    # 找到产物：正常情况是 out_dir/<源文件主名>.pdf
    produced = None
    stem = os.path.splitext(os.path.basename(src_path))[0]
    expected = os.path.join(out_dir, stem + ".pdf")
    if os.path.isfile(expected):
        produced = expected
    else:
        # 某些版本会改名或产生不同扩展名，这里兜底扫一遍
        for name in os.listdir(out_dir) if os.path.isdir(out_dir) else []:
            if name.lower().endswith(".pdf"):
                produced = os.path.join(out_dir, name)
                break

    if not produced or not os.path.isfile(produced) or os.path.getsize(produced) == 0:
        # communicate() 已经把 stderr 读完了，这里直接用捕获到的字节
        stderr_text = (stderr_data or b"").decode("utf-8", errors="replace").strip()
        shutil.rmtree(work_dir, ignore_errors=True)
        detail = ("；LibreOffice 输出：%s" % stderr_text[:300]) if stderr_text else ""
        return False, "LibreOffice 未能生成 PDF，可能是不支持的格式或文件已损坏%s" % detail

    # 原子搬进缓存目录。
    # 临时名必须唯一：此前用的是固定的 "<key>.pdf.tmp"，两个并发的同文件转换
    # 会踩到同一个临时文件，落败方的 os.replace 抛 FileNotFoundError，
    # 于是误报「保存转换结果失败」，尽管缓存其实已经写好了。
    try:
        fd, tmp_target = tempfile.mkstemp(prefix=key + "-", suffix=".tmp", dir=cache_dir)
        os.close(fd)
    except OSError as exc:
        shutil.rmtree(work_dir, ignore_errors=True)
        return False, "保存转换结果失败：%s" % exc

    try:
        shutil.copyfile(produced, tmp_target)
        os.replace(tmp_target, cached_pdf)
    except OSError as exc:
        try:
            os.unlink(tmp_target)
        except OSError:
            pass
        shutil.rmtree(work_dir, ignore_errors=True)
        return False, "保存转换结果失败：%s" % exc
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    # 转换成功后顺手做一次缓存体积检查（内部有节流，不会每次都扫目录）
    _maybe_evict_cache(cache_dir, max_cache_mb)

    return True, cached_pdf


# ---------------------------------------------------------------------------
# OOXML 纯 Python 解析（降级方案）
# ---------------------------------------------------------------------------

def _read_xml_part(zf: zipfile.ZipFile, name: str) -> Optional[ET.Element]:
    """
    从 zip 中读取并解析一个 XML 部件。
    部件不存在、过大或 XML 畸形都返回 None。
    """
    try:
        info = zf.getinfo(name)
    except KeyError:
        return None
    if info.file_size > _MAX_PART_BYTES:
        return None
    try:
        with zf.open(name) as fh:
            data = fh.read(_MAX_PART_BYTES)
    except Exception:  # noqa: BLE001
        return None
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _local(tag: str) -> str:
    """去掉 XML 命名空间，只保留标签名。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _iter_by_local(root: Optional[ET.Element], name: str):
    """按「去掉命名空间的标签名」遍历元素，兼容不同命名空间前缀。"""
    if root is None:
        return
    for elem in root.iter():
        if _local(elem.tag) == name:
            yield elem


def _collect_text(elem: ET.Element, text_tags=("t",)) -> str:
    """收集元素下所有指定标签的文本并拼接。"""
    chunks: List[str] = []
    for node in elem.iter():
        tag = _local(node.tag)
        if tag in text_tags:
            if node.text:
                chunks.append(node.text)
        elif tag == "br":
            chunks.append("\n")
        elif tag == "tab":
            chunks.append("\t")
    return "".join(chunks)


# ---- docx -----------------------------------------------------------------

_W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Word 段落样式 -> HTML 标题级别
_HEADING_STYLES = {
    "heading1": 1, "heading2": 2, "heading3": 3, "heading4": 4,
    "heading5": 5, "heading6": 6,
    "标题1": 1, "标题2": 2, "标题3": 3,
    "title": 1, "subtitle": 2,
}


def docx_to_html(src_path: str) -> Tuple[bool, str]:
    """把 .docx 解析成 HTML 片段（段落 + 表格 + 基本标题层级）。"""
    try:
        with zipfile.ZipFile(src_path) as zf:
            root = _read_xml_part(zf, "word/document.xml")
            if root is None:
                return False, "文档结构异常：缺少 word/document.xml"

            body = None
            for node in root.iter():
                if _local(node.tag) == "body":
                    body = node
                    break
            if body is None:
                return False, "文档结构异常：未找到正文内容"

            parts: List[str] = []
            total_chars = 0

            for child in list(body):
                tag = _local(child.tag)

                if tag == "p":
                    text = _collect_text(child)

                    # 识别标题级别
                    level = 0
                    for style_node in _iter_by_local(child, "pStyle"):
                        for attr_name, attr_value in style_node.attrib.items():
                            if _local(attr_name) == "val":
                                key = str(attr_value).lower().replace(" ", "")
                                level = _HEADING_STYLES.get(key, 0)
                                break
                        break

                    if not text.strip():
                        parts.append('<p class="ox-empty">&nbsp;</p>')
                        continue

                    escaped = html.escape(text).replace("\n", "<br>")
                    if level:
                        parts.append("<h%d>%s</h%d>" % (level, escaped, level))
                    else:
                        parts.append("<p>%s</p>" % escaped)
                    total_chars += len(text)

                elif tag == "tbl":
                    rows_html: List[str] = []
                    for tr in child:
                        if _local(tr.tag) != "tr":
                            continue
                        cells_html: List[str] = []
                        for tc in tr:
                            if _local(tc.tag) != "tc":
                                continue
                            cell_text = " ".join(
                                _collect_text(p).strip()
                                for p in tc
                                if _local(p.tag) == "p"
                            ).strip()
                            cells_html.append("<td>%s</td>" % html.escape(cell_text))
                        if cells_html:
                            rows_html.append("<tr>%s</tr>" % "".join(cells_html))
                    if rows_html:
                        parts.append(
                            '<table class="ox-table"><tbody>%s</tbody></table>'
                            % "".join(rows_html)
                        )

                if total_chars > _MAX_TEXT_CHARS:
                    parts.append('<p class="ox-truncated">…… 内容过长，已截断显示 ……</p>')
                    break

            if not parts:
                return False, "文档没有可显示的正文内容"

            return True, '<div class="ox-doc">%s</div>' % "".join(parts)

    except zipfile.BadZipFile:
        return False, "文件不是有效的 docx（zip 结构损坏）"
    except Exception as exc:  # noqa: BLE001
        return False, "解析 docx 失败：%s" % exc


# ---- pptx -----------------------------------------------------------------

def pptx_to_html(src_path: str) -> Tuple[bool, str]:
    """把 .pptx 解析成 HTML 片段（按页展示文字）。"""
    try:
        with zipfile.ZipFile(src_path) as zf:
            # 幻灯片按 slideN.xml 的数字顺序排列，避免 slide10 排在 slide2 前面
            names = []
            for name in zf.namelist():
                if name.startswith("ppt/slides/slide") and name.endswith(".xml"):
                    stem = os.path.basename(name)[len("slide"):-len(".xml")]
                    if stem.isdigit():
                        names.append((int(stem), name))
            names.sort()

            if not names:
                return False, "演示文稿中没有找到幻灯片"

            slides_html: List[str] = []
            total_chars = 0

            for index, (number, name) in enumerate(names, start=1):
                root = _read_xml_part(zf, name)
                if root is None:
                    continue

                # 每个形状（sp）里的文字作为一段
                blocks: List[str] = []
                for sp in _iter_by_local(root, "sp"):
                    text = _collect_text(sp)
                    text = text.strip()
                    if text:
                        blocks.append(html.escape(text).replace("\n", "<br>"))
                        total_chars += len(text)

                body = "".join('<div class="ox-pptx-block">%s</div>' % b for b in blocks) \
                    or '<div class="ox-pptx-empty">（本页无文字内容，可能全部是图片）</div>'

                slides_html.append(
                    '<section class="ox-slide">'
                    '<div class="ox-slide-head">第 %d 页</div>'
                    '<div class="ox-slide-body">%s</div>'
                    "</section>" % (index, body)
                )

                if total_chars > _MAX_TEXT_CHARS:
                    slides_html.append('<p class="ox-truncated">…… 内容过长，已截断显示 ……</p>')
                    break

            if not slides_html:
                return False, "演示文稿没有可显示的文字内容"

            return True, '<div class="ox-pptx">%s</div>' % "".join(slides_html)

    except zipfile.BadZipFile:
        return False, "文件不是有效的 pptx（zip 结构损坏）"
    except Exception as exc:  # noqa: BLE001
        return False, "解析 pptx 失败：%s" % exc


# ---- xlsx -----------------------------------------------------------------

def _column_index(cell_ref: str) -> int:
    """把 "B7" 这样的单元格引用转成 0 基列号（B -> 1）。"""
    index = 0
    for ch in cell_ref:
        if ch.isalpha():
            index = index * 26 + (ord(ch.upper()) - ord("A") + 1)
        else:
            break
    return max(0, index - 1)


def _xlsx_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    """读取共享字符串表。"""
    root = _read_xml_part(zf, "xl/sharedStrings.xml")
    if root is None:
        return []
    strings: List[str] = []
    for si in root:
        if _local(si.tag) != "si":
            continue
        strings.append(_collect_text(si))
    return strings


def _xlsx_sheets(zf: zipfile.ZipFile) -> List[Tuple[str, str]]:
    """
    返回 [(工作表名, zip 内路径), ...]，按工作簿中定义的顺序。
    解析失败时退回「列出 xl/worksheets 下所有 xml」。
    """
    result: List[Tuple[str, str]] = []

    workbook = _read_xml_part(zf, "xl/workbook.xml")
    rels = _read_xml_part(zf, "xl/_rels/workbook.xml.rels")

    # r:id -> target
    rel_map: Dict[str, str] = {}
    for rel in _iter_by_local(rels, "Relationship"):
        rid = rel.attrib.get("Id") or rel.attrib.get("id")
        target = rel.attrib.get("Target") or rel.attrib.get("target")
        if rid and target:
            target = target.lstrip("/")
            if not target.startswith("xl/"):
                target = "xl/" + target
            rel_map[rid] = target

    if workbook is not None:
        for sheet in _iter_by_local(workbook, "sheet"):
            name = sheet.attrib.get("name") or "Sheet"
            rid = None
            for attr_name, attr_value in sheet.attrib.items():
                if _local(attr_name) == "id":
                    rid = attr_value
                    break
            path = rel_map.get(rid) if rid else None
            if path and path in zf.namelist():
                result.append((name, path))

    if not result:
        # 兜底：直接按文件名顺序使用
        for name in sorted(zf.namelist()):
            if name.startswith("xl/worksheets/") and name.endswith(".xml"):
                result.append((os.path.basename(name), name))

    return result


def _xlsx_sheet_html(zf: zipfile.ZipFile, part_path: str, shared: List[str]) -> Tuple[bool, str]:
    """把单个工作表渲染成 HTML 表格。"""
    root = _read_xml_part(zf, part_path)
    if root is None:
        return False, ""

    sheet_data = None
    for node in root.iter():
        if _local(node.tag) == "sheetData":
            sheet_data = node
            break
    if sheet_data is None:
        return False, ""

    rows_html: List[str] = []
    truncated = False

    for row_index, row in enumerate(sheet_data):
        if _local(row.tag) != "row":
            continue
        if row_index >= _MAX_XLSX_ROWS:
            truncated = True
            break

        # 按列号摆放单元格，中间空缺补空单元格，保证列对齐
        cells: Dict[int, str] = {}
        max_col = -1
        for cell in row:
            if _local(cell.tag) != "c":
                continue
            ref = cell.attrib.get("r") or ""
            col = _column_index(ref) if ref else (max_col + 1)
            if col >= _MAX_XLSX_COLS:
                continue

            cell_type = cell.attrib.get("t") or ""
            value = ""

            if cell_type == "s":
                # 共享字符串
                for v in cell:
                    if _local(v.tag) == "v" and v.text is not None:
                        try:
                            value = shared[int(v.text)]
                        except (ValueError, IndexError):
                            value = ""
                        break
            elif cell_type == "inlineStr":
                value = _collect_text(cell)
            else:
                for v in cell:
                    if _local(v.tag) == "v" and v.text is not None:
                        value = v.text
                        break
                if not value:
                    value = _collect_text(cell)

            cells[col] = value
            max_col = max(max_col, col)

        if max_col < 0:
            rows_html.append("<tr></tr>")
            continue

        tds = "".join(
            "<td>%s</td>" % html.escape(cells.get(c, ""))
            for c in range(max_col + 1)
        )
        rows_html.append("<tr>%s</tr>" % tds)

    if not rows_html:
        return False, ""

    table = '<table class="ox-table ox-sheet"><tbody>%s</tbody></table>' % "".join(rows_html)
    if truncated:
        table += '<p class="ox-truncated">…… 行数过多，仅显示前 %d 行 ……</p>' % _MAX_XLSX_ROWS
    return True, table


def xlsx_to_html(src_path: str) -> Tuple[bool, str]:
    """把 .xlsx 解析成 HTML 片段（每个工作表一张表）。"""
    try:
        with zipfile.ZipFile(src_path) as zf:
            shared = _xlsx_shared_strings(zf)
            sheets = _xlsx_sheets(zf)
            if not sheets:
                return False, "工作簿中没有找到工作表"

            sections: List[str] = []
            for name, part_path in sheets:
                ok, table = _xlsx_sheet_html(zf, part_path, shared)
                if ok and table:
                    sections.append(
                        '<section class="ox-sheet-wrap">'
                        '<div class="ox-sheet-title">%s</div>%s</section>'
                        % (html.escape(name), table)
                    )

            if not sections:
                return False, "工作簿为空或没有可显示的数据"

            return True, '<div class="ox-xlsx">%s</div>' % "".join(sections)

    except zipfile.BadZipFile:
        return False, "文件不是有效的 xlsx（zip 结构损坏）"
    except Exception as exc:  # noqa: BLE001
        return False, "解析 xlsx 失败：%s" % exc


def ooxml_to_html(src_path: str) -> Tuple[bool, str]:
    """按扩展名分发到具体的 OOXML 解析器。"""
    ext = os.path.splitext(src_path)[1].lower()
    if ext in (".docx", ".docm", ".dotx"):
        return docx_to_html(src_path)
    if ext in (".xlsx", ".xlsm", ".xltx"):
        return xlsx_to_html(src_path)
    if ext in (".pptx", ".pptm", ".potx"):
        return pptx_to_html(src_path)
    return False, "不支持的文件类型"


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------

def build_preview(src_path: str, office_cfg: Dict[str, Any]) -> Dict[str, Any]:
    """
    生成 Office 文档预览信息。

    返回字典：
        {"mode": "pdf",         "converter": "libreoffice", "message": "..."}
        {"mode": "html",        "html": "...", "converter": "python-ooxml", "message": "..."}
        {"mode": "unsupported", "message": "..."}   —— 前端据此给出友好提示
    """
    ext = os.path.splitext(src_path)[1].lower()
    filename = os.path.basename(src_path)

    if not office_cfg.get("enabled", True):
        return {
            "mode": "unsupported",
            "message": "服务端已关闭 Office 预览功能（config.json 中 office.enabled=false）。",
        }

    # ---------- 路线一：LibreOffice 转 PDF ----------
    soffice = find_soffice(office_cfg.get("soffice_path") or "")
    convert_error = ""

    if soffice:
        try:
            timeout = int(office_cfg.get("timeout_seconds") or 120)
        except (TypeError, ValueError):
            timeout = 120
        try:
            max_cache_mb = int(office_cfg.get("max_cache_mb") or 1024)
        except (TypeError, ValueError):
            max_cache_mb = 1024

        try:
            ok, result = convert_to_pdf(
                src_path,
                office_cfg.get("cache_dir") or tempfile.gettempdir(),
                soffice,
                timeout,
                max_cache_mb,
            )
        except Exception as exc:  # noqa: BLE001
            # 兜底：转换环节的任何意外（磁盘满、权限不足、路径过长、配置写坏…）
            # 都只当作「这条路走不通」，必须继续往下走纯 Python 解析的降级分支。
            # 否则一个本来能正常预览的 docx，会因为磁盘满而彻底打不开。
            ok, result = False, "LibreOffice 转换过程出错：%s" % exc

        if ok:
            return {
                "mode": "pdf",
                "converter": "libreoffice",
                "message": "已由 LibreOffice 转换为 PDF 预览",
                "pdf_path": result,
            }
        convert_error = result

    # ---------- 路线二：纯 Python 解析 OOXML ----------
    if ext in OOXML_EXTENSIONS:
        ok, result = ooxml_to_html(src_path)
        if ok:
            note = "已使用内置解析器提取内容预览（版式与原文可能有差异）"
            if convert_error:
                note += "；LibreOffice 转换失败：" + convert_error
            return {
                "mode": "html",
                "converter": "python-ooxml",
                "html": result,
                "message": note,
            }

        message = result or "解析失败"
        if convert_error:
            message = "%s；LibreOffice 转换也失败：%s" % (message, convert_error)
        return {"mode": "unsupported", "message": message}

    # ---------- 老的二进制格式：只能靠 LibreOffice ----------
    if ext in LEGACY_BINARY_EXTENSIONS:
        if not soffice:
            return {
                "mode": "unsupported",
                "message": (
                    "「%s」是旧版 Office 二进制格式，需要本机安装 LibreOffice 才能预览。\n"
                    "安装地址：https://www.libreoffice.org/download/download-libreoffice/\n"
                    "安装后重启本服务即可自动识别；也可以直接在 config.json 的 "
                    "office.soffice_path 中指定 soffice.exe 的完整路径。"
                ) % filename,
            }
        return {
            "mode": "unsupported",
            "message": "LibreOffice 转换失败：%s\n可以先下载到本地用 Office 打开。" % (convert_error or "未知原因"),
        }

    return {
        "mode": "unsupported",
        "message": "暂不支持预览该类型的文档，请下载后查看。",
    }


def clear_cache(cache_dir: str) -> int:
    """
    清空 Office 转换缓存，返回删除的文件数。

    注意：以前这里会刻意跳过 office-* 临时目录和 .tmp 文件（注释给的理由是
    「它们会自行清理」），但转换超时被杀掉之后它们恰恰不会自行清理 ——
    结果这两类垃圾永远不会被回收。现在一并清理：文件全删，
    空目录（含遗留的 office-* 工作目录）也删掉。
    """
    removed = 0
    if not os.path.isdir(cache_dir):
        return 0

    root = os.path.normcase(os.path.normpath(cache_dir))
    for dirpath, _dirnames, filenames in os.walk(cache_dir, topdown=False):
        for name in filenames:
            try:
                os.unlink(os.path.join(dirpath, name))
                removed += 1
            except OSError:
                continue
        # 清掉空目录，但不要删掉缓存根目录本身
        if os.path.normcase(os.path.normpath(dirpath)) != root:
            try:
                os.rmdir(dirpath)
            except OSError:
                pass
    return removed

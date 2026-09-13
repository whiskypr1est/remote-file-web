# -*- coding: utf-8 -*-
"""
下载前端离线资源（winbox.js / pdf.js / xterm.js / CodeMirror）到 static/vendor/ 目录。

为什么需要这个脚本：
    目标部署环境是局域网，服务器/客户端有可能完全没有外网。
    因此前端不引用任何 CDN，所有第三方 JS/CSS 都放在 static/vendor/ 里由本服务自己托管。
    本脚本只在「构建阶段」联网跑一次；部署时把整个 static/vendor 目录拷过去即可。

用法：
    python tools/fetch_vendor.py            # 下载缺失的资源
    python tools/fetch_vendor.py --force    # 强制重新下载

只依赖 Python 标准库，不需要 pip 安装任何东西。
"""

import argparse
import json
import os
import sys
import urllib.request

# 统一用 UTF-8 输出，避免中文在 GBK 控制台/管道里变成乱码
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except Exception:  # noqa: BLE001
    pass

# ---------- 基础路径 ----------
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VENDOR_DIR = os.path.join(BASE_DIR, "static", "vendor")
PDF_DIR = os.path.join(VENDOR_DIR, "pdf")

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DSFileWeb/1.0"}

# 校验时在文件里搜特征串的默认窗口（字节）。见 verify() 的说明。
SCAN_BYTES = 200000

# winbox.js 固定版本：窗口系统核心库
WINBOX_VERSION = "0.2.82"
CDN_LIST = [
    "https://cdn.jsdelivr.net/npm/{pkg}@{ver}/{path}",
    "https://unpkg.com/{pkg}@{ver}/{path}",
]

# xterm.js：命令行窗口的终端渲染。
# 真 TTY（ConPTY）的输出带 ANSI 转义序列，必须用终端模拟器渲染才不是乱码；
# 它同时**自带 IME（中文输入法）支持**，因此没有做其它选型。
XTERM_PKG = "@xterm/xterm"
XTERM_FIT_PKG = "@xterm/addon-fit"
XTERM_DIR = os.path.join(VENDOR_DIR, "xterm")

# CodeMirror 5：编辑器窗口的语法高亮。
#
# ★ 为什么锁 CodeMirror 5 而**不是** CodeMirror 6：
#   CM6 是一组 @codemirror/* 的纯 ESM 包，必须经 rollup/vite 打包才能跑；
#   本项目的前端是「无构建步骤」的裸 ES 模块（static/js/ 直接由浏览器加载），
#   引入 CM6 就等于要求所有人先装一遍 Node 工具链，违背离线部署的初衷。
#   CM5 则是 UMD + 每个语言一个 mode/*.js，和已经这么用了的 xterm.js 完全同构，
#   直接 <script> 引进来就能用。
#
# ★ 版本写死，不用 latest：CM5 已经进入维护模式（npm 上的 latest 是 6.x），
#   必须钉住 5.65.x 这一条线，否则某天跑一次脚本就把编辑器换成打包不了的 CM6。
CODEMIRROR_PKG = "codemirror"
CODEMIRROR_VERSION = "5.65.21"
CODEMIRROR_DIR = os.path.join(VENDOR_DIR, "codemirror")

# 编辑器的配色主题。选浅色是因为桌面整体是 Windows 风格的浅色界面，
# 深色主题（darcula / material-darker 之类）在窗口里像贴了一块黑斑。
CODEMIRROR_THEME = "eclipse"

# 需要的 mode。每个 mode 记三项：
#   path   —— 包内路径
#   deps   —— 必须先加载的其它 mode（CM5 的 mode 之间靠全局 CodeMirror 传递，
#             依赖没先加载时**不会报错**，只是高亮静默失效，所以这里必须显式列出）
#   marker —— 文件内容特征，用来确认拿到的确实是这个 mode（防止 CDN 返回错误页）
#
# ★ 顺序即加载顺序，deps 一定排在依赖者前面。
# ★ 特别注意 htmlmixed 必须排在 xml 之后：两者都会 defineMIME("text/html")，
#   后加载的胜出；而 markdown.js 在**定义时**就会 CodeMirror.getMode(cmCfg,
#   "text/html") 抓一份 html 模式，若被 xml 抢走，markdown 里的 HTML 块就废了。
#   （meta.js 只是「按文件名猜 mode」的辅助表，不是语法本身，见 markdown 的 deps。）
CODEMIRROR_MODES = [
    {"name": "properties", "path": "mode/properties/properties.js", "deps": [], "marker": 'defineMode("properties"'},
    {"name": "shell", "path": "mode/shell/shell.js", "deps": [], "marker": "defineMode('shell'"},
    {"name": "powershell", "path": "mode/powershell/powershell.js", "deps": [], "marker": "defineMode('powershell'"},
    {"name": "python", "path": "mode/python/python.js", "deps": [], "marker": 'defineMode("python"'},
    {"name": "javascript", "path": "mode/javascript/javascript.js", "deps": [], "marker": 'defineMode("javascript"'},
    {"name": "clike", "path": "mode/clike/clike.js", "deps": [], "marker": 'defineMode("clike"'},
    {"name": "css", "path": "mode/css/css.js", "deps": [], "marker": 'defineMode("css"'},
    {"name": "xml", "path": "mode/xml/xml.js", "deps": [], "marker": 'defineMode("xml"'},
    {"name": "yaml", "path": "mode/yaml/yaml.js", "deps": [], "marker": 'defineMode("yaml"'},
    {"name": "sql", "path": "mode/sql/sql.js", "deps": [], "marker": 'defineMode("sql"'},
    {"name": "meta", "path": "mode/meta.js", "deps": [], "marker": "CodeMirror.modeInfo"},
    {"name": "markdown", "path": "mode/markdown/markdown.js", "deps": ["xml", "meta"], "marker": 'defineMode("markdown"'},
    {"name": "htmlmixed", "path": "mode/htmlmixed/htmlmixed.js", "deps": ["xml", "javascript", "css"], "marker": 'defineMode("htmlmixed"'},
]


def http_get(url, timeout=40):
    """带 UA 的 GET，返回 bytes；失败抛异常。"""
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_first(urls):
    """依次尝试多个 CDN 地址，返回第一个成功的 bytes。"""
    last_err = None
    for url in urls:
        try:
            return http_get(url)
        except Exception as exc:  # noqa: BLE001 - 逐个镜像容错
            last_err = exc
    raise RuntimeError("全部镜像均失败: %s" % last_err)


def save_file(path, data, quiet=False):
    """写入文件并打印结果。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        fh.write(data)
    if not quiet:
        print("  [OK] %-46s %8d bytes" % (os.path.relpath(path, BASE_DIR), len(data)))


def download_pkg_file(pkg, ver, rel_path, dest, quiet=False):
    """从 CDN 下载 npm 包内的某个文件。"""
    urls = [t.format(pkg=pkg, ver=ver, path=rel_path) for t in CDN_LIST]
    data = fetch_first(urls)
    save_file(dest, data, quiet=quiet)
    return len(data)


def list_package_files(pkg, ver):
    """通过 jsDelivr 数据 API 列出包内所有文件路径（用于自适应不同 pdf.js 版本的文件名）。"""
    url = "https://data.jsdelivr.com/v1/packages/npm/%s@%s" % (pkg, ver)
    raw = http_get(url, timeout=40)
    tree = json.loads(raw.decode("utf-8"))

    out = []

    def walk(node, prefix=""):
        for item in node.get("files", []) or []:
            name = item.get("name", "")
            cur = prefix + "/" + name if prefix else name
            if item.get("type") == "directory":
                walk(item, cur)
            else:
                out.append(cur)

    walk(tree)
    return out


def latest_version(pkg):
    """查询 npm 上的最新版本号。"""
    raw = http_get("https://registry.npmjs.org/%s" % pkg, timeout=40)
    meta = json.loads(raw.decode("utf-8"))
    return meta["dist-tags"]["latest"]


def pick(candidates, available):
    """在候选文件名里挑出实际存在的那个。"""
    for name in candidates:
        if name in available:
            return name
    return None


def do_winbox(force):
    print("[1/4] winbox.js v%s" % WINBOX_VERSION)
    targets = [
        ("dist/js/winbox.min.js", os.path.join(VENDOR_DIR, "winbox.min.js")),
        ("dist/css/winbox.min.css", os.path.join(VENDOR_DIR, "winbox.min.css")),
    ]
    for rel, dest in targets:
        if os.path.exists(dest) and not force:
            print("  [--] 已存在，跳过: %s" % os.path.relpath(dest, BASE_DIR))
            continue
        download_pkg_file("winbox", WINBOX_VERSION, rel, dest)


def do_pdfjs(force):
    print("[2/4] pdf.js (pdfjs-dist)")
    try:
        ver = latest_version("pdfjs-dist")
    except Exception as exc:  # noqa: BLE001
        ver = "4.10.38"
        print("  [!] 查询最新版本失败(%s)，回退到 %s" % (exc, ver))

    print("  版本: %s" % ver)

    try:
        available = set(list_package_files("pdfjs-dist", ver))
    except Exception as exc:  # noqa: BLE001
        available = set()
        print("  [!] 文件清单获取失败(%s)，改用默认路径猜测" % exc)

    # pdf.js 从 v4 起主文件是 .mjs；同时兼容老的 .js 命名
    main_rel = pick(
        ["build/pdf.min.mjs", "build/pdf.min.js"],
        available,
    ) or "build/pdf.min.mjs"
    worker_rel = pick(
        ["build/pdf.worker.min.mjs", "build/pdf.worker.min.js"],
        available,
    ) or "build/pdf.worker.min.mjs"

    plan = [
        (main_rel, os.path.join(PDF_DIR, "pdf.min.mjs")),
        (worker_rel, os.path.join(PDF_DIR, "pdf.worker.min.mjs")),
    ]
    for rel, dest in plan:
        if os.path.exists(dest) and not force:
            print("  [--] 已存在，跳过: %s" % os.path.relpath(dest, BASE_DIR))
            continue
        download_pkg_file("pdfjs-dist", ver, rel, dest)

    # CMaps：中文/日文/韩文 PDF 若是 CID 字体且未内嵌字体，必须靠 cmaps 才能正常显示
    cmap_files = sorted(p for p in available if p.startswith("cmaps/") and p.endswith(".bcmap"))
    if cmap_files and not (os.path.isdir(os.path.join(PDF_DIR, "cmaps")) and not force):
        print("  下载 cmaps（中文 PDF 必需，共 %d 个）..." % len(cmap_files))
        ok = 0
        for rel in cmap_files:
            dest = os.path.join(PDF_DIR, rel.replace("/", os.sep))
            if os.path.exists(dest) and not force:
                ok += 1
                continue
            try:
                download_pkg_file("pdfjs-dist", ver, rel, dest, quiet=True)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                print("    [!] %s 失败: %s" % (rel, exc))
        print("  [OK] cmaps 完成 %d/%d" % (ok, len(cmap_files)))
    else:
        print("  [--] cmaps 已存在或不可用，跳过")

    # 标准字体：部分 PDF 依赖，缺失时 pdf.js 会用系统字体兜底，故失败也不影响可用性
    font_files = sorted(p for p in available if p.startswith("standard_fonts/") and p.endswith(".ttf"))
    if font_files and not (os.path.isdir(os.path.join(PDF_DIR, "standard_fonts")) and not force):
        print("  下载 standard_fonts（共 %d 个）..." % len(font_files))
        ok = 0
        for rel in font_files:
            dest = os.path.join(PDF_DIR, rel.replace("/", os.sep))
            if os.path.exists(dest) and not force:
                ok += 1
                continue
            try:
                download_pkg_file("pdfjs-dist", ver, rel, dest, quiet=True)
                ok += 1
            except Exception as exc:  # noqa: BLE001
                print("    [!] %s 失败: %s" % (rel, exc))
        print("  [OK] standard_fonts 完成 %d/%d" % (ok, len(font_files)))

    return ver


def do_xterm(force):
    """
    下载 xterm.js（终端渲染 + IME）及其 fit 插件。

    版本不写死：这两个都是纯前端库，取 npm 上的 latest 即可，
    版本号会写进 VERSIONS.json，供「关于」对话框展示。
    """
    print("[3/4] xterm.js（命令行终端渲染）")
    try:
        xterm_ver = latest_version(XTERM_PKG)
        fit_ver = latest_version(XTERM_FIT_PKG)
    except Exception as exc:  # noqa: BLE001
        xterm_ver, fit_ver = "6.0.0", "0.11.0"
        print("  [!] 查询最新版本失败(%s)，回退到 xterm %s / addon-fit %s"
              % (exc, xterm_ver, fit_ver))

    print("  xterm %s / addon-fit %s" % (xterm_ver, fit_ver))
    os.makedirs(XTERM_DIR, exist_ok=True)

    plan = [
        (XTERM_PKG, xterm_ver, "lib/xterm.js", "xterm.js"),
        (XTERM_PKG, xterm_ver, "css/xterm.css", "xterm.css"),
        (XTERM_FIT_PKG, fit_ver, "lib/addon-fit.js", "addon-fit.js"),
    ]
    for pkg, ver, rel, name in plan:
        dest = os.path.join(XTERM_DIR, name)
        if os.path.exists(dest) and not force:
            print("  [--] 已存在，跳过: %s" % os.path.relpath(dest, BASE_DIR))
            continue
        download_pkg_file(pkg, ver, rel, dest)

    return xterm_ver, fit_ver


def do_codemirror(force):
    """
    下载 CodeMirror 5（编辑器窗口的语法高亮）+ 需要的语言 mode。

    版本写死在 CODEMIRROR_VERSION，理由见该常量处的说明（CM6 需要打包器）。

    mode 之间是有依赖的：htmlmixed 依赖 xml / javascript / css，
    markdown 依赖 xml / meta。依赖没到位时 CM5 **不会抛错**，
    只会安静地不高亮 —— 这种故障在浏览器里极难排查，所以这里
    按 CODEMIRROR_MODES 的声明顺序下载（依赖已保证排在前面）。
    """
    print("[4/4] CodeMirror %s（编辑器语法高亮）" % CODEMIRROR_VERSION)

    os.makedirs(CODEMIRROR_DIR, exist_ok=True)

    plan = [
        ("lib/codemirror.js", os.path.join(CODEMIRROR_DIR, "lib", "codemirror.js")),
        ("lib/codemirror.css", os.path.join(CODEMIRROR_DIR, "lib", "codemirror.css")),
        ("theme/%s.css" % CODEMIRROR_THEME,
         os.path.join(CODEMIRROR_DIR, "theme", "%s.css" % CODEMIRROR_THEME)),
    ]
    for mode in CODEMIRROR_MODES:
        plan.append((
            mode["path"],
            os.path.join(CODEMIRROR_DIR, mode["path"].replace("/", os.sep)),
        ))

    for rel, dest in plan:
        if os.path.exists(dest) and not force:
            print("  [--] 已存在，跳过: %s" % os.path.relpath(dest, BASE_DIR))
            continue
        download_pkg_file(CODEMIRROR_PKG, CODEMIRROR_VERSION, rel, dest)

    return CODEMIRROR_VERSION


def _codemirror_checks():
    """
    生成 CodeMirror 的校验项，供 verify() 使用。

    这里刻意从上面那份 CODEMIRROR_MODES 反推，而不是再手抄一遍文件名：
    「改了下载清单却忘了改校验清单」正是让半截文件蒙混过关的典型原因。
    """
    checks = [
        # codemirror.js 的版本号落在文件尾部（400KB 处），校验窗口要放大，
        # 顺带把「钉住的版本号确实是 5.65.x」也一起验掉
        (os.path.join(CODEMIRROR_DIR, "lib", "codemirror.js"),
         ('CodeMirror.version = "%s"' % CODEMIRROR_VERSION).encode("utf-8"),
         200000, 600000),
        (os.path.join(CODEMIRROR_DIR, "lib", "codemirror.css"), b"CodeMirror", 3000),
        (os.path.join(CODEMIRROR_DIR, "theme", "%s.css" % CODEMIRROR_THEME),
         CODEMIRROR_THEME.encode("ascii"), 500),
    ]
    for mode in CODEMIRROR_MODES:
        checks.append((
            os.path.join(CODEMIRROR_DIR, mode["path"].replace("/", os.sep)),
            mode["marker"].encode("utf-8"),
            400,
        ))
    return checks


def verify():
    """
    下载后做一次基本校验，避免拿到半截文件还以为成功了。

    特征串扫描的窗口是 200KB —— 大多数资源远小于这个数，一次读进来足够。
    唯一例外是 codemirror.js：它 400KB 出头，而版本号那句
    （CodeMirror.version = "..."）恰好落在 200KB 之后，用默认窗口扫不到，
    会误报一次「内容特征未匹配」。所以给校验项留了可选的第四个元素，
    让这类大文件单独把窗口放大。
    """
    print("\n=== 校验 ===")
    checks = [
        (os.path.join(VENDOR_DIR, "winbox.min.js"), b"WinBox", 5000),
        (os.path.join(VENDOR_DIR, "winbox.min.css"), b"winbox", 2000),
        (os.path.join(PDF_DIR, "pdf.min.mjs"), b"pdfjs", 100000),
        (os.path.join(PDF_DIR, "pdf.worker.min.mjs"), b"pdf", 100000),
        (os.path.join(XTERM_DIR, "xterm.js"), b"Terminal", 100000),
        (os.path.join(XTERM_DIR, "xterm.css"), b"xterm", 2000),
        (os.path.join(XTERM_DIR, "addon-fit.js"), b"FitAddon", 500),
    ]
    checks.extend(_codemirror_checks())
    all_ok = True
    for check in checks:
        path, needle, min_size = check[0], check[1], check[2]
        scan = check[3] if len(check) > 3 else SCAN_BYTES
        rel = os.path.relpath(path, BASE_DIR)
        if not os.path.exists(path):
            print("  [FAIL] %s 不存在" % rel)
            all_ok = False
            continue
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(4096)
        if size < min_size:
            print("  [FAIL] %s 体积异常(%d)" % (rel, size))
            all_ok = False
        elif needle not in head and needle not in open(path, "rb").read(scan):
            print("  [WARN] %s 内容特征未匹配" % rel)
        else:
            print("  [OK] %s (%d bytes)" % (rel, size))
    return all_ok


def main():
    parser = argparse.ArgumentParser(description="下载前端离线资源")
    parser.add_argument("--force", action="store_true", help="强制重新下载")
    args = parser.parse_args()

    print("=" * 62)
    print("下载前端离线资源到: %s" % VENDOR_DIR)
    print("=" * 62)

    pdf_ver = None
    xterm_ver = None
    fit_ver = None
    codemirror_ver = None
    try:
        do_winbox(args.force)
        pdf_ver = do_pdfjs(args.force)
        xterm_ver, fit_ver = do_xterm(args.force)
        codemirror_ver = do_codemirror(args.force)
    except Exception as exc:  # noqa: BLE001
        print("\n[错误] 下载失败: %s" % exc)
        print("提示：如果本机无法联网，可以直接从别的机器拷贝整个 static/vendor 目录过来。")
        return 1

    # 记录版本号，供「关于」对话框展示
    manifest = {
        "winbox": WINBOX_VERSION,
        "pdfjs": pdf_ver,
        "xterm": xterm_ver,
        "xterm-addon-fit": fit_ver,
        "codemirror": codemirror_ver,
    }
    with open(os.path.join(VENDOR_DIR, "VERSIONS.json"), "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    ok = verify()
    print("\n完成。" if ok else "\n部分资源校验未通过，请重试。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

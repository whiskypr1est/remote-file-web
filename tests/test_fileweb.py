# -*- coding: utf-8 -*-
"""
回归测试
========

全部用标准库 unittest 编写，不引入 pytest / httpx 等额外依赖，
直接用项目自带的虚拟环境运行：

    .venv\\Scripts\\python.exe -m unittest discover -s tests -t .

覆盖的都是「曾经真实出过问题」的点，避免以后改动时又退回去：

    * 路径穿越防护（含能骗过字符串层检查的 ``....//`` 写法）
    * X-Forwarded-For 伪造不能再绕过登录失败锁定
    * 根目录本身不能被删除 / 打包
    * 缩略图：max_cache_mb 必须真的生效、像素上限必须真的拦得住、
      陈旧的 .fail 不能屏蔽已经生成好的缩略图
    * Office：LibreOffice 转换抛异常时必须仍能走纯 Python 降级预览
    * Office：含中文的缓存路径要做完整 URL 转义、版本号按数值排序
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import zipfile
from types import SimpleNamespace

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from fileweb import deps, fsops, office, security, thumbs              # noqa: E402
from fileweb.routers import fs as fs_router                            # noqa: E402


TEST_USERNAME = "tester"
TEST_PASSWORD = "test-password-123"


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------

def _free_port() -> int:
    """取一个当前空闲的本地端口。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class _Client:
    """极简 HTTP 客户端：只用标准库，够测试用。"""

    def __init__(self, port: int):
        self.base = "http://127.0.0.1:%d" % port
        self.cookie = ""
        self.csrf = ""

    def request(self, method, path, body=None, headers=None, with_csrf=True):
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"
        if self.cookie:
            hdrs["Cookie"] = self.cookie
        if with_csrf and method != "GET" and self.csrf:
            hdrs["X-CSRF-Token"] = self.csrf

        req = urllib.request.Request(self.base + path, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                self._capture_cookie(resp.headers)
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self._capture_cookie(exc.headers)
            return exc.code, exc.read().decode("utf-8", "replace")

    def _capture_cookie(self, headers) -> None:
        raw = headers.get("Set-Cookie") if headers else None
        if not raw:
            return
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if chunk.startswith(deps.SESSION_COOKIE + "="):
                self.cookie = chunk
                return


# ---------------------------------------------------------------------------
# 路径与文件名安全
# ---------------------------------------------------------------------------

class PathSecurityTests(unittest.TestCase):

    def test_rejects_traversal_and_absolute_paths(self):
        for probe in ["../windows", "..\\..\\windows", "a/../../b",
                      "C:/Windows", "C:\\Windows", "\\\\server\\share", "a\x00b"]:
            with self.assertRaises(security.PathSecurityError, msg=probe):
                security.split_rel_path(probe)

    def test_allows_plain_relative_paths(self):
        self.assertEqual(security.split_rel_path("ok/sub/dir"), ["ok", "sub", "dir"])
        self.assertEqual(security.split_rel_path(""), [])
        self.assertEqual(security.split_rel_path("."), [])

    def test_second_layer_catches_dot_trick(self):
        """
        ``....//Windows`` 不含 ``..`` 段，能通过字符串层检查，
        必须由 realpath 之后的「根目录前缀」比对拦住。
        Windows 会把全点号的路径段当成上跳，因此这里应当越界。
        """
        if os.name != "nt":
            self.skipTest("该行为依赖 Windows 的路径归一化规则")

        # 注意：不能用 os.path.abspath(os.sep)，那取的是「当前盘」的根
        # （项目不在 C: 盘时会取到那个盘的根，路径就不对了）。
        # 这里需要一个确定存在、且上跳之后确实会跑到盘根之外的目录。
        system_drive = os.environ.get("SystemDrive") or "C:"
        probe_root = os.path.join(system_drive + os.sep, "Users")
        if not os.path.isdir(probe_root):
            probe_root = os.path.join(system_drive + os.sep, "Program Files")
        if not os.path.isdir(probe_root):
            self.skipTest("找不到可用的起点目录，跳过")

        with self.assertRaises(security.PathSecurityError):
            security.join_within_root(probe_root, "....//Windows")

    def test_sanitize_filename(self):
        self.assertEqual(security.sanitize_filename("a/b/c.txt"), "c.txt")
        self.assertEqual(security.sanitize_filename("trailing. "), "trailing")
        self.assertEqual(security.sanitize_filename("inva|li*d?.txt"), "inva_li_d_.txt")
        for bad in ["CON", "con.txt", "..", ""]:
            with self.assertRaises(security.PathSecurityError, msg=bad):
                security.sanitize_filename(bad)

    def test_blocked_extension_includes_suffix_chain(self):
        blocked = [".exe", ".bat", ".ps1"]
        self.assertEqual(security.is_blocked_extension("evil.bat.exe", blocked), ".exe")
        self.assertEqual(security.is_blocked_extension("x.EXE", blocked), ".exe")
        self.assertIsNone(security.is_blocked_extension("readme.md", blocked))

    def test_is_within_does_not_trust_prefix_lookalike(self):
        base = tempfile.mkdtemp(prefix="fw-within-")
        try:
            share = os.path.join(base, "Share")
            share2 = os.path.join(base, "Share2")
            os.makedirs(share, exist_ok=True)
            os.makedirs(share2, exist_ok=True)
            self.assertTrue(security.is_within(share, share))
            self.assertFalse(security.is_within(share, share2),
                             "D:\\Share2 不能被误判为在 D:\\Share 之内")
        finally:
            shutil.rmtree(base, ignore_errors=True)


# ---------------------------------------------------------------------------
# 客户端 IP / X-Forwarded-For 信任策略
# ---------------------------------------------------------------------------

def _make_request(headers=None, client=("203.0.113.7", 5000), trusted_proxies=None):
    cfg = {"auth": {"trusted_proxies": trusted_proxies or []}}
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/auth/login",
        "headers": [(k.lower().encode("latin-1"), v.encode("latin-1"))
                    for k, v in (headers or {}).items()],
        "client": client,
        "app": SimpleNamespace(state=SimpleNamespace(app_state=SimpleNamespace(cfg=cfg))),
    }
    return scope


class ClientIpTests(unittest.TestCase):

    def test_ignores_forwarded_header_by_default(self):
        from starlette.requests import Request
        req = Request(_make_request(headers={"x-forwarded-for": "10.1.1.1"}))
        self.assertEqual(deps.client_ip(req), "203.0.113.7",
                         "默认不允许采信 X-Forwarded-For，否则可伪造以绕过登录锁定")

    def test_uses_forwarded_header_only_for_trusted_proxy(self):
        from starlette.requests import Request
        req = Request(_make_request(
            headers={"x-forwarded-for": "10.1.1.1, 10.2.2.2"},
            trusted_proxies=["203.0.113.7"],
        ))
        self.assertEqual(deps.client_ip(req), "10.1.1.1")

    def test_cidr_and_untrusted_peer(self):
        from starlette.requests import Request
        req = Request(_make_request(
            headers={"x-forwarded-for": "10.1.1.1"},
            trusted_proxies=["10.0.0.0/8"],
        ))
        self.assertEqual(deps.client_ip(req), "203.0.113.7",
                         "对端不在网段内时依然不能采信该头")

        req2 = Request(_make_request(
            headers={"x-forwarded-for": "10.1.1.1"},
            trusted_proxies=["203.0.113.0/24"],
        ))
        self.assertEqual(deps.client_ip(req2), "10.1.1.1")


# ---------------------------------------------------------------------------
# 根目录防护
# ---------------------------------------------------------------------------

class RootGuardTests(unittest.TestCase):

    def test_is_root_path(self):
        fs_root = os.path.abspath(os.sep)
        self.assertTrue(fs_router._is_root_path(fs_root))
        self.assertFalse(fs_router._is_root_path(os.path.join(fs_root, "some-child")))

    def test_ensure_not_root(self):
        from fastapi import HTTPException
        fs_root = os.path.abspath(os.sep)
        root_cfg = {"name": "scratch", "path": fs_root}

        with self.assertRaises(HTTPException) as ctx:
            fs_router._ensure_not_root(root_cfg, fs_root, "删除")
        self.assertEqual(ctx.exception.status_code, 403)

        # 普通子路径必须放行（不能误伤正常操作）
        child = os.path.join(fs_root, "definitely-not-a-root")
        fs_router._ensure_not_root(root_cfg, child, "删除")


# ---------------------------------------------------------------------------
# 缩略图
# ---------------------------------------------------------------------------

class ThumbTests(unittest.TestCase):

    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="fw-thumb-")

    def tearDown(self):
        shutil.rmtree(self.work, ignore_errors=True)

    def _make_image(self, name, size=(64, 64)):
        from PIL import Image
        path = os.path.join(self.work, name)
        Image.new("RGB", size, (200, 30, 30)).save(path)
        return path

    def test_stale_fail_marker_does_not_shadow_valid_thumbnail(self):
        src = self._make_image("a.png")
        cache = os.path.join(self.work, "cache")

        ok, path = thumbs.ensure_thumb(src, cache, 100, 512)
        self.assertTrue(ok)

        # 人为制造冲突状态：有效缩略图 + 陈旧的失败标记
        stat = os.stat(src)
        key = thumbs.cache_key(src, stat.st_mtime, stat.st_size, 100)
        _hit, fail = thumbs._cache_paths(cache, key)
        os.makedirs(os.path.dirname(fail), exist_ok=True)
        with open(fail, "wb"):
            pass

        ok2, path2 = thumbs.ensure_thumb(src, cache, 100, 512)
        self.assertTrue(ok2, "存在 .fail 时也必须能命中已经生成好的缩略图")
        self.assertTrue(os.path.isfile(path2))
        self.assertFalse(os.path.exists(fail), "命中有效缩略图后应清掉陈旧的失败标记")

    def test_pixel_limit_rejects_before_decoding(self):
        src = self._make_image("big.png", size=(120, 120))
        cache = os.path.join(self.work, "cache2")
        original = thumbs._MAX_PIXELS
        thumbs._MAX_PIXELS = 1000          # 120*120 = 14400，必然超限
        try:
            ok, _ = thumbs.ensure_thumb(src, cache, 100, 512)
            self.assertFalse(ok, "超过像素上限的图片必须拒绝生成缩略图")
        finally:
            thumbs._MAX_PIXELS = original

    def test_pixel_limit_is_not_weaker_than_pillow_default(self):
        from PIL import Image
        self.assertLessEqual(
            thumbs._MAX_PIXELS, Image.MAX_IMAGE_PIXELS,
            "本模块的像素上限不能比 Pillow 自身的默认防护还宽松",
        )

    def test_eviction_receives_configured_max_mb(self):
        src = self._make_image("b.png")
        cache = os.path.join(self.work, "cache3")
        seen = []
        original = thumbs._maybe_evict
        thumbs._maybe_evict = lambda cache_dir, max_mb: seen.append(max_mb)
        try:
            ok, _ = thumbs.ensure_thumb(src, cache, 100, 64)
            self.assertTrue(ok)
        finally:
            thumbs._maybe_evict = original
        self.assertEqual(seen, [64],
                         "thumbs.max_cache_mb 必须被传进淘汰逻辑（此前调用点漏传，是死配置）")

    def test_eviction_shrinks_over_limit_cache(self):
        cache = os.path.join(self.work, "cache4")
        os.makedirs(cache, exist_ok=True)
        blob = b"x" * (1024 * 1024)
        for index in range(18):                      # 18MB
            with open(os.path.join(cache, "f%02d.bin" % index), "wb") as fh:
                fh.write(blob)

        # 绕过节流：把计数器拨到检查点上
        thumbs._generate_counter = thumbs._EVICT_INTERVAL_COUNT - 1
        thumbs._last_evict = 0.0
        thumbs._maybe_evict(cache, 16)               # 下限 16MB → 应清到 12.8MB

        total = sum(os.path.getsize(os.path.join(cache, n)) for n in os.listdir(cache))
        self.assertLess(total, 18 * 1024 * 1024, "超过上限后必须真的删掉一部分文件")
        self.assertLessEqual(total, int(16 * 1024 * 1024 * 0.8) + 1024 * 1024)


# ---------------------------------------------------------------------------
# Office 预览
# ---------------------------------------------------------------------------

def _make_minimal_docx(path: str) -> None:
    """造一个最小可用的 .docx（本质就是 zip + XML）。"""
    document = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        '<w:body><w:p><w:r><w:t>Hello fallback</w:t></w:r></w:p></w:body>'
        '</w:document>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("word/document.xml", document)


class OfficeTests(unittest.TestCase):

    def test_file_url_escapes_non_ascii(self):
        url = office._path_to_file_url("F:\\示例项目目录\\office_cache")
        self.assertTrue(url.startswith("file:///F:/"))
        self.assertTrue(url.isascii(), "URL 必须整体是 ASCII（非 ASCII 需百分号转义）")
        self.assertIn("%", url)

    def test_version_sort_prefers_newer(self):
        paths = [
            r"C:\Program Files\LibreOffice 7.6\program\soffice.exe",
            r"C:\Program Files\LibreOffice 24.8\program\soffice.exe",
        ]
        paths.sort(key=office._version_sort_key, reverse=True)
        self.assertIn("24.8", paths[0], "24.8 必须排在 7.6 之前（反字典序会排错）")

    def test_build_preview_falls_back_when_conversion_raises(self):
        work = tempfile.mkdtemp(prefix="fw-office-")
        try:
            docx = os.path.join(work, "sample.docx")
            _make_minimal_docx(docx)
            cfg = {
                "enabled": True,
                "cache_dir": os.path.join(work, "cache"),
                "soffice_path": "soffice.exe",
                "timeout_seconds": 10,
                "max_cache_mb": 64,
            }

            original_find = office.find_soffice
            original_convert = office.convert_to_pdf

            def _boom(*args, **kwargs):
                raise OSError("模拟磁盘满")

            office.find_soffice = lambda *a, **k: r"C:\fake\soffice.exe"
            office.convert_to_pdf = _boom
            try:
                result = office.build_preview(docx, cfg)
            finally:
                office.find_soffice = original_find
                office.convert_to_pdf = original_convert

            self.assertEqual(
                result.get("mode"), "html",
                "LibreOffice 转换抛异常时必须降级到纯 Python 预览，而不是整体失败",
            )
            self.assertIn("Hello fallback", result.get("html", ""))
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_evict_removes_stale_orphan_dir(self):
        work = tempfile.mkdtemp(prefix="fw-office2-")
        try:
            orphan = os.path.join(work, "office-abc123")
            os.makedirs(orphan, exist_ok=True)
            old = time.time() - 7200
            os.utime(orphan, (old, old))

            office._evict_state["count"] = office._EVICT_INTERVAL_COUNT - 1
            office._evict_state["last"] = 0.0
            office._maybe_evict_cache(work, 1024)

            self.assertFalse(os.path.isdir(orphan),
                             "超过 1 小时的 office-* 孤儿目录必须被回收")
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ---------------------------------------------------------------------------
# 端到端 HTTP（用真实入口 python app.py 起一个子进程）
# ---------------------------------------------------------------------------

class HttpIntegrationTests(unittest.TestCase):
    """
    端到端验证认证 / CSRF / 根目录防护。

    这里刻意用 `python app.py` 子进程启动，而不是在进程内自己构造
    uvicorn.Config：只有真正走 main() 里的 uvicorn.run()，
    `proxy_headers` / `forwarded_allow_ips` 这两个参数才在测试覆盖范围内
    —— 而它们正是「伪造 X-Forwarded-For 绕过登录锁定」这条漏洞的另一半修复。
    （uvicorn 默认 proxy_headers=True 且信任来自 127.0.0.1 的该头，
    会在我们的安全中间件之前就改写 scope["client"]。）
    """

    @classmethod
    def setUpClass(cls):
        import subprocess

        from fileweb import config as config_module

        cls.work = tempfile.mkdtemp(prefix="fw-http-")
        cls.root_dir = os.path.join(cls.work, "root")
        os.makedirs(cls.root_dir, exist_ok=True)
        with open(os.path.join(cls.root_dir, "hello.txt"), "w", encoding="utf-8") as fh:
            fh.write("hi")

        cls.port = _free_port()

        cfg = config_module.load()
        cfg["server"] = {"host": "127.0.0.1", "port": cls.port, "title": "fileweb-test"}
        # 只暴露一个临时目录，避免测试碰到真实磁盘
        cfg["roots"] = [{"id": "scratch", "name": "scratch",
                         "path": cls.root_dir, "readonly": False}]
        cfg["mount_all_drives"] = False
        cfg["mount_network_drives"] = False
        cfg["mount_removable_drives"] = False
        cfg["protected_paths"] = []
        cfg["thumbs"]["cache_dir"] = os.path.join(cls.work, "thumbs")
        cfg["office"]["cache_dir"] = os.path.join(cls.work, "office")
        cfg["auth"]["username"] = TEST_USERNAME
        cfg["auth"]["password_hash"] = security.hash_password(TEST_PASSWORD)
        cfg["auth"]["password"] = ""
        cfg["auth"]["session_secret"] = security.random_secret(32)
        cfg["auth"]["max_login_fails"] = 3
        cfg["auth"]["lockout_seconds"] = 300
        cfg["auth"]["trusted_proxies"] = []
        cfg["terminal"]["enabled"] = False
        cfg = config_module.prepare(cfg)

        cls.cfg_path = os.path.join(cls.work, "config.json")
        with open(cls.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in cfg.items() if not k.startswith("_")},
                      fh, ensure_ascii=False, indent=2)

        cls.log_path = os.path.join(cls.work, "server.log")
        cls.log_file = open(cls.log_path, "wb")
        cls.proc = subprocess.Popen(
            [sys.executable, "app.py", "--config", cls.cfg_path,
             "--host", "127.0.0.1", "--port", str(cls.port), "--log-level", "warning"],
            cwd=BASE_DIR, stdout=cls.log_file, stderr=subprocess.STDOUT,
        )

        deadline = time.time() + 40
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                break
            try:
                client = _Client(cls.port)
                status, _ = client.request("GET", "/api/auth/status")
                if status == 200:
                    return
            except Exception:  # noqa: BLE001 - 服务还没起来
                pass
            time.sleep(0.2)

        tail = cls._read_log_tail()
        exit_code = cls.proc.poll()
        cls._teardown()
        raise RuntimeError("测试服务器未能启动（退出码 %r）。日志尾部：\n%s"
                           % (exit_code, tail))

    @classmethod
    def _read_log_tail(cls, limit=3000):
        try:
            cls.log_file.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            with open(cls.log_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()[-limit:]
        except OSError:
            return "(无法读取服务器日志)"

    @classmethod
    def _teardown(cls):
        proc = getattr(cls, "proc", None)
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                proc.kill()
        log_file = getattr(cls, "log_file", None)
        if log_file is not None:
            try:
                log_file.close()
            except Exception:  # noqa: BLE001
                pass
        work = getattr(cls, "work", "")
        if work:
            shutil.rmtree(work, ignore_errors=True)

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    # -- 工具 ---------------------------------------------------------------

    def _login(self, headers=None):
        client = _Client(self.port)
        status, text = client.request(
            "POST", "/api/auth/login",
            {"username": TEST_USERNAME, "password": TEST_PASSWORD},
            headers=headers, with_csrf=False,
        )
        if status == 200:
            client.csrf = json.loads(text).get("csrf_token", "")
        return client, status, text

    # -- 测试 ---------------------------------------------------------------

    def test_unauthenticated_api_is_401(self):
        client = _Client(self.port)
        for method, path in (("GET", "/api/fs/roots"), ("POST", "/api/fs/mkdir")):
            status, _ = client.request(method, path, {} if method == "POST" else None,
                                       with_csrf=False)
            self.assertEqual(status, 401, "%s %s 未登录必须 401" % (method, path))

    def test_public_status_endpoint_is_reachable(self):
        client = _Client(self.port)
        status, text = client.request("GET", "/api/auth/status")
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(text)["authenticated"])

    def test_state_changing_request_requires_csrf(self):
        client, status, _ = self._login()
        self.assertEqual(status, 200)
        self.assertTrue(client.csrf)

        # 带会话但故意不带 CSRF 头 → 必须 403
        status, _ = client.request("POST", "/api/fs/mkdir",
                                   {"root": "scratch", "path": "", "name": "x"},
                                   with_csrf=False)
        self.assertEqual(status, 403)

        # 伪造令牌同样 403。
        # 这里必须显式 with_csrf=False，否则请求头会被真实令牌覆盖掉，
        # 测的就变成「正确令牌能通过」了（这个坑我踩过一次）。
        status, _ = client.request("POST", "/api/fs/mkdir",
                                   {"root": "scratch", "path": "", "name": "x"},
                                   headers={"X-CSRF-Token": "forged"},
                                   with_csrf=False)
        self.assertEqual(status, 403)

    def test_root_cannot_be_deleted_or_zipped(self):
        client, status, _ = self._login()
        self.assertEqual(status, 200)

        status, _ = client.request("POST", "/api/fs/delete",
                                   {"root": "scratch", "paths": [""]})
        self.assertEqual(status, 403, "根目录本身不允许删除")
        self.assertTrue(os.path.isdir(self.root_dir), "根目录必须还在")

        status, _ = client.request("POST", "/api/fs/zip",
                                   {"root": "scratch", "paths": [""]})
        self.assertEqual(status, 403, "根目录本身不允许打包")
        self.assertTrue(os.path.isdir(self.root_dir), "根目录必须还在")

    def test_normal_delete_still_works(self):
        """确认根目录防护没有误伤正常删除。"""
        client, status, _ = self._login()
        self.assertEqual(status, 200)

        victim = os.path.join(self.root_dir, "to-delete.txt")
        with open(victim, "w", encoding="utf-8") as fh:
            fh.write("bye")

        status, text = client.request("POST", "/api/fs/delete",
                                      {"root": "scratch", "paths": ["to-delete.txt"],
                                       "permanent": True})
        self.assertEqual(status, 200, text)
        self.assertFalse(os.path.exists(victim))

    def test_login_and_list_directory(self):
        client, status, _ = self._login()
        self.assertEqual(status, 200)

        status, text = client.request("GET", "/api/fs/list?root=scratch&path=")
        self.assertEqual(status, 200, text)
        data = json.loads(text)
        names = [entry["name"] for entry in data["entries"]]
        self.assertIn("hello.txt", names)

    def test_z_forwarded_header_cannot_bypass_login_lockout(self):
        """
        关键回归：以前 client_ip() 无条件采信 X-Forwarded-For，
        攻击者每次换一个伪造值就能让失败计数永远归零，从而无限暴力破解。

        修复要同时到位两层：
          1. deps.client_ip() 默认不采信该头；
          2. uvicorn 不能再在中间件之前按它改写 scope["client"]。

        注意：本用例会真的把 127.0.0.1 锁定一段时间，且被测服务是独立子进程、
        无法从外部清除锁定状态，所以方法名用 test_z_ 前缀确保它最后执行。
        """
        # 用伪造 IP 打满失败次数
        for index, forged in enumerate(["10.9.9.1", "10.9.9.2", "10.9.9.3"]):
            client = _Client(self.port)
            client.request("POST", "/api/auth/login",
                           {"username": TEST_USERNAME, "password": "wrong"},
                           headers={"X-Forwarded-For": forged},
                           with_csrf=False)

        # 换成另一个伪造 IP + 正确密码：修复前这里会返回 200，修复后必须是 429
        client = _Client(self.port)
        status, text = client.request(
            "POST", "/api/auth/login",
            {"username": TEST_USERNAME, "password": TEST_PASSWORD},
            headers={"X-Forwarded-For": "10.9.9.9"}, with_csrf=False,
        )
        self.assertEqual(
            status, 429,
            "换一个 X-Forwarded-For 就能绕过锁定 = 暴力破解防护失效"
            "（返回了 %d：%s）" % (status, text[:120]),
        )


class _FakeWinError(Exception):
    """带 winerror 的假异常，用来验证错误码翻译（不依赖真实系统调用）。"""

    def __init__(self, winerror=None, errno_=None, message="raw error text"):
        super().__init__(message)
        if winerror is not None:
            self.winerror = winerror
        if errno_ is not None:
            self.errno = errno_


class DeleteErrorReportingTests(unittest.TestCase):
    """
    删除失败时的报错质量。

    背景：用户删一个 1GB 的目录时报 WinError 161，界面却提示
    「目标盘不支持回收站（网络驱动器 / 非 NTFS 分区）」把他引偏了 ——
    实测 D: 是 NTFS、回收站配额 48GB、自建探针也能正常回收。
    所以这里钉住三件事：错误码要翻译成真实原因、认不出来不编造、
    并且必须给出「改用永久删除重试」这个真正可行的出路。
    """

    def test_known_winerror_codes_are_explained_in_chinese(self):
        for code in (161, 32, 5, 1223):
            text = fsops._explain_delete_error(_FakeWinError(winerror=code))
            self.assertIn("错误码 %s" % code, text, "应保留错误码便于排查")
            self.assertIn("。", text, "应给出原因与建议")
            self.assertNotEqual(text, "raw error text")

    def test_unknown_error_is_passed_through_verbatim(self):
        text = fsops._explain_delete_error(_FakeWinError(winerror=99999))
        self.assertIn("raw error text", text)
        self.assertNotIn("错误码", text, "认不出来时不能编造原因")

    def test_errno_is_used_when_winerror_missing(self):
        text = fsops._explain_delete_error(_FakeWinError(errno_=32))
        self.assertIn("其它程序使用", text)

    def test_recycle_failure_allows_permanent_retry(self):
        work = tempfile.mkdtemp(prefix="fw-del-")
        try:
            victim = os.path.join(work, "x.txt")
            with open(victim, "w", encoding="utf-8") as fh:
                fh.write("x")

            original = fsops._send_to_recycle_bin
            fsops._send_to_recycle_bin = lambda paths: ["x.txt：路径无效（错误码 161）。…"]
            try:
                result = fsops.delete_entries([victim], use_recycle_bin=True)
            finally:
                fsops._send_to_recycle_bin = original

            self.assertEqual(result["mode"], "recycle")
            self.assertTrue(result["can_retry_permanent"],
                            "回收站失败时必须允许前端提示「改用永久删除重试」")
            self.assertTrue(os.path.exists(victim), "回收站失败时文件必须原封不动")
            joined = "\n".join(result["failures"])
            self.assertIn("永久删除", joined, "必须给出真正可行的出路")
            self.assertNotIn("NTFS", joined, "不能再武断归因于盘格式")
            self.assertNotIn("网络驱动器", joined)
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_permanent_mode_reports_no_retry(self):
        work = tempfile.mkdtemp(prefix="fw-del2-")
        try:
            keep = os.path.join(work, "keep.txt")
            with open(keep, "w", encoding="utf-8") as fh:
                fh.write("x")

            original = fsops._permanent_delete
            fsops._permanent_delete = lambda paths: ["keep.txt：拒绝访问（错误码 5）。…"]
            try:
                result = fsops.delete_entries([keep], use_recycle_bin=False)
            finally:
                fsops._permanent_delete = original

            self.assertEqual(result["mode"], "permanent")
            self.assertFalse(result["can_retry_permanent"],
                             "已经是永久删除，再失败就没有别的退路了")
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def test_successful_recycle_marks_no_failure(self):
        work = tempfile.mkdtemp(prefix="fw-del3-")
        try:
            gone = os.path.join(work, "gone.txt")
            with open(gone, "w", encoding="utf-8") as fh:
                fh.write("x")

            original = fsops._send_to_recycle_bin
            fsops._send_to_recycle_bin = lambda paths: (os.unlink(paths[0]), [])[1]
            try:
                result = fsops.delete_entries([gone], use_recycle_bin=True)
            finally:
                fsops._send_to_recycle_bin = original

            self.assertEqual(result["failures"], [])
            self.assertEqual(result["deleted"], ["gone.txt"])
            self.assertFalse(result["can_retry_permanent"])
        finally:
            shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

# -*- coding: utf-8 -*-
"""
测试脚手架
==========

只用标准库：

    * ServerProcess —— 用真实入口 `python app.py` 起一个服务子进程
    * Client        —— 极简 HTTP 客户端（自动维护 Cookie 与 CSRF 头）
    * free_port     —— 取一个空闲本地端口

为什么坚持用子进程而不是在测试进程里直接构造 uvicorn.Config：
`uvicorn.run()` 的默认参数（尤其 proxy_headers / forwarded_allow_ips）
本身就是被测对象之一，只有在真实入口下才会生效。详见
tests/test_fileweb.py 里 HttpIntegrationTests 的说明。
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from fileweb import config as config_module        # noqa: E402
from fileweb import security                       # noqa: E402
from fileweb.deps import SESSION_COOKIE            # noqa: E402


def free_port() -> int:
    """取一个当前空闲的本地端口。"""
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


class Client:
    """极简 HTTP 客户端：只做测试需要的事，不引入 requests/httpx。"""

    def __init__(self, port: int):
        self.port = port
        self.base = "http://127.0.0.1:%d" % port
        self.cookie = ""
        self.csrf = ""

    # -- 基础请求 -----------------------------------------------------------

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
            with urllib.request.urlopen(req, timeout=120) as resp:
                self._capture_cookie(resp.headers)
                return resp.status, resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            self._capture_cookie(exc.headers)
            return exc.code, exc.read().decode("utf-8", "replace")

    def json(self, method, path, body=None, **kwargs):
        """发请求并尽量把响应解析成 JSON。"""
        status, text = self.request(method, path, body, **kwargs)
        try:
            return status, json.loads(text)
        except Exception:  # noqa: BLE001 - 非 JSON 响应（例如 403 空体）
            return status, {"_raw": text}

    # -- 认证 ---------------------------------------------------------------

    def login(self, username, password, headers=None):
        status, data = self.json("POST", "/api/auth/login",
                                 {"username": username, "password": password},
                                 headers=headers, with_csrf=False)
        if status == 200:
            self.csrf = data.get("csrf_token", "")
        return status, data

    def _capture_cookie(self, headers) -> None:
        raw = headers.get("Set-Cookie") if headers else None
        if not raw:
            return
        for chunk in raw.split(";"):
            chunk = chunk.strip()
            if chunk.startswith(SESSION_COOKIE + "="):
                self.cookie = chunk
                return


class ServerProcess:
    """
    用真实入口起一个服务子进程。

    配置全部写进临时目录，只暴露调用方给定的根目录，
    因此测试不会碰到真实磁盘上的任何文件。
    """

    def __init__(self, roots, *, username="tester", password="test-password-123",
                 extra_config=None):
        self.work = tempfile.mkdtemp(prefix="fw-test-")
        self.username = username
        self.password = password

        self.cfg_path = os.path.join(self.work, "config.json")
        self.log_path = os.path.join(self.work, "server.log")
        self.port = free_port()
        self.proc = None
        self._log_file = None

        # create_if_missing=False 是必须的，不是保守写法：
        # 不带参数时 load() 会以**真实的** config.json 为目标，而它默认
        # create_if_missing=True —— 万一哪天 config.json 被改名或删掉了，
        # 跑一次测试就会「重新生成一份真实配置」，并顺手把随机口令写进
        # 项目的 FIRST_RUN_PASSWORD.txt，等于把线上部署的登录信息毁掉。
        # 这里只是要一份默认配置当基线（下面每一项都会覆盖），
        # 所以明确要求「不存在就别创建」。
        cfg = config_module.load(create_if_missing=False)
        cfg["server"] = {"host": "127.0.0.1", "port": self.port, "title": "fileweb-test"}
        cfg["roots"] = roots
        cfg["mount_all_drives"] = False
        cfg["mount_network_drives"] = False
        cfg["mount_removable_drives"] = False
        cfg["protected_paths"] = []
        cfg["thumbs"]["cache_dir"] = os.path.join(self.work, "thumbs")
        cfg["office"]["cache_dir"] = os.path.join(self.work, "office")
        # 界面状态文件也必须落在临时目录里。
        # ★ 这一项漏掉的后果比其他缓存严重得多：user_state.json 存的是用户
        #   真实的桌面布局（窗口位置、所在目录、视图模式），它的默认位置在
        #   **项目根目录**（即真实部署的同一个文件）。不覆盖它的话，测试里
        #   起的服务器会把真实部署的布局覆盖成测试数据 —— 实测发生过：
        #   项目根目录的 user_state.json 里出现了一个指向临时测试目录
        #   （Temp\sstest\root）的命令行窗口，用户下次打开就会看到这个脏窗口。
        #
        # ★ desktop_shortcuts_path 是同一类东西，必须一起覆盖：
        #   这里的 prepare() 是**不带 cfg_path** 调的，所以相对路径会按代码目录
        #   解析成绝对路径再写进临时配置，子进程看到的就是项目根目录 ——
        #   于是测试建的快捷方式会真的落到真实桌面上。
        #   （实测发生过一次：项目根目录冒出 desktop_shortcuts.stu01.json。）
        cfg["user_state_path"] = os.path.join(self.work, "user_state.json")
        cfg["desktop_shortcuts_path"] = os.path.join(
            self.work, "desktop_shortcuts.json")
        cfg["auth"]["username"] = username
        cfg["auth"]["password_hash"] = security.hash_password(password)
        cfg["auth"]["password"] = ""
        cfg["auth"]["session_secret"] = security.random_secret(32)
        cfg["auth"]["max_login_fails"] = 3
        cfg["auth"]["lockout_seconds"] = 300
        cfg["auth"]["trusted_proxies"] = []
        cfg["terminal"]["enabled"] = False
        if extra_config is not None:
            extra_config(cfg)
        cfg = config_module.prepare(cfg)

        with open(self.cfg_path, "w", encoding="utf-8") as fh:
            json.dump({k: v for k, v in cfg.items() if not k.startswith("_")},
                      fh, ensure_ascii=False, indent=2)

    # -- 生命周期 -----------------------------------------------------------

    def start(self, timeout=40):
        self._log_file = open(self.log_path, "wb")
        self.proc = subprocess.Popen(
            [sys.executable, "app.py", "--config", self.cfg_path,
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=BASE_DIR, stdout=self._log_file, stderr=subprocess.STDOUT,
        )

        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            try:
                status, _ = Client(self.port).request("GET", "/api/auth/status")
                if status == 200:
                    return self
            except Exception:  # noqa: BLE001 - 服务还没起来
                pass
            time.sleep(0.2)

        tail = self.log_tail()
        exit_code = self.proc.poll()
        self.cleanup()
        raise RuntimeError("测试服务未能启动（退出码 %r）。日志尾部：\n%s"
                           % (exit_code, tail))

    def log_tail(self, limit=4000):
        try:
            if self._log_file is not None:
                self._log_file.flush()
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as fh:
                return fh.read()[-limit:]
        except OSError:
            return "(无法读取服务器日志)"

    def stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=15)
            except Exception:  # noqa: BLE001
                self.proc.kill()
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_file = None

    def cleanup(self):
        self.stop()
        shutil.rmtree(self.work, ignore_errors=True)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc_info):
        self.cleanup()
        return False

    # -- 便捷方法 -----------------------------------------------------------

    def client(self) -> Client:
        return Client(self.port)

    def login_client(self) -> Client:
        """返回一个已登录（含 CSRF 令牌）的客户端。"""
        client = Client(self.port)
        status, data = client.login(self.username, self.password)
        if status != 200:
            raise RuntimeError("测试登录失败：%s %s" % (status, data))
        return client

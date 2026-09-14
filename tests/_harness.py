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


# ---------------------------------------------------------------------------
# ★ 「默认落在项目根目录」的状态文件
# ---------------------------------------------------------------------------
# 这几个配置项的共同点：默认值是**项目根目录下的一个裸文件名**，也就是
# 真实部署正在用的那一份。用临时配置起的服务如果不把它们改到临时目录，
# 就会写脏真实部署的数据。
#
# 本项目已经因为这个坑出过三次事故：
#   1. config.json 被测试配置覆盖（端口变随机、口令被换，服务失联）；
#   2. 真实 user_state.json 里出现了指向临时测试目录的窗口；
#   3. 真实 desktop_shortcuts.json / audit.log.jsonl 被测试写入。
# 所以做成一个公用函数 + 一条结构性守卫（见 test_multiuser_state.py 的
# HarnessStatePathIsolationTests）：**新增一个这样的配置项时，
# 守卫会先变红，逼你先把它加到这里**。
STATE_PATH_KEYS = (
    "user_state_path",
    "desktop_shortcuts_path",
    "audit.path",
)


def redirect_state_paths(cfg: dict, work: str) -> None:
    """
    把所有「默认落在项目根目录」的状态文件改到 work 目录下。

    ★ 在**调用 prepare() 之前**调到才有效：prepare() 会把相对路径解析成
    「配置文件所在目录」下的绝对路径，若此时还没有配置文件上下文，
    它就按代码目录解析成项目根目录的绝对路径，之后子进程会照办。
    """
    cfg["user_state_path"] = os.path.join(work, "user_state.json")
    cfg["desktop_shortcuts_path"] = os.path.join(work, "desktop_shortcuts.json")

    audit_cfg = cfg.get("audit")
    if not isinstance(audit_cfg, dict):
        audit_cfg = {}
    else:
        audit_cfg = dict(audit_cfg)
    audit_cfg["path"] = os.path.join(work, "audit.log.jsonl")
    cfg["audit"] = audit_cfg


def state_path_values(cfg: dict) -> dict:
    """把 STATE_PATH_KEYS 里每一项的实际取值取出来（守卫用）。"""
    values = {}
    for key in STATE_PATH_KEYS:
        if key == "audit.path":
            values[key] = (cfg.get("audit") or {}).get("path")
        else:
            values[key] = cfg.get(key)
    return values


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
        # ★ 所有「默认落在项目根目录」的状态文件都改到临时目录里。
        #   必须放在 prepare() **之前**（见 redirect_state_paths 的说明）。
        #   抽成公用函数是因为这里已经出过三次事故，而且 test_fileweb.py
        #   的那个端到端用例会自己拼配置，同样得调它。
        redirect_state_paths(cfg, self.work)
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

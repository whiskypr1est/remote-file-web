# -*- coding: utf-8 -*-
"""
系统监控采集（「任务管理器」窗口的数据来源）
============================================

给前端提供一个快照：CPU、内存、磁盘 IO、网络、GPU 与进程列表。

设计要点
--------
1. **速率类指标必须由服务端算差值。**
   网络/磁盘的「每秒字节数」本质是累积计数器的差分。浏览器两次轮询的
   间隔会被渲染、标签页切到后台等影响，前端自己算会抖得没法看；
   服务端保留上次采样、用单调时钟求间隔，结果稳定得多。

2. **psutil 缺失时不能让服务崩，也不能让界面空白。**
   本项目里 psutil 目前是 py7zr 的传递依赖（requirements.txt 也已显式声明）。
   万一某台机器上确实没有，这里返回 available=False + 原因，
   任务管理器显示一句人话，而不是抛 500 让人摸不着头脑。

3. **刻意不采集命令行（cmdline）。**
   命令行里经常带密码或令牌（`mysql -pXXX`、`-p token=...`），
   而这是个会输出到远端浏览器的接口。只给 名称/PID/CPU/内存/用户/状态。

4. **GPU 尽力而为，读不到就说读不到。**
   利用率没有跨平台统一接口：NVIDIA 可以用 nvidia-smi 直接读；
   Intel / AMD 显卡在 Windows 上要走 PDH 性能计数器（尚未实现）。
   读不到时如实报告不可用，**不会编一个 0% 出来** —— 那比没有更糟。
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

try:  # pragma: no cover - 取决于运行环境
    import psutil
except Exception:  # noqa: BLE001
    psutil = None


# Windows 下不要弹出控制台窗口（以服务身份运行时尤其重要）
_CREATE_NO_WINDOW = 0x08000000 if os.name == "nt" else 0


# ---------------------------------------------------------------------------
# GPU：nvidia-smi 探测（结果缓存，位置不会变，没必要每轮都找）
# ---------------------------------------------------------------------------

_NVIDIA_LOCK = threading.Lock()
_nvidia_path: Any = False        # False=未探测, None=没有, str=路径

_NVIDIA_FIELDS = "name,utilization.gpu,memory.used,memory.total,temperature.gpu"
_NVIDIA_ARGS = [
    "nvidia-smi",
    "--query-gpu=" + _NVIDIA_FIELDS,
    "--format=csv,noheader,nounits",
]


def _find_nvidia_smi() -> Optional[str]:
    """找 nvidia-smi；没有就返回 None（结果会被缓存）。"""
    global _nvidia_path
    with _NVIDIA_LOCK:
        if _nvidia_path is not False:
            return _nvidia_path

        found = shutil.which("nvidia-smi")
        if not found and os.name == "nt":
            for candidate in (
                os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                             "NVIDIA Corporation", "NVSMI", "nvidia-smi.exe"),
                os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                             "System32", "nvidia-smi.exe"),
            ):
                if os.path.isfile(candidate):
                    found = candidate
                    break

        _nvidia_path = found or None
        return _nvidia_path


_GPU_UNSUPPORTED_REASON = (
    "未能读取 GPU 利用率。本功能目前只支持 NVIDIA 显卡（通过 nvidia-smi），"
    "Intel / AMD 显卡需要 Windows PDH 性能计数器，尚未实现。"
)


def _num(text: str) -> Optional[float]:
    """nvidia-smi 读不到时会给 [N/A]，这里统一转成 None。"""
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value


def read_gpu() -> Dict[str, Any]:
    exe = _find_nvidia_smi()
    if not exe:
        return {"available": False, "reason": _GPU_UNSUPPORTED_REASON, "devices": []}

    try:
        done = subprocess.run(
            [exe] + _NVIDIA_ARGS[1:],
            capture_output=True,
            text=True,
            timeout=5,
            creationflags=_CREATE_NO_WINDOW,
        )
    except Exception as exc:  # noqa: BLE001
        return {"available": False,
                "reason": "调用 nvidia-smi 失败：%s" % exc,
                "devices": []}

    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        return {"available": False,
                "reason": "nvidia-smi 返回错误：%s" % (detail[0] if detail else done.returncode),
                "devices": []}

    devices: List[Dict[str, Any]] = []
    for line in (done.stdout or "").strip().splitlines():
        parts = [piece.strip() for piece in line.split(",")]
        if len(parts) < 5:
            continue
        used = _num(parts[2])
        total = _num(parts[3])
        devices.append({
            "name": parts[0],
            "utilization_percent": _num(parts[1]),
            "memory_used": int(used * 1024 * 1024) if used is not None else None,
            "memory_total": int(total * 1024 * 1024) if total is not None else None,
            "memory_percent": (round(used / total * 100.0, 1)
                               if used is not None and total else None),
            "temperature_c": _num(parts[4]),
        })

    if not devices:
        return {"available": False,
                "reason": "nvidia-smi 存在，但没有返回任何 GPU（可能驱动异常）",
                "devices": []}
    return {"available": True, "reason": None, "devices": devices}


# ---------------------------------------------------------------------------
# 采样器
# ---------------------------------------------------------------------------

class _Sampler:
    """
    跨调用保留状态：上一轮的 IO 计数器（算速率）与 Process 对象（算 CPU）。

    为什么要留住 psutil.Process 对象：
        Process.cpu_percent() 是「距本对象上次被调用」的均值，每次新建对象
        都会让它重新开始计时、第一轮恒为 0.0。留住对象，第二次轮询起
        数字才是真的。
    """

    def __init__(self) -> None:
        self.prev_net: Optional[Any] = None
        self.prev_disk: Optional[Any] = None
        self.prev_pernic: Dict[str, Any] = {}
        self.prev_at: Optional[float] = None
        self.procs: Dict[int, Any] = {}
        self.cpu_primed = False
        self.proc_cpu_ready = False

    # -- 速率 -----------------------------------------------------------------

    def rates(self) -> Tuple[float, float, float, float, Dict[str, List[float]]]:
        """返回 (网络收, 网络发, 磁盘读, 磁盘写) 的字节/秒与每网卡速率。"""
        now = time.monotonic()
        sent = recv = dread = dwrite = 0.0
        pernic: Dict[str, List[float]] = {}

        if self.prev_at is not None:
            span = now - self.prev_at
            if span > 0:
                try:
                    net = psutil.net_io_counters()
                    sent = max(0.0, (net.bytes_sent - self.prev_net.bytes_sent) / span)
                    recv = max(0.0, (net.bytes_recv - self.prev_net.bytes_recv) / span)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    disk = psutil.disk_io_counters()
                    if disk is not None and self.prev_disk is not None:
                        dread = max(0.0, (disk.read_bytes - self.prev_disk.read_bytes) / span)
                        dwrite = max(0.0, (disk.write_bytes - self.prev_disk.write_bytes) / span)
                except Exception:  # noqa: BLE001
                    pass
                try:
                    for name, cur in (psutil.net_io_counters(pernic=True) or {}).items():
                        old = self.prev_pernic.get(name)
                        if old is None:
                            continue
                        pernic[name] = [
                            max(0.0, (cur.bytes_recv - old.bytes_recv) / span),
                            max(0.0, (cur.bytes_sent - old.bytes_sent) / span),
                        ]
                except Exception:  # noqa: BLE001
                    pass

        try:
            self.prev_net = psutil.net_io_counters()
            self.prev_disk = psutil.disk_io_counters()
            self.prev_pernic = dict(psutil.net_io_counters(pernic=True) or {})
        except Exception:  # noqa: BLE001
            pass
        self.prev_at = now

        return sent, recv, dread, dwrite, pernic

    # -- 进程 -----------------------------------------------------------------

    def process_rows(self) -> List[Dict[str, Any]]:
        """全量进程列表（未排序、未截断）。"""
        alive = set()
        rows: List[Dict[str, Any]] = []
        cpu_count = psutil.cpu_count(logical=True) or 1

        for proc in psutil.process_iter(["pid"]):
            pid = proc.pid
            obj = self.procs.get(pid)
            if obj is None:
                obj = proc                    # 复用 process_iter 已经建好的对象
                self.procs[pid] = obj
            alive.add(pid)

            try:
                raw_cpu = obj.cpu_percent(None)
            except Exception:  # noqa: BLE001
                raw_cpu = 0.0

            try:
                info = obj.as_dict(attrs=[
                    "name", "username", "status", "create_time",
                    "memory_info", "memory_percent",
                ])
            except Exception:  # noqa: BLE001
                continue

            mem = info.get("memory_info")
            rows.append({
                "pid": pid,
                "name": info.get("name") or "?",
                # 归一化到「整机百分比」，以便和 Windows 任务管理器对得上：
                # psutil 给的是「单核百分比」，8 核跑满 4 核会是 400%，
                # 而任务管理器显示 50%。排序不受影响（同一系数）。
                "cpu_percent": round(raw_cpu / cpu_count, 1),
                "memory": int(getattr(mem, "rss", 0) or 0),
                "memory_percent": round(float(info.get("memory_percent") or 0.0), 1),
                "username": info.get("username") or "",
                "status": info.get("status") or "",
                "create_time": float(info.get("create_time") or 0.0),
            })

        # 清掉已经退出的进程，避免字典无限增长
        for pid in list(self.procs.keys()):
            if pid not in alive:
                self.procs.pop(pid, None)

        return rows


_sampler = _Sampler()
_lock = threading.Lock()
_cache: Dict[str, Any] = {"data": None, "at": 0.0}


# ---------------------------------------------------------------------------
# 采集
# ---------------------------------------------------------------------------

def _cpu_section() -> Dict[str, Any]:
    # 首次调用 cpu_percent(interval=None) 恒为 0.0（它要一个基线），
    # 所以第一轮先用一个极短阻塞采样拿到真实数字，避免首屏显示 0%。
    if not _sampler.cpu_primed:
        total = psutil.cpu_percent(interval=0.1)
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)
        _sampler.cpu_primed = True
    else:
        total = psutil.cpu_percent(interval=None)
        per_cpu = psutil.cpu_percent(interval=None, percpu=True)

    freq = None
    try:
        f = psutil.cpu_freq()
        if f is not None:
            freq = round(f.current, 0)
    except Exception:  # noqa: BLE001
        pass

    load = None
    try:
        load = [round(v, 2) for v in psutil.getloadavg()]
    except Exception:  # noqa: BLE001
        load = None

    return {
        "percent": round(float(total), 1),
        "per_cpu": [round(float(v), 1) for v in (per_cpu or [])],
        "count_logical": psutil.cpu_count(logical=True),
        "count_physical": psutil.cpu_count(logical=False),
        "freq_mhz": freq,
        "load_avg": load,
    }


def _memory_section() -> Dict[str, Any]:
    vm = psutil.virtual_memory()
    data = {
        "total": int(vm.total),
        "used": int(vm.total - vm.available),
        "available": int(vm.available),
        "percent": round(float(vm.percent), 1),
    }
    try:
        sw = psutil.swap_memory()
        data.update({
            "swap_total": int(sw.total),
            "swap_used": int(sw.used),
            "swap_percent": round(float(sw.percent), 1),
        })
    except Exception:  # noqa: BLE001
        data.update({"swap_total": 0, "swap_used": 0, "swap_percent": 0.0})
    return data


def _network_section(sent_bps: float, recv_bps: float,
                     pernic: Dict[str, List[float]]) -> Dict[str, Any]:
    data: Dict[str, Any] = {
        "sent_bps": int(round(sent_bps)),
        "recv_bps": int(round(recv_bps)),
        "sent_total": 0,
        "recv_total": 0,
        "per_nic": [],
    }
    try:
        totals = psutil.net_io_counters()
        data["sent_total"] = int(totals.bytes_sent)
        data["recv_total"] = int(totals.bytes_recv)
    except Exception:  # noqa: BLE001
        pass

    try:
        for name, cur in sorted((psutil.net_io_counters(pernic=True) or {}).items()):
            rate = pernic.get(name) or [0.0, 0.0]
            data["per_nic"].append({
                "name": name,
                "recv_bps": int(round(rate[0])),
                "sent_bps": int(round(rate[1])),
                "recv_total": int(cur.bytes_recv),
                "sent_total": int(cur.bytes_sent),
            })
    except Exception:  # noqa: BLE001
        pass
    return data


def _system_section() -> Dict[str, Any]:
    boot = None
    uptime = None
    try:
        boot = float(psutil.boot_time())
        uptime = max(0.0, time.time() - boot)
    except Exception:  # noqa: BLE001
        pass

    return {
        "hostname": platform.node(),
        "os": "%s %s" % (platform.system(), platform.release()),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "cpu_model": platform.processor() or "",
        "boot_time": boot,
        "uptime_seconds": uptime,
        "pid": os.getpid(),
    }


def _collect(cfg: Dict[str, Any]) -> Dict[str, Any]:
    sent, recv, dread, dwrite, pernic = _sampler.rates()

    try:
        rows = _sampler.process_rows()
    except Exception:  # noqa: BLE001
        rows = []

    if rows and not _sampler.proc_cpu_ready:
        _sampler.proc_cpu_ready = True

    return {
        "available": True,
        "ts": time.time(),
        "cpu": _cpu_section(),
        "memory": _memory_section(),
        "disk": {
            "read_bps": int(round(dread)),
            "write_bps": int(round(dwrite)),
        },
        "network": _network_section(sent, recv, pernic),
        "gpu": read_gpu(),
        "system": _system_section(),
        "processes": {
            "total": len(rows),
            # 第一轮进程 CPU 必然全是 0（psutil 要先建立基线），
            # 前端据此显示一句「首次采样中」，免得用户以为是坏的
            "cpu_ready": _sampler.proc_cpu_ready,
            "list": rows,
        },
    }


def _unavailable(reason: str) -> Dict[str, Any]:
    return {
        "available": False,
        "reason": reason,
        "ts": time.time(),
        "cpu": {}, "memory": {}, "disk": {}, "network": {},
        "gpu": {"available": False, "reason": reason, "devices": []},
        "system": _system_section(),
        "processes": {"total": 0, "cpu_ready": False, "list": []},
    }


def snapshot(cfg: Optional[Dict[str, Any]] = None, force: bool = False) -> Dict[str, Any]:
    """
    取一份全量快照（进程列表**不截断**，由 view() 按请求裁剪）。

    带一个很短的缓存：多个浏览器同时开着任务管理器时，不必各自触发一次
    完整采集（进程枚举在忙的机器上要几百毫秒）。
    顺带保证速率计算只在同一把锁里推进，不会因为并发采集而算错间隔。
    """
    if psutil is None:
        return _unavailable(
            "服务端没有安装 psutil，无法读取系统负载。"
            "请执行 pip install -r requirements.txt 后重启服务。"
        )

    settings = cfg or {}
    try:
        min_interval = float(settings.get("min_interval_seconds") or 0.5)
    except (TypeError, ValueError):
        min_interval = 0.5
    min_interval = max(0.0, min_interval)

    with _lock:
        now = time.monotonic()
        if (not force and _cache["data"] is not None
                and now - _cache["at"] < min_interval):
            return _cache["data"]
        try:
            data = _collect(settings)
        except Exception as exc:  # noqa: BLE001
            # 采集失败也要给出可读原因，而不是 500
            data = _unavailable("采集系统信息失败：%s" % exc)
        _cache["data"] = data
        _cache["at"] = time.monotonic()
        return data


def view(data: Dict[str, Any], sort_by: str = "cpu", top: int = 30) -> Dict[str, Any]:
    """
    按请求裁剪要返回给浏览器的内容（排序 + 取前 N）。

    只在副本上操作：缓存里那份是全量的，不能被这次请求改掉。
    """
    result = dict(data)
    processes = dict(data.get("processes") or {})

    rows = list(processes.get("list") or [])
    key = "memory" if sort_by == "memory" else "cpu"
    if key == "memory":
        rows.sort(key=lambda r: (r.get("memory") or 0), reverse=True)
    else:
        rows.sort(key=lambda r: (r.get("cpu_percent") or 0), reverse=True)

    limit = max(1, min(500, int(top or 30)))
    processes["list"] = rows[:limit]
    processes["shown"] = len(processes["list"])
    processes["sort"] = key
    result["processes"] = processes
    return result


def reset_cache() -> None:
    """清空缓存与采样基线（测试用；也让「重启服务」语义更干净）。"""
    with _lock:
        _cache["data"] = None
        _cache["at"] = 0.0

"""llama-server 子进程管理：自动启动、健康检查、退出清理。"""
import asyncio
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from .config import data_dir, get_config, resolve_path

_proc: Optional[subprocess.Popen] = None
_state: Dict[str, Any] = {"phase": "idle", "detail": "", "spawned": False}
# phase: idle | starting | ready | error | external

_job_handle = None  # Windows Job Object，保持引用防止句柄被回收


def _create_kill_on_close_job():
    """创建 KILL_ON_JOB_CLOSE 的 Job Object。

    把 llama-server 挂进去后，无论主进程以何种方式退出（包括用户直接
    关闭控制台窗口），系统都会自动结束子进程，避免孤儿进程占用显存。
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.windll.kernel32

    class BASIC_LIMIT(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class EXTENDED_LIMIT(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", BASIC_LIMIT),
            ("IoInfo", ctypes.c_ulonglong * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    info = EXTENDED_LIMIT()
    info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    ok = kernel32.SetInformationJobObject(
        job, 9, ctypes.byref(info), ctypes.sizeof(info))  # 9 = ExtendedLimitInformation
    if not ok:
        kernel32.CloseHandle(job)
        return None
    return job


def _attach_to_job(proc: subprocess.Popen) -> None:
    global _job_handle
    if os.name != "nt":
        return
    try:
        import ctypes
        if _job_handle is None:
            _job_handle = _create_kill_on_close_job()
        if _job_handle:
            ctypes.windll.kernel32.AssignProcessToJobObject(
                _job_handle, int(proc._handle))  # type: ignore[attr-defined]
    except Exception:
        pass  # 兜底失败不影响主流程，仅失去自动回收能力


def build_args(cfg: Dict[str, Any]) -> List[str]:
    exe = resolve_path(cfg["server_exe"])
    model = resolve_path(cfg["model"])
    args: List[str] = [
        str(exe),
        "-m", str(model),
        "--host", str(cfg.get("host", "127.0.0.1")),
        "--port", str(cfg["port"]),
        "-c", str(cfg["ctx_size"]),
        "-ngl", str(cfg["n_gpu_layers"]),
        "--alias", str(cfg.get("alias", "local-model")),
    ]
    if cfg.get("cache_type_k"):
        args += ["--cache-type-k", str(cfg["cache_type_k"])]
    if cfg.get("cache_type_v"):
        args += ["--cache-type-v", str(cfg["cache_type_v"])]
    args += [str(a) for a in cfg.get("extra_args", [])]
    return args


_restart_lock: Optional[asyncio.Lock] = None
_last_attempt = 0.0
RETRY_COOLDOWN = 15.0


def _get_lock() -> asyncio.Lock:
    global _restart_lock
    if _restart_lock is None:
        _restart_lock = asyncio.Lock()
    return _restart_lock


def reset_cooldown() -> None:
    """切换模型等主动重启场景下，跳过重启冷却。"""
    global _last_attempt
    _last_attempt = 0.0


async def ensure_started() -> None:
    """应用启动时调用。"""
    await ensure_running()


async def ensure_running() -> bool:
    """健康则直接返回 True；不健康则（带冷却地）自动拉起 llama-server 并等待就绪。

    可以被任意请求路径反复调用：锁保证同时只有一次启动尝试，
    冷却时间避免持续失败时的重启风暴。
    """
    global _last_attempt
    from .llm import health_check

    if await health_check():
        if _state["phase"] != "ready":
            _state.update(phase="ready", detail="模型已就绪")
        return True

    cfg = get_config()["llama"]
    if not cfg.get("autostart", True):
        _state.update(phase="external",
                      detail=f"模型服务未运行，请手动启动 llama-server（{cfg['base_url']}）")
        return False

    async with _get_lock():
        if await health_check():
            return True
        now = time.time()
        if now - _last_attempt < RETRY_COOLDOWN:
            return False
        _last_attempt = now
        stop()  # 清理可能残留的旧子进程
        return await _spawn_and_wait(cfg)


async def _spawn_and_wait(cfg: Dict[str, Any]) -> bool:
    global _proc
    from .llm import health_check

    exe = resolve_path(cfg["server_exe"])
    model = resolve_path(cfg["model"])
    if not exe.exists():
        _state.update(phase="error", detail=f"找不到 llama-server：{exe}")
        return False
    if not model.exists():
        _state.update(phase="error", detail=f"找不到模型文件：{model}")
        return False

    _state.update(phase="starting", detail="正在启动模型服务并加载模型…")
    log_path = data_dir() / "llama-server.log"
    log_f = open(log_path, "a", encoding="utf-8", errors="ignore")
    log_f.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    log_f.flush()
    creationflags = 0x08000000 if os.name == "nt" else 0  # CREATE_NO_WINDOW
    try:
        _proc = subprocess.Popen(
            build_args(cfg),
            stdout=log_f,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
            cwd=str(exe.parent),
        )
    except Exception as e:
        _state.update(phase="error", detail=f"启动 llama-server 失败：{e}")
        return False
    _attach_to_job(_proc)
    _state["spawned"] = True

    deadline = time.time() + 300
    while time.time() < deadline:
        if _proc.poll() is not None:
            _state.update(
                phase="error",
                detail=f"llama-server 异常退出（代码 {_proc.returncode}），"
                       f"详见 data/llama-server.log",
            )
            return False
        if await health_check(2.0):
            _state.update(phase="ready", detail="模型已就绪")
            return True
        await asyncio.sleep(1.0)
    _state.update(phase="error", detail="模型加载超时（300 秒），详见 data/llama-server.log")
    return False


def stop() -> None:
    global _proc
    if _proc is not None and _proc.poll() is None:
        try:
            _proc.terminate()
            _proc.wait(timeout=10)
        except Exception:
            try:
                _proc.kill()
            except Exception:
                pass
    _proc = None


def status() -> Dict[str, Any]:
    return dict(_state)

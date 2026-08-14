"""Race-free lifecycle supervisor for the private llama-server process."""
from __future__ import annotations

import asyncio
import os
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

from .config import data_dir, get_config, resolve_model_path, resolve_path

RETRY_COOLDOWN = 15.0
_API_KEY = secrets.token_urlsafe(32)
_job_handle = None


def _rotate_log(path: Path, max_bytes: int = 5_000_000, backups: int = 2) -> None:
    """Rotate the llama-server log so a long-running instance never grows unbounded."""
    try:
        if path.stat().st_size <= max_bytes:
            return
        for index in range(backups - 1, 0, -1):
            old = Path(f"{path}.{index}")
            if old.exists():
                old.replace(Path(f"{path}.{index + 1}"))
        path.replace(Path(f"{path}.1"))
    except OSError:
        # Rotation is best-effort; the append below still works.
        return


def api_key() -> str:
    """Return the process-local key used only on the backend-to-model hop."""
    return _API_KEY


def _create_kill_on_close_job():
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
    info.BasicLimitInformation.LimitFlags = 0x2000
    if not kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)):
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
                _job_handle, int(proc._handle)  # type: ignore[attr-defined]
            )
    except Exception:
        # Job Objects are defence in depth. Explicit shutdown still runs.
        return


def build_args(cfg: Dict[str, Any]) -> List[str]:
    exe = resolve_path(str(cfg["server_exe"]))
    model = resolve_model_path(str(cfg["model"]))
    app_cfg = get_config()
    allowed_origin = f"http://127.0.0.1:{app_cfg['app_port']}"
    args: List[str] = [
        str(exe),
        "-m", str(model),
        "--host", "127.0.0.1",
        "--port", str(cfg["port"]),
        "-c", str(cfg["ctx_size"]),
        "-ngl", str(cfg["n_gpu_layers"]),
        "--alias", str(cfg.get("alias", "local-model")),
        "--parallel", str(cfg.get("parallel", 1)),
        "--api-key", api_key(),
        "--cors-origins", allowed_origin,
        "--no-webui",
        "--no-slots",
    ]
    if cfg.get("cache_type_k"):
        args += ["--cache-type-k", str(cfg["cache_type_k"])]
    if cfg.get("cache_type_v"):
        args += ["--cache-type-v", str(cfg["cache_type_v"])]
    args += [str(value) for value in cfg.get("extra_args", [])]
    return args


@dataclass
class LlamaSupervisor:
    """Own the process and invalidate stale start attempts with a generation."""

    _process: Optional[subprocess.Popen] = None
    _generation: int = 0
    _last_attempt: float = 0.0
    _state: Dict[str, Any] = field(default_factory=lambda: {
        "phase": "idle", "detail": "", "spawned": False, "generation": 0,
    })
    _lock: Optional[asyncio.Lock] = None
    _tasks: Set[asyncio.Task] = field(default_factory=set)
    _thread_lock: threading.RLock = field(default_factory=threading.RLock)

    def async_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def new_generation(self) -> int:
        with self._thread_lock:
            self._generation += 1
            self._state["generation"] = self._generation
            return self._generation

    def is_current(self, generation: int) -> bool:
        with self._thread_lock:
            return generation == self._generation

    def reset_cooldown(self) -> None:
        self._last_attempt = 0.0

    def status(self) -> Dict[str, Any]:
        with self._thread_lock:
            return dict(self._state)

    def schedule_ensure_running(self) -> asyncio.Task:
        """Create a supervised recovery task that cannot be leaked silently."""
        task = asyncio.create_task(self.ensure_running())
        self._tasks.add(task)

        def reap(done: asyncio.Task) -> None:
            self._tasks.discard(done)
            if not done.cancelled():
                done.exception()  # mark any failure as observed

        task.add_done_callback(reap)
        return task

    async def ensure_running(self) -> bool:
        from .llm import health_check

        if await health_check():
            with self._thread_lock:
                self._state.update(phase="ready", detail="模型已就绪")
            return True

        cfg = get_config()["llama"]
        if not cfg.get("autostart", True):
            with self._thread_lock:
                self._state.update(phase="external", detail="模型服务未运行")
            return False

        async with self.async_lock():
            if await health_check():
                return True
            now = time.monotonic()
            if now - self._last_attempt < RETRY_COOLDOWN:
                return False
            self._last_attempt = now
            await self.stop_async()
            generation = self.new_generation()
            return await self._spawn_and_wait(cfg, generation)

    async def _spawn_and_wait(self, cfg: Dict[str, Any], generation: int) -> bool:
        from .llm import health_check

        exe = resolve_path(str(cfg["server_exe"]))
        model = resolve_model_path(str(cfg["model"]))
        if not exe.is_file() or not model.is_file():
            missing = exe if not exe.is_file() else model
            with self._thread_lock:
                self._state.update(phase="error", detail=f"找不到运行文件：{missing}")
            return False

        with self._thread_lock:
            self._state.update(phase="starting", detail="正在加载本地模型…", spawned=False)
        log_path = data_dir() / "llama-server.log"
        _rotate_log(log_path)
        log_stream = log_path.open("a", encoding="utf-8", errors="ignore")
        log_stream.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
        log_stream.flush()
        try:
            proc = subprocess.Popen(
                build_args(cfg),
                stdout=log_stream,
                stderr=subprocess.STDOUT,
                creationflags=0x08000000 if os.name == "nt" else 0,
                cwd=str(exe.parent),
            )
        except Exception as exc:
            log_stream.close()
            with self._thread_lock:
                self._state.update(phase="error", detail=f"启动模型服务失败：{exc}")
            return False

        _attach_to_job(proc)
        with self._thread_lock:
            if not self.is_current(generation):
                self._terminate_process(proc)
                log_stream.close()
                return False
            self._process = proc
            self._state["spawned"] = True

        try:
            deadline = time.monotonic() + 300
            while time.monotonic() < deadline and self.is_current(generation):
                # Use the local immutable process reference. Another coroutine
                # may clear self._process, but it can never turn proc into None.
                return_code = proc.poll()
                if return_code is not None:
                    with self._thread_lock:
                        if self.is_current(generation):
                            self._state.update(
                                phase="error",
                                detail=f"llama-server 异常退出（代码 {return_code}）",
                            )
                    return False
                if await health_check(2.0):
                    with self._thread_lock:
                        if self.is_current(generation):
                            self._state.update(phase="ready", detail="模型已就绪")
                            return True
                await asyncio.sleep(1.0)
            if self.is_current(generation):
                with self._thread_lock:
                    self._state.update(phase="error", detail="模型加载超时（300 秒）")
            return False
        finally:
            log_stream.close()

    @staticmethod
    def _terminate_process(proc: subprocess.Popen) -> None:
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                return

    async def stop_async(self) -> None:
        current = asyncio.current_task()
        pending = [task for task in self._tasks if task is not current and not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        self.new_generation()
        with self._thread_lock:
            proc = self._process
            self._process = None
            self._state.update(phase="idle", detail="", spawned=False)
        if proc is not None:
            await asyncio.to_thread(self._terminate_process, proc)

    def stop_sync(self) -> None:
        self.new_generation()
        with self._thread_lock:
            proc = self._process
            self._process = None
            self._state.update(phase="idle", detail="", spawned=False)
        if proc is not None:
            self._terminate_process(proc)


_supervisor = LlamaSupervisor()


async def ensure_started() -> None:
    await _supervisor.ensure_running()


async def ensure_running() -> bool:
    return await _supervisor.ensure_running()


def schedule_ensure_running() -> asyncio.Task:
    return _supervisor.schedule_ensure_running()


def reset_cooldown() -> None:
    _supervisor.reset_cooldown()


def stop() -> None:
    """Synchronous compatibility entry point for atexit and existing routes."""
    _supervisor.stop_sync()


async def stop_async() -> None:
    await _supervisor.stop_async()


def status() -> Dict[str, Any]:
    return _supervisor.status()

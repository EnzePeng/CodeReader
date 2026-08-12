"""配置加载与路径解析。

打包成 exe 后，配置文件 config.json 位于 exe 同级目录；
开发模式下位于项目根目录（backend/ 的上一级）。
"""
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict

APP_NAME = "CodeReader"
APP_VERSION = "1.0.0"

DEFAULTS: Dict[str, Any] = {
    "app_host": "127.0.0.1",
    "app_port": 8710,
    "llama": {
        "autostart": True,
        "server_exe": "llama/llama-server.exe",
        "model": "models/Qwen3.5-9B.Q4_K_M.gguf",
        "alias": "local-model",
        "base_url": "http://127.0.0.1:8711",
        "host": "127.0.0.1",
        "port": 8711,
        "ctx_size": 8192,
        "n_gpu_layers": 99,
        "cache_type_k": "q8_0",
        "cache_type_v": "q8_0",
        "temperature": 0.6,
        "top_p": 0.95,
        "top_k": 20,
        "thinking": False,
        "extra_args": [],
    },
    "explain": {
        "max_file_bytes": 2_000_000,
        "overview_max_chars": 12_000,
        "overview_max_tokens": 500,
        "segment_max_tokens": 900,
        "segment_max_tokens_detailed": 1600,
        "chat_max_tokens": 1200,
        "project_overview_context_chars": 5000,
        "project_segment_context_chars": 8000,
        "project_chat_context_chars": 12000,
        "chat_current_file_chars": 8000,
        "project_dependency_depth": 2,
    },
}


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """exe 同级目录（打包后）或项目根目录（开发时）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def static_dir() -> Path:
    """前端构建产物目录。打包后由 PyInstaller 释放到临时目录。"""
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")) / "static"
    return app_root() / "frontend" / "dist"


def data_dir() -> Path:
    d = app_root() / "data"
    d.mkdir(parents=True, exist_ok=True)
    return d


def resolve_path(p: str) -> Path:
    path = Path(p)
    if path.is_absolute():
        return path
    return app_root() / path


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


_config = None


def get_config() -> Dict[str, Any]:
    global _config
    if _config is None:
        cfg_file = app_root() / "config.json"
        user_cfg: Dict[str, Any] = {}
        if cfg_file.exists():
            try:
                user_cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
            except Exception as e:
                print(f"[config] 读取 {cfg_file} 失败，使用默认配置: {e}")
        _config = _deep_merge(DEFAULTS, user_cfg)
    return _config


def model_id() -> str:
    """用于缓存键的模型标识（模型文件名）。"""
    cfg = get_config()
    return Path(cfg["llama"]["model"]).name


def update_config_file(updater) -> None:
    """读取 config.json、应用修改并写回，同时使内存配置失效以便重新加载。"""
    global _config
    cfg_file = app_root() / "config.json"
    raw: Dict[str, Any] = {}
    if cfg_file.exists():
        try:
            raw = json.loads(cfg_file.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
    updater(raw)
    cfg_file.write_text(json.dumps(raw, ensure_ascii=False, indent=2),
                        encoding="utf-8")
    _config = None

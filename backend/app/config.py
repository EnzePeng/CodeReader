"""Validated, atomic application configuration.

The public ``get_config`` function intentionally keeps the legacy dictionary
shape while the process itself operates on validated Pydantic settings.
"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
import warnings
from pathlib import Path, PurePath
from typing import Any, Callable, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

APP_NAME = "CodeReader"
APP_VERSION = "2.0.0"


class LlamaSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    autostart: bool = True
    server_exe: str = "llama/llama-server.exe"
    model: str = "Qwen3.5-9B.Q4_K_M.gguf"
    alias: str = "local-model"
    base_url: str = "http://127.0.0.1:8711"
    host: str = "127.0.0.1"
    port: int = Field(8711, ge=1024, le=65535)
    ctx_size: int = Field(8192, ge=2048, le=262_144)
    n_gpu_layers: int = Field(99, ge=0, le=999)
    cache_type_k: str = "q8_0"
    cache_type_v: str = "q8_0"
    temperature: float = Field(0.2, ge=0.0, le=2.0)
    top_p: float = Field(0.9, gt=0.0, le=1.0)
    top_k: int = Field(20, ge=0, le=1000)
    thinking: bool = False
    thinking_extra_tokens: int = Field(1200, ge=0, le=8192)
    think_prefill: Literal["auto", "on", "off"] = "auto"
    protocol: Literal["chat_completions", "completion"] = "chat_completions"
    parallel: int = Field(1, ge=1, le=8)
    extra_args: List[str] = Field(default_factory=list)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("llama-server host must be 127.0.0.1")
        return value

    @field_validator("model")
    @classmethod
    def validate_model_basename(cls, value: str) -> str:
        # API/config accepts a name, never a path. This makes traversal and an
        # accidentally exposed arbitrary model path impossible by construction.
        if not value or PurePath(value).name != value or "/" in value or "\\" in value:
            raise ValueError("model must be a basename inside models/")
        if not value.lower().endswith(".gguf"):
            raise ValueError("model must be a .gguf file")
        return value

    @model_validator(mode="after")
    def validate_url(self) -> "LlamaSettings":
        expected = f"http://127.0.0.1:{self.port}"
        if self.base_url.rstrip("/") != expected:
            raise ValueError(f"llama.base_url must be {expected}")
        return self


class ExplainSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_file_bytes: int = Field(2_000_000, ge=1, le=100_000_000)
    overview_max_chars: int = Field(12_000, ge=1000, le=1_000_000)
    overview_max_tokens: int = Field(500, ge=64, le=8192)
    segment_max_tokens: int = Field(900, ge=64, le=16_384)
    segment_max_tokens_detailed: int = Field(1600, ge=64, le=32_768)
    chat_max_tokens: int = Field(1200, ge=64, le=32_768)
    project_overview_context_tokens: int = Field(1250, ge=128, le=65_536)
    project_segment_context_tokens: int = Field(2000, ge=128, le=65_536)
    project_chat_context_tokens: int = Field(3000, ge=128, le=65_536)
    chat_current_file_tokens: int = Field(2000, ge=128, le=65_536)
    project_dependency_depth: int = Field(2, ge=0, le=5)


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    app_host: str = "127.0.0.1"
    app_port: int = Field(8710, ge=1024, le=65535)
    llama: LlamaSettings = Field(default_factory=LlamaSettings)
    explain: ExplainSettings = Field(default_factory=ExplainSettings)

    @field_validator("app_host")
    @classmethod
    def validate_app_host(cls, value: str) -> str:
        if value != "127.0.0.1":
            raise ValueError("CodeReader only supports app_host 127.0.0.1")
        return value


def validate_bind_host(value: str) -> str:
    if value != "127.0.0.1":
        raise ValueError("CodeReader only supports binding to 127.0.0.1")
    return value


DEFAULTS: Dict[str, Any] = AppSettings().model_dump()


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def static_dir() -> Path:
    if is_frozen():
        return Path(getattr(sys, "_MEIPASS")) / "static"
    return app_root() / "frontend" / "dist"


def data_dir() -> Path:
    directory = app_root() / "data"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def resolve_path(value: str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else app_root() / path


def resolve_model_path(model_name: Optional[str] = None) -> Path:
    name = model_name or get_settings().llama.model
    # Revalidate values supplied by callers that did not pass through Settings.
    validated = LlamaSettings(model=name).model
    root = (app_root() / "models").resolve()
    candidate = (root / validated).resolve()
    candidate.relative_to(root)
    return candidate


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


_CHAR_TO_TOKEN = {
    "project_overview_context_chars": "project_overview_context_tokens",
    "project_segment_context_chars": "project_segment_context_tokens",
    "project_chat_context_chars": "project_chat_context_tokens",
    "chat_current_file_chars": "chat_current_file_tokens",
}


def _migrate_config(raw: Dict[str, Any]) -> Dict[str, Any]:
    migrated = copy.deepcopy(raw)
    llama = migrated.get("llama")
    if isinstance(llama, dict):
        model = llama.get("model")
        if isinstance(model, str) and model.replace("\\", "/").startswith("models/"):
            # One-time migration from the v1 relative path representation.
            tail = model.replace("\\", "/")[len("models/"):]
            if "/" not in tail:
                llama["model"] = tail
    explain = migrated.get("explain")
    if isinstance(explain, dict):
        used_deprecated = False
        for old, new in _CHAR_TO_TOKEN.items():
            if old in explain:
                if new not in explain:
                    explain[new] = max(128, int(explain[old]) // 4)
                explain.pop(old, None)
                used_deprecated = True
        if used_deprecated:
            warnings.warn(
                "*_context_chars is deprecated; migrated to token budgets",
                DeprecationWarning,
                stacklevel=2,
            )
    return migrated


def _read_user_config() -> Dict[str, Any]:
    path = app_root() / "config.json"
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid configuration file {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    return value


_settings: Optional[AppSettings] = None
_config_lock = threading.RLock()


def reset_config_cache() -> None:
    global _settings
    with _config_lock:
        _settings = None


def get_settings(*, force_reload: bool = False) -> AppSettings:
    global _settings
    with _config_lock:
        if _settings is None or force_reload:
            raw = _migrate_config(_read_user_config())
            _settings = AppSettings.model_validate(_deep_merge(DEFAULTS, raw))
        return _settings


def get_config() -> Dict[str, Any]:
    """Return a defensive dictionary for existing internal consumers."""
    result = get_settings().model_dump()
    # Transitional read-only aliases. New code must consume token budgets.
    explain = result["explain"]
    for old, new in _CHAR_TO_TOKEN.items():
        explain[old] = int(explain[new]) * 4
    return result


def model_id() -> str:
    return get_settings().llama.model


def update_config_file(updater: Callable[[Dict[str, Any]], None]) -> None:
    """Validate, fsync and atomically replace config.json under a process lock."""
    global _settings
    with _config_lock:
        path = app_root() / "config.json"
        raw = _read_user_config()
        updater(raw)
        migrated = _migrate_config(raw)
        validated = AppSettings.model_validate(_deep_merge(DEFAULTS, migrated))
        # Persist only the caller's migrated overrides, after proving the merged
        # result valid. This keeps the user-facing file compact and editable.
        encoded = json.dumps(migrated, ensure_ascii=False, indent=2) + "\n"
        temporary = path.with_name(path.name + ".tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _settings = validated

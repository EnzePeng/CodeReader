"""Hardware-aware, user-controlled llama.cpp tuning helpers."""
from __future__ import annotations

import ctypes
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .config import get_config, resolve_model_path

CACHE_TYPES = ("q4_0", "q8_0", "f16")
RESTART_FIELDS = {
    "ctx_size", "n_gpu_layers", "cache_type_k", "cache_type_v", "parallel",
}


class ModelTuningValues(BaseModel):
    """The small, intentionally curated set of settings exposed by the UI."""

    model_config = ConfigDict(extra="forbid")

    ctx_size: int = Field(ge=2048, le=262_144)
    n_gpu_layers: int = Field(ge=0, le=999)
    cache_type_k: Literal["q4_0", "q8_0", "f16"]
    cache_type_v: Literal["q4_0", "q8_0", "f16"]
    parallel: int = Field(ge=1, le=8)
    temperature: float = Field(ge=0.0, le=2.0)
    top_p: float = Field(gt=0.0, le=1.0)
    top_k: int = Field(ge=0, le=1000)
    thinking: bool

    @field_validator("ctx_size")
    @classmethod
    def context_multiple(cls, value: int) -> int:
        if value % 1024:
            raise ValueError("ctx_size must be a multiple of 1024")
        return value


class ModelRecommendation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    values: ModelTuningValues
    summary: str = Field(min_length=1, max_length=600)
    rationale: Dict[str, str] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list, max_length=6)
    confidence: Literal["low", "medium", "high"] = "medium"


def current_values() -> ModelTuningValues:
    cfg = get_config()["llama"]
    return ModelTuningValues.model_validate({
        "ctx_size": cfg["ctx_size"],
        "n_gpu_layers": cfg["n_gpu_layers"],
        "cache_type_k": cfg["cache_type_k"],
        "cache_type_v": cfg["cache_type_v"],
        "parallel": cfg["parallel"],
        "temperature": cfg["temperature"],
        "top_p": cfg["top_p"],
        "top_k": cfg["top_k"],
        "thinking": cfg.get("thinking", False),
    })


def _system_ram_gb() -> float | None:
    try:
        if os.name == "nt":
            class MemoryStatus(ctypes.Structure):
                _fields_ = [
                    ("length", ctypes.c_ulong),
                    ("memory_load", ctypes.c_ulong),
                    ("total_physical", ctypes.c_ulonglong),
                    ("available_physical", ctypes.c_ulonglong),
                    ("total_page_file", ctypes.c_ulonglong),
                    ("available_page_file", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong),
                    ("available_virtual", ctypes.c_ulonglong),
                    ("available_extended_virtual", ctypes.c_ulonglong),
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(MemoryStatus)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return round(status.total_physical / (1024 ** 3), 1)
        sysconf = getattr(os, "sysconf", None)
        if callable(sysconf):
            page_size = sysconf("SC_PAGE_SIZE")
            pages = sysconf("SC_PHYS_PAGES")
            return round(page_size * pages / (1024 ** 3), 1)
        return None
    except (AttributeError, OSError, ValueError):
        return None


def _nvidia_gpus() -> List[Dict[str, Any]]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,memory.total,memory.free",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
            creationflags=0x08000000 if os.name == "nt" else 0,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if completed.returncode != 0:
        return []
    result: List[Dict[str, Any]] = []
    for line in completed.stdout.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 3:
            continue
        try:
            result.append({
                "name": parts[0],
                "total_vram_gb": round(float(parts[1]) / 1024, 1),
                "free_vram_gb": round(float(parts[2]) / 1024, 1),
            })
        except ValueError:
            continue
    return result


def hardware_snapshot() -> Dict[str, Any]:
    """Return non-sensitive local capacity data used only for recommendation."""
    gpus = _nvidia_gpus()
    return {
        "platform": platform.system(),
        "cpu_logical_cores": os.cpu_count(),
        "system_ram_gb": _system_ram_gb(),
        "gpus": gpus,
        "gpu_note": "NVIDIA 显存来自 nvidia-smi" if gpus else (
            "未检测到 NVIDIA 显存信息；可能无独显、驱动不可用或使用其他品牌 GPU"
        ),
    }


def settings_payload() -> Dict[str, Any]:
    cfg = get_config()["llama"]
    try:
        model_path = resolve_model_path(str(cfg["model"]))
        model_size = round(model_path.stat().st_size / (1024 ** 3), 2)
    except (OSError, ValueError):
        model_size = None
    return {
        "model": cfg["model"],
        "model_size_gb": model_size,
        "current": current_values().model_dump(),
        "hardware": hardware_snapshot(),
        "restart_fields": sorted(RESTART_FIELDS),
        "cache_types": list(CACHE_TYPES),
    }


def recommendation_schema() -> Dict[str, Any]:
    value_properties: Dict[str, Any] = {
        "ctx_size": {"type": "integer", "minimum": 2048, "maximum": 262144, "multipleOf": 1024},
        "n_gpu_layers": {"type": "integer", "minimum": 0, "maximum": 999},
        "cache_type_k": {"type": "string", "enum": list(CACHE_TYPES)},
        "cache_type_v": {"type": "string", "enum": list(CACHE_TYPES)},
        "parallel": {"type": "integer", "minimum": 1, "maximum": 8},
        "temperature": {"type": "number", "minimum": 0, "maximum": 2},
        "top_p": {"type": "number", "exclusiveMinimum": 0, "maximum": 1},
        "top_k": {"type": "integer", "minimum": 0, "maximum": 1000},
        "thinking": {"type": "boolean"},
    }
    return {
        "type": "object",
        "properties": value_properties,
        "required": list(value_properties),
        "additionalProperties": False,
    }


def recommendation_messages(payload: Dict[str, Any]) -> List[Dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是本地 llama.cpp 参数调优助手。请只返回满足 JSON schema 的参数对象。"
                "目标是离线单用户代码阅读：优先稳定、避免显存溢出，其次扩大可用上下文。"
                "上下文必须为 1024 的倍数；不确定模型训练上下文时应保守。"
                "单用户通常 parallel=1。代码解释通常使用低温度。"
                "q4_0 KV 最省显存，q8_0 平衡，f16 最占显存。"
                "n_gpu_layers=999 表示尽可能全部卸载，0 表示 CPU。"
                "推荐只会展示给用户选择，不会自动生效。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请基于以下当前模型、默认启动参数和本机容量给出推荐：\n"
                + json.dumps(payload, ensure_ascii=False, sort_keys=True)
            ),
        },
    ]


def public_recommendation(
    value: Dict[str, Any], context: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    """Normalize both the concise live contract and the older rich contract."""
    if "values" in value:
        recommendation = ModelRecommendation.model_validate(value)
        payload = recommendation.model_dump()
    else:
        values = ModelTuningValues.model_validate(value)
        hardware = (context or {}).get("hardware") or {}
        gpus = hardware.get("gpus") or []
        gpu_text = "/".join(str(gpu.get("name") or "GPU") for gpu in gpus)
        model_name = str((context or {}).get("model") or "当前模型")
        rationales = {
            "ctx_size": f"模型建议使用 {values.ctx_size} token，在容量与显存占用之间取舍。",
            "n_gpu_layers": f"模型建议卸载 {values.n_gpu_layers} 层，以匹配当前 GPU 容量。",
            "cache_type_k": f"模型建议 K 缓存使用 {values.cache_type_k}。",
            "cache_type_v": f"模型建议 V 缓存使用 {values.cache_type_v}。",
            "parallel": f"模型建议保留 {values.parallel} 个并发槽位。",
            "temperature": f"模型建议温度为 {values.temperature}，兼顾稳定性与表达。",
            "top_p": f"模型建议 Top-P 为 {values.top_p}。",
            "top_k": f"模型建议 Top-K 为 {values.top_k}。",
            "thinking": "模型建议开启思考模式。" if values.thinking else "模型建议关闭思考模式以优先响应速度。",
        }
        warnings = ["更改上下文、GPU 卸载、缓存精度或并发后，模型需要重新加载。"]
        if not gpus:
            warnings.append("未检测到 NVIDIA 显存，GPU 相关建议的可信度较低。")
        payload = {
            "values": values.model_dump(),
            "summary": (
                f"{model_name} 已结合当前参数"
                + (f"与 {gpu_text} 的可用显存" if gpu_text else "与可检测到的系统资源")
                + "给出一组偏稳定的本地代码阅读配置。"
            ),
            "rationale": rationales,
            "warnings": warnings,
            "confidence": "high" if gpus else "low",
        }
    return {
        "source": "model",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        **payload,
    }

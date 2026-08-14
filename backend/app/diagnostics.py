"""Bounded, source-free local diagnostics for the troubleshooting page."""
from __future__ import annotations

import statistics
import threading
from collections import deque
from dataclasses import dataclass
from typing import Deque, Dict, Union


@dataclass(frozen=True)
class GenerationMetric:
    task: str
    queue_ms: float
    ttft_ms: float
    duration_ms: float
    output_tokens: int
    context_tokens: int
    cache_hit: bool


@dataclass(frozen=True)
class IndexMetric:
    duration_ms: float
    files: int
    failed: int


Number = Union[int, float]


class Diagnostics:
    def __init__(self, capacity: int = 500) -> None:
        if capacity < 1:
            raise ValueError("capacity must be positive")
        self._generation: Deque[GenerationMetric] = deque(maxlen=capacity)
        self._index: Deque[IndexMetric] = deque(maxlen=capacity)
        self._lock = threading.Lock()

    def record_generation(self, metric: GenerationMetric) -> None:
        with self._lock:
            self._generation.append(metric)

    def record_index(self, *, duration_ms: float, files: int, failed: int) -> None:
        with self._lock:
            self._index.append(IndexMetric(duration_ms, files, failed))

    @staticmethod
    def _median(items, field: str) -> float:
        values = [float(getattr(item, field)) for item in items]
        return round(statistics.median(values), 2) if values else 0.0

    def snapshot(self) -> Dict[str, Dict[str, Number]]:
        with self._lock:
            generation = tuple(self._generation)
            index = tuple(self._index)
        hits = sum(1 for metric in generation if metric.cache_hit)
        token_rates = [
            metric.output_tokens / (metric.duration_ms / 1000)
            for metric in generation
            if not metric.cache_hit and metric.duration_ms > 0
        ]
        return {
            "generation": {
                "count": len(generation),
                "cache_hit_rate": round(hits / len(generation), 4) if generation else 0.0,
                "median_queue_ms": self._median(generation, "queue_ms"),
                "median_ttft_ms": self._median(generation, "ttft_ms"),
                "median_duration_ms": self._median(generation, "duration_ms"),
                "median_context_tokens": self._median(generation, "context_tokens"),
                "median_output_tokens": self._median(generation, "output_tokens"),
                "median_tokens_per_second": (
                    round(statistics.median(token_rates), 2) if token_rates else 0.0
                ),
            },
            "index": {
                "count": len(index),
                "median_duration_ms": self._median(index, "duration_ms"),
                "files": sum(metric.files for metric in index),
                "failed": sum(metric.failed for metric in index),
            },
        }


diagnostics = Diagnostics()

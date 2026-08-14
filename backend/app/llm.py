"""Model backend abstraction backed by llama.cpp's native chat API."""
from __future__ import annotations

import asyncio
import json
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol

import httpx

from .config import get_config
from .diagnostics import GenerationMetric, diagnostics

_semaphore: Optional[asyncio.Semaphore] = None
_client: Optional[httpx.AsyncClient] = None


@dataclass(frozen=True)
class _Telemetry:
    task: str = "generation"
    context_tokens: int = 0


_telemetry: ContextVar[_Telemetry] = ContextVar(
    "codereader_generation_telemetry", default=_Telemetry())


class _GenerationProbe:
    def __init__(self) -> None:
        self.meta = _telemetry.get()
        self.queued_at = time.perf_counter()
        self.acquired_at = self.queued_at
        self.first_at: Optional[float] = None
        self.output_chars = 0
        self.finished = False

    def acquired(self) -> None:
        self.acquired_at = time.perf_counter()

    def piece(self, value: str) -> None:
        if self.first_at is None:
            self.first_at = time.perf_counter()
        self.output_chars += len(value)

    def finish(self) -> None:
        if self.finished:
            return
        self.finished = True
        now = time.perf_counter()
        first = self.first_at or now
        diagnostics.record_generation(GenerationMetric(
            task=self.meta.task,
            queue_ms=(self.acquired_at - self.queued_at) * 1000,
            ttft_ms=(first - self.acquired_at) * 1000,
            duration_ms=(now - self.acquired_at) * 1000,
            output_tokens=max(0, self.output_chars // 3),
            context_tokens=max(0, self.meta.context_tokens),
            cache_hit=False,
        ))


def record_cache_hit(task: str, context_tokens: int = 0) -> None:
    diagnostics.record_generation(GenerationMetric(
        task=task, queue_ms=0, ttft_ms=0, duration_ms=0,
        output_tokens=0, context_tokens=max(0, int(context_tokens)), cache_hit=True,
    ))


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        parallel = int(_llama_cfg().get("parallel", 1))
        _semaphore = asyncio.Semaphore(parallel)
    return _semaphore


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(600.0, connect=10.0),
            limits=httpx.Limits(max_connections=8, max_keepalive_connections=4),
        )
    return _client


async def close_client() -> None:
    global _client, _semaphore
    client, _client = _client, None
    _semaphore = None
    if client is not None:
        await client.aclose()


def _llama_cfg() -> Dict[str, Any]:
    return get_config()["llama"]


def _authorization_headers() -> Dict[str, str]:
    from .llama_launcher import api_key
    return {"Authorization": f"Bearer {api_key()}"}


async def health_check(timeout: float = 3.0) -> bool:
    cfg = _llama_cfg()
    try:
        response = await _get_client().get(
            cfg["base_url"].rstrip("/") + "/health",
            headers=_authorization_headers(),
            timeout=timeout,
        )
        return response.status_code == 200
    except (httpx.HTTPError, RuntimeError):
        return False


async def count_tokens(content: str) -> int:
    """Count tokens with the tokenizer loaded by the active llama-server."""
    if not content:
        return 0
    cfg = _llama_cfg()
    response = await _get_client().post(
        cfg["base_url"].rstrip("/") + "/tokenize",
        json={"content": content, "add_special": False},
        headers=_authorization_headers(),
    )
    if response.status_code != 200:
        raise RuntimeError(f"模型 tokenizer 返回 {response.status_code}")
    payload = response.json()
    tokens = payload.get("tokens")
    if isinstance(tokens, list):
        return len(tokens)
    count = payload.get("count")
    if isinstance(count, int) and count >= 0:
        return count
    raise RuntimeError("模型 tokenizer 返回了未知格式")


def is_thinking_model(cfg: Dict[str, Any]) -> bool:
    override = str(cfg.get("think_prefill", "auto")).lower()
    if override in ("on", "true"):
        return True
    if override in ("off", "false"):
        return False
    name = str(cfg.get("model", "")).lower()
    return any(marker in name for marker in ("qwen3", "qwq", "think", "-r1"))


def generation_profile(*, thinking: bool) -> Dict[str, Any]:
    """Stable task profiles, with no-thinking as the fast default."""
    if thinking:
        return {
            "enable_thinking": True,
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
        }
    return {
        "enable_thinking": False,
        "temperature": 0.2,
        "top_p": 0.8,
        "top_k": 20,
    }


@dataclass(frozen=True)
class ModelCapabilities:
    context_tokens: int
    template: str = "native"
    thinking: bool = True
    tools: bool = False
    structured_output: bool = True
    multimodal: bool = False


class ModelBackend(Protocol):
    async def stream(
        self, messages: List[Dict[str, str]], max_tokens: int,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[str]: ...


class LlamaCppBackend:
    @staticmethod
    def capabilities() -> ModelCapabilities:
        cfg = _llama_cfg()
        return ModelCapabilities(
            context_tokens=int(cfg["ctx_size"]),
            thinking=is_thinking_model(cfg),
        )

    async def stream(
        self, messages: List[Dict[str, str]], max_tokens: int,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[str]:
        cfg = _llama_cfg()
        thinking = bool(cfg.get("thinking", False)) and is_thinking_model(cfg)
        profile = generation_profile(thinking=thinking)
        if temperature is not None:
            profile["temperature"] = temperature
        budget = max_tokens
        if thinking:
            budget += int(cfg.get("thinking_extra_tokens", 1200))
        payload = {
            "model": cfg.get("alias", "local-model"),
            "messages": messages,
            "stream": True,
            "max_tokens": budget,
            "temperature": profile["temperature"],
            "top_p": profile["top_p"],
            "top_k": profile["top_k"],
            "chat_template_kwargs": {"enable_thinking": profile["enable_thinking"]},
            "cache_prompt": True,
        }
        url = cfg["base_url"].rstrip("/") + "/v1/chat/completions"
        probe = _GenerationProbe()
        try:
            async with _get_semaphore():
                probe.acquired()
                async with _get_client().stream(
                    "POST", url, json=payload, headers=_authorization_headers()
                ) as response:
                    if response.status_code != 200:
                        body = await response.aread()
                        detail = body.decode("utf-8", "ignore")[:300]
                        raise RuntimeError(f"模型服务返回 {response.status_code}: {detail}")
                    finished = False
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        raw = line[5:].strip()
                        if not raw or raw == "[DONE]":
                            continue
                        try:
                            event = json.loads(raw)
                            choice = event.get("choices", [])[0]
                        except (json.JSONDecodeError, IndexError, TypeError):
                            continue
                        delta = choice.get("delta") or {}
                        # reasoning_content is intentionally not rendered or cached.
                        content = delta.get("content")
                        if content:
                            piece = str(content)
                            probe.piece(piece)
                            yield piece
                        reason = choice.get("finish_reason")
                        if reason:
                            if reason not in ("stop", "length"):
                                raise RuntimeError(f"模型异常停止：{reason}")
                            finished = True
                    if not finished:
                        raise RuntimeError("模型流未返回合法 stop reason")
        finally:
            probe.finish()


class CompletionCompatibilityBackend:
    """Explicit compatibility fallback for old GGUF templates only."""

    @staticmethod
    def _prompt(messages: List[Dict[str, str]]) -> str:
        parts = [
            f"<|im_start|>{message['role']}\n{message['content']}<|im_end|>\n"
            for message in messages
        ]
        parts.append("<|im_start|>assistant\n")
        return "".join(parts)

    async def stream(
        self, messages: List[Dict[str, str]], max_tokens: int,
        temperature: Optional[float] = None,
    ) -> AsyncIterator[str]:
        cfg = _llama_cfg()
        profile = generation_profile(thinking=bool(cfg.get("thinking", False)))
        payload = {
            "prompt": self._prompt(messages),
            "stream": True,
            "temperature": temperature if temperature is not None else profile["temperature"],
            "top_p": profile["top_p"],
            "top_k": profile["top_k"],
            "n_predict": max_tokens,
            "stop": ["<|im_end|>"],
            "cache_prompt": True,
        }
        url = cfg["base_url"].rstrip("/") + "/completion"
        probe = _GenerationProbe()
        try:
            async with _get_semaphore():
                probe.acquired()
                async with _get_client().stream(
                    "POST", url, json=payload, headers=_authorization_headers()
                ) as response:
                    if response.status_code != 200:
                        raise RuntimeError(f"模型服务返回 {response.status_code}")
                    finished = False
                    async for line in response.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except json.JSONDecodeError:
                            continue
                        if event.get("content"):
                            piece = str(event["content"])
                            probe.piece(piece)
                            yield piece
                        if event.get("stop"):
                            finished = True
                            break
                    if not finished:
                        raise RuntimeError("兼容模型流未返回合法 stop reason")
        finally:
            probe.finish()


def get_backend() -> ModelBackend:
    if _llama_cfg().get("protocol") == "completion":
        return CompletionCompatibilityBackend()
    return LlamaCppBackend()


async def stream_chat(
    messages: List[Dict[str, str]],
    max_tokens: int = 1024,
    temperature: Optional[float] = None,
    *,
    task: str = "generation",
    context_tokens: int = 0,
) -> AsyncIterator[str]:
    token = _telemetry.set(_Telemetry(task=task, context_tokens=context_tokens))
    try:
        async for piece in get_backend().stream(messages, max_tokens, temperature):
            yield piece
    finally:
        _telemetry.reset(token)


async def complete(
    messages: List[Dict[str, str]],
    max_tokens: int = 1024,
    temperature: Optional[float] = None,
) -> str:
    parts = [
        piece async for piece in stream_chat(
            messages, max_tokens=max_tokens, temperature=temperature
        )
    ]
    result = "".join(parts).strip()
    if not result:
        raise RuntimeError("模型返回空内容")
    return result

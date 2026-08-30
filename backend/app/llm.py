"""Model backend abstraction backed by llama.cpp's native chat API."""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Dict, List, Optional, Protocol, Sequence

import httpx

from .config import get_config, resolve_model_path
from .diagnostics import GenerationMetric, diagnostics

_semaphore: Optional[asyncio.Semaphore] = None
_client: Optional[httpx.AsyncClient] = None
_tool_protocol_cache: Dict[str, str] = {}
_tool_protocol_latest: Dict[str, str] = {}


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
    _tool_protocol_cache.clear()
    _tool_protocol_latest.clear()


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


async def count_message_tokens(
    messages: Sequence[Dict[str, Any]],
    tools: Optional[Sequence[Dict[str, Any]]] = None,
) -> int:
    """Count the rendered chat request, including template and tool schema."""
    cfg = _llama_cfg()
    base_url = cfg["base_url"].rstrip("/")
    payload: Dict[str, Any] = {"messages": list(messages), "add_generation_prompt": True}
    if tools:
        payload["tools"] = list(tools)
    try:
        response = await _get_client().post(
            base_url + "/apply-template",
            json=payload,
            headers=_authorization_headers(),
            timeout=20.0,
        )
        if response.status_code == 200:
            value = response.json()
            prompt = value.get("prompt") or value.get("content")
            if isinstance(prompt, str):
                return await count_tokens(prompt)
    except (httpx.HTTPError, RuntimeError, ValueError):
        pass
    rendered = json.dumps(
        {"messages": list(messages), "tools": list(tools or ())},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return await count_tokens(rendered)


def _chat_payload(messages: Sequence[Dict[str, Any]], max_tokens: int) -> Dict[str, Any]:
    cfg = _llama_cfg()
    return {
        "model": cfg.get("alias", "local-model"),
        "messages": list(messages),
        "stream": False,
        "max_tokens": max(1, int(max_tokens)),
        "temperature": 0.0,
        "top_p": 0.8,
        "top_k": 20,
        "chat_template_kwargs": {"enable_thinking": False},
        "cache_prompt": True,
    }


async def _post_chat(payload: Dict[str, Any], timeout: float = 180.0) -> Dict[str, Any]:
    cfg = _llama_cfg()
    async with _get_semaphore():
        response = await _get_client().post(
            cfg["base_url"].rstrip("/") + "/v1/chat/completions",
            json=payload,
            headers=_authorization_headers(),
            timeout=timeout,
        )
    if response.status_code != 200:
        raise RuntimeError(f"模型服务返回 {response.status_code}: {response.text[:300]}")
    value = response.json()
    if not isinstance(value, dict):
        raise RuntimeError("模型服务返回了未知 JSON 格式")
    return value


def _message_from_completion(value: Dict[str, Any]) -> Dict[str, Any]:
    try:
        message = value["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("模型响应缺少 choices[0].message") from exc
    if not isinstance(message, dict):
        raise RuntimeError("模型响应 message 格式无效")
    return message


async def probe_tool_protocol(requested: str = "auto") -> str:
    """Probe and cache the native tool-call contract; safely fall back to JSON schema."""
    if requested == "json_schema" or _llama_cfg().get("protocol") == "completion":
        _tool_protocol_latest[requested] = "json_schema"
        return "json_schema"
    cfg = _llama_cfg()
    props_value: Dict[str, Any] = {}
    try:
        response = await _get_client().get(
            cfg["base_url"].rstrip("/") + "/props",
            headers=_authorization_headers(),
            timeout=5.0,
        )
        if response.status_code == 200 and isinstance(response.json(), dict):
            props_value = response.json()
        else:
            props_value = {"unavailable_status": response.status_code}
    except (httpx.HTTPError, RuntimeError, ValueError):
        props_value = {"unavailable": True}

    # Hash the GGUF metadata region plus file revision. A full multi-GB hash would
    # make startup impractical; the header contains the model/tensor metadata and
    # size/mtime prevents reusing a probe after an in-place model replacement.
    model_digest = hashlib.sha256(str(cfg.get("model", "")).encode()).hexdigest()
    try:
        model_path = resolve_model_path(str(cfg.get("model") or ""))
        stat = model_path.stat()
        digest = hashlib.sha256(
            f"{model_path.name}:{stat.st_size}:{stat.st_mtime_ns}".encode()
        )
        with model_path.open("rb") as stream:
            digest.update(stream.read(1_048_576))
            if stat.st_size > 1_114_112:
                stream.seek(-65_536, 2)
                digest.update(stream.read(65_536))
        model_digest = digest.hexdigest()
    except (OSError, ValueError):
        pass
    identity = hashlib.sha256(json.dumps(
        {
            "base_url": cfg.get("base_url"),
            "model_fingerprint": model_digest,
            # Includes llama.cpp build/version and chat-template data when exposed.
            "props": props_value,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()).hexdigest()
    key = f"{requested}|{identity}"
    if key in _tool_protocol_cache:
        result = _tool_protocol_cache[key]
        _tool_protocol_latest[requested] = result
        return result
    # `/props` contributes model/template/build identity, but absence of a
    # descriptive "tool" field is not proof that the active chat template cannot
    # emit tool_calls. The fixed contract request is the authoritative probe.
    candidate = requested in {"auto", "native"}
    result = "json_schema"
    if candidate:
        probe_tool = {
            "type": "function",
            "function": {
                "name": "codereader_contract_probe",
                "description": "Return the fixed value ok.",
                "parameters": {
                    "type": "object",
                    "properties": {"value": {"type": "string", "const": "ok"}},
                    "required": ["value"],
                    "additionalProperties": False,
                },
            },
        }
        try:
            payload = _chat_payload([
                {"role": "system", "content": "Call the supplied probe tool exactly once."},
                {"role": "user", "content": "Run the contract probe with value ok."},
            ], 48)
            payload.update({"tools": [probe_tool], "tool_choice": "required"})
            message = _message_from_completion(await _post_chat(payload, timeout=30.0))
            calls = message.get("tool_calls")
            if isinstance(calls, list) and calls:
                function = calls[0].get("function") if isinstance(calls[0], dict) else None
                arguments = function.get("arguments") if isinstance(function, dict) else None
                parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
                if function and function.get("name") == "codereader_contract_probe" and parsed == {"value": "ok"}:
                    result = "native"
        except (RuntimeError, httpx.HTTPError, ValueError, json.JSONDecodeError):
            result = "json_schema"
    _tool_protocol_cache[key] = result
    _tool_protocol_latest[requested] = result
    return result


def cached_tool_protocol(requested: str = "auto") -> str:
    cfg = _llama_cfg()
    if requested == "json_schema" or cfg.get("protocol") == "completion":
        return "json_schema"
    return _tool_protocol_latest.get(requested, "not_probed")


def mark_tool_protocol_degraded(requested: str) -> None:
    """Remember that this model/template cannot sustain structured planning."""
    if requested != "auto":
        return
    _tool_protocol_latest[requested] = "deterministic"
    prefix = f"{requested}|"
    for key in list(_tool_protocol_cache):
        if key.startswith(prefix):
            _tool_protocol_cache[key] = "deterministic"


async def structured_complete(
    messages: Sequence[Dict[str, Any]],
    schema: Dict[str, Any],
    max_tokens: int = 256,
) -> Dict[str, Any]:
    payload = _chat_payload(messages, max_tokens)
    payload["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": "codereader_decision", "strict": True, "schema": schema},
    }
    message = _message_from_completion(await _post_chat(payload))
    content = str(
        message.get("content") or message.get("reasoning_content") or ""
    ).strip()
    if content.startswith("```"):
        content = re.sub(r"^```(?:json)?\s*|\s*```$", "", content, flags=re.IGNORECASE)
    try:
        value = json.loads(content)
    except json.JSONDecodeError as first_error:
        # Some Qwen/llama.cpp combinations prepend a short reasoning sentence
        # even under response_format. Accept the first complete object, never a
        # repaired/truncated object whose tool arguments would be guesswork.
        decoder = json.JSONDecoder()
        value = None
        for position, character in enumerate(content):
            if character != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(content[position:])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                value = candidate
                break
        if value is None:
            raise first_error
    if not isinstance(value, dict):
        raise ValueError("structured model output must be an object")
    return value


async def native_tool_complete(
    messages: Sequence[Dict[str, Any]],
    tools: Sequence[Dict[str, Any]],
    max_tokens: int = 256,
) -> Dict[str, Any]:
    payload = _chat_payload(messages, max_tokens)
    payload.update({"tools": list(tools), "tool_choice": "auto", "parallel_tool_calls": True})
    return _message_from_completion(await _post_chat(payload))


def is_thinking_model(cfg: Dict[str, Any]) -> bool:
    override = str(cfg.get("think_prefill", "auto")).lower()
    if override in ("on", "true"):
        return True
    if override in ("off", "false"):
        return False
    name = str(cfg.get("model", "")).lower()
    return any(marker in name for marker in ("qwen3", "qwq", "think", "-r1"))


def generation_profile(*, thinking: bool) -> Dict[str, Any]:
    """Build the live sampling profile from user-confirmed configuration."""
    cfg = _llama_cfg()
    return {
        "enable_thinking": thinking,
        "temperature": float(cfg.get("temperature", 0.2)),
        "top_p": float(cfg.get("top_p", 0.9)),
        "top_k": int(cfg.get("top_k", 20)),
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
    def stream(
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

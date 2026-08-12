"""llama-server (OpenAI 兼容接口) 的流式客户端。

- 全局信号量保证同一时刻只有一个生成请求占用 GPU；
- ThinkFilter 兜底过滤 <think>...</think>（正常情况下已通过
  chat_template_kwargs 禁用思考模式）。
"""
import asyncio
import json
from typing import AsyncIterator, Dict, List, Optional

import httpx

from .config import get_config

# 同一时刻只允许一个生成请求占用 GPU。
# 注意：必须在运行中的事件循环里惰性创建（Python 3.9 的 asyncio 原语
# 在构造时就会绑定事件循环，模块导入时创建会绑到错误的循环上）。
_semaphore: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(1)
    return _semaphore

THINK_OPEN = "<think>"
THINK_CLOSE = "</think>"


class ThinkFilter:
    def __init__(self) -> None:
        self.buffer = ""
        self.mode = "detect"  # detect | think | pass

    def feed(self, chunk: str) -> str:
        if self.mode == "pass":
            return chunk
        self.buffer += chunk
        if self.mode == "detect":
            stripped = self.buffer.lstrip()
            if not stripped:
                return ""
            if len(stripped) < len(THINK_OPEN):
                if THINK_OPEN.startswith(stripped):
                    return ""
                self.mode = "pass"
                out, self.buffer = self.buffer, ""
                return out
            if stripped.startswith(THINK_OPEN):
                self.mode = "think"
            else:
                self.mode = "pass"
                out, self.buffer = self.buffer, ""
                return out
        if self.mode == "think":
            idx = self.buffer.find(THINK_CLOSE)
            if idx == -1:
                return ""
            out = self.buffer[idx + len(THINK_CLOSE):].lstrip("\n")
            self.buffer = ""
            self.mode = "pass"
            return out
        return ""

    def flush(self) -> str:
        if self.mode == "detect":
            out, self.buffer = self.buffer, ""
            return out
        return ""


def _llama_cfg() -> Dict:
    return get_config()["llama"]


async def health_check(timeout: float = 3.0) -> bool:
    cfg = _llama_cfg()
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(cfg["base_url"].rstrip("/") + "/health")
            return resp.status_code == 200
    except Exception:
        return False


def is_thinking_model(cfg: Dict) -> bool:
    """公开别名：当前模型是否为思考型（供 API 层与缓存键使用）。"""
    return _is_thinking_model(cfg)


def _is_thinking_model(cfg: Dict) -> bool:
    """判断当前模型是否为思考型（决定是否需要预填思考块）。

    可用 llama.think_prefill 配置强制指定："auto" | "on" | "off"。
    """
    mode = str(cfg.get("think_prefill", "auto")).lower()
    if mode in ("on", "true"):
        return True
    if mode in ("off", "false"):
        return False
    from pathlib import Path
    name = Path(cfg.get("model", "")).name.lower()
    return ("qwen3" in name) or ("qwq" in name) or ("think" in name) or ("-r1" in name)


def _build_chatml_prompt(messages: List[Dict[str, str]], thinking: bool,
                         thinking_model: bool) -> str:
    """手工构造 ChatML 提示词，绕过模型自带的 chat 模板。

    Qwen3.5 等思考型模型的模板会无条件在 assistant 开头插入 <think>，
    服务端的 --reasoning off / --reasoning-budget 0 对其均不生效。
    这里在 assistant 起始处预填一个已闭合的空思考块，从 token 层面跳过思考，
    这也是 Qwen 官方认可的 no-think 用法。
    非思考型模型（如 qwen2.5-coder）不注入任何 <think> 标签。
    """
    parts: List[str] = []
    for m in messages:
        parts.append("<|im_start|>" + m["role"] + "\n" + m["content"] + "<|im_end|>\n")
    parts.append("<|im_start|>assistant\n")
    if thinking_model:
        if thinking:
            parts.append("<think>\n")
        else:
            parts.append("<think>\n\n</think>\n\n")
    return "".join(parts)


async def stream_chat(
    messages: List[Dict[str, str]],
    max_tokens: int = 1024,
    temperature: Optional[float] = None,
) -> AsyncIterator[str]:
    """流式返回内容增量。调用方拿到的文本已过滤思考内容。"""
    cfg = _llama_cfg()
    thinking = bool(cfg.get("thinking", False))
    thinking_model = _is_thinking_model(cfg)
    if thinking_model and thinking:
        # 思考内容也占用生成配额，额外留出预算避免正文被截断
        max_tokens = max_tokens + int(cfg.get("thinking_extra_tokens", 1200))
    payload = {
        "prompt": _build_chatml_prompt(messages, thinking, thinking_model),
        "stream": True,
        "temperature": cfg["temperature"] if temperature is None else temperature,
        "top_p": cfg.get("top_p", 0.95),
        "top_k": cfg.get("top_k", 20),
        "n_predict": max_tokens,
        "stop": ["<|im_end|>"],
        "cache_prompt": True,
    }
    url = cfg["base_url"].rstrip("/") + "/completion"
    filt = ThinkFilter()
    if thinking_model and thinking:
        # 提示词已包含 <think>，让过滤器直接进入思考吞吐状态
        filt.feed(THINK_OPEN)
    async with _get_semaphore():
        timeout = httpx.Timeout(600.0, connect=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=payload) as resp:
                if resp.status_code != 200:
                    body = await resp.aread()
                    detail = body.decode("utf-8", "ignore")[:300]
                    raise RuntimeError(f"模型服务返回 {resp.status_code}: {detail}")
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if not data or data == "[DONE]":
                        continue
                    try:
                        obj = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    piece = obj.get("content")
                    if piece:
                        out = filt.feed(piece)
                        if out:
                            yield out
                    if obj.get("stop"):
                        break
    tail = filt.flush()
    if tail:
        yield tail


async def complete(
    messages: List[Dict[str, str]],
    max_tokens: int = 1024,
    temperature: Optional[float] = None,
) -> str:
    parts: List[str] = []
    async for piece in stream_chat(messages, max_tokens=max_tokens, temperature=temperature):
        parts.append(piece)
    return "".join(parts)

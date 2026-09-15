"""LLM layer: provider-neutral message types and the providers that speak them.

Model strings look like "<provider>:<model>", e.g. "groq:openai/gpt-oss-120b".
Every OpenAI-compatible host (Groq, Ollama, OpenRouter) shares one adapter;
Anthropic has its own. FakeProvider scripts replies so the rest of the system
can be tested with no network.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from sea.config import settings

# --------------------------------------------------------------------------- types


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]


@dataclass
class Message:
    role: str  # system | user | assistant | tool
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set on role="tool"
    raw: Any = None  # provider-specific assistant content (e.g. Anthropic thinking blocks)

    @staticmethod
    def system(text: str) -> Message:
        return Message("system", text)

    @staticmethod
    def user(text: str) -> Message:
        return Message("user", text)

    @staticmethod
    def tool(call_id: str, text: str) -> Message:
        return Message("tool", text, tool_call_id=call_id)

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "tool_calls": [tc.__dict__ for tc in self.tool_calls],
            "tool_call_id": self.tool_call_id,
            "raw": self.raw,
        }

    @staticmethod
    def from_dict(d: dict[str, Any]) -> Message:
        return Message(
            role=d["role"],
            content=d.get("content", ""),
            tool_calls=[ToolCall(**tc) for tc in d.get("tool_calls", [])],
            tool_call_id=d.get("tool_call_id"),
            raw=d.get("raw"),
        )


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class Completion:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    raw: Any = None

    def as_message(self) -> Message:
        return Message("assistant", self.text, tool_calls=self.tool_calls, raw=self.raw)


class MalformedToolCall(Exception):
    """The model produced a tool call the provider could not parse. Retryable by telling the model."""


class RequestTooLarge(Exception):
    """The conversation exceeds what the provider accepts in one request. The loop compacts and retries."""


class Provider(Protocol):
    model: str

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec] | None = None, json_mode: bool = False
    ) -> Completion: ...


# --------------------------------------------------------------------------- concurrency

# One semaphore per event loop so tests (which create fresh loops) don't share state.
_semaphores: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}


def _limiter() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    if loop not in _semaphores:
        _semaphores[loop] = asyncio.Semaphore(settings.max_concurrent_llm_calls)
    return _semaphores[loop]


# --------------------------------------------------------------------------- OpenAI-compatible

_BASE_URLS = {
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    "ollama": ("http://localhost:11434/v1", None),
    "openai": (None, "OPENAI_API_KEY"),
}


class OpenAICompatProvider:
    """Groq, OpenRouter, Ollama, OpenAI - anything that speaks chat.completions."""

    def __init__(self, provider: str, model: str):
        from openai import AsyncOpenAI

        base_url, key_env = _BASE_URLS[provider]
        api_key = os.environ.get(key_env, "") if key_env else "ollama"
        if key_env and not api_key:
            raise RuntimeError(f"{key_env} is not set (needed for provider '{provider}')")
        self.model = model
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, max_retries=4)

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec] | None = None, json_mode: bool = False
    ) -> Completion:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [self._to_openai(m) for m in messages],
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {"name": t.name, "description": t.description, "parameters": t.input_schema},
                }
                for t in tools
            ]
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        from openai import APIStatusError, BadRequestError

        try:
            async with _limiter():
                resp = await self.client.chat.completions.create(**kwargs)
        except BadRequestError as e:
            # Groq reports unparsable model output as a 400 (tool_use_failed / output_parse_failed).
            # That's the model's fault, not the request's: let the loop tell it to try again.
            if any(k in str(e) for k in ("tool_use_failed", "output_parse_failed", "failed_generation")):
                raise MalformedToolCall(str(e)[:500]) from e
            raise
        except APIStatusError as e:
            if e.status_code == 413 or "Request too large" in str(e):
                raise RequestTooLarge(str(e)[:300]) from e
            raise

        choice = resp.choices[0].message
        calls = [
            ToolCall(id=tc.id, name=tc.function.name, arguments=_parse_args(tc.function.arguments))
            for tc in (choice.tool_calls or [])
        ]
        usage = Usage(resp.usage.prompt_tokens, resp.usage.completion_tokens) if resp.usage else Usage()
        return Completion(text=choice.content or "", tool_calls=calls, usage=usage)

    @staticmethod
    def _to_openai(m: Message) -> dict[str, Any]:
        if m.role == "tool":
            return {"role": "tool", "tool_call_id": m.tool_call_id, "content": m.content}
        if m.role == "assistant" and m.tool_calls:
            return {
                "role": "assistant",
                "content": m.content or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
                    }
                    for tc in m.tool_calls
                ],
            }
        return {"role": m.role, "content": m.content}


def _parse_args(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {"value": parsed}
    except json.JSONDecodeError:
        return {"_raw": raw}


# --------------------------------------------------------------------------- Anthropic (optional)


class AnthropicProvider:
    """Claude via the official SDK. Installed with `uv sync --extra anthropic`."""

    def __init__(self, model: str):
        from anthropic import AsyncAnthropic

        self.model = model
        self.client = AsyncAnthropic(max_retries=4)

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec] | None = None, json_mode: bool = False
    ) -> Completion:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        if json_mode:
            system += "\n\nRespond with a single JSON object and nothing else."
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "thinking": {"type": "adaptive"},
            "messages": self._to_anthropic(messages),
        }
        if system:
            kwargs["system"] = system
        if tools:
            kwargs["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.input_schema} for t in tools
            ]

        async with _limiter():
            async with self.client.messages.stream(**kwargs) as stream:
                resp = await stream.get_final_message()

        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, arguments=dict(b.input))
            for b in resp.content
            if b.type == "tool_use"
        ]
        raw = [b.model_dump() for b in resp.content]
        return Completion(
            text=text,
            tool_calls=calls,
            usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens),
            raw=raw,
        )

    @staticmethod
    def _to_anthropic(messages: list[Message]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.role == "system":
                continue
            if m.role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                # Consecutive tool results must live in one user message.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
            elif m.role == "assistant":
                if m.raw:
                    content: Any = m.raw  # replay thinking/tool_use blocks unchanged
                elif m.tool_calls:
                    content = [{"type": "text", "text": m.content}] if m.content else []
                    content += [
                        {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                        for tc in m.tool_calls
                    ]
                else:
                    content = m.content
                out.append({"role": "assistant", "content": content})
            else:
                out.append({"role": "user", "content": m.content})
        return out


# --------------------------------------------------------------------------- fake (tests)


class FakeProvider:
    """Replays scripted completions in order. Records every request it receives."""

    def __init__(self, script: list[Completion | str], model: str = "fake:test"):
        self.model = model
        self.script = [Completion(text=s) if isinstance(s, str) else s for s in script]
        self.requests: list[dict[str, Any]] = []

    async def complete(
        self, messages: list[Message], tools: list[ToolSpec] | None = None, json_mode: bool = False
    ) -> Completion:
        self.requests.append({"messages": list(messages), "tools": tools or [], "json_mode": json_mode})
        if not self.script:
            raise RuntimeError("FakeProvider script exhausted")
        return self.script.pop(0)


def tool_call(name: str, arguments: dict[str, Any] | None = None, call_id: str | None = None) -> Completion:
    """Shortcut for building scripted tool-call completions in tests."""
    call_id = call_id or f"call_{name}_{abs(hash(json.dumps(arguments, sort_keys=True))) % 10_000}"
    return Completion(text="", tool_calls=[ToolCall(id=call_id, name=name, arguments=arguments or {})])


# --------------------------------------------------------------------------- factory

_cache: dict[str, Provider] = {}


def get_provider(model_string: str) -> Provider:
    """'groq:llama-3.3-70b-versatile' -> provider instance (cached per model string)."""
    if model_string in _cache:
        return _cache[model_string]
    provider, _, model = model_string.partition(":")
    if not model:
        raise ValueError(f"Model string must be '<provider>:<model>', got {model_string!r}")
    if provider == "anthropic":
        inst: Provider = AnthropicProvider(model)
    elif provider in _BASE_URLS:
        inst = OpenAICompatProvider(provider, model)
    else:
        raise ValueError(f"Unknown provider {provider!r}. Known: anthropic, {', '.join(_BASE_URLS)}")
    _cache[model_string] = inst
    return inst

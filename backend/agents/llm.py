"""One chat function for every model backend the agents use.

Providers:
  openrouter  OpenAI-compatible API; lets us use cheap or free models.
  anthropic   Anthropic SDK.
  gemini      google-genai (checker only).

Tools are described once in a neutral shape ({name, description, input_schema})
and converted per provider. Tool calls are executed by the caller's handler,
which is where access control happens; this module never touches GitHub.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.config import settings

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ToolHandler = Callable[[str, dict[str, Any]], Awaitable[str]]


class LLMError(Exception):
    pass


@dataclass
class ChatResult:
    text: str
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    calls: int = 0
    cached_calls: int = 0
    notes: list[str] = field(default_factory=list)

    def usage(self) -> dict[str, Any]:
        return {
            "model": self.model, "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
            "cost_usd": round(self.cost_usd, 6), "calls": self.calls, "cached_calls": self.cached_calls,
            "notes": self.notes,
        }


# ------------------------------------------------------------------ cache

def _cache_path(body: dict[str, Any]):
    digest = hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()
    return settings.runs_dir / "llm_cache" / f"{digest}.json"


def _cache_get(body: dict[str, Any]) -> dict[str, Any] | None:
    if not settings.llm_cache:
        return None
    path = _cache_path(body)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def _cache_put(body: dict[str, Any], data: dict[str, Any]) -> None:
    if not settings.llm_cache:
        return
    path = _cache_path(body)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")


# ------------------------------------------------------------- openrouter

def _error_text(data: Any) -> str:
    if isinstance(data, dict) and data.get("error"):
        err = data["error"]
        return str(err.get("message") if isinstance(err, dict) else err)
    return ""


async def _openrouter_request(body: dict[str, Any], result: ChatResult) -> dict[str, Any]:
    cached = _cache_get(body)
    if cached is not None:
        result.calls += 1
        result.cached_calls += 1
        return cached

    headers = {"Authorization": f"Bearer {settings.openrouter_api_key}", "X-Title": "PRGuard"}
    last = ""
    async with httpx.AsyncClient(timeout=httpx.Timeout(40, connect=10)) as client:
        for attempt in range(3):
            try:
                response = await client.post(OPENROUTER_URL, json=body, headers=headers)
            except httpx.HTTPError as exc:
                last = f"network error: {type(exc).__name__}"
                await asyncio.sleep(1.5 * (attempt + 1))
                continue
            try:
                data = response.json()
            except ValueError:
                data = {}
            error = _error_text(data)
            if response.status_code == 200 and not error and data.get("choices"):
                result.calls += 1
                usage = data.get("usage") or {}
                result.prompt_tokens += int(usage.get("prompt_tokens") or 0)
                result.completion_tokens += int(usage.get("completion_tokens") or 0)
                result.cost_usd += float(usage.get("cost") or 0)
                _cache_put(body, data)
                return data
            last = f"HTTP {response.status_code}: {error or response.text[:200]}"
            if response.status_code in (429, 500, 502, 503, 529) or "rate" in error.lower():
                retry_after = response.headers.get("Retry-After", "")
                await asyncio.sleep(min(float(retry_after) if retry_after.isdigit() else 2.5 * (attempt + 1), 12))
                continue
            break
    raise LLMError(last)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") for part in content if isinstance(part, dict))
    return ""


async def _openrouter(model: str, system: str, user: str, tools: list[dict[str, Any]] | None,
                      on_tool: ToolHandler | None, max_tool_turns: int, max_tokens: int) -> ChatResult:
    result = ChatResult(text="")
    models = [m.strip() for m in model.split(",") if m.strip()][:3]
    # Low reasoning effort: cheap models otherwise spend the whole budget thinking.
    base: dict[str, Any] = {"max_tokens": max_tokens, "temperature": 0, "reasoning": {"effort": "low"}}
    # A comma-separated LLM_MODEL becomes an OpenRouter fallback chain.
    base.update({"models": models} if len(models) > 1 else {"model": models[0]})
    if settings.openrouter_sort:
        # Each model is served by several providers of very different speed.
        base["provider"] = {"sort": settings.openrouter_sort}
    messages: list[dict[str, Any]] = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    wire_tools = [
        {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["input_schema"]}}
        for t in tools or []
    ]

    for turn in range(max_tool_turns + 1):
        body = {**base, "messages": messages}
        if wire_tools and on_tool and turn < max_tool_turns:
            body["tools"] = wire_tools
        try:
            data = await _openrouter_request(body, result)
        except LLMError as exc:
            if "tools" in body and "tool" in str(exc).lower():
                # This model (or its free endpoint) cannot do tool calls: carry on without them.
                result.notes.append("model does not support tools; ran without them")
                wire_tools = []
                body.pop("tools")
                data = await _openrouter_request(body, result)
            else:
                raise
        result.model = data.get("model") or result.model
        message = data["choices"][0].get("message") or {}
        calls = message.get("tool_calls") or []
        if not calls or not on_tool:
            result.text = _text_of(message.get("content"))
            return result
        messages.append({"role": "assistant", "content": message.get("content") or "", "tool_calls": calls})
        for call in calls:
            function = call.get("function") or {}
            try:
                arguments = json.loads(function.get("arguments") or "{}")
            except ValueError:
                arguments = {}
            output = await on_tool(str(function.get("name", "")), arguments if isinstance(arguments, dict) else {})
            messages.append({"role": "tool", "tool_call_id": call.get("id", ""), "content": output})
    return result


# -------------------------------------------------------------- anthropic

async def _anthropic(model: str, system: str, user: str, tools: list[dict[str, Any]] | None,
                     on_tool: ToolHandler | None, max_tool_turns: int, max_tokens: int) -> ChatResult:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    result = ChatResult(text="", model=model)
    messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
    for turn in range(max_tool_turns + 1):
        use_tools = bool(tools and on_tool and turn < max_tool_turns)
        response = await client.messages.create(
            model=model, max_tokens=max_tokens, system=system, messages=messages, tools=tools if use_tools else [],
        )
        result.calls += 1
        result.prompt_tokens += response.usage.input_tokens
        result.completion_tokens += response.usage.output_tokens
        uses = [block for block in response.content if block.type == "tool_use"]
        if not uses or not on_tool:
            result.text = "".join(block.text for block in response.content if block.type == "text")
            return result
        messages.append({"role": "assistant", "content": response.content})
        messages.append({"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": use.id, "content": await on_tool(use.name, dict(use.input))}
            for use in uses
        ]})
    return result


# ----------------------------------------------------------------- gemini

async def _gemini(model: str, system: str, user: str, max_tokens: int) -> ChatResult:
    from google import genai

    client = genai.Client(api_key=settings.gemini_api_key)
    response = await client.aio.models.generate_content(
        model=model, contents=f"{system}\n\n{user}",
        config={"response_mime_type": "application/json", "max_output_tokens": max_tokens},
    )
    return ChatResult(text=response.text or "", model=model, calls=1)


async def chat(provider: str, model: str, system: str, user: str, *, tools: list[dict[str, Any]] | None = None,
               on_tool: ToolHandler | None = None, max_tool_turns: int = 3, max_tokens: int = 6000) -> ChatResult:
    try:
        if provider == "openrouter":
            return await _openrouter(model, system, user, tools, on_tool, max_tool_turns, max_tokens)
        if provider == "anthropic":
            return await _anthropic(model, system, user, tools, on_tool, max_tool_turns, max_tokens)
        if provider == "gemini":
            return await _gemini(model, system, user, max_tokens)
    except LLMError:
        raise
    except Exception as exc:
        raise LLMError(f"{type(exc).__name__}: {str(exc)[:200]}") from exc
    raise LLMError(f"unknown LLM provider {provider!r}")


def family(provider: str, model: str) -> str:
    """Vendor family of a model id, used to check that maker and checker differ."""
    first = model.split(",")[0].strip().lower().lstrip("~")
    if "/" in first:
        return first.split("/")[0]
    return {"anthropic": "anthropic", "gemini": "google"}.get(provider, first.split("-")[0])

"""ZenProvider — OpenCode Zen gateway via the openai SDK (plan Mk10).

Zen serves two APIs behind one base URL (`https://opencode.ai/zen/v1`):
- chat-family (`/chat/completions`): big-pickle, mimo/ling/nemotron `-free`,
  deepseek/glm/kimi/minimax — driven by `client.chat.completions.create`.
- responses-family (`/responses`): `muse-spark-*` (incl. the free
  `muse-spark-1.3-contributor-free` default), `gpt-*`, `grok-*` — driven by
  `client.responses.create`.

The dispatcher routes by model id so free IDs work without the caller caring
which endpoint they live on; unknown future IDs are allowed (responses for the
known responses prefixes, chat otherwise) rather than rejected.
"""

import json
from collections.abc import Sequence
from typing import Any

from app.llm.base import (
    LLMResponse,
    Message,
    ToolCall,
    ToolSchema,
    classify_llm_error,
    strip_routing_prefix,
)
from app.llm.openai_provider import OpenAIProvider

BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "muse-spark-1.3-contributor-free"

ZEN_FREE_MODELS = (
    "muse-spark-1.3-contributor-free",
    "muse-spark-1.2-contributor-free",
    "big-pickle",
    "mimo-v2.5-free",
    "ling-3.0-flash-fin-free",
    "nemotron-3-ultra-free",
    "nemotron-3.5-lightning-free",
    "deepseek-v4-flash-free",
)

# Model prefixes served on the Responses endpoint (per Zen docs endpoint table).
# Everything else goes to chat-completions.
RESPONSE_PREFIXES = ("muse-spark", "gpt-", "grok-")


def is_responses_model(model: str) -> bool:
    name = (model or "").strip().lower()
    return name.startswith(RESPONSE_PREFIXES)


def _unpack(m: Message | dict[str, Any]) -> tuple[str, str | None, list, str]:
    if isinstance(m, Message):
        return m.role, m.content, list(m.tool_calls or []), str(getattr(m, "name", "") or "")
    return (
        str(m.get("role", "")),
        m.get("content"),
        list(m.get("tool_calls") or []),
        str(m.get("name", "") or ""),
    )


def _format_responses_input(
    messages: Sequence[Message | dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Harness conversation -> (instructions, Responses `input` items).

    History function calls are replayed as `function_call` /
    `function_call_output` items with synthesized call ids (the harness stores
    calls as `{"name", "args"}` without wire ids). Unanswered calls are dropped
    so a guardrail-truncated run cannot poison the next turn.
    """
    instructions: str | None = None
    items: list[dict[str, Any]] = []
    pending: list[tuple[str, str]] = []
    counter = 0

    def _next_id() -> str:
        nonlocal counter
        counter += 1
        return f"zen_call_{counter}"

    for m in messages:
        role, content, tool_calls, name = _unpack(m)
        text = str(content) if content else ""

        if role == "system":
            if text:
                instructions = f"{instructions}\n{text}" if instructions else text
            continue

        if role == "assistant" and tool_calls:
            if text:
                items.append({"role": "assistant", "content": text})
            for tc in tool_calls:
                if isinstance(tc, dict) and tc.get("type") in ("tool_result", "function_call_output"):
                    continue
                if isinstance(tc, dict):
                    tc_name = str(tc.get("name", ""))
                    tc_args = tc.get("args") or tc.get("input") or tc.get("arguments") or {}
                else:
                    tc_name = str(getattr(tc, "name", ""))
                    tc_args = getattr(tc, "args", {}) or {}
                call_id = str(tc.get("id") or tc.get("call_id") or _next_id()) if isinstance(tc, dict) else _next_id()
                items.append({
                    "type": "function_call",
                    "call_id": call_id,
                    "name": tc_name,
                    "arguments": tc_args if isinstance(tc_args, str) else json.dumps(tc_args),
                })
                pending.append((tc_name, call_id))
            continue

        if role == "tool":
            call_id: str | None = None
            for i, (call_name, cid) in enumerate(pending):
                if not name or call_name == name:
                    call_id = pending.pop(i)[1]
                    break
            if call_id is None:
                items.append({"role": "user", "content": f"[tool result] {name}: {text}"})
            else:
                items.append({"type": "function_call_output", "call_id": call_id, "output": text})
            continue

        if text:
            items.append({"role": "assistant" if role == "assistant" else "user", "content": text})

    answered = {it.get("call_id") for it in items if it.get("type") == "function_call_output"}
    items = [it for it in items if it.get("type") != "function_call" or it.get("call_id") in answered]
    return instructions, items


def _format_responses_tools(
    tools: Sequence[ToolSchema | dict[str, Any]] | None,
) -> list[dict[str, Any]] | None:
    if not tools:
        return None
    out: list[dict[str, Any]] = []
    for t in tools:
        if isinstance(t, ToolSchema):
            out.append({
                "type": "function",
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
            })
        else:
            fn = t.get("function", t) if isinstance(t, dict) else {}
            out.append({
                "type": "function",
                "name": fn.get("name"),
                "description": fn.get("description"),
                "parameters": fn.get("parameters", {}),
            })
    return out or None


def _parse_responses_output(response: Any) -> tuple[str, list[ToolCall]]:
    text = str(getattr(response, "output_text", "") or "")
    tool_calls: list[ToolCall] = []
    for item in getattr(response, "output", None) or []:
        if isinstance(item, dict):
            item_type = item.get("type")
            if item_type != "function_call":
                if not text and item.get("type") in ("message", "text"):
                    text += str(item.get("content") or item.get("text") or "")
                continue
            name = str(item.get("name", ""))
            raw_args = item.get("arguments") or "{}"
        else:
            if getattr(item, "type", "") != "function_call":
                continue
            name = str(getattr(item, "name", ""))
            raw_args = getattr(item, "arguments", None) or "{}"
        try:
            args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
        except (json.JSONDecodeError, TypeError, ValueError):
            args = {}
        tool_calls.append(ToolCall(name=name, args=args))
    return text, tool_calls


class ZenProvider(OpenAIProvider):
    """Zen gateway dispatcher: chat-completions vs Responses by model id."""

    PROVIDER_NAME = "zen"
    DEFAULT_MODEL = DEFAULT_MODEL
    ROUTING_PREFIXES = ("zen", "opencode")
    BASE_URL = BASE_URL

    def __init__(self, api_key: str, base_url: str | None = None, **kwargs: Any):
        super().__init__(api_key=api_key, base_url=base_url or self.BASE_URL, **kwargs)

    def chat(
        self,
        messages: Sequence[Message | dict[str, Any]],
        tools: Sequence[ToolSchema | dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        model_name = strip_routing_prefix(model or self.model, *self.ROUTING_PREFIXES)
        if is_responses_model(model_name):
            return self._responses_chat(messages, tools, model_name, temperature, max_tokens, top_p, **kwargs)
        return super().chat(
            messages, tools, model=model_name, temperature=temperature,
            max_tokens=max_tokens, top_p=top_p, **kwargs,
        )

    def _responses_chat(
        self,
        messages: Sequence[Message | dict[str, Any]],
        tools: Sequence[ToolSchema | dict[str, Any]] | None,
        model_name: str,
        temperature: float | None,
        max_tokens: int | None,
        top_p: float | None,
        **kwargs: Any,
    ) -> LLMResponse:
        if temperature is None:
            temperature = self.temperature
        if max_tokens is None:
            max_tokens = self.max_tokens
        instructions, input_items = _format_responses_input(messages)
        if not input_items and not instructions:
            raise ValueError(
                "LLM provider input is required: at least one user/assistant message "
                f"with content needed. Got model={model_name}, provider=zen."
            )
        params: dict[str, Any] = {
            "model": model_name,
            "input": input_items or [{"role": "user", "content": "..."}],
        }
        if instructions:
            params["instructions"] = instructions
        if temperature is not None:
            params["temperature"] = temperature
        if max_tokens is not None:
            params["max_output_tokens"] = max_tokens
        if top_p is not None:
            params["top_p"] = top_p
        formatted_tools = _format_responses_tools(tools)
        if formatted_tools:
            params["tools"] = formatted_tools
        try:
            response = self.client.responses.create(**params, **kwargs)
        except Exception as exc:
            if "tools" in params:
                from app.llm.base import tool_unsupported_error as _tool_err

                tool_err = _tool_err(exc, provider=self.PROVIDER_NAME, model=model_name)
                if tool_err:
                    raise RuntimeError(tool_err) from exc
            raise RuntimeError(
                classify_llm_error(exc, provider=self.PROVIDER_NAME, model=model_name)
            ) from exc
        text, tool_calls = _parse_responses_output(response)
        usage_dict = None
        usage = getattr(response, "usage", None)
        if usage is not None:
            prompt = getattr(usage, "input_tokens", 0)
            completion = getattr(usage, "output_tokens", 0)
            usage_dict = {
                "prompt_tokens": prompt,
                "completion_tokens": completion,
                "total_tokens": getattr(usage, "total_tokens", (prompt or 0) + (completion or 0)),
            }
        return LLMResponse(
            model=model_name,
            provider=self.PROVIDER_NAME,
            text=text,
            content=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            finish_reason=None,
        )

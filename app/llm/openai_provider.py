"""OpenAIProvider using the official openai Python SDK."""

import json
from collections.abc import Sequence
from typing import Any

from openai import OpenAI

from app.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
    ToolSchema,
    classify_llm_error,
    strip_routing_prefix,
    tool_unsupported_error,
)


def _unpack(m: Message | dict[str, Any]) -> tuple[str, str, list[dict[str, Any]], str | None]:
    """(role, content, tool_calls, tool_call_id) from either message shape."""
    if isinstance(m, Message):
        return m.role, m.content or "", list(m.tool_calls or []), m.tool_call_id
    return (
        str(m.get("role", "")),
        str(m.get("content") or ""),
        list(m.get("tool_calls") or []),
        m.get("tool_call_id"),
    )


def _wire_tool_calls(
    raw_calls: list[dict[str, Any]], index: int
) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Harness tool calls -> OpenAI `tool_calls`, plus the (name, id) pairs made.

    The harness records a call as `{"name": ..., "args": {...}}`; the API needs
    `{"id", "type", "function": {"name", "arguments": <json string>}}`. Ids are
    synthesized deterministically from the message position so the matching tool
    result can reference one.
    """
    calls: list[dict[str, Any]] = []
    made: list[tuple[str, str]] = []
    for j, tc in enumerate(raw_calls):
        if isinstance(tc, dict) and "function" in tc:  # already wire-shaped
            calls.append(tc)
            made.append((str(tc.get("function", {}).get("name", "")), str(tc.get("id", ""))))
            continue
        name = str(tc.get("name", ""))
        call_id = f"call_{index}_{j}"
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(tc.get("args") or {})},
            }
        )
        made.append((name, call_id))
    return calls, made


def _format_messages(messages: Sequence[Message | dict[str, Any]]) -> list[dict[str, Any]]:
    """Harness conversation -> OpenAI chat-completions messages.

    The harness speaks its own compact shape. Sending it raw is a 400: a `tool`
    message without `tool_call_id` is rejected ("must be a response to a preceding
    message with tool_calls"), so every multi-step run — the whole point of the
    harness — failed on OpenAI-compatible providers while single-turn chat worked.
    """
    out: list[dict[str, Any]] = []
    pending: list[tuple[str, str]] = []  # (tool name, call id) awaiting its result
    for idx, m in enumerate(messages):
        role, content, tool_calls, tool_call_id = _unpack(m)

        if role == "assistant" and tool_calls:
            calls, made = _wire_tool_calls(tool_calls, idx)
            pending.extend(made)
            out.append({"role": "assistant", "content": content, "tool_calls": calls})
            continue

        if role == "tool":
            name = str(getattr(m, "name", "") if isinstance(m, Message) else m.get("name", ""))
            call_id = tool_call_id or _take_pending(pending, name)
            if call_id is None:
                # Nothing to attach to (e.g. a replayed transcript): send it as
                # context rather than letting the API reject the whole request.
                out.append({"role": "user", "content": f"[tool result] {name}: {content}"})
            else:
                out.append({"role": "tool", "tool_call_id": call_id, "content": content})
            continue

        out.append({"role": role, "content": content})
    return _drop_unanswered_calls(out)


def _drop_unanswered_calls(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove tool calls that never got a result.

    "An assistant message with 'tool_calls' must be followed by tool messages
    responding to each tool_call_id" — a guardrail that ends a run mid-batch
    (budget/max-steps) leaves exactly that gap in a thread's history, and the next
    turn on that thread would be rejected.
    """
    answered = {m.get("tool_call_id") for m in out if m.get("role") == "tool"}
    for m in out:
        if m.get("role") != "assistant" or not m.get("tool_calls"):
            continue
        kept = [c for c in m["tool_calls"] if c.get("id") in answered]
        if kept:
            m["tool_calls"] = kept
        else:
            m.pop("tool_calls")
    return out


def _take_pending(pending: list[tuple[str, str]], name: str) -> str | None:
    """Consume the oldest unanswered call id for `name` (or any, if unnamed)."""
    for i, (call_name, call_id) in enumerate(pending):
        if not name or call_name == name:
            pending.pop(i)
            return call_id
    return None


class OpenAIProvider(LLMProvider):
    PROVIDER_NAME = "openai"
    DEFAULT_MODEL = "gpt-4o-mini"
    # `<provider>/` prefixes to strip from a stored model id (see strip_routing_prefix).
    # Empty for OpenAI/OpenRouter, whose model ids may legitimately contain "/".
    ROUTING_PREFIXES: tuple[str, ...] = ()

    def __init__(
        self,
        api_key: str,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        **kwargs: Any,
    ):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model or self.DEFAULT_MODEL
        self.temperature = temperature
        self.max_tokens = max_tokens
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = OpenAI(**client_kwargs)

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
        # The caller's saved model/decoding settings are the defaults; a per-call
        # argument overrides them (never the other way round).
        model_name = strip_routing_prefix(model or self.model, *self.ROUTING_PREFIXES)
        if temperature is None:
            temperature = self.temperature
        if max_tokens is None:
            max_tokens = self.max_tokens
        formatted_messages = _format_messages(messages)

        params: dict[str, Any] = {
            "model": model_name,
            "messages": formatted_messages,
        }
        if temperature is not None:
            params["temperature"] = temperature
        if max_tokens is not None:
            params["max_tokens"] = max_tokens
        if top_p is not None:
            params["top_p"] = top_p

        if tools:
            formatted_tools = []
            for t in tools:
                if isinstance(t, ToolSchema):
                    formatted_tools.append({
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.parameters,
                        },
                    })
                else:
                    formatted_tools.append(t)
            params["tools"] = formatted_tools

        try:
            response = self.client.chat.completions.create(**params, **kwargs)
        except Exception as exc:
            if "tools" in params:
                tool_err = tool_unsupported_error(
                    exc, provider=self.PROVIDER_NAME, model=model_name
                )
                if tool_err:
                    raise RuntimeError(tool_err) from exc
            raise RuntimeError(
                classify_llm_error(exc, provider=self.PROVIDER_NAME, model=model_name)
            ) from exc

        choice = response.choices[0]
        message = choice.message
        text = message.content or ""

        tool_calls = []
        if getattr(message, "tool_calls", None):
            for tc in message.tool_calls:
                args = json.loads(tc.function.arguments or "{}")
                tool_calls.append(ToolCall(name=tc.function.name, args=args))

        usage_dict = None
        if getattr(response, "usage", None):
            usage_dict = {
                "prompt_tokens": response.usage.prompt_tokens,
                "completion_tokens": response.usage.completion_tokens,
                "total_tokens": response.usage.total_tokens,
            }

        return LLMResponse(
            model=model_name,
            provider=self.PROVIDER_NAME,
            text=text,
            content=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            finish_reason=choice.finish_reason,
        )

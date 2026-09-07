"""AnthropicProvider using the official anthropic Python SDK."""

from collections.abc import Sequence
from typing import Any

from anthropic import Anthropic

from app.llm.base import (
    LLMProvider,
    LLMResponse,
    Message,
    ToolCall,
    ToolSchema,
    classify_llm_error,
    strip_routing_prefix,
)


def _blocks(content: Any) -> list[dict[str, Any]]:
    """Content as Anthropic content blocks."""
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}] if content else []


def _format_messages(
    messages: Sequence[Message | dict[str, Any]],
) -> tuple[str | None, list[dict[str, Any]]]:
    """Harness conversation -> (system prompt, Anthropic messages).

    Two things the previous mapping got wrong, both fatal once a tool ran:
    a tool result was sent as a second *assistant* turn (Anthropic requires
    alternating roles), and tool calls never became `tool_use` / `tool_result`
    blocks, so the model could not see what its own call returned.
    """
    system_text: str | None = None
    out: list[dict[str, Any]] = []
    pending: list[tuple[str, str]] = []  # (tool name, tool_use id) awaiting a result

    for idx, m in enumerate(messages):
        if isinstance(m, Message):
            role, content, tool_calls = m.role, m.content, list(m.tool_calls or [])
            name = str(getattr(m, "name", ""))
        else:
            role = str(m.get("role", ""))
            content = m.get("content")
            tool_calls = list(m.get("tool_calls") or [])
            name = str(m.get("name", ""))

        if role == "system":
            if content:
                system_text = str(content)
            continue

        if role == "assistant" and tool_calls:
            blocks = _blocks(content)
            for j, tc in enumerate(tool_calls):
                call_id = str(tc.get("id") or f"toolu_{idx}_{j}")
                tool_name = str(tc.get("name", ""))
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call_id,
                        "name": tool_name,
                        "input": tc.get("args") or tc.get("input") or {},
                    }
                )
                pending.append((tool_name, call_id))
            out.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            call_id = _take_pending(pending, name)
            if call_id is None:
                out.append({"role": "user", "content": _blocks(f"[tool result] {name}: {content}")})
            else:
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": call_id,
                                "content": str(content or ""),
                            }
                        ],
                    }
                )
            continue

        out.append({"role": "user" if role == "user" else "assistant", "content": _blocks(content)})

    return system_text, _coalesce(_drop_unanswered_calls(out))


def _drop_unanswered_calls(out: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop `tool_use` blocks that never got a `tool_result`.

    Anthropic rejects a request whose tool_use ids have no matching tool_result —
    the gap a guardrail leaves when it ends a run mid-batch, which would then
    poison every later turn on that thread.
    """
    answered = {
        block.get("tool_use_id")
        for msg in out
        for block in msg["content"]
        if isinstance(block, dict) and block.get("type") == "tool_result"
    }
    for msg in out:
        msg["content"] = [
            block
            for block in msg["content"]
            if not (
                isinstance(block, dict)
                and block.get("type") == "tool_use"
                and block.get("id") not in answered
            )
        ]
    return out


def _take_pending(pending: list[tuple[str, str]], name: str) -> str | None:
    for i, (call_name, call_id) in enumerate(pending):
        if not name or call_name == name:
            pending.pop(i)
            return call_id
    return None


def _coalesce(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Merge adjacent same-role turns and drop empties (Anthropic alternates)."""
    merged: list[dict[str, Any]] = []
    for msg in messages:
        if not msg["content"]:
            continue
        if merged and merged[-1]["role"] == msg["role"]:
            merged[-1]["content"] = merged[-1]["content"] + msg["content"]
        else:
            merged.append({"role": msg["role"], "content": list(msg["content"])})
    return merged


class AnthropicProvider(LLMProvider):
    PROVIDER_NAME = "anthropic"
    DEFAULT_MODEL = "claude-3-5-sonnet-20241022"

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
        self.model = model or self.DEFAULT_MODEL
        self.temperature = temperature
        self.max_tokens = max_tokens
        client_kwargs: dict[str, Any] = {"api_key": api_key}
        if base_url:
            client_kwargs["base_url"] = base_url
        self.client = Anthropic(**client_kwargs)

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
        # Saved keys may carry a routing prefix ("anthropic/claude-...") from the
        # dashboard's model normalization; the SDK wants the bare model id.
        model_name = strip_routing_prefix(model or self.model, "anthropic", "claude")
        if temperature is None:
            temperature = self.temperature
        system_text, formatted_messages = _format_messages(messages)

        params: dict[str, Any] = {
            "model": model_name,
            "messages": formatted_messages,
            "max_tokens": max_tokens or self.max_tokens or 1024,
        }
        if system_text:
            params["system"] = system_text
        if temperature is not None:
            params["temperature"] = temperature
        if top_p is not None:
            params["top_p"] = top_p

        if tools:
            formatted_tools = []
            for t in tools:
                if isinstance(t, ToolSchema):
                    formatted_tools.append({
                        "name": t.name,
                        "description": t.description,
                        "input_schema": t.parameters,
                    })
                else:
                    fn = t.get("function", t)
                    formatted_tools.append({
                        "name": fn.get("name"),
                        "description": fn.get("description"),
                        "input_schema": fn.get("parameters", {}),
                    })
            params["tools"] = formatted_tools

        try:
            response = self.client.messages.create(**params, **kwargs)
        except Exception as exc:
            raise RuntimeError(
                classify_llm_error(exc, provider=self.PROVIDER_NAME, model=model_name)
            ) from exc

        text_parts = []
        tool_calls = []

        for block in response.content:
            if block.type == "text":
                text_parts.append(block.text)
            elif block.type == "tool_use":
                tool_calls.append(ToolCall(name=block.name, args=block.input))

        text = "".join(text_parts)

        usage_dict = None
        if getattr(response, "usage", None):
            usage_dict = {
                "prompt_tokens": response.usage.input_tokens,
                "completion_tokens": response.usage.output_tokens,
                "total_tokens": response.usage.input_tokens + response.usage.output_tokens,
            }

        return LLMResponse(
            model=model_name,
            provider=self.PROVIDER_NAME,
            text=text,
            content=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            finish_reason=response.stop_reason,
        )

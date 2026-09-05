"""AnthropicProvider using the official anthropic Python SDK."""

from collections.abc import Sequence
from typing import Any

from anthropic import Anthropic

from app.llm.base import LLMProvider, LLMResponse, Message, ToolCall, ToolSchema, classify_llm_error


class AnthropicProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None, **kwargs: Any):
        self.api_key = api_key
        self.model = model or "claude-3-5-sonnet-20241022"
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
        model_name = model or "claude-3-5-sonnet-20241022"
        system_text = None
        formatted_messages = []

        for m in messages:
            role = m.role if isinstance(m, Message) else m.get("role")
            content = m.content if isinstance(m, Message) else m.get("content")

            if role == "system":
                if content:
                    system_text = content
                continue

            # Anthropic expects alternating user/assistant or roles "user" / "assistant"
            anthropic_role = "user" if role == "user" else "assistant"
            msg_dict: dict[str, Any] = {"role": anthropic_role, "content": content or ""}
            formatted_messages.append(msg_dict)

        params: dict[str, Any] = {
            "model": model_name,
            "messages": formatted_messages,
            "max_tokens": max_tokens or 1024,
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
            raise RuntimeError(classify_llm_error(exc)) from exc

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
            provider="anthropic",
            text=text,
            content=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            finish_reason=response.stop_reason,
        )

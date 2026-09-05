"""OpenAIProvider using the official openai Python SDK."""

import json
from collections.abc import Sequence
from typing import Any

from openai import OpenAI

from app.llm.base import LLMProvider, LLMResponse, Message, ToolCall, ToolSchema, classify_llm_error


class OpenAIProvider(LLMProvider):
    def __init__(self, api_key: str, base_url: str | None = None, model: str | None = None, **kwargs: Any):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model or "gpt-4o-mini"
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
        model_name = model or "gpt-4o-mini"
        formatted_messages = []
        for m in messages:
            if isinstance(m, Message):
                msg_dict = {"role": m.role}
                if m.content is not None:
                    msg_dict["content"] = m.content
                if m.tool_calls:
                    msg_dict["tool_calls"] = m.tool_calls
                if m.tool_call_id:
                    msg_dict["tool_call_id"] = m.tool_call_id
                formatted_messages.append(msg_dict)
            else:
                formatted_messages.append(m)

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
            raise RuntimeError(classify_llm_error(exc)) from exc

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
            provider="openai",
            text=text,
            content=text,
            tool_calls=tool_calls,
            usage=usage_dict,
            finish_reason=choice.finish_reason,
        )

"""LiteLLMProvider — thin wrapper over litellm.completion.

litellm is imported lazily so offline tests/CI never touch it.
Default model: Gemini free tier for dev; OpenAI/Claude via LiteLLM model prefixes.
"""

import json
from typing import Any

from app.llm.base import LLMResponse, ToolCall


class LiteLLMProvider:
    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        temperature: float = 0.2,
        max_tokens: int = 1024,
    ):
        self.model = model
        self.api_key = api_key
        self.temperature = temperature
        self.max_tokens = max_tokens

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        import litellm  # lazy import

        params: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if self.api_key:
            params["api_key"] = self.api_key
        if tools:
            params["tools"] = tools

        response = litellm.completion(**params, **kwargs)
        message = response.choices[0].message
        text = message.content or ""
        tool_calls = []
        for tc in message.tool_calls or []:
            args = json.loads(tc.function.arguments or "{}")
            tool_calls.append(ToolCall(name=tc.function.name, args=args))
        return LLMResponse(text=text, tool_calls=tool_calls)

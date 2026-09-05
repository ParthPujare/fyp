"""OpenRouterProvider inheriting from OpenAIProvider with OpenRouter base URL."""

from typing import Any

from app.llm.openai_provider import OpenAIProvider


class OpenRouterProvider(OpenAIProvider):
    def __init__(self, api_key: str, base_url: str | None = None, **kwargs: Any):
        base = base_url or "https://openrouter.ai/api/v1"
        super().__init__(api_key=api_key, base_url=base, **kwargs)

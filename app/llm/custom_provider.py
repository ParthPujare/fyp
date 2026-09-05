"""CustomOpenAICompatibleProvider inheriting from OpenAIProvider for arbitrary OpenAI-compatible endpoints."""

from typing import Any

from app.llm.openai_provider import OpenAIProvider


class CustomOpenAICompatibleProvider(OpenAIProvider):
    def __init__(self, api_key: str, base_url: str | None = None, **kwargs: Any):
        if not base_url:
            raise ValueError("base_url is required for CustomOpenAICompatibleProvider")
        super().__init__(api_key=api_key, base_url=base_url, **kwargs)

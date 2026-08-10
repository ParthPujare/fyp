"""Provider factory — picks MockLLM (offline/CI) or LiteLLMProvider from settings."""

from app.config import Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.litellm import LiteLLMProvider
from app.llm.mock import MockLLM


def get_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.llm.provider == "litellm":
        return LiteLLMProvider(
            model=settings.llm.model,
            api_key=settings.llm.api_key,
            temperature=settings.llm.temperature,
            max_tokens=settings.llm.max_tokens,
        )
    return MockLLM()

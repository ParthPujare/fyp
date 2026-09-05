"""Provider registry and factory (Hermes-style, no LiteLLM)."""

from typing import Any

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.custom_provider import CustomOpenAICompatibleProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.openrouter_provider import OpenRouterProvider

PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "openrouter": OpenRouterProvider,
    "custom_openai": CustomOpenAICompatibleProvider,
}


def get_provider_instance(
    provider_type: str,
    api_key: str,
    base_url: str | None = None,
    **kwargs: Any,
) -> LLMProvider:
    p_type = (provider_type or "openai").lower().strip()
    # Normalize common aliases
    if p_type in ["google", "google_ai", "google_ai_studio"]:
        p_type = "gemini"
    elif p_type in ["claude"]:
        p_type = "anthropic"

    if p_type not in PROVIDERS:
        raise ValueError(f"Unknown provider type: {provider_type!r}. Supported: {list(PROVIDERS.keys())}")

    cls = PROVIDERS[p_type]
    return cls(api_key=api_key, base_url=base_url, **kwargs)

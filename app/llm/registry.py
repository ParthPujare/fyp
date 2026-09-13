"""Provider registry and factory (Hermes-style, no LiteLLM)."""

from typing import Any

from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import LLMProvider
from app.llm.custom_provider import CustomOpenAICompatibleProvider
from app.llm.gemini_provider import GeminiProvider
from app.llm.groq_provider import GroqProvider
from app.llm.lmstudio_provider import LlamaCppProvider, LmStudioProvider, OllamaProvider
from app.llm.openai_provider import OpenAIProvider
from app.llm.openrouter_provider import OpenRouterProvider
from app.llm.zen_provider import ZenProvider

PROVIDERS: dict[str, type[LLMProvider]] = {
    "openai": OpenAIProvider,
    "gemini": GeminiProvider,
    "anthropic": AnthropicProvider,
    "groq": GroqProvider,
    "openrouter": OpenRouterProvider,
    "custom_openai": CustomOpenAICompatibleProvider,
    "zen": ZenProvider,
    "lmstudio": LmStudioProvider,
    "ollama": OllamaProvider,
    "llamacpp": LlamaCppProvider,
}

# Spellings accepted for a provider besides its registry key.
ALIASES = {
    "google": "gemini",
    "google_ai": "gemini",
    "google_ai_studio": "gemini",
    # A "vertex" key already stored by an earlier build resolves to the Gemini
    # provider, which handles both `AIza…` (?key=) and `AQ.` (Bearer) credentials.
    "vertex": "gemini",
    "claude": "anthropic",
    "groqcloud": "groq",
    "opencode": "zen",
    "opencode_zen": "zen",
    "opencode-zen": "zen",
    "lm_studio": "lmstudio",
    "lm-studio": "lmstudio",
    "llama.cpp": "llamacpp",
    "llama_cpp": "llamacpp",
}


def normalize_provider_type(provider_type: str) -> str:
    """Registry key for a user-supplied provider name (alias-tolerant)."""
    p_type = (provider_type or "openai").lower().strip()
    return ALIASES.get(p_type, p_type)


def is_supported(provider_type: str) -> bool:
    """True iff this provider name resolves to a real provider class."""
    return normalize_provider_type(provider_type) in PROVIDERS


def get_provider_instance(
    provider_type: str,
    api_key: str,
    base_url: str | None = None,
    **kwargs: Any,
) -> LLMProvider:
    p_type = normalize_provider_type(provider_type)
    if p_type not in PROVIDERS:
        raise ValueError(f"Unknown provider type: {provider_type!r}. Supported: {list(PROVIDERS.keys())}")

    cls = PROVIDERS[p_type]
    return cls(api_key=api_key, base_url=base_url, **kwargs)

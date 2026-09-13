"""Provider factory — picks MockLLM (offline/CI) or custom Hermes-style providers from registry/settings."""

from typing import Any

from app.auth.store import MemberStore
from app.config import Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.mock import MockLLM
from app.llm.registry import get_provider_instance, is_supported

# Pre-Mk5 configs said `LLM__PROVIDER=litellm` and carried the real backend in the
# model prefix (`gemini/…`, `anthropic/…`). LiteLLM is gone; resolve those configs
# to the provider the model actually names instead of silently assuming OpenAI.
LEGACY_PROVIDER = "litellm"
_MODEL_PREFIX_PROVIDERS = {
    "gemini": "gemini",
    "vertex": "gemini",
    "anthropic": "anthropic",
    "claude": "anthropic",
    "groq": "groq",
    "openrouter": "openrouter",
    "zen": "zen",
    "opencode": "zen",
    "lmstudio": "lmstudio",
    "ollama": "ollama",
    "llamacpp": "llamacpp",
}


def resolve_provider_type(settings: Settings) -> str:
    """The provider name to build from settings, resolving the legacy alias."""
    name = (settings.llm.provider or "").strip().lower()
    if name != LEGACY_PROVIDER:
        return name
    prefix = (settings.llm.model or "").split("/", 1)[0].lower()
    return _MODEL_PREFIX_PROVIDERS.get(prefix, "openai")


def get_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    p_type = resolve_provider_type(settings)
    if not is_supported(p_type) or not settings.llm.api_key:
        return MockLLM()
    return get_provider_instance(
        provider_type=p_type,
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
        model=settings.llm.model,
        temperature=settings.llm.temperature,
        max_tokens=settings.llm.max_tokens,
    )


def get_provider_for_user(
    settings: Settings | None = None,
    username: str | None = None,
    store: MemberStore | Any | None = None,
    key_id: str | None = None,
) -> LLMProvider:
    """Resolution chain: User specified key_id > User's active key > global provider config > MockLLM."""
    settings = settings or get_settings()
    if username:
        from fastapi import HTTPException

        from app.auth.users import get_store
        member_store = get_store()
        key_data = None
        try:
            if key_id:
                key_data = member_store.get_llm_key_by_id(username, key_id)
            if not key_data:
                key_data = member_store.get_active_llm_key(username)
        except HTTPException as exc:
            # Stale token (rotated LLM__FERNET_KEY) or missing master key: don't
            # kill the turn — fall back to the global/mock provider so chat keeps
            # working. The row is flagged stale in /api/users; re-saving the key
            # restores per-user routing.
            print(f"[LLM] Stored key for {username} unusable ({exc.detail}); using fallback provider.")
            key_data = None
        if key_data:
            provider_type, model_name, decrypted_key, base_url = key_data
            return get_provider_instance(
                provider_type=provider_type,
                api_key=decrypted_key,
                base_url=base_url or None,
                model=model_name,
                temperature=settings.llm.temperature,
                max_tokens=settings.llm.max_tokens,
            )
    return get_provider(settings)


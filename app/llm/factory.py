"""Provider factory — picks MockLLM (offline/CI) or custom Hermes-style providers from registry/settings."""

from typing import Any

from app.auth.store import MemberStore
from app.config import Settings, get_settings
from app.llm.base import LLMProvider
from app.llm.mock import MockLLM
from app.llm.registry import get_provider_instance


def get_provider(settings: Settings | None = None) -> LLMProvider:
    settings = settings or get_settings()
    if settings.llm.provider in ["openai", "gemini", "anthropic", "openrouter", "custom_openai", "litellm"]:
        # If provider type is configured, map litellm to openai or use registry
        p_type = "openai" if settings.llm.provider == "litellm" else settings.llm.provider
        if not settings.llm.api_key:
            return MockLLM()
        return get_provider_instance(
            provider_type=p_type,
            api_key=settings.llm.api_key,
            base_url=settings.llm.base_url,
        )
    return MockLLM()


def get_provider_for_user(
    settings: Settings | None = None,
    username: str | None = None,
    store: MemberStore | Any | None = None,
    key_id: str | None = None,
) -> LLMProvider:
    """Resolution chain: User specified key_id > User's active key > global provider config > MockLLM."""
    settings = settings or get_settings()
    if username:
        try:
            from app.auth.users import get_store
            member_store = get_store()
            key_data = None
            if key_id:
                key_data = member_store.get_llm_key_by_id(username, key_id)
            if not key_data:
                key_data = member_store.get_active_llm_key(username)
            if key_data:
                provider_type, model_name, decrypted_key = key_data
                return get_provider_instance(
                    provider_type=provider_type,
                    api_key=decrypted_key,
                    model=model_name,
                )
        except Exception as exc:
            print(f"[LLM Error] Failed to resolve user provider for {username}: {exc}")
            raise
    return get_provider(settings)


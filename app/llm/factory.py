"""Provider factory — picks MockLLM (offline/CI) or LiteLLMProvider from settings."""

from app.auth.store import MemberStore
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


def get_provider_for_user(
    settings: Settings | None = None,
    username: str | None = None,
    store: MemberStore | None = None,
) -> LLMProvider:
    """Resolution chain: User's active key > global provider config > MockLLM."""
    settings = settings or get_settings()
    if username:
        try:
            from app.auth.users import get_store
            store = store or get_store()
            active = store.get_active_llm_key(username)
            if active:
                _, model_name, decrypted_key = active
                return LiteLLMProvider(
                    model=model_name,
                    api_key=decrypted_key,
                    temperature=settings.llm.temperature,
                    max_tokens=settings.llm.max_tokens,
                )
        except (ValueError, TypeError, LookupError, AttributeError):
            pass
    return get_provider(settings)


def classify_llm_error(exc: Exception) -> str:
    msg = str(exc).lower()
    if any(k in msg for k in ["auth", "api key", "unauthorized", "invalid key", "authentication"]):
        return "LLM provider authentication failed: invalid API key."
    if any(k in msg for k in ["rate limit", "quota", "exceeded", "too many requests", "429"]):
        return "LLM provider rate limit or quota exceeded."
    if any(k in msg for k in ["timeout", "timed out", "deadline"]):
        return "LLM provider request timed out."
    return f"LLM provider error: {exc.__class__.__name__} - {exc}"


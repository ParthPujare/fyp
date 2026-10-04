"""Platform-owned background LLM singleton (Mk16) — NOT part of BYOK.

One shared OpenRouter key serves every user server-side for memory extraction
only. Chat resolution (`get_provider_for_user`) is untouched and stays
BYOK-only. The key is env-only (never DB/JWT/API/trace) and the model id is
pinned to a `:free` suffix so it can never bill.
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any

from app.config import Settings, get_settings
from app.llm.openrouter_provider import OpenRouterProvider

SYSTEM_MODEL = "qwen/qwen3.8-27b:free"


def resolve_system_key(settings: Settings | None = None) -> str | None:
    """Canonical `SYSTEM__OPENROUTER_API_KEY` first, legacy `OPENROUTER_API_KEY` next."""
    settings = settings or get_settings()
    for candidate in (
        getattr(settings.system, "openrouter_api_key", None),
        getattr(settings, "openrouter_api_key", None),
        os.environ.get("OPENROUTER_API_KEY"),
    ):
        if (candidate or "").strip():
            return candidate.strip()
    return None


def build_system_provider(settings: Settings | None = None) -> OpenRouterProvider | None:
    """Fresh instance (tests); None when no key. Raises ValueError on non-`:free` model."""
    settings = settings or get_settings()
    key = resolve_system_key(settings)
    if not key:
        return None
    model = (settings.system.model or SYSTEM_MODEL).strip() or SYSTEM_MODEL
    if ":free" not in model:
        raise ValueError(
            f"system model {model!r} is not a free-tier id (must end with ':free')"
        )
    return OpenRouterProvider(
        api_key=key,
        model=model,
        temperature=settings.system.temperature,
        max_tokens=settings.system.max_tokens,
        timeout=settings.system.timeout_seconds,
    )


@lru_cache(maxsize=1)
def _cached_provider(key: str, model: str, temperature: float, max_tokens: int, timeout: float) -> Any:
    return OpenRouterProvider(
        api_key=key,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
    )


def get_system_provider(settings: Settings | None = None) -> OpenRouterProvider | None:
    """Process-wide singleton; None when unconfigured. Never raises on missing key."""
    settings = settings or get_settings()
    key = resolve_system_key(settings)
    if not key:
        return None
    model = (settings.system.model or SYSTEM_MODEL).strip() or SYSTEM_MODEL
    if ":free" not in model:
        raise ValueError(
            f"system model {model!r} is not a free-tier id (must end with ':free')"
        )
    return _cached_provider(
        key, model, float(settings.system.temperature or 0.0),
        int(settings.system.max_tokens or 512), float(settings.system.timeout_seconds or 15.0),
    )


def clear_system_provider_cache() -> None:
    _cached_provider.cache_clear()

"""Platform-owned background LLM singleton (Mk16) — NOT part of BYOK.

One shared OpenRouter key serves every user server-side for memory extraction
only. Chat resolution (`get_provider_for_user`) is untouched and stays
BYOK-only. Resolution order: shared-DB row first (set once by an admin, so any
instance on the same database uses that exact key), per-machine env as the
pre-bootstrap fallback, unconfigured otherwise. The model id must carry a
`:free` suffix so it can never bill.
"""

from __future__ import annotations

import logging
import os
from functools import lru_cache
from typing import Any

from app.config import Settings, get_settings
from app.llm.openrouter_provider import OpenRouterProvider

log = logging.getLogger(__name__)

SYSTEM_MODEL = "qwen/qwen3.8-27b:free"


def resolve_env_key(settings: Settings | None = None) -> str | None:
    """Per-machine env key: `SYSTEM__OPENROUTER_API_KEY` first, legacy `OPENROUTER_API_KEY` next."""
    settings = settings or get_settings()
    for candidate in (
        getattr(settings.system, "openrouter_api_key", None),
        getattr(settings, "openrouter_api_key", None),
        os.environ.get("OPENROUTER_API_KEY"),
    ):
        if (candidate or "").strip():
            return candidate.strip()
    return None


def resolve_platform_config(settings: Settings | None = None) -> tuple[str, str, str] | None:
    """(model, api_key, source) with source in {"db", "env"}. Never raises.

    The shared-DB row wins when present and decryptable; a stale Fernet key
    (decrypt failure) is recorded as `decrypt-failed` and falls through to
    the env key; anything else (no row, no env) means unconfigured.
    """
    settings = settings or get_settings()
    try:
        from app.auth.users import get_store

        row = get_store().get_platform_llm()
    except Exception as exc:  # noqa: BLE001 - stale Fernet / store hiccup: fall back, never sink
        from fastapi import HTTPException

        from app.memory import system_status

        if isinstance(exc, HTTPException):
            # decrypt_secret's 502: the row exists but this machine's
            # LLM__FERNET_KEY cannot read it (key drift between laptops).
            system_status.record_failure("decrypt-failed")
            log.warning("platform key in shared DB is undecryptable here; using env fallback")
        else:
            log.debug("platform key unreadable (%s); using env fallback", type(exc).__name__)
        row = None
    if row is not None:
        model, key, _by, _at = row
        if (key or "").strip() and ":free" in (model or ""):
            return model.strip(), key.strip(), "db"
        log.warning("shared platform row is unusable (blank key or non-free model); using env fallback")
    env_key = resolve_env_key(settings)
    if env_key:
        model = (settings.system.model or SYSTEM_MODEL).strip() or SYSTEM_MODEL
        return model, env_key, "env"
    return None


def resolve_system_key(settings: Settings | None = None) -> str | None:
    """Just the key (DB first, env fallback); None when unconfigured."""
    resolved = resolve_platform_config(settings)
    return resolved[1] if resolved else None


def describe_platform_source(settings: Settings | None = None) -> tuple[str, str | None]:
    """(source, model) WITHOUT decrypting — safe for status surfaces.

    source in {"db", "env", "none"}; model is the DB row's model or the env
    default. A stale Fernet key never breaks this (meta path never decrypts).
    """
    settings = settings or get_settings()
    try:
        from app.auth.users import get_store

        meta = get_store().get_platform_llm_meta()
    except Exception:  # noqa: BLE001 - status must never fail; fall through to env
        meta = None
    if meta is not None and ":free" in (meta[0] or ""):
        return "db", meta[0]
    if resolve_env_key(settings):
        return "env", (settings.system.model or SYSTEM_MODEL).strip() or SYSTEM_MODEL
    return "none", None


def _provider_for(model: str, key: str, settings: Settings) -> OpenRouterProvider:
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


def build_system_provider(settings: Settings | None = None) -> OpenRouterProvider | None:
    """Fresh instance (tests); None when no key. Raises ValueError on non-`:free` model."""
    settings = settings or get_settings()
    resolved = resolve_platform_config(settings)
    if not resolved:
        return None
    model, key, _source = resolved
    return _provider_for(model, key, settings)


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
    resolved = resolve_platform_config(settings)
    if not resolved:
        return None
    model, key, _source = resolved
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

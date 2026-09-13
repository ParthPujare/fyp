"""Per-user BYO Composio key vault — Fernet at rest, same pattern as LLM BYOK (Mk3)."""

from __future__ import annotations

from app.auth.crypto import decrypt_secret, encrypt_secret
from app.auth.users import get_store


def save_composio_key(username: str, api_key_plaintext: str, actor: str | None = None) -> None:
    store = get_store()
    store.set_composio_key(username, api_key_plaintext, actor=actor or username)


def get_composio_key(username: str, settings_api_key: str | None = None) -> str | None:
    """Per-user key > global COMPOSIO__API_KEY > None (external tools invisible)."""
    store = get_store()
    try:
        per_user = store.get_composio_key(username)
    except Exception:  # noqa: BLE001 - store miss means fall back to global
        per_user = None
    if per_user:
        from app.auth.crypto import decrypt_secret as _dec

        try:
            return _dec(per_user)
        except Exception:  # noqa: BLE001, S110 - corrupt token falls back to global
            pass
    return settings_api_key or None


__all__ = ["decrypt_secret", "encrypt_secret", "get_composio_key", "save_composio_key"]

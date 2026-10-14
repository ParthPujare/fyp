"""Per-user BYO Composio key vault — Fernet at rest, same pattern as LLM BYOK (Mk3)."""

from __future__ import annotations

from app.auth.crypto import decrypt_secret, encrypt_secret
from app.auth.users import get_store


def save_composio_key(username: str, api_key_plaintext: str, actor: str | None = None) -> None:
    store = get_store()
    store.set_composio_key(username, api_key_plaintext, actor=actor or username)


def get_composio_key(username: str, settings_api_key: str | None = None) -> str | None:
    """Mk18 Phase F: strict per-user isolation — the member's own saved key or
    None (external tools invisible). The old global `COMPOSIO__API_KEY`
    fallback silently shared one Composio project across users and masked
    decrypt failures; both are gone. Callers needing the legacy demo path
    must pass an explicit key.
    """
    _ = settings_api_key  # retained for call compatibility; deliberately ignored
    store = get_store()
    try:
        per_user = store.get_composio_key(username)
    except Exception:  # noqa: BLE001 - store miss means no key
        return None
    if not per_user:
        return None
    from app.auth.crypto import decrypt_secret as _dec

    try:
        return _dec(per_user)
    except Exception:  # noqa: BLE001 - corrupt token is unusable, never silent global
        return None


__all__ = ["decrypt_secret", "encrypt_secret", "get_composio_key", "save_composio_key"]

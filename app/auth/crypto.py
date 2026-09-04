"""Fernet encryption helpers for per-user API keys at rest."""

from cryptography.fernet import Fernet, InvalidToken
from fastapi import HTTPException, status

from app.config import get_settings


def get_fernet() -> Fernet:
    settings = get_settings()
    key = settings.llm.fernet_key
    if not key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Encryption master key LLM__FERNET_KEY is not configured on the server.",
        )
    try:
        if isinstance(key, str):
            key_bytes = key.encode("utf-8")
        else:
            key_bytes = key
        return Fernet(key_bytes)
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Invalid encryption master key configuration: {e}",
        ) from e


def encrypt_secret(plaintext: str) -> str:
    """Encrypt a plaintext secret (e.g. API key) using Fernet."""
    if not plaintext:
        return ""
    f = get_fernet()
    token_bytes = f.encrypt(plaintext.encode("utf-8"))
    return token_bytes.decode("utf-8")


def decrypt_secret(token: str) -> str:
    """Decrypt a Fernet token back to plaintext secret."""
    if not token:
        return ""
    f = get_fernet()
    try:
        decrypted_bytes = f.decrypt(token.encode("utf-8"))
        return decrypted_bytes.decode("utf-8")
    except InvalidToken:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to decrypt API key. The encryption master key may have changed; please re-save your API key.",
        )
    except (ValueError, TypeError) as e:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Decryption error: {e}",
        ) from e

"""Unit tests for Plan Mk3 BYOK LLM Keys (crypto & store layer)."""

import pytest
from fastapi import HTTPException

from app.auth.crypto import decrypt_secret, encrypt_secret
from app.auth.store import MemberError


def test_crypto_roundtrip(monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("LLM__FERNET_KEY", key)
    # Clear lru_cache or settings if cached, or patch settings
    from app.config import get_settings
    get_settings.cache_clear()

    secret = "sk-ant-api03-secret-key-12345"
    token = encrypt_secret(secret)
    assert token != secret
    assert decrypt_secret(token) == secret


def test_crypto_missing_key_raises_503(monkeypatch):
    monkeypatch.setenv("LLM__FERNET_KEY", "")
    from app.config import get_settings
    get_settings.cache_clear()

    with pytest.raises(HTTPException) as exc_info:
        encrypt_secret("test")
    assert exc_info.value.status_code == 503


def test_store_llm_keys_crud_and_active_switching(member_store, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("LLM__FERNET_KEY", key)
    from app.config import get_settings
    get_settings.cache_clear()

    username = "alice@acme.com"

    # Initially no keys
    assert member_store.list_llm_keys(username) == []
    assert member_store.get_active_llm_key(username) is None

    # Set first key (active by default)
    k1 = member_store.set_llm_key(username, "openai", "gpt-4o", "sk-openai-abc", active=True)
    assert k1.provider == "openai"
    assert k1.model == "gpt-4o"
    assert k1.active is True

    keys = member_store.list_llm_keys(username)
    assert len(keys) == 1

    prov, model, decrypted, base_url = member_store.get_active_llm_key(username)
    assert prov == "openai"
    assert model == "gpt-4o"
    assert decrypted == "sk-openai-abc"
    assert base_url == ""

    # Set second key (active=True should deactivate first)
    k2 = member_store.set_llm_key(username, "anthropic", "claude-3-5-sonnet", "sk-ant-xyz", active=True)
    assert k2.active is True

    keys = member_store.list_llm_keys(username)
    assert len(keys) == 2
    # k1 should now be inactive
    for k in keys:
        if k.id == k1.id:
            assert k.active is False
        elif k.id == k2.id:
            assert k.active is True

    # Active key should be anthropic
    prov, model, decrypted, _ = member_store.get_active_llm_key(username)
    assert prov == "anthropic"
    assert model == "claude-3-5-sonnet"
    assert decrypted == "sk-ant-xyz"

    # Switch active back to k1
    member_store.activate_llm_key(username, k1.id)
    prov, model, decrypted, _ = member_store.get_active_llm_key(username)
    assert prov == "openai"
    assert decrypted == "sk-openai-abc"

    # Delete k2
    member_store.delete_llm_key(username, k2.id)
    assert len(member_store.list_llm_keys(username)) == 1

    # Delete unknown key raises
    with pytest.raises(MemberError):
        member_store.delete_llm_key(username, "non-existent-id")


def test_set_llm_key_duplicate_updates_secret(member_store, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("LLM__FERNET_KEY", key)
    from app.config import get_settings
    get_settings.cache_clear()

    username = "alice@acme.com"
    k1 = member_store.set_llm_key(username, "openai", "gpt-4o", "old-key", active=True)
    k2 = member_store.set_llm_key(username, "openai", "gpt-4o", "new-key", active=True)

    # Should update in place (same provider + model)
    assert k1.id == k2.id
    assert len(member_store.list_llm_keys(username)) == 1
    _, _, decrypted, _ = member_store.get_active_llm_key(username)
    assert decrypted == "new-key"


def test_custom_openai_key_round_trips_base_url(member_store, monkeypatch):
    """A custom OpenAI-compatible endpoint is only usable if its URL is stored."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    from app.config import get_settings

    get_settings.cache_clear()

    record = member_store.set_llm_key(
        "alice@acme.com",
        "custom_openai",
        "local-model",
        "sk-local",
        active=True,
        base_url="http://localhost:1234/v1",
    )
    assert record.base_url == "http://localhost:1234/v1"
    assert record.to_public()["base_url"] == "http://localhost:1234/v1"

    prov, model, decrypted, base_url = member_store.get_active_llm_key("alice@acme.com")
    assert (prov, model, decrypted) == ("custom_openai", "local-model", "sk-local")
    assert base_url == "http://localhost:1234/v1"


def test_llm_keys_audit_logged(member_store, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("LLM__FERNET_KEY", key)
    from app.config import get_settings
    get_settings.cache_clear()

    audit = member_store._audit_log
    before = len(audit.entries())

    k = member_store.set_llm_key("alice@acme.com", "openai", "gpt-4o", "secret", actor="admin@acme.com")
    member_store.activate_llm_key("alice@acme.com", k.id, actor="admin@acme.com")
    member_store.delete_llm_key("alice@acme.com", k.id, actor="admin@acme.com")

    entries = audit.entries()[before:]
    assert len(entries) == 3
    assert {e["resource"]["type"] for e in entries} == {"llm_key"}
    assert {e["resource"]["id"] for e in entries} == {"alice@acme.com"}

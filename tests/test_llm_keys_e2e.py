"""End-to-end and integration tests for Plan Mk3 BYOK LLM Keys."""

from fastapi.testclient import TestClient

from app.auth.jwt import create_token
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.llm.factory import get_provider_for_user


def make_token(username: str, member_store) -> str:
    settings = get_settings()
    user = member_store.get(username)
    return create_token(user, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)


def test_dashboard_account_llm_keys_flow(member_store, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("LLM__FERNET_KEY", key)
    get_settings.cache_clear()

    client = TestClient(dashboard_app)
    token = make_token("alice@acme.com", member_store)
    headers = {"Authorization": f"Bearer {token}"}

    # Initial keys should be empty
    keys_resp = client.get("/api/account/llm-keys", headers=headers)
    assert keys_resp.status_code == 200
    assert keys_resp.json() == []

    # Add a key
    add_resp = client.post(
        "/api/account/llm-keys",
        json={"provider": "openai", "model": "openai/gpt-4o-mini", "api_key": "sk-test-12345"},
        headers=headers,
    )
    assert add_resp.status_code == 200
    body = add_resp.json()
    assert body["provider"] == "openai"
    assert body["model"] == "openai/gpt-4o-mini"
    assert body["active"] is True
    assert "api_key" not in body
    assert "api_key_encrypted" not in body
    key_id = body["id"]

    # Verify provider resolution picks up user key
    provider = get_provider_for_user(get_settings(), "alice@acme.com", member_store)
    assert provider.model == "openai/gpt-4o-mini"
    assert provider.api_key == "sk-test-12345"

    # List keys
    list_resp = client.get("/api/account/llm-keys", headers=headers)
    assert len(list_resp.json()) == 1

    # Delete key
    del_resp = client.delete(f"/api/account/llm-keys/{key_id}", headers=headers)
    assert del_resp.status_code == 200
    assert client.get("/api/account/llm-keys", headers=headers).json() == []


def test_admin_member_llm_keys_management(member_store, monkeypatch):
    from cryptography.fernet import Fernet
    key = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("LLM__FERNET_KEY", key)
    get_settings.cache_clear()

    client = TestClient(dashboard_app)
    token = make_token("admin@acme.com", member_store)
    headers = {"Authorization": f"Bearer {token}"}

    # Admin sets key for alice
    set_resp = client.post(
        "/api/members/alice%40acme.com/llm-keys",
        json={"provider": "anthropic", "model": "anthropic/claude-3", "api_key": "sk-ant-admin"},
        headers=headers,
    )
    assert set_resp.status_code == 200
    assert set_resp.json()["provider"] == "anthropic"

    # Admin gets alice's keys
    get_resp = client.get("/api/members/alice%40acme.com/llm-keys", headers=headers)
    assert len(get_resp.json()) == 1

    # Admin checks /api/users shows active_llm_key status
    users_resp = client.get("/api/users", headers=headers)
    alice_record = next(u for u in users_resp.json() if u["username"] == "alice@acme.com")
    assert alice_record["active_llm_key"] == {"provider": "anthropic", "model": "anthropic/claude-3"}

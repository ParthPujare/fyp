"""Mk9 external tools — offline tests (FakeComposioClient, no network/keys)."""

from __future__ import annotations

from app.harness.filter import filter_tools
from app.integrations.adapter import all_specs, spec_for, to_composio_args
from app.integrations.client import FakeComposioClient
from app.integrations.service import execute_external, visible_external_tools


def test_adapter_allow_list_shapes():
    specs = {s.name: s for s in all_specs()}
    assert set(specs) == {"gmail_fetch", "gmail_send", "github_list_repos", "github_create_issue"}
    assert specs["gmail_fetch"].required_scope == "gmail:read"
    assert specs["gmail_send"].risk_level == "critical"
    assert specs["github_create_issue"].required_scope == "github:create"
    assert spec_for("gmail_send") is not None
    assert spec_for("nope") is None


def test_composio_arg_mapping_caps():
    assert to_composio_args("gmail_fetch", {"query": "x", "max_results": 99})["max_results"] == 20
    assert to_composio_args("gmail_send", {"to": "a@b.co", "subject": "s", "body": "b"})[
        "recipient_email"
    ] == "a@b.co"
    assert to_composio_args("github_create_issue", {"repo": "acme/web", "title": "t"})[
        "repository"
    ] == "acme/web"


def test_fake_client_deterministic():
    c = FakeComposioClient()
    res = c.execute("GMAIL_FETCH_EMAILS", "alice@acme.com", {"query": "standup"})
    assert res.ok and "messages" in res.data
    res2 = c.execute("GMAIL_SEND_EMAIL", "alice@acme.com", {"recipient_email": "x"})
    assert res2.ok and res2.data["id"] == "sent-001"


def test_visible_only_when_connected(member_store):
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    assert visible_external_tools(alice) == []
    # Mk18 Phase F: visibility also requires the member's own saved key.
    member_store.set_composio_key("alice@acme.com", "ck-alice-1", actor="alice@acme.com")
    assert visible_external_tools(alice) == []  # still nothing: not connected
    member_store.set_connected_account("alice@acme.com", "gmail", "acc-1", "active")
    names = {t.name for t in visible_external_tools(alice)}
    assert "gmail_fetch" in names and "gmail_send" in names
    assert "github_list_repos" not in names


def test_execute_requires_connection(member_store):
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    res = execute_external("gmail_fetch", {"query": "hi"}, alice)
    assert not res.ok and res.denied and res.reason == "auth"


def test_execute_fake_success(member_store, monkeypatch):
    """Fake-client execution with a saved key (offline stand-in for the real SDK)."""
    import app.integrations.service as _svc
    from app.integrations.client import FakeComposioClient

    monkeypatch.setattr(_svc, "build_client", lambda *a, **k: FakeComposioClient())
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    member_store.set_composio_key("alice@acme.com", "ck-alice-1", actor="alice@acme.com")
    member_store.set_connected_account("alice@acme.com", "gmail", "acc-1", "active")
    res = execute_external("gmail_fetch", {"query": "standup"}, alice)
    assert res.ok and "msg-001" in res.content


def test_execute_denies_without_key(member_store):
    """Mk18 Phase F: connected but key deleted → fail closed, never fake-send."""
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    member_store.set_composio_key("alice@acme.com", "ck-alice-1", actor="alice@acme.com")
    member_store.set_connected_account("alice@acme.com", "gmail", "acc-1", "active")
    member_store.delete_composio_key("alice@acme.com", actor="alice@acme.com")
    res = execute_external("gmail_fetch", {"query": "standup"}, alice)
    assert not res.ok and res.denied and res.reason == "auth"
    assert visible_external_tools(alice) == []


def test_filter_tools_respects_casbin(member_store, enforcer):
    member_store.set_connected_account("alice@acme.com", "gmail", "acc-1", "active")
    member_store.set_connected_account("alice@acme.com", "github", "acc-2", "active")
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    visible = {t.name for t in filter_tools(all_specs(), enforcer, alice)}
    assert "gmail_fetch" in visible and "gmail_send" in visible
    assert "github_create_issue" not in visible  # no policy row for marketing


def test_composio_key_vault_roundtrip(member_store, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    from app.config import get_settings

    get_settings.cache_clear()
    member_store.set_composio_key("alice@acme.com", "ck-test-123", actor="alice@acme.com")
    from app.integrations.vault import get_composio_key

    assert get_composio_key("alice@acme.com", None) == "ck-test-123"


def test_connect_url_requires_key_and_config():
    import pytest

    from app.integrations.client import ComposioClient

    with pytest.raises(RuntimeError, match="API key"):
        ComposioClient(api_key=None).get_connect_url("gmail", "alice@acme.com", "ac_123")
    client = ComposioClient(api_key="ck-x")
    client._sdk = object()  # bypass missing package so auth_config guard is hit
    with pytest.raises(RuntimeError, match="no auth config"):
        client.get_connect_url("gmail", "alice@acme.com", None)


def test_refresh_marks_active_when_composio_reports_it(member_store, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    from app.config import get_settings as _gs

    _gs.cache_clear()
    from fastapi.testclient import TestClient

    from app.auth.jwt import create_token
    from app.dashboard.app import app as dash_app
    from app.integrations import client as client_mod

    member_store.set_composio_key("alice@acme.com", "ck-x", actor="alice@acme.com")
    member_store.set_connected_account("alice@acme.com", "gmail", "", "pending")

    class _Fake:
        def list_for_user(self, user_id):
            assert user_id == "alice@acme.com"
            return [{"id": "ca_1", "toolkit": "gmail", "status": "ACTIVE"}]

    monkeypatch.setattr(client_mod, "build_client", lambda *a, **k: _Fake())
    from app.auth.users import USERS
    from app.config import get_settings

    settings = get_settings()
    token = create_token(
        USERS["alice@acme.com"], settings.jwt.secret, settings.jwt.algorithm,
        settings.jwt.expiry_minutes,
    )
    resp = TestClient(dash_app).post(
        "/api/integrations/refresh",
        json={"app": "gmail"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "active"
    assert member_store.is_app_connected("alice@acme.com", "gmail")


def test_catalog_seeds_and_dynamic_tool_visibility(member_store):
    from app.integrations.adapter import risk_for_slug, spec_from_discovered
    from app.integrations.service import visible_external_tools

    assert risk_for_slug("SLACK_SEND_MESSAGE") == "critical"
    # Mk18: unknown slugs fail closed (was "read" by bare heuristic).
    assert risk_for_slug("NOTION_SEARCH") == "critical"
    spec = spec_from_discovered("slack", "SLACK_SEND_MESSAGE", "Send", {"type": "object"})
    assert spec.name == "slack_slack_send_message"
    assert spec.required_scope == "slack:write"
    assert spec.risk_level == "critical"

    # Mk18 Phase F: dynamic visibility also requires the member's own key.
    member_store.set_composio_key("alice@acme.com", "ck-alice-1", actor="alice@acme.com")
    member_store.upsert_toolkit("slack", enabled=True, actor="admin")
    member_store.cache_toolkit_tools(
        "slack",
        [{"slug": "SLACK_SEND_MESSAGE", "description": "Send", "input_schema": {"type": "object"}}],
    )
    member_store.set_connected_account("alice@acme.com", "slack", "ca-9", "active")
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    names = {t.name for t in visible_external_tools(alice)}
    assert "slack_slack_send_message" in names


def test_dynamic_execute_unknown_and_not_connected(member_store):
    from app.integrations.service import execute_external

    member_store.upsert_toolkit("slack", enabled=True, actor="admin")
    member_store.cache_toolkit_tools(
        "slack",
        [{"slug": "SLACK_SEND_MESSAGE", "description": "Send", "input_schema": {"type": "object"}}],
    )
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    res = execute_external("slack_slack_send_message", {"text": "hi"}, alice)
    assert not res.ok and res.denied and "slack" in res.content


def test_connect_refuses_unprovisioned_toolkit(member_store):
    """Mk18 Phase F: members cannot auto-provision shared auth configs — a
    toolkit without a catalog auth_config is 422 (ask an admin)."""
    from fastapi.testclient import TestClient

    from app.auth.jwt import create_token
    from app.auth.users import USERS
    from app.config import get_settings
    from app.dashboard.app import app as dash_app

    settings = get_settings()
    member_store.set_composio_key("alice@acme.com", "ck-alice-1", actor="alice@acme.com")
    token = create_token(
        USERS["alice@acme.com"], settings.jwt.secret, settings.jwt.algorithm,
        settings.jwt.expiry_minutes,
    )
    resp = TestClient(dash_app).post(
        "/api/integrations/connect",
        json={"app": "gmail"},
        headers={"Authorization": f"Bearer {token}"},
    )
    # No catalog row with auth_config in this isolated store → admin path.
    assert resp.status_code == 422
    assert "admin" in resp.text.lower()


def test_external_results_carry_provenance(member_store, monkeypatch):
    """Mk18 Phase F: external data carries toolkit/user/ts provenance."""
    import app.integrations.service as _svc
    from app.integrations.client import FakeComposioClient
    from app.integrations.service import execute_external

    monkeypatch.setattr(_svc, "build_client", lambda *a, **k: FakeComposioClient())
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    member_store.set_composio_key("alice@acme.com", "ck-alice-1", actor="alice@acme.com")
    member_store.set_connected_account("alice@acme.com", "gmail", "acc-1", "active")
    res = execute_external("gmail_fetch", {"query": "standup"}, alice)
    assert res.ok
    assert res.data.get("_provenance", {}).get("toolkit") == "gmail"
    assert res.data["_provenance"]["user"] == "alice@acme.com"


def test_key_save_validates_key(member_store, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    from app.config import get_settings as _gs2

    _gs2.cache_clear()
    from fastapi.testclient import TestClient

    from app.auth.jwt import create_token
    from app.auth.users import USERS
    from app.dashboard.app import app as dash_app
    from app.integrations import client as client_mod

    class _Bad:
        def validate_key(self):
            return False, "key rejected by Composio (401)"

    monkeypatch.setattr(client_mod, "build_client", lambda *a, **k: _Bad())
    settings = _gs2()
    token = create_token(
        USERS["alice@acme.com"], settings.jwt.secret, settings.jwt.algorithm,
        settings.jwt.expiry_minutes,
    )
    resp = TestClient(dash_app).post(
        "/api/integrations/key",
        json={"api_key": "bad-key"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 502
    assert "401" in resp.text

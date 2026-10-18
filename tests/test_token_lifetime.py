"""Mk19 Phase 6 — credential-version claim: superseded tokens die at every
verify site (plan Mk19, finding 8, acceptance #6).

A password reset, role/dept/scope edit or TOTP set bumps `members.credentials_version`;
`create_token` stamps the mint-time generation (`cred_ver`) into the JWT and
`verify_token` rejects an older value as superseded — at ALL 7 verify sites,
because they all funnel through `verify_token`. Regression cover: the old token
is 401 at gateway, dashboard, dept service and `/auth/me` after a reset while a
fresh login works; role change and TOTP set supersede too; expired/forged
tokens keep their original error reasons; a member absent from the store keeps
the ghost path (both sides 0) honest.
"""

from __future__ import annotations

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.auth.jwt import AuthError, create_token, verify_token
from app.auth.service import app as auth_app
from app.auth.users import USERS, User
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_dashboard_checkpointer, get_gateway_sender
from app.gateway.app import app as gateway_app
from app.gateway.app import get_runner
from app.services import crm

settings = get_settings()


def _login(username: str, password: str | None = None) -> str:
    pw = password if password is not None else USERS[username].password
    resp = TestClient(auth_app).post("/auth/login", json={"username": username, "password": pw})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _raise_if_called(*_a, **_k):
    raise AssertionError("runner/sender must not run for a superseded token")


class _ExplodingRunner:
    """Callable runner stand-in: any actual invocation fails the test."""

    def __call__(self, *_a, **_k):
        raise AssertionError("runner must not run for a superseded token")


def _dash_client() -> TestClient:
    client = TestClient(dashboard_app)
    dashboard_app.dependency_overrides[get_dashboard_checkpointer] = lambda: InMemorySaver()
    dashboard_app.dependency_overrides[get_gateway_sender] = lambda: _raise_if_called
    return client


def test_password_reset_supersedes_token_at_every_verify_site(member_store):
    token = _login("alice@acme.com")
    member_store.reset_password("alice@acme.com", "NewPass!2345", actor="admin@acme.com")

    # every verify site rejects the old token as superseded (401),
    # before any runner/sender work happens.
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = lambda: _ExplodingRunner()
    try:
        r = gateway.post("/chat", json={"token": token, "message": "hi"})
    finally:
        gateway_app.dependency_overrides.clear()
    assert r.status_code == 401
    assert "superseded" in r.json()["detail"]

    r = TestClient(auth_app).get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "superseded" in r.json()["detail"]

    dash = _dash_client()
    try:
        r = dash.get("/api/chat/threads", headers={"Authorization": f"Bearer {token}"})
    finally:
        dashboard_app.dependency_overrides.clear()
    assert r.status_code == 401

    c = TestClient(crm.create_app(member_store.path.parent / "crm.sqlite"))
    r = c.get("/customers", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401  # dept-service middleware maps any AuthError to 401

    # a fresh login with the NEW password works at the same sites.
    fresh = _login("alice@acme.com", "NewPass!2345")
    r = TestClient(auth_app).get("/auth/me", headers={"Authorization": f"Bearer {fresh}"})
    assert r.status_code == 200
    r = c.get("/customers", headers={"Authorization": f"Bearer {fresh}"})
    assert r.status_code == 200


def test_role_change_supersedes_token(member_store):
    token = _login("alice@acme.com")
    member_store.update("alice@acme.com", role="hr_agent", dept="hr", actor="admin@acme.com")
    r = TestClient(auth_app).get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "superseded" in r.json()["detail"]


def test_totp_set_supersedes_token(member_store):
    token = _login("alice@acme.com")
    member_store.set_totp_secret("alice@acme.com", "JBSWY3DPEHPK3PXP", actor="admin@acme.com")
    r = TestClient(auth_app).get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert "superseded" in r.json()["detail"]


def test_expired_and_forged_tokens_keep_original_reasons(member_store):
    client = TestClient(auth_app)
    user = USERS["alice@acme.com"]
    expired = create_token(user, settings.jwt.secret, settings.jwt.algorithm, -5)
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {expired}"})
    assert r.status_code == 401
    assert "expired" in r.json()["detail"]

    forged = pyjwt.encode(
        {"sub": "alice@acme.com", "exp": 9999999999}, "attacker-secret", algorithm="HS256"
    )
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {forged}"})
    assert r.status_code == 401
    assert "superseded" not in r.json()["detail"]
    with pytest.raises(AuthError, match="invalid"):
        verify_token(forged, settings.jwt.secret, settings.jwt.algorithm)


def test_ghost_member_token_keeps_both_sides_zero(member_store):
    """A signed token for a member absent from the store still verifies
    (0 == 0) — the ghost path — and is superseded the moment the member exists."""
    ghost = User("ghost@none.com", None, "viewer", "marketing", [])
    token = create_token(ghost, settings.jwt.secret, settings.jwt.algorithm, 60)
    claims = verify_token(token, settings.jwt.secret, settings.jwt.algorithm)
    assert claims["sub"] == "ghost@none.com"
    assert member_store.credentials_version("ghost@none.com") == 0

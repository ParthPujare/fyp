"""Mk18 Phase D — httpOnly session cookies + CSRF tests."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth.service import app as auth_app
from app.dashboard.app import app as dashboard_app


@pytest.fixture(autouse=True)
def _insecure_cookies():
    """TestClient speaks plain http: Secure cookies would never be sent back."""
    from app.config import get_settings

    original = get_settings().jwt.cookie_secure
    get_settings().jwt.cookie_secure = False
    try:
        yield
    finally:
        get_settings().jwt.cookie_secure = original


def _login_cookies(username="admin@acme.com", password="admin123"):
    c = TestClient(auth_app)
    r = c.post("/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r


def test_login_sets_httponly_cookies(member_store):
    r = _login_cookies()
    raw = r.headers.get("set-cookie", "")
    assert "harness_token_admin=" in raw
    assert "HttpOnly" in raw
    assert "harness_token_active=admin" in raw
    body = r.json()
    assert body["access_token"]  # dual-read release: body token retained


def test_me_works_via_cookie(member_store):
    c = TestClient(auth_app)
    r = c.post("/auth/login", json={"username": "alice@acme.com", "password": "alice123"})
    assert r.status_code == 200
    # TestClient persists cookies: no Authorization header needed.
    me = c.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["sub"] == "alice@acme.com"


def test_logout_clears_cookies(member_store):
    c = TestClient(auth_app)
    c.post("/auth/login", json={"username": "admin@acme.com", "password": "admin123"})
    r = c.post("/auth/logout")
    assert r.status_code == 200
    assert "harness_token_admin=" in r.headers.get("set-cookie", "")


def test_dashboard_cookie_session_and_csrf(member_store):
    from app.auth.jwt import create_token
    from app.config import get_settings

    settings = get_settings()
    admin = member_store.get("admin@acme.com")
    token = create_token(admin, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)

    c = TestClient(dashboard_app)
    # Bearer baseline still works (API clients exempt from CSRF).
    r = c.get("/api/approvals/pending", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200

    # Cookie session: plant dual-slot cookies like a logged-in browser.
    c.cookies.set("harness_token_admin", token)
    c.cookies.set("harness_token_active", "admin")
    r2 = c.get("/api/approvals/pending")
    assert r2.status_code == 200

    # Mutating without CSRF over cookies → 403.
    r3 = c.post("/api/logout")
    assert r3.status_code == 403

    # With CSRF → through.
    csrf = c.get("/api/csrf").json()["csrf_token"]
    r4 = c.post("/api/logout", headers={"X-CSRF-Token": csrf})
    assert r4.status_code == 200


def test_csrf_token_derived_and_validated(member_store):
    from app.auth.jwt import create_token
    from app.config import get_settings
    from app.harness.csrf import csrf_token_for, csrf_valid

    settings = get_settings()
    admin = member_store.get("admin@acme.com")
    token = create_token(admin, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)
    c = TestClient(dashboard_app)
    got = c.get("/api/csrf", headers={"Authorization": f"Bearer {token}"}).json()["csrf_token"]
    assert got == csrf_token_for("admin@acme.com", settings.jwt.secret)
    assert csrf_valid(got, "admin@acme.com", settings.jwt.secret)
    assert not csrf_valid(got, "alice@acme.com", settings.jwt.secret)
    assert not csrf_valid(None, "admin@acme.com", settings.jwt.secret)

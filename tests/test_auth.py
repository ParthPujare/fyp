"""Auth: JWT issue/verify + login service endpoints."""

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import AuthError, create_token, verify_token
from app.auth.service import app as auth_app
from app.auth.users import USERS
from app.config import get_settings

SETTINGS = get_settings()
SECRET = SETTINGS.jwt.secret
ALG = SETTINGS.jwt.algorithm


def test_create_and_verify_roundtrip():
    user = USERS["alice@acme.com"]
    token = create_token(user, SECRET, ALG, 60)
    claims = verify_token(token, SECRET, ALG)
    assert claims["sub"] == "alice@acme.com"
    assert claims["role"] == "marketing_agent"
    assert claims["dept"] == "marketing"
    assert claims["scopes"] == ["crm:read", "crm:create"]


def test_expired_token_rejected():
    token = create_token(USERS["bob@acme.com"], SECRET, ALG, expiry_minutes=-1)
    with pytest.raises(AuthError):
        verify_token(token, SECRET, ALG)


def test_tampered_token_rejected():
    user = USERS["bob@acme.com"]
    token = create_token(user, SECRET, ALG, 60)
    payload = pyjwt.decode(token, options={"verify_signature": False})
    payload["role"] = "admin"
    forged = pyjwt.encode(payload, "wrong-secret", algorithm=ALG)
    with pytest.raises(AuthError):
        verify_token(forged, SECRET, ALG)


def test_garbage_token_rejected():
    with pytest.raises(AuthError):
        verify_token("not.a.jwt", SECRET, ALG)


def test_login_success():
    client = TestClient(auth_app)
    resp = client.post(
        "/auth/login", json={"username": "alice@acme.com", "password": "alice123"}
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["access_token"]
    assert data["token_type"] == "bearer"
    assert data["claims"]["role"] == "marketing_agent"
    assert data["claims"]["dept"] == "marketing"


def test_login_wrong_password():
    client = TestClient(auth_app)
    resp = client.post(
        "/auth/login", json={"username": "alice@acme.com", "password": "nope"}
    )
    assert resp.status_code == 401


def test_login_unknown_user():
    client = TestClient(auth_app)
    resp = client.post(
        "/auth/login", json={"username": "ghost@acme.com", "password": "x"}
    )
    assert resp.status_code == 401


def test_me_returns_claims():
    client = TestClient(auth_app)
    token = client.post(
        "/auth/login", json={"username": "carol@acme.com", "password": "carol123"}
    ).json()["access_token"]
    resp = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    claims = resp.json()
    assert claims["sub"] == "carol@acme.com"
    assert claims["role"] == "finance_agent"
    assert claims["dept"] == "finance"


def test_me_missing_token():
    client = TestClient(auth_app)
    assert client.get("/auth/me").status_code == 401


def test_login_reads_store_after_member_creation(member_store):
    """Mk2 Phase 1: auth reads the MemberStore — a freshly created member can log in."""
    member_store.create(
        "molly@acme.com",
        "molly123",
        role="marketing_agent",
        dept="marketing",
        scopes=["crm:read", "crm:create"],
        actor="admin@acme.com",
    )
    client = TestClient(auth_app)
    resp = client.post("/auth/login", json={"username": "molly@acme.com", "password": "molly123"})
    assert resp.status_code == 200
    claims = resp.json()["claims"]
    assert claims["role"] == "marketing_agent"
    # USERS mapping is store-backed: the new member shows up through the shim too.
    assert USERS["molly@acme.com"].dept == "marketing"

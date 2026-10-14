"""Mk18 Phase C — dept-service auth + login throttle tests."""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.services import crm
from tests.attack.helpers import service_headers


def test_service_rejects_anonymous_and_forged(tmp_path, member_store):
    app = crm.create_app(tmp_path / "crm.sqlite")
    c = TestClient(app)
    assert c.get("/customers").status_code == 401
    assert c.get("/customers", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post("/customers", json={"name": "x"}).status_code == 401
    assert c.get("/health").json()["status"] == "ok"


def test_service_accepts_member_passthrough(tmp_path, member_store):
    app = crm.create_app(tmp_path / "crm.sqlite")
    c = TestClient(app)
    r = c.get("/customers", headers=service_headers("alice@acme.com"))
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_service_rejects_deactivated(tmp_path, member_store):
    member_store.set_active("alice@acme.com", False, actor="admin@acme.com")
    try:
        app = crm.create_app(tmp_path / "crm.sqlite")
        c = TestClient(app)
        assert c.get("/customers", headers=service_headers("alice@acme.com")).status_code == 403
    finally:
        member_store.set_active("alice@acme.com", True, actor="admin@acme.com")


def test_executor_attaches_bearer_token(tmp_path, member_store):
    """ToolExecutor sends the per-run token; services see the caller."""
    import httpx

    from app.agent.tools import TOOL_REGISTRY, ToolExecutor
    from app.config import get_settings

    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization", "")
        from tests.conftest import serve_request

        return serve_request(crm.create_app(tmp_path / "crm2.sqlite"), request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    ex = ToolExecutor(client=client, settings=get_settings(), auth_token="tok-123")
    out = ex.execute(TOOL_REGISTRY["crm_search_customer"], {"query": "acme"})
    assert seen["auth"] == "Bearer tok-123"
    assert not out.ok and out.status_code == 401  # forged token rejected at the service
    # Real member token passes through and executes.
    ex.set_auth_token(service_headers("alice@acme.com")["Authorization"].split(" ", 1)[1])
    out2 = ex.execute(TOOL_REGISTRY["crm_search_customer"], {"query": "acme"})
    assert out2.ok
    ex.set_auth_token(None)
    assert ex._auth_token is None


def test_login_throttle_locks_after_ten_fails(member_store):
    for _ in range(10):
        assert member_store.verify("alice@acme.com", "wrong-pw") is False
    # Locked now: even the right password fails closed with 401 semantics.
    assert member_store.verify("alice@acme.com", "alice123") is False
    # Other members unaffected.
    assert member_store.verify("bob@acme.com", "bob123") is True


def test_totp_opt_in(member_store):
    import base64
    import hmac as _hmac
    import struct as _struct
    import time

    from app.auth.store import verify_totp

    secret = base64.b32encode(b"12345678901234567890").decode()
    member_store.set_totp_secret("alice@acme.com", secret, actor="admin@acme.com")
    try:
        step = int(time.time()) // 30
        key = base64.b32decode(secret)
        msg = _struct.pack(">Q", step)
        d = _hmac.new(key, msg, __import__("hashlib").sha1).digest()
        o = d[-1] & 0x0F
        num = (_struct.unpack(">I", d[o : o + 4])[0] & 0x7FFFFFFF) % 1_000_000
        code = f"{num:06d}"
        assert verify_totp(secret, code) is True
        assert verify_totp(secret, "000000") is False
        # Password still verifies (second factor is enforced at the service layer).
        assert member_store.verify("alice@acme.com", "alice123") is True
    finally:
        member_store.set_totp_secret("alice@acme.com", "", actor="admin@acme.com")


def test_totp_enforced_at_login_service(member_store):
    """With a secret set, /auth/login without (or with wrong) totp is 401."""
    import base64

    from fastapi.testclient import TestClient

    from app.auth.service import app as auth_app

    secret = base64.b32encode(b"12345678901234567890").decode()
    member_store.set_totp_secret("alice@acme.com", secret, actor="admin@acme.com")
    try:
        c = TestClient(auth_app)
        assert c.post("/auth/login", json={"username": "alice@acme.com", "password": "alice123"}).status_code == 401
        assert c.post("/auth/login", json={"username": "alice@acme.com", "password": "alice123", "totp": "000000"}).status_code == 401
    finally:
        member_store.set_totp_secret("alice@acme.com", "", actor="admin@acme.com")
    # Cleared → password-only login works again.
    c = TestClient(auth_app)
    assert c.post("/auth/login", json={"username": "alice@acme.com", "password": "alice123"}).status_code == 200

"""MemberStore — Mk2 Phase 1 (plan_mk2 §4 row 1).

Covers: migration/seeding of the 5 users, scrypt password hashing (no
plaintext in the DB), store-backed `/auth/login` + `/auth/me`, creating a new
member (the Phase-1 acceptance: `molly@acme.com` logs in), the lockout guard
(last admin / self), and audit logging on every change.

Same fixtures as the rest of the suite: the autouse `member_store` fixture
swaps the shared store to a fresh seeded tmp DB per test.
"""

import pytest
from fastapi.testclient import TestClient

from app.auth.service import app as auth_app
from app.auth.store import (
    SEED_MEMBERS,
    LockoutGuardError,
    MemberError,
    MemberExistsError,
    hash_password,
    verify_password,
)
from app.auth.users import USERS, claims_for, get_user
from app.config import get_settings


def login(client, username: str, password: str):
    return client.post("/auth/login", json={"username": username, "password": password})


def test_seed_migrated_into_store(member_store):
    members = member_store.list()
    assert {m.username for m in members} == {s["username"] for s in SEED_MEMBERS}
    admin = member_store.get("admin@acme.com")
    assert admin is not None
    assert admin.role == "admin" and admin.active is True
    assert member_store.get("ghost@acme.com") is None


def test_passwords_stored_hashed_not_plaintext(member_store):
    for seed in SEED_MEMBERS:
        record = member_store.get(seed["username"])
        assert record is not None
        assert record.password_hash != seed["password"]
        assert seed["password"] not in record.password_hash
        assert record.password_hash.startswith("scrypt$")


def test_hash_verify_roundtrip():
    digest = hash_password("s3cret!")
    assert digest.startswith("scrypt$")
    assert verify_password("s3cret!", digest) is True
    assert verify_password("wrong", digest) is False
    assert verify_password("s3cret!", "not-a-hash") is False


def test_login_seed_success_and_rejects_wrong_and_unknown(member_store):
    client = TestClient(auth_app)

    ok = login(client, "alice@acme.com", "alice123")
    assert ok.status_code == 200
    assert ok.json()["claims"]["role"] == "marketing_agent"
    assert ok.json()["claims"]["dept"] == "marketing"

    assert login(client, "alice@acme.com", "nope").status_code == 401
    assert login(client, "ghost@acme.com", "x").status_code == 401


def test_inactive_member_cannot_login(member_store):
    member_store.set_active("alice@acme.com", False, actor="admin@acme.com")
    assert login(TestClient(auth_app), "alice@acme.com", "alice123").status_code == 401
    # wrong-password AND inactive both collapse to the same 401 envelope.
    assert member_store.verify("alice@acme.com", "alice123") is False


def test_create_member_then_login_phase1_acceptance(member_store):
    """Mk2 Phase-1 acceptance: `POST /auth/login` works for molly after creation."""
    member_store.create(
        "molly@acme.com",
        "molly123",
        role="marketing_agent",
        dept="marketing",
        scopes=["crm:read", "crm:create"],
        actor="admin@acme.com",
    )
    resp = login(TestClient(auth_app), "molly@acme.com", "molly123")
    assert resp.status_code == 200
    claims = resp.json()["claims"]
    assert claims["sub"] == "molly@acme.com"
    assert claims["role"] == "marketing_agent"
    assert claims["dept"] == "marketing"
    assert claims["scopes"] == ["crm:read", "crm:create"]

    with pytest.raises(MemberExistsError):
        member_store.create("molly@acme.com", "x", role="marketing_agent", dept="marketing", actor="admin@acme.com")


def test_me_reflects_store_claims_after_update(member_store):
    member_store.create(
        "molly@acme.com", "molly123", role="marketing_agent", dept="marketing",
        scopes=["crm:read"], actor="admin@acme.com",
    )
    client = TestClient(auth_app)
    token = login(client, "molly@acme.com", "molly123").json()["access_token"]

    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["role"] == "marketing_agent"

    # Mk19 Phase 6: an admin edit supersedes her old token (cred_ver bump)...
    member_store.update("molly@acme.com", role="hr_agent", dept="hr", scopes=["hr:read"], actor="admin@acme.com")
    stale = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert stale.status_code == 401
    assert "superseded" in stale.json()["detail"]

    # ...and a fresh login's /auth/me reflects the store claims.
    refreshed = login(client, "molly@acme.com", "molly123").json()["access_token"]
    me2 = client.get("/auth/me", headers={"Authorization": f"Bearer {refreshed}"})
    assert me2.status_code == 200
    assert me2.json()["role"] == "hr_agent"
    assert me2.json()["dept"] == "hr"


def test_update_unknown_member_raises(member_store):
    with pytest.raises(MemberError):
        member_store.update("ghost@acme.com", role="hr_agent", dept="hr", actor="admin@acme.com")


def test_reset_password_then_login(member_store):
    member_store.reset_password("carol@acme.com", "newpass99", actor="admin@acme.com")
    assert login(TestClient(auth_app), "carol@acme.com", "newpass99").status_code == 200
    assert login(TestClient(auth_app), "carol@acme.com", "carol123").status_code == 401


def test_lockout_self_deactivation_rejected(member_store):
    with pytest.raises(LockoutGuardError):
        member_store.set_active("admin@acme.com", False, actor="admin@acme.com")
    assert member_store.get("admin@acme.com").active is True


def test_lockout_last_active_admin_rejected(member_store):
    with pytest.raises(LockoutGuardError):
        member_store.set_active("admin@acme.com", False, actor="alice@acme.com")
    assert member_store.get("admin@acme.com").active is True


def test_deactivate_admin_allowed_when_another_admin_exists(member_store):
    member_store.create("sec@acme.com", "sec123", role="admin", dept="admin", scopes=["*"], actor="admin@acme.com")
    updated = member_store.set_active("admin@acme.com", False, actor="sec@acme.com")
    assert updated.active is False


def test_deactivate_regular_member_and_reactivate(member_store):
    member_store.create("molly@acme.com", "molly123", role="marketing_agent", dept="marketing", actor="admin@acme.com")
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    assert member_store.get("molly@acme.com").active is False
    assert member_store.verify("molly@acme.com", "molly123") is False
    member_store.set_active("molly@acme.com", True, actor="admin@acme.com")
    assert member_store.verify("molly@acme.com", "molly123") is True


def test_changes_are_audit_logged(member_store):
    audit = member_store._audit_log
    before = len(audit.entries())
    member_store.create("molly@acme.com", "molly123", role="marketing_agent", dept="marketing", actor="admin@acme.com")
    member_store.update("molly@acme.com", role="hr_agent", dept="hr", actor="admin@acme.com")
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    member_store.reset_password("molly@acme.com", "zzz", actor="admin@acme.com")

    entries = audit.entries()[before:]
    assert [e["decision"] for e in entries] == ["ALLOW"] * 4
    assert {e["resource"]["type"] for e in entries} == {"member"}
    assert {e["resource"]["id"] for e in entries} == {"molly@acme.com"}
    reasons = {e["reason"] for e in entries}
    assert {"admin:members:create", "admin:members:update", "admin:members:deactivated", "admin:members:password_reset"} <= reasons


def test_claims_for_and_get_user(member_store):
    assert claims_for("alice@acme.com") == {
        "sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing",
        "scopes": ["crm:read", "crm:create"],
    }
    assert get_user("alice@acme.com").role == "marketing_agent"
    assert claims_for("ghost@acme.com") is None
    assert get_user("ghost@acme.com") is None


def test_usres_mapping_reads_store_and_seed_password_only(member_store):
    # The compat shim reads the live store (plan_mk2 §3.3).
    assert "alice@acme.com" in USERS
    assert len(USERS) == len(SEED_MEMBERS)
    # seed plaintext returned ONLY for the 5 demo accounts; new members get None.
    assert USERS["alice@acme.com"].password == "alice123"
    member_store.create("molly@acme.com", "molly123", role="marketing_agent", dept="marketing", actor="admin@acme.com")
    assert USERS["molly@acme.com"].password is None
    assert {u.username for u in USERS.values()} == {
        s["username"] for s in SEED_MEMBERS
    } | {"molly@acme.com"}


def test_me_missing_member_is_404(member_store):
    """A validly-signed token for a member absent from the store -> 404."""
    from app.auth.jwt import create_token
    from app.auth.users import User

    secret = get_settings()
    token = create_token(
        User("ghost@acme.com", None, "hr_agent", "hr", []),
        secret.jwt.secret,
        secret.jwt.algorithm,
        secret.jwt.expiry_minutes,
    )
    resp = TestClient(auth_app).get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 404
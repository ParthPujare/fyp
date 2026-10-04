"""Mk16 follow-up — DB-backed shared platform key (set once, used by every instance).

Contract tests run the same assertions on SQLite and Postgres (`make test-pg`
for the latter); endpoint + resolution tests run offline on an isolated tmp store.
"""

from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app.auth.jwt import create_token
from app.auth.pg_store import PgMemberStore
from app.auth.store import MemberError, MemberStore
from app.auth.users import USERS
from app.config import Settings, get_settings
from app.dashboard.app import app as dashboard_app
from app.harness.audit import AuditLog
from tests.conftest import TEST_URL

FREE_MODEL = "qwen/qwen3.8-27b:free"


@pytest.fixture(params=["sqlite", "postgres"])
def pstores(request, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    if request.param == "postgres":
        if not TEST_URL:
            pytest.skip("set TEST_DATABASE_URL (make test-pg) for Postgres tests")
        pool, _admin, _schema = request.getfixturevalue("pg")
        return SimpleNamespace(
            kind="postgres",
            store=PgMemberStore(pool, audit=audit, version_ttl=0, read_ttl=0),
        )
    return SimpleNamespace(kind="sqlite", store=MemberStore(tmp_path / "members.sqlite", audit=audit))


@pytest.fixture(autouse=True)
def _clean_platform_state():
    from app.llm import system as system_mod
    from app.memory import system_status

    system_mod.clear_system_provider_cache()
    system_status.reset()
    yield
    system_mod.clear_system_provider_cache()
    system_status.reset()


def _env_settings(key: str = "") -> Settings:
    s = Settings()
    s.system.openrouter_api_key = key or None
    s.openrouter_api_key = None
    return s


# ---- store contract (both back-ends) -------------------------------------------


def test_set_get_meta_roundtrip(pstores):
    store = pstores.store
    assert store.get_platform_llm() is None
    assert store.get_platform_llm_meta() is None
    model, by, at = store.set_platform_llm(model=FREE_MODEL, api_key_plaintext="sk-live", actor="admin@acme.com")
    assert model == FREE_MODEL and by == "admin@acme.com" and at
    meta = store.get_platform_llm_meta()
    assert meta == (FREE_MODEL, "admin@acme.com", at)
    full = store.get_platform_llm()
    assert full == (FREE_MODEL, "sk-live", "admin@acme.com", at)


def test_key_never_plaintext_at_rest(pstores, tmp_path):
    store = pstores.store
    store.set_platform_llm(model=FREE_MODEL, api_key_plaintext="sk-super-secret", actor="admin@acme.com")
    if pstores.kind == "sqlite":
        conn = sqlite3.connect(store.path)
        try:
            row = conn.execute("SELECT api_key_encrypted FROM platform_llm").fetchone()
        finally:
            conn.close()
        assert row[0] != "sk-super-secret" and "sk-super-secret" not in row[0]


def test_model_only_change_keeps_key(pstores):
    store = pstores.store
    store.set_platform_llm(model=FREE_MODEL, api_key_plaintext="sk-live", actor="admin@acme.com")
    store.set_platform_llm(model="qwen/qwen3.8-27b:free", api_key_plaintext=None, actor="admin@acme.com")
    assert store.get_platform_llm()[1] == "sk-live"


def test_paid_model_rejected(pstores):
    with pytest.raises(ValueError, match=":free"):
        pstores.store.set_platform_llm(model="openai/gpt-4o-mini", api_key_plaintext="sk-x", actor="a")


def test_no_key_no_row_rejected(pstores):
    with pytest.raises(MemberError, match="no platform key"):
        pstores.store.set_platform_llm(model=FREE_MODEL, api_key_plaintext=None, actor="a")


def test_clear_reverts(pstores):
    store = pstores.store
    assert store.clear_platform_llm(actor="a") is False
    store.set_platform_llm(model=FREE_MODEL, api_key_plaintext="sk-live", actor="a")
    assert store.clear_platform_llm(actor="a") is True
    assert store.get_platform_llm() is None


def test_cross_instance_visibility_pg(tmp_path, pg):
    """A second store object (another laptop) sees the row: the sharing claim."""
    from app.auth.pg_store import PgMemberStore as _Pg
    from app.harness.audit import AuditLog as _Audit

    pool, _admin, _schema = pg
    a = _Pg(pool, audit=_Audit(tmp_path / "a.jsonl"), version_ttl=0, read_ttl=0)
    b = _Pg(pool, audit=_Audit(tmp_path / "b.jsonl"), version_ttl=0, read_ttl=0)
    a.set_platform_llm(model=FREE_MODEL, api_key_plaintext="sk-shared", actor="admin@acme.com")
    assert b.get_platform_llm() == a.get_platform_llm()
    assert b.get_platform_llm()[1] == "sk-shared"


# ---- resolution precedence (offline, isolated tmp store) ------------------------


def test_db_wins_over_env(member_store):
    from app.llm import system as system_mod

    member_store.set_platform_llm(model=FREE_MODEL, api_key_plaintext="sk-db", actor="a")
    resolved = system_mod.resolve_platform_config(_env_settings("sk-env"))
    assert resolved == (FREE_MODEL, "sk-db", "db")


def test_env_fallback_when_no_row(member_store):
    from app.llm import system as system_mod

    resolved = system_mod.resolve_platform_config(_env_settings("sk-env"))
    assert resolved is not None and resolved[1:] == ("sk-env", "env")


def test_none_when_neither(member_store):
    from app.llm import system as system_mod

    assert system_mod.resolve_platform_config(_env_settings("")) is None
    source, model = system_mod.describe_platform_source(_env_settings(""))
    assert (source, model) == ("none", None)


def test_decrypt_failure_falls_back_to_env_and_records(member_store, tmp_path):
    """Foreign Fernet row (key drift) → env fallback + decrypt-failed status."""
    from app.llm import system as system_mod
    from app.memory import system_status

    foreign = Fernet.generate_key()
    token = Fernet(foreign).encrypt(b"sk-db").decode()
    conn = sqlite3.connect(member_store.path)
    try:
        conn.execute(
            "INSERT INTO platform_llm (id, model, api_key_encrypted, updated_by, updated_at)"
            " VALUES ('openrouter', ?, ?, 'a', 'now')",
            (FREE_MODEL, token),
        )
        conn.commit()
    finally:
        conn.close()
    resolved = system_mod.resolve_platform_config(_env_settings("sk-env"))
    assert resolved is not None and resolved[1:] == ("sk-env", "env")
    assert system_status.get_status(True)["last_error"] == "decrypt-failed"


# ---- endpoints (offline, isolated tmp store via autouse member_store) ------------


def _auth(username: str) -> dict[str, str]:
    settings = get_settings()
    token = create_token(
        USERS[username], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )
    return {"Authorization": f"Bearer {token}"}


def test_config_endpoints_gate():
    client = TestClient(dashboard_app)
    assert client.get("/api/system-memory/config").status_code == 401
    assert client.put("/api/system-memory/config", json={"model": FREE_MODEL}).status_code == 401
    assert client.delete("/api/system-memory/config").status_code == 401
    alice = _auth("alice@acme.com")
    assert client.get("/api/system-memory/config", headers=alice).status_code == 403
    assert client.put("/api/system-memory/config", json={"model": FREE_MODEL}, headers=alice).status_code == 403
    assert client.delete("/api/system-memory/config", headers=alice).status_code == 403


def test_config_put_get_delete_flow(member_store):
    client = TestClient(dashboard_app)
    admin = _auth("admin@acme.com")
    assert client.put("/api/system-memory/config", json={}, headers=admin).status_code == 422
    assert client.put(
        "/api/system-memory/config", json={"model": "openai/gpt-4o", "api_key": "sk-x"}, headers=admin
    ).status_code == 422
    body = client.put(
        "/api/system-memory/config",
        json={"model": FREE_MODEL, "api_key": "sk-live"},
        headers=admin,
    ).json()
    assert body["configured"] is True and body["source"] == "db" and body["model"] == FREE_MODEL
    assert "sk-live" not in str(body)
    got = client.get("/api/system-memory/config", headers=admin).json()
    assert got["updated_by"] == "admin@acme.com" and "sk-live" not in str(got)
    # model-only change keeps the key
    client.put("/api/system-memory/config", json={"model": FREE_MODEL}, headers=admin)
    from app.llm import system as system_mod

    assert system_mod.resolve_system_key() == "sk-live"
    # clear reverts to env (blank in tests → unconfigured)
    cleared = client.delete("/api/system-memory/config", headers=admin).json()
    assert cleared["removed"] is True and cleared["configured"] is False


def test_status_reports_db_source(member_store):
    from app.llm import system as system_mod

    member_store.set_platform_llm(model=FREE_MODEL, api_key_plaintext="sk-live", actor="a")
    source, model = system_mod.describe_platform_source(_env_settings("sk-env"))
    assert (source, model) == ("db", FREE_MODEL)

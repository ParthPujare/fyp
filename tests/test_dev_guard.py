"""Mk19 Phase 4 — fail-closed dev secrets + seeded-credential lockdown (plan Mk19, finding 4/5).

The repo-public dev secrets (JWT__SECRET, LLM__FERNET_KEY) and the 5 seeded
demo passwords are dev conveniences that must never run, or stay usable, in a
real deployment. The hatch is explicit (`ALLOW_DEV_SECRETS=1`) AND dev-only
(`ENV=dev`); non-dev stores never seed members and deactivate any member whose
password still matches the published seed plaintext.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.auth.store import SEED_PASSWORDS, MemberStore
from app.config import DEFAULT_FERNET_KEY, DEFAULT_JWT_SECRET, Settings, get_settings

# Prod-mode env: real-looking secrets so the built-in-default check passes and
# the test exercises ONLY the seeding/lockdown behavior. Mk19 Phase 5: prod
# also requires keyed audit/trace chains, so the HMAC keys are part of "real".
_PROD_REAL_SECRETS = {
    "ENV": "prod",
    "JWT__SECRET": "prod-secret-0123456789abcdef-prod-secret",
    "LLM__FERNET_KEY": "prod-fernet-key-not-a-real-one-but-nondefault-000=",
    "SERVICES__AUDIT_HMAC_KEY": "prod-audit-hmac-not-a-real-key-0001",
    "SERVICES__TRACE_HMAC_KEY": "prod-trace-hmac-not-a-real-key-0001",
}


def _settings(**overrides) -> Settings:
    """Settings from kwargs only — a dev's real `.env` must not leak in."""
    return Settings(_env_file=None, **overrides)


# ---------------------------------------------------------------- guard matrix

def test_default_secrets_with_env_prod_raise():
    # keyed chains first: the HMAC validator must not mask the dev-defaults refusal
    with pytest.raises(ValidationError, match="built-in dev default"):
        _settings(
            env="prod",
            allow_dev_secrets=False,
            services={"audit_hmac_key": "k", "trace_hmac_key": "k"},
        )


def test_dev_without_hatch_raises():
    """ENV=dev alone is no longer enough — the hatch is explicit (finding 4)."""
    with pytest.raises(ValidationError, match="ALLOW_DEV_SECRETS"):
        _settings(env="dev", allow_dev_secrets=False)


def test_dev_with_hatch_loads():
    s = _settings(env="dev", allow_dev_secrets=True)
    assert s.allow_dev_secrets is True
    assert s.jwt.secret == DEFAULT_JWT_SECRET
    assert s.llm.fernet_key == DEFAULT_FERNET_KEY


def test_hatch_with_env_prod_still_raises():
    """The hatch never applies outside dev — even a stray ALLOW_DEV_SECRETS=1
    in prod is refused loudly, not silently ignored."""
    with pytest.raises(ValidationError, match="only valid with ENV=dev"):
        _settings(
            env="prod",
            allow_dev_secrets=True,
            services={"audit_hmac_key": "k", "trace_hmac_key": "k"},
        )


def test_prod_with_real_secrets_loads_without_hatch(monkeypatch):
    # env vars carry the secret names (JWT__SECRET etc.); explicit kwargs carry
    # the field names — and beat conftest's suite-wide hatch env var.
    for k in ("JWT__SECRET", "LLM__FERNET_KEY", "SERVICES__AUDIT_HMAC_KEY", "SERVICES__TRACE_HMAC_KEY"):
        monkeypatch.setenv(k, _PROD_REAL_SECRETS[k])
    s = Settings(_env_file=None, env="prod", allow_dev_secrets=False)
    assert s.env == "prod"
    assert s.allow_dev_secrets is False


# ------------------------------------------------------- store seeding/lockdown

def test_non_dev_fresh_store_has_zero_members(tmp_path, monkeypatch):
    for k, v in _PROD_REAL_SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ALLOW_DEV_SECRETS", raising=False)
    get_settings.cache_clear()
    try:
        # distinct filename: conftest's autouse fixture already seeded
        # tmp_path/"members.sqlite" in dev mode
        store = MemberStore(tmp_path / "fresh.sqlite")
        assert store.list() == []  # departments may seed; members must not
    finally:
        get_settings.cache_clear()


def test_dev_seeded_store_deactivates_seed_passwords_in_prod(tmp_path, monkeypatch):
    """A store that ran dev seeding must not be promotable to prod with those
    passwords live: init with ENV=prod deactivates every unrotated seed account."""
    # 1) seed in dev (conftest hatch applies — same as a real dev machine);
    #    distinct filename: conftest's autouse fixture owns "members.sqlite"
    monkeypatch.setenv("ENV", "dev")
    get_settings.cache_clear()
    try:
        db = tmp_path / "promotion.sqlite"
        dev_store = MemberStore(db)
        seeded = {m.username for m in dev_store.list()}
        assert seeded == set(SEED_PASSWORDS)
        assert all(m.active for m in dev_store.list())
    finally:
        get_settings.cache_clear()

    # 2) rotate ONE account's password — that member must survive lockdown
    rotated = next(iter(SEED_PASSWORDS))
    dev_store2 = MemberStore(db)
    dev_store2.reset_password(rotated, "a-long-rotated-password-123!", actor="admin@acme.com")

    # 3) "promote to prod": same file, ENV=prod with real-looking secrets
    for k, v in _PROD_REAL_SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ALLOW_DEV_SECRETS", raising=False)
    get_settings.cache_clear()
    try:
        prod_store = MemberStore(db)
        by_user = {m.username: m for m in prod_store.list()}
        for username in SEED_PASSWORDS:
            if username == rotated:
                assert by_user[username].active, "rotated account must stay active"
            else:
                assert not by_user[username].active, f"{username} still holds a seed password"
    finally:
        get_settings.cache_clear()


def test_lockdown_is_idempotent(tmp_path, monkeypatch):
    for k, v in _PROD_REAL_SECRETS.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("ALLOW_DEV_SECRETS", raising=False)
    get_settings.cache_clear()
    try:
        # dev seed first
        monkeypatch.setenv("ENV", "dev")
        get_settings.cache_clear()
        db = tmp_path / "idem.sqlite"
        MemberStore(db)
        get_settings.cache_clear()
        monkeypatch.setenv("ENV", "prod")
        get_settings.cache_clear()
        MemberStore(db)  # lockdown pass 1
        store = MemberStore(db)  # pass 2: no crash, no change
        assert all(not m.active for m in store.list())
    finally:
        get_settings.cache_clear()


# --------------------------------------------- Mk19 Phase 5: HMAC enforcement

def test_prod_without_hmac_keys_raises():
    """ENV=prod with real secrets but unkeyed chains must refuse to boot."""
    s_base = {k: v for k, v in _PROD_REAL_SECRETS.items() if k != "ENV"}
    with pytest.raises(ValidationError, match="HMAC_KEY"):
        Settings(
            _env_file=None,
            env="prod",
            allow_dev_secrets=False,
            jwt={"secret": s_base["JWT__SECRET"]},
            llm={"fernet_key": s_base["LLM__FERNET_KEY"]},
            services={},  # audit/trace keys blank
        )


def test_prod_with_only_audit_key_raises():
    """One chain keyed, the other blank: still refuses."""
    with pytest.raises(ValidationError, match="SERVICES__TRACE_HMAC_KEY"):
        Settings(
            _env_file=None,
            env="prod",
            allow_dev_secrets=False,
            jwt={"secret": _PROD_REAL_SECRETS["JWT__SECRET"]},
            llm={"fernet_key": _PROD_REAL_SECRETS["LLM__FERNET_KEY"]},
            services={"audit_hmac_key": "a-key"},
        )


def test_dev_without_hmac_keys_loads_with_warning(caplog, monkeypatch):
    """Dev keeps the unkeyed default; open_audit_log warns once (finding 7 fix)."""
    import logging

    from app.harness import audit as audit_mod

    # hermetic: a dev's real .env must not carry a key into this test
    monkeypatch.delenv("SERVICES__AUDIT_HMAC_KEY", raising=False)
    get_settings.cache_clear()
    try:
        s = Settings(_env_file=None, env="dev", allow_dev_secrets=True)
        assert s.services.audit_hmac_key is None
        audit_mod._warned_unkeyed.clear()  # other tests may have consumed the one-shot
        with caplog.at_level(logging.WARNING, logger="app.harness.audit"):
            audit_mod.open_audit_log("data/audit.jsonl")
            audit_mod.open_audit_log("data/audit.jsonl")  # second open: no repeat
        warnings = [r for r in caplog.records if "UNKEYED" in r.getMessage()]
        assert len(warnings) == 1
    finally:
        get_settings.cache_clear()

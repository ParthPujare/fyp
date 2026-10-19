"""Mk13 Phase 2 — the stores on the shared Postgres.

Three layers:
1. **Adapter unit tests** (always run): `Row` / `Cursor` behave like sqlite3's.
2. **Contract tests** (parametrised `sqlite` + `postgres`): the *same assertions* run against
   the original SQLite stores and the Postgres ones — the point of running one store
   implementation on two back-ends. The Postgres cases need `TEST_DATABASE_URL`.
3. **Postgres-only tests**: the read cache (throttling, cross-"laptop" invalidation, own-write
   visibility), races (duplicate create, simultaneous first start), and the session's
   transaction / dead-connection behaviour.

Contract tests build `PgMemberStore` with `version_ttl=0, read_ttl=0` so the cache is off and
they exercise store *logic*; the cache has its own tests below.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

import app.memory.store as store_mod
from app.auth.pg_store import PgMemberStore
from app.auth.store import (
    LockoutGuardError,
    MemberError,
    MemberExistsError,
    MemberStore,
)
from app.config import MemorySettings
from app.db.compat import Cursor, PgSession, Row
from app.db.migrate import apply_pending
from app.harness.audit import AuditLog
from app.memory.approvals import ApprovalStore
from app.memory.edit import MemoryEditor
from app.memory.pg_stores import PgApprovalStore, PgBaseStore
from app.memory.scopes import scope_to_namespace, scope_user
from app.memory.store import SqliteBaseStore
from tests.conftest import TEST_URL

# ================================================================ adapter (unit, always runs)


def test_row_supports_name_index_keys_len_and_value_iteration():
    row = Row({"id": 0, "name": 1}, (7, "alice"))
    assert row["id"] == 7 and row["name"] == "alice"
    assert row[0] == 7 and row[1] == "alice"
    assert row.keys() == ["id", "name"] and len(row) == 2
    assert list(row) == [7, "alice"]  # like sqlite3.Row: iterates VALUES
    assert dict(row) == {"id": 7, "name": "alice"}
    with pytest.raises(KeyError):
        row["nope"]


def test_cursor_fetch_semantics_match_sqlite():
    cur = Cursor(["n"], [(1,), (2,), (3,)], rowcount=3)
    assert cur.fetchone()["n"] == 1
    assert [r["n"] for r in cur.fetchall()] == [2, 3]  # fetchall returns only what is left
    assert cur.fetchone() is None and cur.fetchall() == []
    assert cur.rowcount == 3
    empty = Cursor([], [], rowcount=0)
    assert empty.fetchone() is None


# ================================================================ backend fixture


@pytest.fixture(params=["sqlite", "postgres"])
def stores(request, tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    if request.param == "postgres":
        if not TEST_URL:
            pytest.skip("set TEST_DATABASE_URL (make test-pg) for Postgres tests")
        pool, _admin, _schema = request.getfixturevalue("pg")
        return SimpleNamespace(
            kind="postgres",
            members=PgMemberStore(pool, audit=audit, version_ttl=0, read_ttl=0),
            approvals=PgApprovalStore(pool),
            base=PgBaseStore(pool),
        )
    return SimpleNamespace(
        kind="sqlite",
        members=MemberStore(tmp_path / "members.sqlite", audit=audit),
        approvals=ApprovalStore(tmp_path / "memory.sqlite"),
        base=SqliteBaseStore(tmp_path / "memory.sqlite"),
    )


# ================================================================ contract: members


def test_first_start_seeds_the_demo_members_and_departments(stores):
    m = stores.members
    names = {x.username for x in m.list()}
    assert {"alice@acme.com", "bob@acme.com", "carol@acme.com", "dave@acme.com", "admin@acme.com"} <= names
    assert m.get("admin@acme.com").role == "admin" and m.is_active("admin@acme.com")
    assert m.get("nobody@acme.com") is None and not m.is_active("nobody@acme.com")
    assert {"marketing", "hr", "finance", "devops"} <= {d.name for d in m.list_departments()}


def test_create_update_and_duplicate(stores):
    m = stores.members
    rec = m.create("eve@acme.com", "pw-eve-1", "hr_agent", "hr", ["hr:read"], actor="admin@acme.com")
    assert (rec.role, rec.dept, rec.scopes, rec.active) == ("hr_agent", "hr", ["hr:read"], True)
    with pytest.raises(MemberExistsError):
        m.create("eve@acme.com", "other", "hr_agent", "hr")
    upd = m.update("eve@acme.com", role="finance_agent", dept="finance", scopes=[], actor="admin@acme.com")
    assert (upd.role, upd.dept) == ("finance_agent", "finance")
    with pytest.raises(MemberError, match="head role"):
        m.create("head@acme.com", "pw", "hr_head", "finance")  # a *_head role must own its dept
    with pytest.raises(MemberError):
        m.update("ghost@acme.com", role="x", dept="y", actor="admin@acme.com")


def test_every_member_change_bumps_the_shared_version(stores):
    m = stores.members
    seen = [m.version]
    m.create("v@acme.com", "pw", "hr_agent", "hr")
    seen.append(m.version)
    m.update("v@acme.com", role="hr_agent", dept="hr", scopes=["x"], actor="admin@acme.com")
    seen.append(m.version)
    m.set_active("v@acme.com", False, actor="admin@acme.com")
    seen.append(m.version)
    m.reset_password("v@acme.com", "newpw", actor="admin@acme.com")
    seen.append(m.version)
    m.register_department("ops", "ops-svc", "http://127.0.0.1:9", ["x"], actor="admin@acme.com")
    seen.append(m.version)
    assert seen == sorted(set(seen)) and len(seen) == 6  # strictly increasing


def test_admin_lockout_guards(stores):
    m = stores.members
    with pytest.raises(LockoutGuardError):
        m.set_active("admin@acme.com", False, actor="admin@acme.com")  # not yourself
    with pytest.raises(LockoutGuardError):
        m.set_active("admin@acme.com", False, actor="someone-else")  # not the last active admin
    m.create("admin2@acme.com", "pw", "admin", "admin")
    assert m.set_active("admin@acme.com", False, actor="admin2@acme.com").active is False


def test_password_verification_lifecycle(stores):
    m = stores.members
    m.create("pw@acme.com", "secret-1", "hr_agent", "hr")
    assert m.verify("pw@acme.com", "secret-1") is True
    assert m.verify("pw@acme.com", "wrong") is False
    assert m.verify("ghost@acme.com", "secret-1") is False
    m.reset_password("pw@acme.com", "secret-2", actor="admin@acme.com")
    assert m.verify("pw@acme.com", "secret-1") is False and m.verify("pw@acme.com", "secret-2") is True
    m.set_active("pw@acme.com", False, actor="admin@acme.com")
    assert m.verify("pw@acme.com", "secret-2") is False  # inactive members cannot sign in


def test_department_registration(stores):
    m = stores.members
    d = m.register_department("legal", "legal-svc", "http://127.0.0.1:9", ["contracts"], actor="admin@acme.com")
    assert (d.name, d.role, d.resources) == ("legal", "legal_agent", ["contracts"])
    assert m.get_department("legal") is not None
    with pytest.raises(Exception, match="already exists"):
        m.register_department("legal", "x", "http://x", actor="admin@acme.com")


def test_llm_keys_keep_a_single_active_key_and_reject_duplicates(stores):
    m = stores.members
    a = m.set_llm_key("alice@acme.com", "openai", "gpt-4o-mini", "sk-aaa", actor="alice@acme.com")
    b = m.set_llm_key("alice@acme.com", "gemini", "gemini-3.6-flash", "g-bbb", actor="alice@acme.com")
    keys = {k.model: k for k in m.list_llm_keys("alice@acme.com")}
    assert keys["gpt-4o-mini"].active is False and keys["gemini-3.6-flash"].active is True
    assert m.get_active_llm_key("alice@acme.com")[:3] == ("gemini", "gemini-3.6-flash", "g-bbb")
    assert m.get_active_llm_key_meta("alice@acme.com") == ("gemini", "gemini-3.6-flash")
    m.activate_llm_key("alice@acme.com", a.id)
    assert m.get_active_llm_key("alice@acme.com")[2] == "sk-aaa"  # decrypts to what was stored
    assert m.get_llm_key_by_id("alice@acme.com", b.id)[2] == "g-bbb"
    c = m.set_llm_key("alice@acme.com", "openai", "gpt-4.1", "sk-ccc", active=False)
    with pytest.raises(MemberError, match="already exists"):  # (openai, gpt-4o-mini) is taken by `a`
        m.update_llm_key("alice@acme.com", c.id, model="gpt-4o-mini")
    m.delete_llm_key("alice@acme.com", a.id)
    m.delete_llm_key("alice@acme.com", c.id)
    assert [k.id for k in m.list_llm_keys("alice@acme.com")] == [b.id]
    with pytest.raises(MemberError, match="not found"):
        m.delete_llm_key("alice@acme.com", "no-such-id")


def test_composio_key_connected_accounts_and_catalog(stores):
    m = stores.members
    assert m.get_composio_key("alice@acme.com") is None
    m.set_composio_key("alice@acme.com", "ck_123")
    assert m.get_composio_key("alice@acme.com") not in (None, "ck_123")  # stored encrypted
    m.delete_composio_key("alice@acme.com")
    assert m.get_composio_key("alice@acme.com") is None

    assert m.is_app_connected("alice@acme.com", "gmail") is False
    m.set_connected_account("alice@acme.com", "gmail", "ca_1", "pending")
    assert m.is_app_connected("alice@acme.com", "gmail") is False
    m.set_connected_account("alice@acme.com", "gmail", "ca_1", "active")  # upsert
    assert m.is_app_connected("alice@acme.com", "gmail") is True
    assert [a["app"] for a in m.list_connected_accounts("alice@acme.com")] == ["gmail"]

    assert {t["toolkit"] for t in m.list_toolkits()} >= {"gmail", "github"}  # seeded catalog
    m.upsert_toolkit("slack", label="Slack", auth_config_id="ac_x")
    m.set_toolkit_enabled("slack", False)
    assert "slack" not in {t["toolkit"] for t in m.list_toolkits(enabled_only=True)}
    with pytest.raises(MemberError, match="not in catalog"):
        m.set_toolkit_enabled("nope", True)

    assert m.cache_toolkit_tools("slack", [{"slug": "SLACK_SEND_MESSAGE", "description": "d"}]) == 1
    cached = m.list_cached_tools("slack")
    assert cached[0]["slug"] == "SLACK_SEND_MESSAGE" and cached[0]["risk"] == "critical"


# ================================================================ contract: approvals


def test_approvals_lifecycle_dedup_and_atomic_decide(stores):
    a = stores.approvals
    res, actor, payload = {"type": "memory", "id": "dept/hr"}, {"sub": "bob@acme.com"}, {"op": "add", "text": "x"}
    first = a.create("run-1", "t-1", res, actor, payload)
    assert a.create("run-1", "t-1", res, actor, payload) == first  # dedup within a run
    assert a.create("run-2", "t-2", res, actor, payload) != first
    assert {i["id"] for i in a.pending_items()} >= {first}
    assert [i["id"] for i in a.pending_for_dept("hr")].count(first) == 1
    assert a.dept_of(a.get(first)) == "hr"

    decided = a.decide(first, True, "admin@acme.com")
    assert decided["status"] == "approved" and decided["decided_by"] == "admin@acme.com"
    with pytest.raises(ValueError, match="already approved"):
        a.decide(first, False, "other")
    with pytest.raises(KeyError):
        a.decide("missing", True, "admin@acme.com")
    assert first not in {i["id"] for i in a.pending_items()}


# ================================================================ contract: memory facts


def test_memory_items_crud_search_and_namespaces(stores):
    s = stores.base
    s.put(("user", "alice"), "k1", {"text": "likes tea"})
    s.put(("user", "alice"), "k2", {"text": "likes coffee"})
    s.put(("dept", "hr"), "k3", {"text": "policy"})
    assert s.get(("user", "alice"), "k1").value == {"text": "likes tea"}
    assert s.get(("user", "alice"), "nope") is None
    s.put(("user", "alice"), "k1", {"text": "likes green tea"})  # upsert
    assert s.get(("user", "alice"), "k1").value["text"] == "likes green tea"
    assert {i.key for i in s.search(("user",))} == {"k1", "k2"}
    assert {i.key for i in s.search(("dept", "hr"))} == {"k3"}
    assert ("dept", "hr") in s.list_namespaces()
    s.delete(("user", "alice"), "k2")
    assert s.get(("user", "alice"), "k2") is None


def test_memory_ties_break_in_insertion_order(stores, monkeypatch):
    """Equal timestamps must not make 'oldest-out' eviction random (audit finding G8)."""

    class Frozen(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(store_mod, "datetime", Frozen)
    s = stores.base
    for i in range(12):
        s.put(("ns",), f"zz-{12 - i:02d}", {"text": f"fact {i}"})  # keys sort opposite to insertion
    assert [it.value["text"] for it in s.items_in(("ns",), oldest_first=True)] == [f"fact {i}" for i in range(12)]
    assert [it.value["text"] for it in s.items_in(("ns",))] == [f"fact {i}" for i in reversed(range(12))]


def test_memory_editor_budget_eviction_works_on_both_backends(stores):
    editor = MemoryEditor(stores.base, MemorySettings(user_bytes=30))
    scope = scope_user("alice@acme.com")
    editor.add(scope, "fact alpha alpha alpha alpha")
    assert len(editor.add(scope, "fact beta beta beta beta").evicted) == 1
    assert len(editor.add(scope, "fact gamma gamma gamma gamma").evicted) == 1
    remaining = stores.base.items_in(scope_to_namespace(scope))
    assert len(remaining) == 1 and "gamma" in remaining[0].value["text"]


# ================================================================ postgres-only: cache


def _count_statements(monkeypatch) -> list[str]:
    seen: list[str] = []
    original = PgSession.execute

    def counting(self, sql, params=()):
        seen.append(sql.split()[0].upper() + " " + sql.split("FROM")[-1].split()[0] if "FROM" in sql else sql.split()[0])
        return original(self, sql, params)

    monkeypatch.setattr(PgSession, "execute", counting)
    return seen


def test_hot_reads_are_served_from_the_cache_not_the_network(pg, monkeypatch):
    """The enforcer asks for `version` on every enforce() and every request calls is_active():
    over a ~150 ms link that must not be one round trip each."""
    pool, _admin, _schema = pg
    m = PgMemberStore(pool)  # real TTLs: version 1 s, reads 3 s
    m.is_active("alice@acme.com")  # warm
    _ = m.version
    seen = _count_statements(monkeypatch)
    for _ in range(200):
        assert m.is_active("alice@acme.com")
        assert m.get("bob@acme.com").dept == "hr"
        _ = m.version
        m.list()
    assert len(seen) <= 3, seen  # at most the first cold reads, never one per call


def test_your_own_write_is_visible_immediately_despite_the_cache(pg):
    pool, _admin, _schema = pg
    m = PgMemberStore(pool)
    assert m.get("new@acme.com") is None  # caches the miss
    m.create("new@acme.com", "pw", "hr_agent", "hr")
    assert m.get("new@acme.com") is not None
    m.set_active("new@acme.com", False, actor="admin@acme.com")
    assert m.is_active("new@acme.com") is False  # no waiting for a TTL to see your own change


def test_another_laptops_change_is_seen_after_the_version_poll(pg):
    """Two stores on one database = two laptops. B must notice A's change without restarting."""
    pool, _admin, _schema = pg
    laptop_a = PgMemberStore(pool, version_ttl=0, read_ttl=60)
    laptop_b = PgMemberStore(pool, version_ttl=0, read_ttl=60)  # long read TTL: only the version can save us
    assert laptop_b.get("teammate@acme.com") is None  # B caches "does not exist"
    v0 = laptop_b.version
    laptop_a.create("teammate@acme.com", "pw", "hr_agent", "hr")
    assert laptop_b.get("teammate@acme.com") is not None  # version changed -> B dropped its cache
    assert laptop_b.version > v0
    laptop_a.set_active("teammate@acme.com", False, actor="admin@acme.com")
    assert laptop_b.is_active("teammate@acme.com") is False


def test_credentials_version_is_cached_but_never_stale(pg, monkeypatch):
    """Mk20: verify_token reads the credential version on EVERY token check, so it must
    come from the cache — yet a password reset must supersede tokens on this laptop at
    once and on another laptop after the version poll."""
    pool, _admin, _schema = pg
    a = PgMemberStore(pool, version_ttl=0, read_ttl=60)
    b = PgMemberStore(pool, version_ttl=0, read_ttl=60)
    v0 = a.credentials_version("alice@acme.com")
    assert b.credentials_version("alice@acme.com") == v0
    seen = _count_statements(monkeypatch)
    for _ in range(50):
        a.credentials_version("alice@acme.com")
    assert not [s for s in seen if s.startswith("SELECT members")], seen  # cached (only the version poll runs)
    a.reset_password("alice@acme.com", "newpass1", actor="admin@acme.com")
    assert a.credentials_version("alice@acme.com") == v0 + 1  # own write: immediate
    assert b.credentials_version("alice@acme.com") == v0 + 1  # other laptop: after the version poll
    a.set_totp_secret("alice@acme.com", "JBSWY3DPEHPK3PXP", actor="admin@acme.com")
    assert b.credentials_version("alice@acme.com") == v0 + 2  # TOTP set now bumps members_version too


def test_other_laptop_change_is_visible_within_the_version_ttl(pg):
    pool, _admin, _schema = pg
    a = PgMemberStore(pool)
    b = PgMemberStore(pool)  # default 1 s version TTL
    assert b.get("late@acme.com") is None
    a.create("late@acme.com", "pw", "hr_agent", "hr")
    deadline = time.monotonic() + 3.0  # 1 s poll + slack
    while b.get("late@acme.com") is None and time.monotonic() < deadline:
        time.sleep(0.1)
    assert b.get("late@acme.com") is not None


def test_sign_in_never_uses_the_cache(pg):
    """A password reset or deactivation elsewhere must apply to the very next login attempt."""
    pool, _admin, _schema = pg
    a = PgMemberStore(pool, version_ttl=60, read_ttl=60)  # cache would keep serving old data for a minute
    b = PgMemberStore(pool, version_ttl=60, read_ttl=60)
    a.create("login@acme.com", "old-pw", "hr_agent", "hr")
    assert b.verify("login@acme.com", "old-pw") is True
    a.reset_password("login@acme.com", "new-pw", actor="admin@acme.com")
    assert b.verify("login@acme.com", "old-pw") is False
    assert b.verify("login@acme.com", "new-pw") is True
    a.set_active("login@acme.com", False, actor="admin@acme.com")
    assert b.verify("login@acme.com", "new-pw") is False


def test_cache_returns_copies_so_callers_cannot_poison_it(pg):
    pool, _admin, _schema = pg
    m = PgMemberStore(pool)
    m.list().clear()
    assert len(m.list()) >= 5
    m.list_connected_accounts("alice@acme.com").append({"app": "evil"})
    assert m.list_connected_accounts("alice@acme.com") == []


def test_a_read_that_raced_a_write_cannot_repopulate_the_cache_with_old_data(pg):
    pool, _admin, _schema = pg
    m = PgMemberStore(pool, version_ttl=60, read_ttl=60)

    def slow_loader():
        m._invalidate()  # a write commits while this read is in flight
        return "value read before the write"

    assert m._memo(("k",), slow_loader) == "value read before the write"  # caller still gets its data
    assert m._memo(("k",), lambda: "fresh value") == "fresh value"  # …but it was not cached


# ================================================================ postgres-only: races


def test_concurrent_creates_of_one_username_yield_exactly_one_winner(pg):
    pool, _admin, _schema = pg
    stores_ = [PgMemberStore(pool, version_ttl=0, read_ttl=0) for _ in range(6)]
    wins: list[str] = []
    losses: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker(i: int) -> None:
        barrier.wait(timeout=10)
        try:
            stores_[i].create("race@acme.com", "pw", "hr_agent", "hr")
            wins.append("won")
        except BaseException as exc:  # noqa: BLE001 - collected and asserted
            losses.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert len(wins) == 1
    assert len(losses) == 5 and all(isinstance(e, MemberExistsError) for e in losses), losses


def test_teammates_booting_at_once_seed_the_demo_data_exactly_once(pg):
    pool, _admin, _schema = pg
    apply_pending(pool)
    errors: list[BaseException] = []
    barrier = threading.Barrier(5)

    def boot() -> None:
        barrier.wait(timeout=10)
        try:
            PgMemberStore(pool)
        except BaseException as exc:  # noqa: BLE001 - collected and asserted
            errors.append(exc)

    threads = [threading.Thread(target=boot) for _ in range(5)]
    [t.start() for t in threads]
    [t.join(timeout=90) for t in threads]
    assert not errors, errors
    assert len(PgMemberStore(pool, version_ttl=0, read_ttl=0).list()) == 5


# ================================================================ postgres-only: session


def test_session_commit_rollback_and_atomicity(pg):
    pool, _admin, _schema = pg
    apply_pending(pool)
    s = PgSession(pool, hold=False)
    s.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("a", "1"))  # auto-commits
    assert s.execute("SELECT value FROM meta WHERE key = ?", ("a",)).fetchone()["value"] == "1"

    with pytest.raises(RuntimeError), s:  # exception inside `with`: both statements roll back
        s.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("b", "2"))
        s.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("c", "3"))
        raise RuntimeError("boom")
    assert s.execute("SELECT COUNT(*) FROM meta WHERE key IN ('b','c')").fetchone()[0] == 0

    with s:  # success: both commit together
        s.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("d", "4"))
        s.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("e", "5"))
    assert s.execute("SELECT COUNT(*) FROM meta WHERE key IN ('d','e')").fetchone()[0] == 2


def test_failed_statements_never_leak_pooled_connections(pg):
    """Every error returns its connection; the pool (max 8) must not run dry."""
    pool, _admin, _schema = pg
    apply_pending(pool)
    s = PgSession(pool, hold=False)
    for _ in range(40):
        with pytest.raises(Exception):  # noqa: B017 - any DB error will do
            s.execute("INSERT INTO meta (key, value) VALUES (?, ?)", ("members_version", "dup"))  # PK violation
    assert s.execute("SELECT 1 AS one").fetchone()["one"] == 1  # would time out if connections leaked


def test_held_session_keeps_one_connection_until_closed(pg):
    pool, _admin, _schema = pg
    apply_pending(pool)
    s = PgSession(pool, hold=True)
    pid1 = s.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    pid2 = s.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    assert pid1 == pid2
    s.execute("INSERT INTO meta (key, value) VALUES ('h', '1')")
    s.close()  # sqlite semantics: closing without commit discards
    assert PgSession(pool, hold=False).execute("SELECT COUNT(*) FROM meta WHERE key = 'h'").fetchone()[0] == 0


def test_first_statement_is_retried_when_its_connection_is_dead(pg):
    """Neon can cut a connection that was used moments ago; nothing ran yet, so retrying is safe."""
    pool, admin, _schema = pg
    with pool.connection() as conn:
        pid = conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.3)  # the recycled connection is recent, so the pool's idle ping trusts it
    row = PgSession(pool, hold=False).execute("SELECT 42 AS answer").fetchone()
    assert row["answer"] == 42  # succeeded via the one-time retry on a fresh connection


def test_a_dead_connection_inside_a_transaction_is_an_error_not_a_silent_retry(pg):
    """A read on a dead connection is retried (harmless). Once a write has opened a transaction,
    re-running statements on a fresh connection would silently drop the half-done work."""
    pool, admin, _schema = pg
    apply_pending(pool)
    s = PgSession(pool, hold=True)
    s.execute("INSERT INTO meta (key, value) VALUES ('tx', '1')")  # opens the transaction
    pid = s.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.3)
    with pytest.raises(Exception):  # noqa: B017 - the open transaction is gone; must not be papered over
        s.execute("SELECT 1")
    s.close()


def test_writes_are_never_retried_after_a_lost_connection(pg):
    """After a lost reply there is no telling whether the server applied the write."""
    pool, admin, _schema = pg
    apply_pending(pool)
    with pool.connection() as conn:
        pid = conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.3)
    with pytest.raises(Exception):  # noqa: B017 - surfaced to the caller instead of guessed at
        PgSession(pool, hold=False).execute("INSERT INTO meta (key, value) VALUES ('w', '1')")


# ---- lazy transactions (the reason a read costs one round trip, not three)


def _status(session: PgSession):
    return session._st.raw.info.transaction_status


def test_reads_never_open_a_transaction_but_the_first_write_does(pg):
    from psycopg.pq import TransactionStatus

    pool, _admin, _schema = pg
    apply_pending(pool)
    s = PgSession(pool, hold=True)
    s.execute("SELECT 1")
    s.execute("SELECT value FROM meta WHERE key = ?", ("members_version",))
    assert _status(s) == TransactionStatus.IDLE  # two reads, no BEGIN
    s.execute("INSERT INTO meta (key, value) VALUES ('lazy', '1')")
    assert _status(s) == TransactionStatus.INTRANS  # the first write opened it
    s.execute("SELECT 1")
    assert _status(s) == TransactionStatus.INTRANS  # and later statements stay inside it
    s.commit()
    assert _status(s) == TransactionStatus.IDLE
    s.close()


def test_a_transaction_sees_its_own_writes_and_others_do_not_until_commit(pg):
    pool, _admin, _schema = pg
    apply_pending(pool)
    writer, observer = PgSession(pool, hold=True), PgSession(pool, hold=True)
    writer.execute("INSERT INTO meta (key, value) VALUES ('iso', '1')")
    assert writer.execute("SELECT COUNT(*) FROM meta WHERE key = 'iso'").fetchone()[0] == 1  # read-your-writes
    assert observer.execute("SELECT COUNT(*) FROM meta WHERE key = 'iso'").fetchone()[0] == 0  # not committed
    writer.commit()
    assert observer.execute("SELECT COUNT(*) FROM meta WHERE key = 'iso'").fetchone()[0] == 1
    writer.close()
    observer.close()


def test_standalone_writes_autocommit_in_one_statement(pg):
    """`hold=False` outside a `with` block: each statement is its own atomic unit, no BEGIN/COMMIT."""
    pool, _admin, _schema = pg
    apply_pending(pool)
    s = PgSession(pool, hold=False)
    s.execute("INSERT INTO meta (key, value) VALUES ('solo', '1')")
    other = PgSession(pool, hold=False)
    assert other.execute("SELECT COUNT(*) FROM meta WHERE key = 'solo'").fetchone()[0] == 1
    with pytest.raises(Exception):  # noqa: B017 - a failing single statement leaves nothing behind
        s.execute("INSERT INTO meta (key, value) VALUES ('solo', '2')")
    assert other.execute("SELECT value FROM meta WHERE key = 'solo'").fetchone()["value"] == "1"


def test_commit_hook_fires_only_when_something_was_written(pg):
    """The member cache is dropped on writes; read-only `with` blocks must not flush it."""
    pool, _admin, _schema = pg
    apply_pending(pool)
    fired: list[int] = []
    s = PgSession(pool, hold=True, on_commit=lambda: fired.append(1))
    with s:
        s.execute("SELECT 1")
    s.commit()
    assert fired == []  # only reads
    with s:
        s.execute("INSERT INTO meta (key, value) VALUES ('hook', '1')")
    assert fired == [1]
    s.close()


def test_one_shared_store_instance_is_safe_across_request_threads(pg):
    """FastAPI runs sync endpoints in a thread pool and every store instance is shared by all of
    them. A connection borrowed by one thread must never be used or returned by another (more
    threads than pooled connections, to force waiting and reuse)."""
    pool, _admin, _schema = pg
    base, approvals = PgBaseStore(pool), PgApprovalStore(pool)
    errors: list[BaseException] = []
    barrier = threading.Barrier(12)

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=20)
            for j in range(12):
                base.put(("user", f"u{i}"), f"k{j}", {"text": f"{i}-{j}"})
                assert base.get(("user", f"u{i}"), f"k{j}").value["text"] == f"{i}-{j}"
                aid = approvals.create(f"run-{i}-{j}", "t", {"type": "tool", "id": "x"}, {"sub": "s"}, {"k": j})
                assert approvals.decide(aid, True, "admin")["status"] == "approved"
        except BaseException as exc:  # noqa: BLE001 - collected and asserted
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join(timeout=120) for t in threads]
    assert not errors, errors[:2]
    with pool.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM memory_items").fetchone()["n"] == 12 * 12
        assert conn.execute("SELECT COUNT(*) AS n FROM approvals WHERE status = 'approved'").fetchone()["n"] == 12 * 12
    stats = pool.get_stats()
    assert stats["requests_waiting"] == 0 and stats["pool_available"] == stats["pool_size"]  # nothing leaked


# ================================================================ contract: Mk15 levels + allowlist


def test_mk15_approval_steps_on_both_backends(stores):
    a = stores.approvals
    res, actor = {"type": "memory", "id": "dept/hr"}, {"sub": "bob@acme.com"}
    aid = a.create("run-s", "t-s", res, actor, {"op": "add", "text": "s"},
                   route="head+admin", dept="hr", action="memory:dept/hr:add", args_hash="h1")
    got = a.get(aid)
    assert got["level"] == "head" and got["steps"] == [] and got["route"] == "head+admin"
    assert got["action"] == "memory:dept/hr:add" and got["args_hash"] == "h1"
    assert a.find("run-s", res, {"op": "add", "text": "s"}) == aid

    mid = a.decide_step(aid, True, "hhead@acme.com", as_level="head", comment="ok", always_allow=True)
    assert mid["status"] == "pending" and mid["level"] == "admin"
    assert mid["steps"][0]["by"] == "hhead@acme.com" and mid["steps"][0]["always_allow"] is True
    assert aid not in {i["id"] for i in a.pending_for_dept("hr")}
    assert aid in {i["id"] for i in a.pending_for_level("admin")}
    with pytest.raises(ValueError, match="waiting on an admin"):
        a.decide_step(aid, True, "hhead@acme.com", as_level="head")
    with pytest.raises(ValueError, match="has not been decided"):
        a.claim_resume(aid, "bob@acme.com")

    done = a.decide_step(aid, True, "admin@acme.com", as_level="admin")
    assert done["status"] == "approved" and done["level"] == "done"
    assert [s["level"] for s in done["steps"]] == ["head", "admin"]
    assert a.claim_resume(aid, "bob@acme.com")["resumed_by"] == "bob@acme.com"

    # a tool item is head-owned only through the dept column; legacy rows default to head/[]
    tool = a.get(a.create("run-t", "t-t", {"type": "tool", "id": "finance_transfer"}, actor, {"k": 1}, dept="finance"))
    assert a.dept_of(tool) == "finance" and a.level_of(tool) == "head"
    plain = a.get(a.create("run-p", "t-p", {"type": "tool", "id": "finance_transfer"}, actor, {"k": 2}))
    assert a.dept_of(plain) is None and a.level_of(plain) == "admin" and plain["steps"] == []


def test_mk15_step_race_has_one_winner_on_both_backends(stores):
    a = stores.approvals
    res = {"type": "memory", "id": "dept/hr"}
    for n in range(5):
        aid = a.create(f"run-r{n}", "t", res, {"sub": "bob@acme.com"}, {"op": "add", "text": str(n)}, route="head+admin")
        barrier, out = threading.Barrier(2), [None, None]

        def worker(i, aid=aid, barrier=barrier, out=out):
            barrier.wait()
            try:
                out[i] = a.decide_step(aid, True, f"head{i}@acme.com", as_level="head")
            except ValueError as exc:
                out[i] = exc

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(isinstance(o, dict) for o in out) == 1, out
        assert len(a.get(aid)["steps"]) == 1


def test_mk15_department_mode_and_allowlist_on_both_backends(stores):
    m = stores.members
    assert m.get_department("marketing").approval_mode == "head-only"
    before = m.version
    assert m.set_department_mode("marketing", "head+admin", actor="admin@acme.com").approval_mode == "head+admin"
    assert m.department_mode("marketing") == "head+admin" and m.version > before
    assert m.register_department("legal", "legal", "", approval_mode="head+admin").approval_mode == "head+admin"
    assert m.get_department("legal").to_public()["approval_mode"] == "head+admin"

    assert m.active_head_count("marketing") == 0
    m.create("mhead@acme.com", "head1234", role="marketing_head", dept="marketing")
    assert m.active_head_count("marketing") == 1

    args = {"op": "add", "text": "Our team prefers morning standups.", "match": None}
    rule = m.allowlist_add("alice@acme.com", "memory:dept/marketing:add", "h1", args, "marketing", "mhead@acme.com")
    assert rule["args"] == args and rule["dept"] == "marketing" and rule["created_by"] == "mhead@acme.com"
    m.allowlist_add("alice@acme.com", "memory:dept/marketing:add", "h1", args, "marketing", "someone-else")
    assert len(m.allowlist_list()) == 1 and m.allowlist_list("hr") == []
    assert m.allowlist_match("alice@acme.com", "memory:dept/marketing:add", "h1") is True
    assert m.allowlist_match("alice@acme.com", "memory:dept/marketing:add", "h2") is False
    assert m.allowlist_match("bob@acme.com", "memory:dept/marketing:add", "h1") is False
    assert m.allowlist_revoke("alice@acme.com", "memory:dept/marketing:add", "h1", actor="admin@acme.com") is True
    assert m.allowlist_match("alice@acme.com", "memory:dept/marketing:add", "h1") is False  # no stale cache
    assert m.allowlist_revoke("alice@acme.com", "memory:dept/marketing:add", "h1", actor="admin@acme.com") is False

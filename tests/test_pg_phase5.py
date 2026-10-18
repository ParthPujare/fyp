"""Mk13 Phase 5 — the audit chain on the shared Postgres.

What must hold, on the shared database, for the audit trail to still be evidence:
1. contract     — same entries / filtering / length / verification as the JSONL file (both back-ends);
2. tamper       — editing, deleting, reordering, truncating rows or the head, editing an indexed
                  column, or recomputing the chain without the HMAC key is detected;
3. incremental  — the per-run eval's cheap check sees new tampering; its documented blind spot
                  (rows older than the anchor) is seen by a full verification;
4. concurrency  — many threads, many "laptops" (separate pools = separate head caches) and many
                  processes append to one chain and it stays linear and valid; a lost reply
                  never duplicates an entry; a corrupted head refuses to append instead of forking;
5. cost         — an append is one statement when the head is known; the evals read only their run;
6. wiring       — the factory picks the back-end; a real agent run is audited and its evals pass.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from pathlib import Path

import psycopg
import pytest
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.config import get_settings
from app.db import compat
from app.evals.deterministic import DeterministicEvaluator
from app.harness.audit import GENESIS, AuditLog, _hash_entry, open_audit_log
from app.harness.pg_audit import PgAuditLog
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from tests.attack.helpers import ALICE
from tests.conftest import TEST_URL

ROOT = Path(__file__).resolve().parents[1]
TOOL = {"type": "tool", "id": "crm_search"}


def write(log, n=1, run_id="r", decision="ALLOW", actor=None):
    for i in range(n):
        log.log(decision=decision, resource={"type": "tool", "id": f"t{i}"}, actor=actor or {"sub": "u"}, run_id=run_id)


@pytest.fixture(params=["sqlite", "postgres"])
def audit(request, tmp_path):
    if request.param == "postgres":
        if not TEST_URL:
            pytest.skip("set TEST_DATABASE_URL (make test-pg) for Postgres tests")
        pool, _admin, _schema = request.getfixturevalue("pg")
        return PgAuditLog(pool, hmac_key="")
    return AuditLog(tmp_path / "audit.jsonl", hmac_key="")


@pytest.fixture
def pga(pg):
    """(PgAuditLog, run(sql, *params) on the test schema as a superuser 'attacker')."""
    pool, admin, schema = pg

    def run(sql, *params):
        admin.execute(f'SET search_path TO "{schema}"')
        cur = admin.execute(sql, params)
        return cur.fetchall() if cur.description else None

    return PgAuditLog(pool, hmac_key=""), run


# ==================================================================== 1. contract (both back-ends)


def test_log_entries_len_and_verify(audit):
    assert audit.entries() == [] and len(audit) == 0
    audit.log(decision="ALLOW", resource={"type": "memory", "id": "dept"}, actor=ALICE, run_id="r1")
    audit.log(decision="DENY", resource=TOOL, actor=ALICE, run_id="r2", reason="rbac")
    audit.log(decision="APPROVED", resource=TOOL, actor=ALICE, run_id="r1", reason="hitl")
    got = audit.entries()
    assert [(e["decision"], e["run_id"]) for e in got] == [("ALLOW", "r1"), ("DENY", "r2"), ("APPROVED", "r1")]
    assert set(got[0]) == {"ts", "event_id", "run_id", "decision", "resource", "actor", "guardrail_hit", "reason", "keyed"}
    assert got[1]["resource"] == TOOL and got[2]["reason"] == "hitl" and got[0]["actor"] == ALICE
    assert len(audit) == 3
    assert audit.verify_integrity() == (True, [])
    assert audit.verify_integrity(full=False) == (True, [])


def test_entries_can_be_filtered_by_run(audit):
    write(audit, 2, run_id="a")
    write(audit, 3, run_id="b")
    write(audit, 1, run_id="a")
    assert [e["resource"]["id"] for e in audit.entries(run_id="a")] == ["t0", "t1", "t0"]
    assert len(audit.entries(run_id="b")) == 3 and audit.entries(run_id="zzz") == []


def test_invalid_decision_is_refused_and_writes_nothing(audit):
    with pytest.raises(ValueError):
        audit.log(decision="MAYBE", resource=TOOL, actor=ALICE, run_id="r")
    assert len(audit) == 0


def test_unicode_and_key_order_do_not_break_the_chain(audit):
    audit.log(decision="DENY", resource=TOOL, actor={"sub": "ü@acme.com", "role": "x", "a": [1, {"z": 1, "b": 2}]}, run_id="r")
    audit.log(decision="DENY", resource=TOOL, actor={"role": "x", "sub": "ü@acme.com"}, run_id="r")
    assert audit.verify_integrity() == (True, [])


def test_the_stored_chain_is_the_documented_one(pga):
    """Recompute from genesis with the shared hash function: the database holds the same chain
    the file would (so the formal argument about the file carries over)."""
    log, run = pga
    write(log, 4)
    prev = GENESIS
    for seq, entry, prev_hash, h in run("SELECT seq, entry, prev_hash, hash FROM audit_log ORDER BY seq"):
        assert prev_hash == prev and h == _hash_entry(prev, json.loads(entry))
        prev = h
    assert run("SELECT seq, head_hash FROM audit_heads WHERE log = 'audit'") == [(4, prev)]


def test_separate_logs_are_separate_chains(pg):
    pool, _admin, _schema = pg
    a, b = PgAuditLog(pool, log_name="audit", hmac_key=""), PgAuditLog(pool, log_name="other", hmac_key="")
    write(a, 3)
    write(b, 2)
    assert (len(a), len(b)) == (3, 2)
    assert a.verify_integrity()[0] and b.verify_integrity()[0]


# ==================================================================== 2. tamper detection (Postgres)


def test_edited_entry_is_a_hash_mismatch(pga):
    log, run = pga
    write(log, 3)
    run("UPDATE audit_log SET entry = replace(entry, '\"ALLOW\"', '\"DENY\"'), decision = 'DENY' WHERE seq = 2")
    ok, issues = log.verify_integrity()
    assert not ok and any("row 2: hash mismatch" in i for i in issues), issues


def test_edited_indexed_column_is_detected(pga):
    """run_id/decision are copied into columns for indexing; editing only the copy (to hide a
    decision from the per-run evals) must not go unnoticed."""
    log, run = pga
    write(log, 2, run_id="victim")
    run("UPDATE audit_log SET run_id = 'elsewhere' WHERE seq = 1")
    assert log.entries(run_id="victim") != log.entries()  # the edit does change what evals see ...
    ok, issues = log.verify_integrity()
    assert not ok and any("columns disagree" in i for i in issues), issues  # ... and is caught


def test_deleted_interior_row_is_detected(pga):
    log, run = pga
    write(log, 3)
    run("DELETE FROM audit_log WHERE seq = 2")
    ok, issues = log.verify_integrity()
    assert not ok and any("prev_hash mismatch" in i for i in issues) and any("gap" in i for i in issues)


def test_reordered_rows_are_detected(pga):
    log, run = pga
    write(log, 3)
    run("UPDATE audit_log SET seq = -seq WHERE seq IN (1, 2)")
    run("UPDATE audit_log SET seq = CASE seq WHEN -1 THEN 2 ELSE 1 END WHERE seq < 0")
    ok, issues = log.verify_integrity()
    assert not ok and any("prev_hash mismatch" in i for i in issues), issues


def test_truncated_tail_is_a_head_mismatch(pga):
    log, run = pga
    write(log, 3)
    run("DELETE FROM audit_log WHERE seq = 3")
    ok, issues = log.verify_integrity()
    assert not ok and any("head marker mismatch" in i for i in issues), issues


def test_a_deleted_head_is_detected_and_blocks_appends(pga, monkeypatch):
    log, run = pga
    write(log, 2)
    run("DELETE FROM audit_heads")
    ok, issues = log.verify_integrity()
    assert not ok and "head marker missing" in issues
    monkeypatch.setattr("app.harness.pg_audit.time.sleep", lambda s: None)
    with pytest.raises(TimeoutError):  # no head => "empty log" => seq 1 is taken: refuse, never re-genesis
        write(log, 1)
    assert len(log) == 2


def test_wiping_every_row_is_detected(pga):
    log, run = pga
    write(log, 2)
    run("DELETE FROM audit_log")
    ok, issues = log.verify_integrity()
    assert not ok and any("head marker mismatch" in i for i in issues)


def test_hmac_chain_rejects_a_chain_recomputed_without_the_key(pg):
    pool, admin, schema = pg
    log = PgAuditLog(pool, hmac_key="server-only-secret")
    write(log, 4)
    assert log.verify_integrity()[0]
    admin.execute(f'SET search_path TO "{schema}"')
    rows = admin.execute("SELECT seq, entry FROM audit_log ORDER BY seq").fetchall()
    prev = GENESIS
    for seq, entry in rows:  # attacker flips a decision and re-chains with plain SHA-256
        e = json.loads(entry)
        if seq == 2:
            e["decision"] = "DENY"
        h = _hash_entry(prev, e)
        admin.execute(
            "UPDATE audit_log SET entry = %s, decision = %s, prev_hash = %s, hash = %s WHERE seq = %s",
            (json.dumps(e, sort_keys=True), e["decision"], prev, h, seq),
        )
        prev = h
    admin.execute("UPDATE audit_heads SET head_hash = %s", (prev,))
    assert not PgAuditLog(pool, hmac_key="server-only-secret").verify_integrity()[0]
    assert PgAuditLog(pool, hmac_key="").verify_integrity()[0]  # (the forgery IS a valid unkeyed chain)


def test_a_laptop_with_a_different_key_sees_the_chain_as_broken(pg):
    """Documents the operational rule: every laptop must share SERVICES__AUDIT_HMAC_KEY."""
    pool, _admin, _schema = pg
    write(PgAuditLog(pool, hmac_key="key-A"), 2)
    assert not PgAuditLog(pool, hmac_key="key-B").verify_integrity()[0]


# ==================================================================== 3. incremental verification


def test_incremental_check_reads_only_new_rows_and_sees_new_tampering(pga, monkeypatch):
    log, run = pga
    write(log, 5)
    assert log.verify_integrity() == (True, [])  # full; sets the anchor at row 5
    write(log, 3)
    seen = []
    real = compat.PgSession.execute
    monkeypatch.setattr(compat.PgSession, "execute", lambda self, sql, params=(): seen.append(params) or real(self, sql, params))
    assert log.verify_integrity(full=False) == (True, [])
    assert len(seen) == 1 and seen[0][-1] == 5  # one statement, starting at the anchor row
    monkeypatch.undo()

    write(log, 2)
    run("UPDATE audit_log SET entry = replace(entry, '\"ALLOW\"', '\"DENY\"') WHERE seq = 9")
    ok, issues = log.verify_integrity(full=False)
    assert not ok and any("row 9: hash mismatch" in i for i in issues)


def test_incremental_check_detects_an_edited_or_deleted_anchor(pga):
    log, run = pga
    write(log, 3)
    assert log.verify_integrity()[0]  # anchor = row 3
    run("UPDATE audit_log SET entry = replace(entry, '\"ALLOW\"', '\"DENY\"') WHERE seq = 3")
    ok, issues = log.verify_integrity(full=False)
    assert not ok and any("row 3: changed or deleted" in i for i in issues)

    run("UPDATE audit_log SET entry = replace(entry, '\"DENY\"', '\"ALLOW\"') WHERE seq = 3")  # undo
    assert log.verify_integrity()[0]  # anchor = row 3 again
    run("DELETE FROM audit_log WHERE seq = 3")
    ok, issues = log.verify_integrity(full=False)
    assert not ok and any("row 3: changed or deleted" in i for i in issues)


def test_incremental_blind_spot_is_covered_by_a_full_check(pga):
    """Documented limit: an edit to a row OLDER than the anchor is not re-checked by the cheap
    per-run check; the next full verification (a new process, `python -m app.db verify-audit`,
    or any failure) finds it."""
    log, run = pga
    write(log, 4)
    assert log.verify_integrity()[0]
    run("UPDATE audit_log SET entry = replace(entry, '\"ALLOW\"', '\"DENY\"') WHERE seq = 1")
    assert log.verify_integrity(full=False)[0]  # the blind spot, stated plainly
    ok, issues = log.verify_integrity(full=True)
    assert not ok and any("row 1: hash mismatch" in i for i in issues)
    ok, _ = log.verify_integrity(full=False)  # after a failure the anchor is dropped: full again
    assert not ok


def test_a_new_process_starts_with_a_full_check(pga):
    log, run = pga
    write(log, 3)
    assert log.verify_integrity()[0]
    run("UPDATE audit_log SET entry = replace(entry, '\"ALLOW\"', '\"DENY\"') WHERE seq = 1")
    from app.harness import pg_audit

    pg_audit._CHAINS.clear()  # what a fresh process looks like
    fresh = PgAuditLog(log._pool, hmac_key="")
    assert not fresh.verify_integrity(full=False)[0]


# ==================================================================== 4. concurrency & failure


def test_threads_with_separate_instances_keep_one_linear_chain(pga):
    log, run = pga
    pool = log._pool

    def worker(i):
        mine = PgAuditLog(pool, hmac_key="")  # fresh instance per thread: the head cache is per (pool, log)
        write(mine, 15, run_id=f"r{i}", actor={"sub": str(i)})

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert log.verify_integrity() == (True, [])
    assert len(log) == 120
    assert [r[0] for r in run("SELECT seq FROM audit_log ORDER BY seq")] == list(range(1, 121))


def test_several_laptops_racing_never_fork_the_chain(pg):
    """Separate pools = separate processes' head caches (like separate laptops): every append
    races against stale views of the head. The primary key must keep the chain linear."""
    from app.db import make_pool

    _pool, _admin, schema = pg
    pools = [make_pool(TEST_URL, kwargs={"options": f"-c search_path={schema}"}, max_size=4, name=f"{schema}-{i}") for i in range(4)]
    try:
        errors = []

        def laptop(i, j):
            try:
                write(PgAuditLog(pools[i], hmac_key=""), 12, run_id=f"l{i}-{j}")
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=laptop, args=(i, j)) for i in range(4) for j in range(2)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert not errors, errors[:3]
        log = PgAuditLog(pools[0], hmac_key="")
        assert log.verify_integrity() == (True, [])
        assert len(log) == 4 * 2 * 12
    finally:
        for p in pools:
            p.close()


def test_a_writer_that_loses_every_optimistic_race_still_gets_through(pga, monkeypatch):
    """Found on real Neon: pure optimistic retries can starve (one writer lost 30 races in a row
    under 3 laptops x 2 threads). After _FAST_TRIES misses the locked path must succeed."""
    from app.harness import pg_audit

    log, _run = pga
    write(log, 3)
    calls = []

    def always_loses(self, entry, body):
        calls.append(1)
        return False

    monkeypatch.setattr(pg_audit.PgAuditLog, "_append_fast", always_loses)
    entry = log.log(decision="DENY", resource=TOOL, actor=ALICE, run_id="starved")
    monkeypatch.undo()
    assert len(calls) == pg_audit._FAST_TRIES  # gave up on optimism, then one locked append
    assert [e["event_id"] for e in log.entries(run_id="starved")] == [entry["event_id"]]
    assert log.verify_integrity() == (True, []) and len(log) == 4


def test_heavy_contention_at_neon_like_latency_never_fails_an_append(pg, monkeypatch):
    """The measured failure, reproduced: every statement pays ~25 ms (a WAN round trip) while
    4 laptops x 3 threads append non-stop. Every append must land and the chain must be linear."""
    from app.db import make_pool

    _pool, _admin, schema = pg
    real = compat.PgSession.execute

    def slow(self, sql, params=()):
        time.sleep(0.025)
        return real(self, sql, params)

    monkeypatch.setattr(compat.PgSession, "execute", slow)
    pools = [make_pool(TEST_URL, kwargs={"options": f"-c search_path={schema}"}, max_size=4, name=f"{schema}-h{i}") for i in range(4)]
    try:
        errors = []

        def laptop(i, j):
            try:
                write(PgAuditLog(pools[i], hmac_key=""), 10, run_id=f"h{i}-{j}")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=laptop, args=(i, j)) for i in range(4) for j in range(3)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        monkeypatch.undo()
        assert not errors, errors[:2]
        log = PgAuditLog(pools[0], hmac_key="")
        assert log.verify_integrity() == (True, []) and len(log) == 4 * 3 * 10
    finally:
        for p in pools:
            p.close()


def test_separate_processes_share_one_valid_chain(pg):
    _pool, _admin, schema = pg
    code = (
        "import os, sys; sys.path.insert(0, sys.argv[1]); os.environ['DATABASE_URL'] = ''\n"
        "from app.db import make_pool\n"
        "from app.harness.pg_audit import PgAuditLog\n"
        "pool = make_pool(sys.argv[2], kwargs={'options': '-c search_path=' + sys.argv[3]}, max_size=2)\n"
        "log = PgAuditLog(pool, hmac_key='')\n"
        "for i in range(12):\n"
        "    log.log(decision='ALLOW', resource={'type':'tool','id':'x'}, actor={'sub':sys.argv[4]}, run_id='p'+sys.argv[4])\n"
        "pool.close()\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(ROOT), TEST_URL, schema, str(i)]) for i in range(3)]
    assert [p.wait(timeout=180) for p in procs] == [0, 0, 0]
    log = PgAuditLog(_pool, hmac_key="")
    assert log.verify_integrity() == (True, [])
    assert len(log) == 36


def test_a_stale_head_from_another_laptop_is_corrected_not_forked(pg):
    from app.db import make_pool

    pool_a, _admin, schema = pg
    pool_b = make_pool(TEST_URL, kwargs={"options": f"-c search_path={schema}"}, max_size=2, name=schema + "-b")
    try:
        a, b = PgAuditLog(pool_a, hmac_key=""), PgAuditLog(pool_b, hmac_key="")
        write(a, 2)  # A caches head = row 2
        write(b, 3)  # B: guess empty -> corrected -> rows 3..5
        write(a, 1)  # A's cached head is stale; must land as row 6, chained to row 5
        assert len(a) == 6 and a.verify_integrity() == (True, [])
    finally:
        pool_b.close()


def test_a_lost_reply_is_retried_without_duplicating_the_entry(pga, monkeypatch):
    """The statement commits but the reply is lost (connection dropped): the retry must
    recognise its own row (unique event_id) instead of appending it twice."""
    log, _run = pga
    write(log, 2)
    real = compat.PgSession.execute
    state = {"armed": True}

    def flaky(self, sql, params=()):
        result = real(self, sql, params)
        if state["armed"] and "INSERT INTO audit_log" in sql:
            state["armed"] = False
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return result

    monkeypatch.setattr(compat.PgSession, "execute", flaky)
    entry = log.log(decision="DENY", resource=TOOL, actor=ALICE, run_id="lost")
    monkeypatch.undo()
    assert [e["event_id"] for e in log.entries(run_id="lost")] == [entry["event_id"]]
    assert len(log) == 3 and log.verify_integrity() == (True, [])
    write(log, 1)  # and the chain keeps going afterwards
    assert len(log) == 4 and log.verify_integrity()[0]


def test_a_second_connection_failure_is_raised(pga, monkeypatch):
    log, _run = pga

    def dead(self, sql, params=()):
        raise psycopg.OperationalError("no route to host")

    monkeypatch.setattr(compat.PgSession, "execute", dead)
    with pytest.raises(psycopg.OperationalError):
        log.log(decision="ALLOW", resource=TOOL, actor=ALICE, run_id="r")


def test_a_corrupted_head_refuses_to_append_instead_of_forking(pga, monkeypatch):
    log, run = pga
    write(log, 3)
    run("UPDATE audit_heads SET seq = 1, head_hash = (SELECT hash FROM audit_log WHERE seq = 1)")
    monkeypatch.setattr("app.harness.pg_audit.time.sleep", lambda s: None)
    with pytest.raises(TimeoutError, match="verify-audit"):
        write(log, 1)
    assert len(log) == 3  # nothing was appended onto the wrong predecessor


# ==================================================================== 5. cost


def _count_statements(monkeypatch):
    seen = []
    real = compat.PgSession.execute
    monkeypatch.setattr(compat.PgSession, "execute", lambda self, sql, params=(): seen.append(sql) or real(self, sql, params))
    return seen


def test_an_append_is_one_statement_when_the_head_is_known(pga, monkeypatch):
    log, _run = pga
    seen = _count_statements(monkeypatch)
    write(log, 1)  # empty log: the "empty" guess is right
    write(log, 3)  # head cached from our own writes
    assert len(seen) == 4


def test_a_stale_head_costs_exactly_one_extra_statement(pg, monkeypatch):
    pool, _admin, _schema = pg
    write(PgAuditLog(pool, log_name="audit", hmac_key=""), 3)
    from app.harness import pg_audit

    pg_audit._CHAINS.clear()  # another process wrote those rows: our cache knows nothing
    log = PgAuditLog(pool, hmac_key="")
    seen = _count_statements(monkeypatch)
    write(log, 1)
    assert len(seen) == 2
    monkeypatch.undo()
    assert log.verify_integrity()[0] and len(log) == 4


def test_evals_read_only_their_own_runs_decisions(pga, monkeypatch):
    log, _run = pga
    write(log, 30, run_id="someone-else")
    write(log, 2, run_id="mine")
    seen = _count_statements(monkeypatch)
    assert [e["run_id"] for e in log.entries(run_id="mine")] == ["mine", "mine"]
    assert len(seen) == 1 and "run_id = %s" in compat.translate(seen[0])


# ==================================================================== 6. wiring


def test_factory_picks_the_backend(pg, monkeypatch, tmp_path):
    assert type(open_audit_log(tmp_path / "audit.jsonl")) is AuditLog  # DATABASE_URL is blank in tests
    pool, _admin, _schema = pg
    monkeypatch.setattr("app.db.is_enabled", lambda: True)
    monkeypatch.setattr("app.harness.pg_audit.get_pool", lambda: pool)
    log = open_audit_log(tmp_path / "members_audit.jsonl")
    assert isinstance(log, PgAuditLog) and log.log_name == "members_audit" and log.path is None


def test_member_admin_actions_are_audited_to_the_shared_chain(pg, tmp_path):
    from app.auth.store import MemberStore

    pool, _admin, _schema = pg
    log = PgAuditLog(pool, hmac_key="")
    store = MemberStore(tmp_path / "members.sqlite", audit=log)
    store.create("new@acme.com", "pw-123456789", "employee", "marketing", actor="admin@acme.com")
    (entry,) = log.entries(run_id="members")
    assert entry["reason"] == "admin:members:create" and entry["resource"] == {"type": "member", "id": "new@acme.com"}


DENY_SCRIPT = {"attack": {"text": "attack", "tool_calls": [{"name": "hr_delete_employee", "args": {"q": "1"}}]}}
ALLOW_SCRIPT = {"hello": {"text": "looking", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]}}


def _run(tmp_path, executor, enforcer, audit, message, script, thread):
    db = tmp_path / "m.sqlite"
    episodic = EpisodicStore(db)
    return run_agent(
        ALICE, message, thread_id=thread, provider=MockLLM(script), executor=executor,
        trace=TraceStore(tmp_path / "trace.sqlite"), enforcer=enforcer, audit=audit,
        store=SqliteBaseStore(db), episodic=episodic, approvals=ApprovalStore(db),
        recall=RecallService(episodic, enforcer, get_settings().memory), checkpointer=InMemorySaver(),
    )


def test_real_runs_are_audited_on_postgres_and_their_evals_pass(pg, tmp_path, dept_apps, executor, enforcer):
    pool, admin, schema = pg
    audit = PgAuditLog(pool, hmac_key="")
    denied = _run(tmp_path, executor, enforcer, audit, "attack", DENY_SCRIPT, "t-deny")
    allowed = _run(tmp_path, executor, enforcer, audit, "hello", ALLOW_SCRIPT, "t-allow")
    assert {e["decision"] for e in audit.entries(run_id=denied.run_id)} == {"DENY"}
    assert "ALLOW" in {e["decision"] for e in audit.entries(run_id=allowed.run_id)}
    trace = TraceStore(tmp_path / "trace.sqlite")
    for result in (denied, allowed):
        checks = {c.name: c for c in DeterministicEvaluator(trace, audit).evaluate(result.run_id).checks}
        for name in ("rbac_contract", "audit_pairs", "audit_integrity"):
            assert checks[name].passed, (name, checks[name].detail)

    # Someone rewrites the DENY as an ALLOW. A fresh process (full first check) must flag it.
    admin.execute(f'SET search_path TO "{schema}"')
    admin.execute("UPDATE audit_log SET entry = replace(entry, '\"DENY\"', '\"ALLOW\"') WHERE decision = 'DENY'")
    from app.harness import pg_audit

    pg_audit._CHAINS.clear()
    checks = {c.name: c for c in DeterministicEvaluator(trace, PgAuditLog(pool, hmac_key="")).evaluate(denied.run_id).checks}
    assert checks["audit_integrity"].passed is False and "hash mismatch" in checks["audit_integrity"].detail


def test_file_log_ignores_the_full_flag(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl", hmac_key="")
    write(log, 2)
    lines = log.path.read_text(encoding="utf-8").splitlines()
    log.path.write_text(lines[0] + "\n", encoding="utf-8")
    assert not log.verify_integrity(full=False)[0]  # a local file is always re-read in full

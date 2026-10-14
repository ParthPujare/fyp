"""Mk13 Phase 3 — run traces (batched) and chat checkpoints on the shared Postgres.

1. **Trace contract tests** run the same assertions on the SQLite `TraceStore` and the Postgres
   `PgTraceStore`: chain validity, ordering, source filter, tamper and truncation detection.
2. **Batching tests** (Postgres): a run's events cost zero statements until the flush, then
   exactly one; reads answer from stored rows + the in-memory tail; the chain survives chunking,
   early flushes, a resume from another "machine", and refuses to fork.
3. **Checkpoint tests**: thread listing (exact prefix — the old LIKE matched look-alike names),
   `PostgresSaver` set-up races, and the headline scenario: a run started on laptop A, paused
   for approval, approved and resumed on laptop B.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.sqlite import SqliteSaver

import app.agent.checkpoints as cp
from app.agent.checkpoints import invoke_options, list_user_threads, postgres_saver
from app.agent.runner import resume_agent, run_agent
from app.db import compat
from app.gateway.app import _thread_messages
from app.llm.mock import MockLLM
from app.memory.pg_stores import PgApprovalStore, PgBaseStore
from app.ops import pg_trace
from app.ops.pg_trace import PgTraceStore
from app.ops.schema import TraceEventType
from app.ops.store import GENESIS, TraceStore
from tests.attack.helpers import CAROL
from tests.conftest import TEST_URL

ACTOR = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}


# ============================================================== trace contract (both back-ends)


@pytest.fixture(params=["sqlite", "postgres"])
def traces(request, tmp_path):
    if request.param == "postgres":
        if not TEST_URL:
            pytest.skip("set TEST_DATABASE_URL (make test-pg) for Postgres tests")
        pool, _admin, _schema = request.getfixturevalue("pg")

        def tamper(sql: str, params: tuple = ()) -> None:
            with pool.connection() as conn:
                conn.execute(sql.replace("?", "%s"), params)

        return SimpleNamespace(kind="postgres", store=PgTraceStore(pool), tamper=tamper, pool=pool)
    path = tmp_path / "trace.sqlite"

    def tamper(sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(path)
        conn.execute(sql, params)
        conn.commit()
        conn.close()

    return SimpleNamespace(kind="sqlite", store=TraceStore(path), tamper=tamper, pool=None)


def _record_run(store, run_id="run-1", events=6):
    store.start_run(ACTOR, {"message": "hi"}, run_id=run_id)
    for i in range(events):
        store.add_event(run_id, TraceEventType.NODE, ACTOR, {"node": f"n{i}"})
    store.end_run(run_id, {"output": "done"})
    store.flush(run_id)
    return run_id


def test_run_tree_is_ordered_and_hash_chained(traces):
    s = traces.store
    rid = _record_run(s)
    tree = s.get_run_tree(rid)
    assert [e["seq"] for e in tree] == list(range(len(tree))) and len(tree) == 8
    assert tree[0]["type"] == "run_start" and tree[-1]["type"] == "run_end"
    assert tree[0]["prev_hash"] == GENESIS
    assert all(tree[i]["prev_hash"] != tree[i + 1]["prev_hash"] for i in range(len(tree) - 1))
    assert s.verify_integrity(rid) == (True, [])
    assert s.verify_integrity() == (True, [])


def test_events_appended_after_run_end_extend_the_same_chain(traces):
    """The evals write their own events after `run_end`; the chain must keep going."""
    s = traces.store
    rid = _record_run(s)
    s.add_event(rid, TraceEventType.EVAL, ACTOR, {"suite": "deterministic", "score": 1.0})
    s.flush(rid)
    tree = s.get_run_tree(rid)
    assert tree[-1]["type"] == "eval" and tree[-1]["seq"] == len(tree) - 1
    assert s.verify_integrity(rid)[0]


def test_source_filter_and_run_listing(traces):
    s = traces.store
    _record_run(s, "run-a")
    _record_run(s, "run-b", events=2)
    s.start_run(ACTOR, {"message": "resumed one"}, run_id="run-c")
    s.end_run("run-c", {"output": "ok", "resumed": True})
    s.flush()
    sources = {e["source"] for e in s.get_run_tree("run-a")}
    assert len(sources) >= 1
    only = s.get_run_tree("run-a", source=next(iter(sources)))
    assert only and all(e["source"] == next(iter(sources)) for e in only)
    runs = {r["run_id"]: r for r in s.list_runs()}
    assert {"run-a", "run-b", "run-c"} <= set(runs)
    assert runs["run-a"]["event_count"] == 8 and runs["run-b"]["event_count"] == 4
    assert runs["run-a"]["actor"]["sub"] == "alice@acme.com"
    assert runs["run-a"]["outcome"] == "completed" and runs["run-c"]["outcome"] == "resumed"


def test_editing_an_event_breaks_verification(traces):
    s = traces.store
    rid = _record_run(s)
    traces.tamper("UPDATE trace_events SET payload = ? WHERE run_id = ? AND seq = 3", ('{"node": "evil"}', rid))
    ok, issues = s.verify_integrity(rid)
    assert not ok and issues


def test_truncating_the_last_event_is_detected_by_the_head_hash(traces):
    s = traces.store
    rid = _record_run(s)
    traces.tamper("DELETE FROM trace_events WHERE run_id = ? AND seq = 7", (rid,))
    ok, issues = s.verify_integrity(rid)
    assert not ok and any("head hash mismatch" in i for i in issues)


# ============================================================== batching (postgres only)


def _spy(monkeypatch) -> list[str]:
    seen: list[str] = []
    original = compat.PgSession.execute

    def spying(self, sql, params=()):
        seen.append(" ".join(sql.split())[:60])
        return original(self, sql, params)

    monkeypatch.setattr(compat.PgSession, "execute", spying)
    return seen


def test_a_runs_events_cost_no_statements_until_the_single_flush(pg, monkeypatch):
    pool, _admin, _schema = pg
    store = PgTraceStore(pool)
    seen = _spy(monkeypatch)
    rid = "run-batch"
    store.start_run(ACTOR, {"message": "hi"}, run_id=rid)
    for i in range(40):
        store.add_event(rid, TraceEventType.NODE, ACTOR, {"node": i})
    store.end_run(rid, {"output": "x"})
    assert store.get_run_tree(rid) and store.verify_integrity(rid)[0]  # answered from memory
    assert seen == [], f"a fresh run must not touch the database before the flush: {seen}"
    store.flush(rid)
    assert len(seen) == 1 and seen[0].startswith("WITH ins AS (INSERT INTO trace_events"), seen
    assert len(PgTraceStore(pool).get_run_tree(rid)) == 42  # a different process sees all of it


def test_reads_combine_stored_rows_with_the_unflushed_tail(pg):
    pool, _admin, _schema = pg
    store = PgTraceStore(pool)
    rid = "run-mixed"
    store.start_run(ACTOR, {"message": "hi"}, run_id=rid)
    store.add_event(rid, TraceEventType.NODE, ACTOR, {"node": 1})
    store.flush(rid)  # 2 stored
    store.add_event(rid, TraceEventType.NODE, ACTOR, {"node": 2})
    store.add_event(rid, TraceEventType.NODE, ACTOR, {"node": 3})  # 2 in memory
    tree = store.get_run_tree(rid)
    assert [e["seq"] for e in tree] == [0, 1, 2, 3]
    assert store.verify_integrity(rid) == (True, [])
    store.flush(rid)
    assert PgTraceStore(pool).verify_integrity(rid) == (True, [])


def test_a_run_suspended_on_one_machine_is_continued_by_another(pg):
    pool, _admin, _schema = pg
    laptop_a, laptop_b = PgTraceStore(pool), PgTraceStore(pool)
    rid = "run-hop"
    laptop_a.start_run(ACTOR, {"message": "hi"}, run_id=rid)
    laptop_a.add_event(rid, TraceEventType.GUARDRAIL, ACTOR, {"type": "hitl_suspend"})
    laptop_a.flush(rid)  # what the runner does when a run suspends
    laptop_b.add_event(rid, TraceEventType.NODE, ACTOR, {"node": "resumed"})  # loads head + seq from the DB
    laptop_b.end_run(rid, {"output": "done", "resumed": True})
    laptop_b.flush(rid)
    third = PgTraceStore(pool)
    tree = third.get_run_tree(rid)
    assert [e["seq"] for e in tree] == [0, 1, 2, 3]
    assert third.verify_integrity(rid) == (True, [])


def test_two_machines_resuming_the_same_run_cannot_fork_its_chain(pg):
    pool, _admin, _schema = pg
    seed = PgTraceStore(pool)
    seed.start_run(ACTOR, {"message": "hi"}, run_id="run-fork")
    seed.flush("run-fork")
    a, b = PgTraceStore(pool), PgTraceStore(pool)
    a.add_event("run-fork", TraceEventType.NODE, ACTOR, {"who": "a"})  # both load the same head
    b.add_event("run-fork", TraceEventType.NODE, ACTOR, {"who": "b"})
    a.flush("run-fork")
    with pytest.raises(psycopg.errors.UniqueViolation):
        b.flush("run-fork")  # (run_id, seq) is unique: the second writer is refused, not merged
    assert PgTraceStore(pool).verify_integrity("run-fork") == (True, [])  # chain still a single line
    assert len(b._runs["run-fork"].pending) == 1  # and B keeps its event for inspection/retry


def test_the_buffer_flushes_early_when_a_run_gets_long(pg, monkeypatch):
    pool, _admin, _schema = pg
    monkeypatch.setattr(PgTraceStore, "FLUSH_AT", 5)
    store = PgTraceStore(pool)
    seen = _spy(monkeypatch)
    store.start_run(ACTOR, {"message": "hi"}, run_id="run-long")
    for i in range(11):
        store.add_event("run-long", TraceEventType.NODE, ACTOR, {"n": i})
    assert len(seen) == 2  # events 5 and 10 triggered writes on their own
    store.flush("run-long")
    assert PgTraceStore(pool).verify_integrity("run-long") == (True, [])
    assert len(PgTraceStore(pool).get_run_tree("run-long")) == 12


def test_chunked_writes_keep_the_chain_and_head_exact(pg, monkeypatch):
    pool, _admin, _schema = pg
    monkeypatch.setattr(pg_trace, "_CHUNK", 3)
    store = PgTraceStore(pool)
    store.start_run(ACTOR, {"message": "hi"}, run_id="run-chunks")
    for i in range(9):
        store.add_event("run-chunks", TraceEventType.NODE, ACTOR, {"n": i})
    store.flush("run-chunks")  # 10 events -> 4 statements of <= 3
    fresh = PgTraceStore(pool)
    assert fresh.verify_integrity("run-chunks") == (True, [])  # head hash matches the last event
    assert len(fresh.get_run_tree("run-chunks")) == 10


def test_a_failed_flush_keeps_the_events_and_the_next_flush_writes_them(pg, monkeypatch):
    pool, _admin, _schema = pg
    store = PgTraceStore(pool)
    store.start_run(ACTOR, {"message": "hi"}, run_id="run-retry")
    store.add_event("run-retry", TraceEventType.NODE, ACTOR, {"n": 1})
    real = PgTraceStore._write_chunk
    calls = {"n": 0}

    def flaky(self, *a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise psycopg.OperationalError("network blip")
        return real(self, *a, **k)

    monkeypatch.setattr(PgTraceStore, "_write_chunk", flaky)
    with pytest.raises(psycopg.OperationalError):
        store.flush("run-retry")
    assert len(store._runs["run-retry"].pending) == 2  # nothing was dropped
    store.flush("run-retry")
    assert PgTraceStore(pool).verify_integrity("run-retry") == (True, [])
    assert len(PgTraceStore(pool).get_run_tree("run-retry")) == 2


def test_flushed_runs_are_evicted_from_memory_but_unflushed_ones_are_kept(pg, monkeypatch):
    pool, _admin, _schema = pg
    monkeypatch.setattr(PgTraceStore, "KEEP_RUNS", 2)
    store = PgTraceStore(pool)
    for i in range(5):
        store.start_run(ACTOR, {"message": "x"}, run_id=f"run-{i}")
        store.flush(f"run-{i}")
    assert len(store._runs) <= 2
    store.start_run(ACTOR, {"message": "pending"}, run_id="run-open")  # unflushed: must survive
    store.start_run(ACTOR, {"message": "pending2"}, run_id="run-open2")
    store.start_run(ACTOR, {"message": "pending3"}, run_id="run-open3")
    store.flush("run-0")
    assert {"run-open", "run-open2", "run-open3"} <= set(store._runs)


def test_concurrent_events_on_one_run_still_form_a_single_valid_chain(pg):
    pool, _admin, _schema = pg
    store = PgTraceStore(pool)
    store.start_run(ACTOR, {"message": "hi"}, run_id="run-threads")
    threads = [
        threading.Thread(target=lambda i=i: [store.add_event("run-threads", TraceEventType.NODE, ACTOR, {"t": i}) for _ in range(20)])
        for i in range(8)
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    store.flush("run-threads")
    fresh = PgTraceStore(pool)
    assert len(fresh.get_run_tree("run-threads")) == 1 + 8 * 20
    assert fresh.verify_integrity("run-threads") == (True, [])


# ============================================================== checkpoints


def test_thread_listing_uses_an_exact_prefix_not_like_wildcards(tmp_path):
    """`_` is a LIKE wildcard: `a_b@x.com` used to list `aXb@x.com`'s threads too."""
    path = tmp_path / "cp.sqlite"
    conn = sqlite3.connect(path, check_same_thread=False)
    saver = SqliteSaver(conn)
    saver.setup()
    for tid in ("user:a_b@x.com", "user:a_b@x.com:chat1", "user:aXb@x.com", "user:aXb@x.com:chat9", "user:a_b@x.com.evil"):
        conn.execute(
            "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)"
            " VALUES (?, '', 'c1', 'json', x'00', x'00')",
            (tid,),
        )
    conn.commit()
    settings = SimpleNamespace(services=SimpleNamespace(checkpoint_path=path))
    assert list_user_threads("a_b@x.com", settings) == ["user:a_b@x.com", "user:a_b@x.com:chat1"]
    assert list_user_threads("aXb@x.com", settings) == ["user:aXb@x.com", "user:aXb@x.com:chat9"]


def test_postgres_thread_listing_is_exact_too(pg, monkeypatch):
    pool, _admin, _schema = pg
    saver = postgres_saver(pool)
    with pool.connection() as conn:
        for tid in ("user:a_b@x.com:1", "user:aXb@x.com:1", "user:a_b@x.com"):
            conn.execute(
                "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata)"
                " VALUES (%s, '', 'c1', 'json', '{}'::jsonb, '{}'::jsonb)",
                (tid,),
            )
    monkeypatch.setattr(cp, "is_enabled", lambda: True)
    monkeypatch.setattr(cp, "connection", pool.connection)
    assert saver is postgres_saver(pool)  # cached per pool
    assert list_user_threads("a_b@x.com") == ["user:a_b@x.com", "user:a_b@x.com:1"]


def test_saver_setup_survives_several_machines_starting_at_once():
    """Five containers booting together on a brand-new database used to deadlock inside
    LangGraph's own `setup()` (its migrations use CREATE INDEX CONCURRENTLY). Repeated on fresh
    schemas because the race only sometimes trips."""
    if not TEST_URL:
        pytest.skip("set TEST_DATABASE_URL (make test-pg) for Postgres tests")
    from langgraph.checkpoint.postgres import PostgresSaver

    from tests.conftest import _private_pg_schema

    for _round in range(4):
        pool, _admin, _schema, cleanup = _private_pg_schema()
        try:
            errors: list[BaseException] = []
            barrier = threading.Barrier(5)

            def boot(pool=pool, errors=errors, barrier=barrier) -> None:
                barrier.wait(timeout=10)
                try:
                    cp._setup(PostgresSaver(pool), pool)  # each machine builds its own saver and sets up
                except BaseException as exc:  # noqa: BLE001 - collected and asserted
                    errors.append(exc)

            threads = [threading.Thread(target=boot) for _ in range(5)]
            [t.start() for t in threads]
            [t.join(timeout=120) for t in threads]
            assert not errors, errors[:2]
            assert cp._schema_current(PostgresSaver(pool), pool)  # and the result is a complete schema
        finally:
            cleanup()


def test_setup_is_skipped_when_the_schema_is_already_current(pg, monkeypatch):
    from langgraph.checkpoint.postgres import PostgresSaver

    pool, _admin, _schema = pg
    saver = PostgresSaver(pool)
    cp._setup(saver, pool)  # first time: creates everything
    called: list[int] = []
    monkeypatch.setattr(PostgresSaver, "setup", lambda self: called.append(1))
    cp._setup(PostgresSaver(pool), pool)  # every later start-up: one read, no DDL, no locks
    assert called == []


def test_only_postgres_savers_get_the_exit_durability_option(pg):
    from langgraph.checkpoint.memory import InMemorySaver

    pool, _admin, _schema = pg
    assert invoke_options(postgres_saver(pool)) == {"durability": "exit"}
    assert invoke_options(InMemorySaver()) == {}


# ---- the headline scenario: paused on one laptop, approved and resumed on another

T1 = {"from_account": "1000", "to_account": "2000", "amount": 1.0,
      "memo": "first", "actor": "carol@acme.com", "status": "pending"}


class Laptop:
    """One machine: its own store/saver/trace instances over the ONE shared database."""

    def __init__(self, pool, executor, enforcer, audit_log):
        from langgraph.checkpoint.postgres import PostgresSaver

        self.saver = PostgresSaver(pool)
        cp._setup(self.saver, pool)
        self.kwargs = {
            "store": PgBaseStore(pool), "approvals": PgApprovalStore(pool), "trace": PgTraceStore(pool),
            "checkpointer": self.saver, "executor": executor, "enforcer": enforcer, "audit": audit_log,
        }


@pytest.fixture
def two_laptops(pg, executor, enforcer, audit_log, tmp_path, monkeypatch):
    from app.memory.episodic import EpisodicStore
    from app.memory.recall import RecallService

    pool, _admin, _schema = pg
    a = Laptop(pool, executor, enforcer, audit_log)
    b = Laptop(pool, executor, enforcer, audit_log)
    epi = EpisodicStore(tmp_path / "epi.sqlite")  # episodic recall stays local until Phase 4
    for laptop in (a, b):
        laptop.kwargs["episodic"] = epi
        laptop.kwargs["recall"] = RecallService(epi, enforcer, __import__("app.config", fromlist=["x"]).get_settings().memory)
    return SimpleNamespace(a=a, b=b, pool=pool)


def test_a_run_paused_on_laptop_a_is_approved_and_resumed_on_laptop_b(two_laptops, dept_apps):
    a, b, pool = two_laptops.a, two_laptops.b, two_laptops.pool
    provider = MockLLM(sequence=[{"text": "sending", "tool_calls": [{"name": "finance_transfer", "args": T1}]}])
    first = run_agent(CAROL, "transfer 1", provider=provider, thread_id="user:carol@acme.com:hop", **a.kwargs)
    assert first.interrupted and first.approval_id
    from tests.attack.helpers import service_headers as _sh
    before = len(TestClient(dept_apps[8005]).get("/transfers", headers=_sh()).json())

    # laptop B: a different process/machine. It sees the approval, decides it, and resumes the run.
    assert b.kwargs["approvals"].get(first.approval_id)["status"] == "pending"
    b.kwargs["approvals"].decide(first.approval_id, True, "admin@acme.com")
    resumed = resume_agent(first.approval_id, provider=MockLLM(sequence=[]), **b.kwargs)

    assert not resumed.interrupted and resumed.reply
    assert len(TestClient(dept_apps[8005]).get("/transfers", headers=_sh()).json()) == before + 1  # executed exactly once
    # one continuous, verifiable trace across both machines
    fresh = PgTraceStore(pool)
    types = [e["type"] for e in fresh.get_run_tree(first.run_id)]
    assert types[0] == "run_start" and "run_end" in types
    assert fresh.verify_integrity(first.run_id) == (True, [])
    # chat history is shared too: laptop B can read the conversation A started
    assert any(m["role"] == "user" for m in _thread_messages(b.saver, "user:carol@acme.com:hop"))


def test_run_agent_persists_its_trace_and_uses_exit_durability(two_laptops):
    a, pool = two_laptops.a, two_laptops.pool
    provider = MockLLM(sequence=[
        {"text": "step", "tool_calls": [{"name": "finance_list_accounts", "args": {}}]},
        {"text": "all done"},
    ])
    res = run_agent(CAROL, "list accounts", provider=provider, thread_id="user:carol@acme.com:dur", **a.kwargs)
    assert res.reply == "all done"
    fresh = PgTraceStore(pool)  # a different instance: only what was flushed is visible
    tree = fresh.get_run_tree(res.run_id)
    assert tree[0]["type"] == "run_start" and tree[-1]["type"] in ("run_end", "eval")
    assert fresh.verify_integrity(res.run_id) == (True, [])
    with pool.connection() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id = %s", ("user:carol@acme.com:dur",)).fetchone()["n"]
    assert n <= 2, f"durability='exit' should persist ~one checkpoint per run, found {n}"


def test_a_failure_while_building_the_run_still_leaves_its_start_event(two_laptops, monkeypatch):
    import app.agent.runner as runner_mod

    def boom(*_a, **_k):
        raise RuntimeError("provider construction failed")

    monkeypatch.setattr(runner_mod, "get_provider_for_user", boom)
    with pytest.raises(RuntimeError):
        run_agent(CAROL, "hi", thread_id="user:carol@acme.com:boom", **two_laptops.a.kwargs)
    with two_laptops.pool.connection() as conn:
        rows = conn.execute("SELECT payload FROM trace_events WHERE type = 'run_start'").fetchall()
    assert rows and "hi" in json.dumps([r["payload"] for r in rows])


# ---- retention (`python -m app.db prune`)


def _prune_env(two_laptops, monkeypatch):
    """Point the CLI at the test schema's pool."""
    import app.db.__main__ as cli

    monkeypatch.setattr(cli, "is_enabled", lambda: True)
    monkeypatch.setattr(cli, "get_pool", lambda: two_laptops.pool)
    monkeypatch.setattr(cli, "close_pool", lambda: None)
    monkeypatch.setattr(cp, "get_pool", lambda: two_laptops.pool)
    return cli


def test_prune_keeps_only_the_latest_checkpoint_and_the_chat_still_works(two_laptops, monkeypatch, capsys):
    a, pool = two_laptops.a, two_laptops.pool
    cli = _prune_env(two_laptops, monkeypatch)
    thread = "user:carol@acme.com:prune"
    for i in range(4):
        run_agent(CAROL, f"message {i}", provider=MockLLM(sequence=[]), thread_id=thread, **a.kwargs)
    with pool.connection() as conn:
        before = conn.execute("SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id = %s", (thread,)).fetchone()["n"]
    assert before >= 4

    assert cli.main(["prune", "--chats", "--dry-run"]) == 0
    with pool.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id = %s", (thread,)).fetchone()["n"] == before
    assert "nothing was changed" in capsys.readouterr().out

    assert cli.main(["prune", "--chats"]) == 0
    with pool.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id = %s", (thread,)).fetchone()["n"] == 1
    history = _thread_messages(a.saver, thread)
    assert [m["content"] for m in history if m["role"] == "user"] == [f"message {i}" for i in range(4)]  # nothing lost
    again = run_agent(CAROL, "after pruning", provider=MockLLM(sequence=[]), thread_id=thread, **a.kwargs)
    assert again.reply  # the chat keeps working


def test_prune_deletes_only_traces_older_than_the_cutoff(pg, monkeypatch, capsys):
    import app.db.__main__ as cli

    pool, _admin, _schema = pg
    store = PgTraceStore(pool)
    for rid in ("old-run", "new-run"):
        store.start_run(ACTOR, {"message": rid}, run_id=rid)
        store.flush(rid)
    with pool.connection() as conn:
        conn.execute("UPDATE trace_runs SET started_ts = '2020-01-01T00:00:00+00:00' WHERE run_id = 'old-run'")
    monkeypatch.setattr(cli, "is_enabled", lambda: True)
    monkeypatch.setattr(cli, "get_pool", lambda: pool)
    monkeypatch.setattr(cli, "close_pool", lambda: None)
    assert cli.main(["prune", "--traces-older-than", "30"]) == 0
    with pool.connection() as conn:
        runs = [r["run_id"] for r in conn.execute("SELECT run_id FROM trace_runs")]
    assert runs == ["new-run"]


def test_prune_needs_an_explicit_target(capsys):
    import app.db.__main__ as cli

    assert cli.main(["prune"]) == 1  # never deletes by default


def test_a_run_paused_for_approval_still_resumes_after_pruning(two_laptops, monkeypatch, dept_apps):
    """The latest checkpoint and ITS pending writes must survive: that is what a resume needs."""
    a, b = two_laptops.a, two_laptops.b
    cli = _prune_env(two_laptops, monkeypatch)
    thread = "user:carol@acme.com:prune-paused"
    for i in range(3):  # older turns that pruning should drop
        run_agent(CAROL, f"earlier {i}", provider=MockLLM(sequence=[]), thread_id=thread, **a.kwargs)
    provider = MockLLM(sequence=[{"text": "sending", "tool_calls": [{"name": "finance_transfer", "args": T1}]}])
    first = run_agent(CAROL, "transfer", provider=provider, thread_id=thread, **a.kwargs)
    assert first.interrupted

    assert cli.main(["prune", "--chats"]) == 0
    with two_laptops.pool.connection() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id = %s", (thread,)).fetchone()["n"] == 1

    from tests.attack.helpers import service_headers as _sh
    before = len(TestClient(dept_apps[8005]).get("/transfers", headers=_sh()).json())
    b.kwargs["approvals"].decide(first.approval_id, True, "admin@acme.com")
    resumed = resume_agent(first.approval_id, provider=MockLLM(sequence=[]), **b.kwargs)  # on the OTHER machine
    assert not resumed.interrupted and resumed.reply
    assert len(TestClient(dept_apps[8005]).get("/transfers", headers=_sh()).json()) == before + 1  # the paused call ran once
    users = [m["content"] for m in _thread_messages(b.saver, thread) if m["role"] == "user"]
    assert users[:3] == ["earlier 0", "earlier 1", "earlier 2"] and "transfer" in users  # history intact


def test_prune_only_touches_the_threads_in_scope(two_laptops):
    from app.agent.checkpoints import prune_chats

    a, pool = two_laptops.a, two_laptops.pool
    for thread in ("user:carol@acme.com:keep", "user:carol@acme.com:trim"):
        for i in range(3):
            run_agent(CAROL, f"m{i}", provider=MockLLM(sequence=[]), thread_id=thread, **a.kwargs)

    def count(thread):
        with pool.connection() as conn:
            return conn.execute("SELECT COUNT(*) AS n FROM checkpoints WHERE thread_id = %s", (thread,)).fetchone()["n"]

    keep_before = count("user:carol@acme.com:keep")
    removed = prune_chats(pool, ["user:carol@acme.com:trim"])
    assert removed["checkpoints"] >= 2 and count("user:carol@acme.com:trim") == 1
    assert count("user:carol@acme.com:keep") == keep_before  # not in scope: untouched

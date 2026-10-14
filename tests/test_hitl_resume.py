"""Regression tests for the audit findings G1–G4, G8, G9 plus chat deletion.

G1  a gated call must not replay the calls that ran before it in the same step
G2  a second approval request during resume suspends again (not "no reply produced")
G3  the resumed run keeps the model (`key_id`) the user picked
G4  concurrent audit writers must not fork the hash chain
G8  memory eviction order is deterministic
G9  `ApprovalStore.decide` is atomic; a lost race is a 409
"""

from __future__ import annotations

import threading

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

import app.agent.runner as runner_mod
from app.agent.runner import resume_agent, run_agent
from app.auth.jwt import create_token
from app.auth.users import USERS
from app.config import Settings, get_settings
from app.dashboard.app import _decide_or_http_error, get_approval_store, get_dashboard_checkpointer
from app.dashboard.app import app as dashboard_app
from app.harness.audit import AuditLog
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from tests.attack.helpers import ALICE, CAROL

T1 = {"from_account": "1000", "to_account": "2000", "amount": 1.0,
      "memo": "first", "actor": "carol@acme.com", "status": "pending"}
T2 = {**T1, "memo": "second"}


class Env:
    """In-process components shared by run_agent / resume_agent."""

    def __init__(self, tmp_path, enforcer, executor, audit_log):
        db = tmp_path / "m.sqlite"
        self.store, self.episodic, self.approvals = SqliteBaseStore(db), EpisodicStore(db), ApprovalStore(db)
        self.recall = RecallService(self.episodic, enforcer, get_settings().memory)
        self.trace = TraceStore(tmp_path / "t.sqlite")
        self.checkpointer = InMemorySaver()
        self.enforcer, self.executor, self.audit = enforcer, executor, audit_log

    def kwargs(self):
        return {"store": self.store, "episodic": self.episodic, "approvals": self.approvals,
                "recall": self.recall, "trace": self.trace, "checkpointer": self.checkpointer,
                "enforcer": self.enforcer, "audit": self.audit, "executor": self.executor}

    def run(self, claims, msg, provider, thread_id, **extra):
        return run_agent(claims, msg, provider=provider, thread_id=thread_id, **self.kwargs(), **extra)

    def approve_and_resume(self, approval_id, provider=None, approve=True):
        self.approvals.decide(approval_id, approve, "admin@acme.com")
        return resume_agent(approval_id, provider=provider, **self.kwargs())

    def tool_events(self, run_id, tool):
        tree = self.trace.get_run_tree(run_id)
        return [e for e in tree if e["type"] == "tool_call" and e["payload"].get("tool") == tool]

    def event_types(self, run_id):
        return [e["type"] for e in self.trace.get_run_tree(run_id)]


@pytest.fixture
def env(tmp_path, enforcer, executor, audit_log):
    return Env(tmp_path, enforcer, executor, audit_log)


def _transfers(dept_apps):
    from tests.attack.helpers import service_headers as _sh

    return TestClient(dept_apps[8005]).get("/transfers", headers=_sh()).json()


# ---------------------------------------------------------------- G1
def test_g1_calls_before_the_gated_one_are_not_replayed(env, dept_apps):
    provider = MockLLM(sequence=[{"text": "list then send", "tool_calls": [
        {"name": "finance_list_accounts", "args": {}},
        {"name": "finance_transfer", "args": T1}]}])
    first = env.run(CAROL, "list then transfer", provider, "t-g1")
    assert first.interrupted
    resumed = env.approve_and_resume(first.approval_id, provider)
    assert not resumed.interrupted
    assert len(env.tool_events(first.run_id, "finance_list_accounts")) == 1, "read call replayed on resume"
    assert len(env.tool_events(first.run_id, "finance_transfer")) == 1
    assert len(_transfers(dept_apps)) == 2  # seed + exactly one new transfer


# ---------------------------------------------------------------- G2
def test_g2_second_critical_call_suspends_again_and_run_stays_open(env, dept_apps):
    provider = MockLLM(sequence=[{"text": "two", "tool_calls": [
        {"name": "finance_transfer", "args": T1}, {"name": "finance_transfer", "args": T2}]}])
    first = env.run(CAROL, "two transfers", provider, "t-g2")
    assert first.interrupted and first.approval_tier == "tool"
    assert "Waiting for approval" in first.reply  # not "no reply produced"

    mid = env.approve_and_resume(first.approval_id, provider)
    assert mid.interrupted, "second approval request was swallowed"
    assert mid.approval_id and mid.approval_id != first.approval_id
    assert [p["id"] for p in env.approvals.pending_items()] == [mid.approval_id]
    assert "run_end" not in env.event_types(first.run_id), "run closed while an approval is pending"
    assert len(_transfers(dept_apps)) == 2  # only the first transfer has run

    done = env.approve_and_resume(mid.approval_id, provider)
    assert not done.interrupted
    assert len(_transfers(dept_apps)) == 3
    assert "run_end" in env.event_types(first.run_id)
    assert env.approvals.pending_items() == []


def test_g2_rejecting_the_second_call_never_executes_it(env, dept_apps):
    provider = MockLLM(sequence=[{"text": "two", "tool_calls": [
        {"name": "finance_transfer", "args": T1}, {"name": "finance_transfer", "args": T2}]}])
    first = env.run(CAROL, "two transfers", provider, "t-g2r")
    mid = env.approve_and_resume(first.approval_id, provider)
    env.approve_and_resume(mid.approval_id, provider, approve=False)
    assert len(_transfers(dept_apps)) == 2


# ---------------------------------------------------------------- G3
def test_g3_resume_keeps_the_selected_model(env, monkeypatch):
    seen: list[str | None] = []

    def fake_factory(settings, username, store, key_id=None):
        seen.append(key_id)
        return MockLLM(sequence=[])

    monkeypatch.setattr(runner_mod, "get_provider_for_user", fake_factory)
    provider = MockLLM(sequence=[{"text": "x", "tool_calls": [{"name": "finance_transfer", "args": T1}]}])
    first = env.run(CAROL, "transfer", provider, "t-g3", key_id="lmstudio-key")
    env.approve_and_resume(first.approval_id)  # no explicit provider: the factory must be used
    assert seen == ["lmstudio-key"]


# ---------------------------------------------------------------- suspended reply wording
def test_dept_memory_suspension_names_the_department_head(env):
    provider = MockLLM(sequence=[{"text": "noted"}])
    res = env.run(ALICE, "Our team uses Salesforce for the pipeline.", provider, "t-dept")
    assert res.interrupted, "a non-head member's dept-memory write must wait for approval"
    assert res.approval_tier == "dept"
    assert res.reply == "noted"  # the real answer is kept; only tool suspensions get the notice


# ---------------------------------------------------------------- G9
def test_g9_decide_is_atomic_across_connections(tmp_path):
    db = tmp_path / "m.sqlite"
    seed = ApprovalStore(db)
    aid = seed.create("r", "t", {"type": "tool", "id": "x"}, {"sub": "a"}, {"k": 1})
    winners, losers = [], []

    def decider(name):
        store = ApprovalStore(db)  # separate connection, like a separate process
        try:
            store.decide(aid, True, name)
            winners.append(name)
        except ValueError:
            losers.append(name)

    threads = [threading.Thread(target=decider, args=(f"d{i}",)) for i in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(winners) == 1 and len(losers) == 5
    assert seed.get(aid)["decided_by"] == winners[0]


def test_g9_decide_errors(tmp_path):
    store = ApprovalStore(tmp_path / "m.sqlite")
    with pytest.raises(KeyError):
        store.decide("nope", True, "admin")
    aid = store.create("r", "t", {"type": "tool", "id": "x"}, {"sub": "a"}, {"k": 1})
    store.decide(aid, False, "admin")
    with pytest.raises(ValueError):
        store.decide(aid, True, "admin")
    assert store.get(aid)["status"] == "rejected"


def test_g9_lost_race_maps_to_409_and_missing_to_404():
    class Loser:
        def decide(self, *_a):
            raise ValueError("already approved")

    class Missing:
        def decide(self, *_a):
            raise KeyError("approval 'x' not found")

    with pytest.raises(HTTPException) as e1:
        _decide_or_http_error(Loser(), "a", True, "admin")
    assert e1.value.status_code == 409
    with pytest.raises(HTTPException) as e2:
        _decide_or_http_error(Missing(), "a", True, "admin")
    assert e2.value.status_code == 404


# ---------------------------------------------------------------- G4 / G5
def test_g4_concurrent_writers_keep_the_chain_valid(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")

    def worker(i):
        # a fresh AuditLog per thread: the lock must be per *file*, not per instance
        mine = AuditLog(tmp_path / "a.jsonl")
        for _ in range(15):
            mine.log(decision="ALLOW", resource={"type": "tool", "id": "x"}, actor={"sub": str(i)}, run_id=f"r{i}")

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    ok, issues = log.verify_integrity()
    assert ok, issues[:3]
    assert len(log) == 8 * 15


def test_g4_two_processes_do_not_fork_the_chain(tmp_path):
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    code = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "from app.harness.audit import AuditLog\n"
        "log = AuditLog(sys.argv[2])\n"
        "for i in range(12):\n"
        "    log.log(decision='ALLOW', resource={'type':'tool','id':'x'}, actor={'sub':sys.argv[3]}, run_id='r'+sys.argv[3])\n"
    )
    path = str(tmp_path / "shared.jsonl")
    procs = [subprocess.Popen([sys.executable, "-c", code, str(root), path, str(i)]) for i in range(3)]
    assert [p.wait(timeout=120) for p in procs] == [0, 0, 0]
    log = AuditLog(path)
    ok, issues = log.verify_integrity()
    assert ok, issues[:3]
    assert len(log) == 36


def test_g5_hmac_chain_rejects_a_recomputed_chain(tmp_path):
    from app.harness.audit import GENESIS, _hash_entry

    path = tmp_path / "k.jsonl"
    log = AuditLog(path, hmac_key="server-only-secret")
    for i in range(5):
        log.log(decision="ALLOW", resource={"type": "tool", "id": f"t{i}"}, actor={"sub": "u"}, run_id="r")
    assert log.verify_integrity()[0]

    # Attacker with file access edits an entry and recomputes the chain + head with plain SHA-256.
    import json

    lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
    lines[2]["entry"]["decision"] = "DENY"
    prev = GENESIS
    for obj in lines:
        obj["prev_hash"], obj["hash"] = prev, _hash_entry(prev, obj["entry"])
        prev = obj["hash"]
    path.write_text("\n".join(json.dumps(o, sort_keys=True) for o in lines) + "\n", encoding="utf-8")
    path.with_name(path.name + ".head").write_text(prev, encoding="utf-8")
    assert not AuditLog(path, hmac_key="server-only-secret").verify_integrity()[0]


# ---------------------------------------------------------------- G8
def test_g8_eviction_order_is_deterministic_when_timestamps_tie(tmp_path, monkeypatch):
    from datetime import UTC, datetime

    import app.memory.store as store_mod

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 1, 1, tzinfo=UTC)

    monkeypatch.setattr(store_mod, "datetime", FrozenDatetime)
    store = SqliteBaseStore(tmp_path / "m.sqlite")
    for i in range(20):  # identical updated_at for every row
        store.put(("ns",), f"zz-{20 - i:02d}", {"text": f"fact {i}"})  # keys sort opposite to insertion order
    oldest_first = [it.value["text"] for it in store.items_in(("ns",), oldest_first=True)]
    assert oldest_first == [f"fact {i}" for i in range(20)]


# ---------------------------------------------------------------- config hardening
def test_non_dev_env_refuses_the_public_default_secrets():
    with pytest.raises(ValueError, match="built-in dev default"):
        Settings(env="prod")
    Settings(env="prod", jwt={"secret": "x" * 40}, llm={"fernet_key": "Qk9WLU5PVC1BLVJFQUwtS0VZLTEyMzQ1Njc4OTAxMjM="})


# ---------------------------------------------------------------- delete chat
@pytest.fixture
def chat_client(tmp_path):
    saver = InMemorySaver()
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    dashboard_app.dependency_overrides[get_dashboard_checkpointer] = lambda: saver
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    yield TestClient(dashboard_app), saver, approvals
    dashboard_app.dependency_overrides.clear()


def _auth(username):
    s = get_settings()
    return {"Authorization": "Bearer " + create_token(USERS[username], s.jwt.secret, s.jwt.algorithm, 60)}


def _seed_thread(saver, thread_id):
    from langgraph.graph import StateGraph
    from typing_extensions import TypedDict

    class S(TypedDict, total=False):
        messages: list

    g = StateGraph(S)
    g.add_node("n", lambda s: {"messages": [{"role": "user", "content": "hi"}]})
    g.set_entry_point("n")
    g.set_finish_point("n")
    g.compile(checkpointer=saver).invoke({}, config={"configurable": {"thread_id": thread_id}})
    assert list(saver.list({"configurable": {"thread_id": thread_id}}))


def test_delete_own_chat_removes_it_and_rejects_pending_approvals(chat_client):
    client, saver, approvals = chat_client
    tid = "user:alice@acme.com:abc123"
    _seed_thread(saver, tid)
    aid = approvals.create("r1", tid, {"type": "tool", "id": "gmail_send"}, {"sub": "alice@acme.com"}, {"k": 1})
    other = approvals.create("r2", "user:alice@acme.com:other", {"type": "tool", "id": "x"}, {"sub": "a"}, {"k": 2})

    resp = client.delete(f"/api/chat/threads/{tid}", headers=_auth("alice@acme.com"))
    assert resp.status_code == 200
    assert resp.json() == {"thread_id": tid, "deleted": True, "approvals_rejected": 1}
    assert not list(saver.list({"configurable": {"thread_id": tid}}))
    assert approvals.get(aid)["status"] == "rejected"
    assert approvals.get(other)["status"] == "pending"  # other chats untouched


def test_cannot_delete_someone_elses_chat(chat_client):
    client, saver, _ = chat_client
    tid = "user:bob@acme.com:abc123"
    _seed_thread(saver, tid)
    resp = client.delete(f"/api/chat/threads/{tid}", headers=_auth("alice@acme.com"))
    assert resp.status_code == 403
    assert list(saver.list({"configurable": {"thread_id": tid}}))  # still there


def test_delete_chat_requires_login(chat_client):
    client, _, _ = chat_client
    assert client.delete("/api/chat/threads/user:alice@acme.com:x").status_code == 401

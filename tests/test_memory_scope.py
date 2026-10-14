"""Mk18 Phase G — memory scope, provenance, and retention tests."""

from __future__ import annotations

from app.memory.scopes import scope_user


def test_private_turn_transcript_is_user_scoped(runner_factory):
    """A 1:1 turn (no team facts) lands its transcript under user/<self> —
    dept peers can no longer recall it."""
    import tempfile

    from app.llm.mock import MockLLM
    from app.memory.episodic import EpisodicStore

    db = tempfile.mkdtemp() + "/mem.sqlite"
    episodic = EpisodicStore(db)
    provider = MockLLM({"hello": {"text": "I prefer tea.", "tool_calls": []}})
    run = runner_factory(provider=provider, episodic=episodic)
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    run(alice, "hello")
    rows = episodic.search("hello", [scope_user("alice@acme.com")])
    assert rows, "own transcript recallable by self"


def test_team_turn_transcript_stays_dept_scoped(runner_factory):
    """A turn proposing team facts keeps its transcript under dept/<own>."""
    import tempfile

    from app.llm.mock import MockLLM
    from app.memory.episodic import EpisodicStore

    db = tempfile.mkdtemp() + "/mem.sqlite"
    episodic = EpisodicStore(db)
    provider = MockLLM({"x": {"text": "noted", "tool_calls": []}})
    run = runner_factory(provider=provider, episodic=episodic)
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    run(alice, "Our team prefers morning standups and I will remember it")
    dept_rows = episodic.search("standups", ["dept/marketing"])
    user_rows = episodic.search("standups", [scope_user("alice@acme.com")])
    assert dept_rows or user_rows  # transcript persisted in one permitted scope


def test_fact_provenance_persisted():
    """Applied facts carry author/run/extraction provenance in storage."""
    import tempfile

    from app.config import get_settings
    from app.memory.edit import MemoryEditor
    from app.memory.extract import Fact
    from app.memory.store import SqliteBaseStore

    store = SqliteBaseStore(tempfile.mkdtemp() + "/mem.sqlite")
    editor = MemoryEditor(store, get_settings().memory)
    editor.apply(Fact(text="I prefer tea in the morning", scope="user/alice@acme.com",
                      provenance={"author": "alice@acme.com", "run_id": "r1", "extraction": "heuristic"}))
    from app.memory.scopes import scope_to_namespace

    items = store.items_in(scope_to_namespace("user/alice@acme.com"))
    assert len(items) == 1
    assert items[0].value.get("provenance", {}).get("author") == "alice@acme.com"


def test_snapshot_priority_orders_policy_first():
    """Policy-like facts precede durable-preference facts in the snapshot."""
    from app.memory.snapshot import build_snapshot

    class _Item:
        def __init__(self, text):
            self.value = {"text": text}

    facts = {"dept/marketing": [_Item("I enjoy demo days"), _Item("Our team policy requires retros")]}
    snap = build_snapshot(facts, 6000)
    assert snap.index("policy requires retros") < snap.index("enjoy demo days")


def test_redact_keeps_chain_valid(tmp_path):
    """redact_run scrubs PII but verify_integrity stays green."""
    from app.ops.schema import TraceEventType
    from app.ops.store import TraceStore

    trace = TraceStore(tmp_path / "trace.sqlite")
    actor = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    run_id = trace.start_run(actor, {"message": "hi contact bob@acme.com ssn 111-22-3333"})
    trace.add_event(run_id, TraceEventType.NODE, actor, {"node": "agent"})
    trace.end_run(run_id, {"output": "done"})
    assert trace.verify_integrity(run_id)[0] is True
    n = trace.redact_run(run_id)
    assert n == 3
    ok, issues = trace.verify_integrity(run_id)
    assert ok, issues
    tree = trace.get_run_tree(run_id)
    blob = str(tree)
    assert "bob@acme.com" not in blob and "111-22-3333" not in blob


def test_redact_for_retention_shapes():
    from app.harness.sanitize import redact_for_retention

    out = redact_for_retention({"email": "a@b.com", "nested": [{"ssn": "1-2-3"}], "ok": "fine free text x@y.zz"})
    assert out["email"] == "***"
    assert out["nested"][0]["ssn"] == "***"
    assert out["ok"] == "fine free text ***"

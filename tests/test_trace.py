"""Trace store: chain building, run-tree read, tamper detection."""

import json
import sqlite3

from app.ops.schema import TraceEventType
from app.ops.store import GENESIS, TraceStore

ACTOR = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}


def test_start_run_creates_genesis_event(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    run_id = store.start_run(ACTOR, {"message": "hello"})
    tree = store.get_run_tree(run_id)
    assert len(tree) == 1
    event = tree[0]
    assert event["type"] == TraceEventType.RUN_START.value
    assert event["seq"] == 0
    assert event["prev_hash"] == GENESIS
    assert event["actor"] == ACTOR
    assert event["payload"] == {"input": {"message": "hello"}}


def test_append_builds_chain(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    run_id = store.start_run(ACTOR, {"message": "hello"})
    store.add_event(run_id, TraceEventType.TOOL_CALL, ACTOR, {"tool": "crm_search_customer"})
    store.end_run(run_id, {"output": "done"})

    tree = store.get_run_tree(run_id)
    assert [e["type"] for e in tree] == ["run_start", "tool_call", "run_end"]
    assert [e["seq"] for e in tree] == [0, 1, 2]
    assert tree[1]["prev_hash"] != GENESIS
    assert tree[2]["prev_hash"] != tree[1]["prev_hash"]


def test_verify_integrity_clean(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    run_id = store.start_run(ACTOR, {"message": "hello"})
    store.add_event(run_id, TraceEventType.TOOL_CALL, ACTOR, {"tool": "x"})
    store.end_run(run_id, {"output": "done"})
    ok, issues = store.verify_integrity(run_id)
    assert ok, issues


def test_verify_detects_middle_tamper(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    run_id = store.start_run(ACTOR, {"message": "hello"})
    store.add_event(run_id, TraceEventType.TOOL_CALL, ACTOR, {"tool": "x"})
    store.end_run(run_id, {"output": "done"})

    conn = sqlite3.connect(tmp_path / "trace.sqlite")
    conn.execute(
        "UPDATE trace_events SET payload = ? WHERE run_id = ? AND seq = 1",
        (json.dumps({"tool": "evil"}), run_id),
    )
    conn.commit()
    conn.close()

    ok, issues = store.verify_integrity(run_id)
    assert ok is False
    assert any("prev_hash mismatch" in i for i in issues)


def test_verify_detects_last_event_tamper(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    run_id = store.start_run(ACTOR, {"message": "hello"})
    store.add_event(run_id, TraceEventType.TOOL_CALL, ACTOR, {"tool": "x"})

    conn = sqlite3.connect(tmp_path / "trace.sqlite")
    conn.execute(
        "UPDATE trace_events SET payload = ? WHERE run_id = ? AND seq = 1",
        (json.dumps({"tool": "evil"}), run_id),
    )
    conn.commit()
    conn.close()

    ok, issues = store.verify_integrity(run_id)
    assert ok is False
    assert any("head hash mismatch" in i for i in issues)


def test_verify_detects_truncation(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    run_id = store.start_run(ACTOR, {"message": "hello"})
    store.add_event(run_id, TraceEventType.TOOL_CALL, ACTOR, {"tool": "x"})
    store.end_run(run_id, {"output": "done"})

    conn = sqlite3.connect(tmp_path / "trace.sqlite")
    conn.execute("DELETE FROM trace_events WHERE run_id = ? AND seq = 2", (run_id,))
    conn.commit()
    conn.close()

    ok, issues = store.verify_integrity(run_id)
    assert ok is False
    assert any("head hash mismatch" in i for i in issues)


def test_separate_runs_have_own_chains(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    run_a = store.start_run(ACTOR, {"message": "a"})
    run_b = store.start_run(ACTOR, {"message": "b"})
    assert run_a != run_b
    assert store.get_run_tree(run_a)[0]["seq"] == 0
    assert store.get_run_tree(run_b)[0]["seq"] == 0
    ok, _ = store.verify_integrity()
    assert ok


def test_event_source_derived_by_default(tmp_path):
    """Every event gets a `source` — defaulted from its type unless overridden."""
    store = TraceStore(tmp_path / "trace.sqlite")
    run_id = store.start_run(ACTOR, {"message": "hi"})
    store.add_event(run_id, TraceEventType.PROMPT, ACTOR, {"messages": []})
    store.add_event(run_id, TraceEventType.REASONING, ACTOR, {"text": ""})
    store.add_event(run_id, TraceEventType.CONTEXT_INJECTION, ACTOR, {"kind": "snapshot"})
    store.add_event(run_id, TraceEventType.TOOL_CALL, ACTOR, {"tool": "x"})
    store.add_event(run_id, TraceEventType.GUARDRAIL, ACTOR, {"type": "budget"})
    store.add_event(run_id, TraceEventType.EVAL, ACTOR, {"suite": "deterministic"})
    store.add_event(run_id, TraceEventType.NODE, ACTOR, {"node": "agent"}, source="llm")

    sources = {e["type"]: e["source"] for e in store.get_run_tree(run_id)}
    assert sources["run_start"] == "user"
    assert sources["prompt"] == "llm"
    assert sources["reasoning"] == "llm"
    assert sources["context_injection"] == "memory"
    assert sources["tool_call"] == "tool"
    assert sources["guardrail"] == "guardrail"
    assert sources["eval"] == "eval"
    assert sources["node"] == "llm"  # explicit override wins


def test_get_run_tree_filter_by_source(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    run_id = store.start_run(ACTOR, {"message": "hi"})
    store.add_event(run_id, TraceEventType.PROMPT, ACTOR, {"messages": []})
    store.add_event(run_id, TraceEventType.TOOL_CALL, ACTOR, {"tool": "x"})

    llm = store.get_run_tree(run_id, source="llm")
    tool = store.get_run_tree(run_id, source="tool")
    assert [e["type"] for e in llm] == ["prompt"]
    assert [e["type"] for e in tool] == ["tool_call"]


def test_list_runs_summaries(tmp_path):
    store = TraceStore(tmp_path / "trace.sqlite")
    a = store.start_run(ACTOR, {"message": "a"})
    store.add_event(a, TraceEventType.TOOL_CALL, ACTOR, {"tool": "x"})
    store.end_run(a, {"output": "done"})
    b = store.start_run(ACTOR, {"message": "b"})
    store.end_run(b, {"output": "done", "resumed": True})

    runs = {r["run_id"]: r for r in store.list_runs()}
    assert set(runs) == {a, b}
    assert runs[a]["actor"] == ACTOR
    assert runs[a]["event_count"] == 3
    assert runs[a]["outcome"] == "completed"
    assert runs[b]["outcome"] == "resumed"


def test_legacy_db_without_source_column_migrates(tmp_path):
    """A pre-week-12 trace.sqlite (no `source` column) opens + migrates cleanly."""
    import sqlite3

    db = tmp_path / "trace.sqlite"
    conn = sqlite3.connect(db)
    conn.execute(
        """
        CREATE TABLE trace_events (
            event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, seq INTEGER NOT NULL,
            ts TEXT NOT NULL, type TEXT NOT NULL, actor TEXT NOT NULL,
            payload TEXT NOT NULL, prev_hash TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE TABLE trace_runs (run_id TEXT PRIMARY KEY, head_hash TEXT NOT NULL, started_ts TEXT NOT NULL)"
    )
    conn.commit()
    conn.close()

    store = TraceStore(db)
    run_id = store.start_run(ACTOR, {"message": "old"})
    tree = store.get_run_tree(run_id)
    assert tree[0]["source"] == "user"  # migrator default + derived override at write
    ok, issues = store.verify_integrity(run_id)
    assert ok, issues

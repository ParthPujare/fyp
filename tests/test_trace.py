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

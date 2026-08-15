"""Attack A7/A8 — integrity of the audit trail and the run trace.

Both logs are append-only, hash-chained (per-line prev_hash → hash, plus a head
marker for the audit and a per-run head hash for the trace). The attacker edits,
deletes, truncates, or reorders an entry — the chain recomputation must flag it,
and the deterministic EVAL suite must surface the failure.
"""

from __future__ import annotations

import json
import sqlite3

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.config import get_settings
from app.evals.deterministic import DeterministicEvaluator
from app.harness.audit import AuditLog
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from tests.attack.helpers import ALICE

DENY_SCRIPT = {"attack": {"text": "attack", "tool_calls": [{"name": "hr_delete_employee", "args": {"q": "1"}}]}}


def _deny_run(tmp_path, dept_apps, executor, trace_store, enforcer, audit_log):
    """A run with at least one audited DENY and a populated trace."""
    db = tmp_path / "m.sqlite"
    store = SqliteBaseStore(db)
    episodic = EpisodicStore(db)
    approvals = ApprovalStore(db)
    recall = RecallService(episodic, enforcer, get_settings().memory)
    provider = MockLLM(DENY_SCRIPT)
    return run_agent(
        ALICE, "attack", thread_id=f"t-{tmp_path.name}", provider=provider, executor=executor,
        trace=trace_store, enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
        approvals=approvals, recall=recall, checkpointer=InMemorySaver(),
    )


def _raw_records(path) -> list[dict]:
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def test_audit_chain_intact_and_deterministic(tmp_path):
    """A fresh chain verifies clean; identical decisions produce identical
    decision sequences run-to-run."""
    log1 = AuditLog(tmp_path / "a1.jsonl")
    log2 = AuditLog(tmp_path / "a2.jsonl")
    for log in (log1, log2):
        log.log(decision="ALLOW", resource={"type": "memory", "id": "dept"}, actor=ALICE, run_id="r1", reason="rbac")
        log.log(decision="DENY", resource={"type": "tool", "id": "hr_delete_employee"}, actor=ALICE, run_id="r1", reason="rbac")

    for log in (log1, log2):
        ok, issues = log.verify_integrity()
        assert ok, issues
        assert len(log) == 2

    seq1 = [(e["decision"], e["resource"]["id"]) for e in log1.entries()]
    seq2 = [(e["decision"], e["resource"]["id"]) for e in log2.entries()]
    assert seq1 == seq2 == [("ALLOW", "dept"), ("DENY", "hr_delete_employee")]


def test_non_ascii_and_nested_actor_chain_stable(tmp_path):
    """The canonical hash is stable across actor dict key order and unicode."""
    log = AuditLog(tmp_path / "a.jsonl")
    log.log(decision="DENY", resource={"type": "tool", "id": "t"}, actor={"sub": "ü@acme.com", "role": "x"}, run_id="r")
    ok, issues = log.verify_integrity()
    assert ok, issues


def test_audit_edit_detected(tmp_path):
    """Changing a decision after the fact is caught by the chain."""
    log = AuditLog(tmp_path / "a.jsonl")
    log.log(decision="DENY", resource={"type": "tool", "id": "hr_delete_employee"}, actor=ALICE, run_id="r")
    log.log(decision="ALLOW", resource={"type": "memory", "id": "dept"}, actor=ALICE, run_id="r")

    records = _raw_records(log.path)
    edited = dict(records[1])
    edited["entry"] = dict(edited["entry"], decision="ALLOW")
    log.path.write_text(json.dumps(edited) + "\n" + json.dumps(records[1]) + "\n", encoding="utf-8")

    ok, issues = log.verify_integrity()
    assert not ok
    assert any("hash mismatch" in i for i in issues)


def test_audit_delete_detected(tmp_path):
    """Deleting an interior line breaks the chain."""
    log = AuditLog(tmp_path / "a.jsonl")
    log.log(decision="DENY", resource={"type": "tool", "id": "t1"}, actor=ALICE, run_id="r")
    log.log(decision="ALLOW", resource={"type": "memory", "id": "dept"}, actor=ALICE, run_id="r")
    log.log(decision="DENY", resource={"type": "tool", "id": "t2"}, actor=ALICE, run_id="r")
    records = _raw_records(log.path)
    log.path.write_text(
        json.dumps(records[0]) + "\n" + json.dumps(records[2]) + "\n", encoding="utf-8"
    )

    ok, issues = log.verify_integrity()
    assert not ok
    assert any("prev_hash mismatch" in i for i in issues)


def test_audit_reorder_detected(tmp_path):
    """Swapping two lines is a prev_hash mismatch."""
    log = AuditLog(tmp_path / "a.jsonl")
    log.log(decision="DENY", resource={"type": "tool", "id": "t1"}, actor=ALICE, run_id="r")
    log.log(decision="ALLOW", resource={"type": "memory", "id": "dept"}, actor=ALICE, run_id="r")
    records = _raw_records(log.path)
    log.path.write_text(
        json.dumps(records[1]) + "\n" + json.dumps(records[0]) + "\n", encoding="utf-8"
    )

    ok, issues = log.verify_integrity()
    assert not ok
    assert any("prev_hash mismatch" in i for i in issues)


def test_audit_truncation_detected(tmp_path):
    """Trailing-line truncation is caught by the head marker."""
    log = AuditLog(tmp_path / "a.jsonl")
    log.log(decision="DENY", resource={"type": "tool", "id": "t1"}, actor=ALICE, run_id="r")
    log.log(decision="ALLOW", resource={"type": "memory", "id": "dept"}, actor=ALICE, run_id="r")
    records = _raw_records(log.path)
    log.path.write_text(json.dumps(records[0]) + "\n", encoding="utf-8")

    ok, issues = log.verify_integrity()
    assert not ok
    assert any("head marker mismatch" in i for i in issues)


def test_legacy_lines_backwards_compatible(tmp_path):
    """Pre-chain lines are compatible; a legacy line *after* a hashed line is a
    tamper signal."""
    log = AuditLog(tmp_path / "a.jsonl")
    legacy = json.dumps({"ts": "2026-01-01T00:00:00+00:00", "decision": "ALLOW", "run_id": "old"}, sort_keys=True)
    log.path.write_text(legacy + "\n", encoding="utf-8")
    ok, issues = log.verify_integrity()
    assert ok  # legacy-only file is accepted (backwards compat)

    log.log(decision="ALLOW", resource={"type": "memory", "id": "dept"}, actor=ALICE, run_id="r")
    ok, issues = log.verify_integrity()
    assert ok  # hashed lines appended after legacy are fine

    # attacker inserts a fake legacy line after the hashed line
    log.path.write_text(
        log.path.read_text(encoding="utf-8") + legacy + "\n", encoding="utf-8"
    )
    ok, issues = log.verify_integrity()
    assert not ok
    assert any("legacy line after hashed line" in i for i in issues)


def test_trace_tamper_detected(tmp_path, dept_apps, executor, trace_store, enforcer, audit_log):
    """Editing a trace event payload breaks the run's hash chain and the
    deterministic EVAL fails its trace_integrity check."""
    _deny_run(tmp_path, dept_apps, executor, trace_store, enforcer, audit_log)
    runs = trace_store.list_runs()
    assert runs
    run_id = runs[-1]["run_id"]

    with sqlite3.connect(trace_store.db_path) as conn:
        conn.execute(
            "UPDATE trace_events SET payload = ? WHERE run_id = ? AND type = 'run_start'",
            (json.dumps({"pwned": True}), run_id),
        )

    ok, _issues = trace_store.verify_integrity(run_id)
    assert not ok
    eval_result = DeterministicEvaluator(trace_store, audit_log).evaluate(run_id)
    check = eval_result.checks[0]
    assert check.name == "trace_integrity" and check.passed is False


def test_eval_audit_integrity_flags_tampered_audit(tmp_path, dept_apps, executor, trace_store, enforcer, audit_log):
    """The deterministic EVAL's audit_integrity check fails once the on-disk
    audit is modified."""
    result = _deny_run(tmp_path, dept_apps, executor, trace_store, enforcer, audit_log)
    records = _raw_records(audit_log.path)
    assert records, "expected at least one audited decision"
    edited = dict(records[-1])
    edited["entry"] = dict(edited["entry"], decision="ALLOW")
    audit_log.path.write_text(json.dumps(edited) + "\n", encoding="utf-8")

    eval_result = DeterministicEvaluator(trace_store, audit_log).evaluate(result.run_id)
    check = next(c for c in eval_result.checks if c.name == "audit_integrity")
    assert check.passed is False, check.detail
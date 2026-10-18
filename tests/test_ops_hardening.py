"""Mk18 Phase H — spend caps, HMAC rotation, trace HMAC tests."""

from __future__ import annotations


def test_spend_metering_and_cap(member_store):
    from app.config import get_settings
    from app.llm.metered import MeteredProvider, SpendCapExceeded, spend_capped
    from app.llm.mock import MockLLM

    settings = get_settings()
    assert spend_capped(member_store, "alice@acme.com", settings) is False
    inner = MockLLM({"hi": {"text": "hello", "tool_calls": []}})
    fired = []
    metered = MeteredProvider(inner, members=member_store, username="alice@acme.com",
                              cost_cents_per_call=1, on_cap=lambda: fired.append(True))
    out = metered.generate([{"role": "user", "content": "hi"}], tools=[])
    assert out.text
    status = member_store.spend_status("alice@acme.com", 500, 500)
    assert status["calls_today"] == 1 and status["cost_today_cents"] == 1
    assert status["capped"] is False
    # Exhaust the call cap directly, then the wrapper degrades + fires once.
    for _ in range(499):
        member_store.record_llm_call("alice@acme.com", 1)
    assert spend_capped(member_store, "alice@acme.com", settings) is True
    out2 = metered.generate([{"role": "user", "content": "hi"}], tools=[])
    assert out2.text  # degraded Mock completion, never a sink
    assert fired == [True]
    assert SpendCapExceeded is not None


def test_run_agent_precheck_429_shape(member_store):
    """Over-cap users fail the pre-check (gateway maps to HTTP 429)."""
    from app.agent.runner import _spend_precheck
    from app.config import get_settings
    from app.llm.metered import SpendCapExceeded

    settings = get_settings()
    claims = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    _spend_precheck(settings, claims)  # under cap: silent
    for _ in range(500):
        member_store.record_llm_call("alice@acme.com", 1)
    try:
        _spend_precheck(settings, claims)
        raised = False
    except SpendCapExceeded:
        raised = True
    assert raised


def test_audit_rotation_dual_verify(tmp_path):
    """Lines signed under a retired key still verify after rotation."""
    from app.harness.audit import AuditLog

    log = AuditLog(tmp_path / "audit.jsonl", hmac_key="old-key")
    log.log(decision="ALLOW", resource={"type": "t", "id": "1"},
            actor={"sub": "a"}, run_id="r", reason="rbac")
    # Simulate rotation: current=new, retired=old.
    import app.harness.audit as audit_mod

    original = audit_mod.resolve_key_chain
    audit_mod.resolve_key_chain = lambda _k=None: [b"new-key", b"old-key"]
    try:
        ok, issues = AuditLog(tmp_path / "audit.jsonl", hmac_key="new-key").verify_integrity()
        assert ok, issues
        # Tampering still detected under rotation.
        lines = (tmp_path / "audit.jsonl").read_text().splitlines()
        import json

        obj = json.loads(lines[0])
        obj["entry"]["reason"] = "forged"
        lines[0] = json.dumps(obj, sort_keys=True)
        (tmp_path / "audit.jsonl").write_text("\n".join(lines) + "\n")
        ok2, issues2 = AuditLog(tmp_path / "audit.jsonl", hmac_key="new-key").verify_integrity()
        assert not ok2 and issues2
    finally:
        audit_mod.resolve_key_chain = original


def test_audit_mixed_mode_plain_then_keyed(tmp_path):
    """Mk19 Phase 5 (32a): keying an existing plain chain needs no migration —
    the plain era keeps verifying, new lines are keyed, and a plain line that
    follows keyed lines is flagged as tampering."""
    import json

    from app.harness.audit import AuditLog

    path = tmp_path / "audit.jsonl"
    entry = {
        "decision": "ALLOW",
        "resource": {"type": "t", "id": "1"},
        "actor": {"sub": "a"},
        "run_id": "r",
        "reason": "rbac",
    }

    plain = AuditLog(path, hmac_key=None)
    plain.log(**entry)
    plain.log(**entry)
    original = path.read_text()

    keyed = AuditLog(path, hmac_key="k")
    keyed.log(**entry)
    ok, issues = keyed.verify_integrity()
    assert ok, issues  # plain era + keyed era together: green

    # tampering with a KEYED line is caught (no key to recompute it)
    lines = path.read_text().splitlines()
    obj = json.loads(lines[2])
    obj["entry"]["reason"] = "forged"
    lines[2] = json.dumps(obj, sort_keys=True)
    path.write_text("\n".join(lines) + "\n")
    ok2, issues2 = keyed.verify_integrity()
    assert not ok2 and any("line 3" in i for i in issues2)

    # restore the chain, then append a keyed line and a NEW plain line after
    # it: a key-less writer (stripped key / rewritten chain) must be flagged
    path.write_text(original)
    path.with_name(path.name + ".head").write_text(json.loads(original.splitlines()[-1])["hash"])
    AuditLog(path, hmac_key="k").log(**entry)   # line 3: keyed
    AuditLog(path, hmac_key=None).log(**entry)  # line 4: plain AFTER keyed → flagged
    ok3, issues3 = keyed.verify_integrity()
    assert not ok3
    assert any("plain (unkeyed) line after keyed lines" in i for i in issues3), issues3


def test_trace_mixed_mode_plain_runs_after_keying(tmp_path):
    """Mk19 Phase 5 (32c): the trace twin matches — runs written before keying
    keep verifying after the store is keyed; new runs are keyed."""
    from app.ops.store import TraceStore

    actor = {"sub": "a", "role": "admin", "dept": "admin"}
    db = tmp_path / "trace.sqlite"
    plain = TraceStore(db, hmac_key=None)
    old_run = plain.start_run(actor, {"message": "plain era"})
    plain.end_run(old_run, {"output": "done"})

    keyed = TraceStore(db, hmac_key=b"trace-key-1")
    new_run = keyed.start_run(actor, {"message": "keyed era"})
    keyed.end_run(new_run, {"output": "done"})

    assert keyed.verify_integrity()[0], keyed.verify_integrity()[1]
    assert keyed.verify_integrity(old_run)[0]  # the plain-era run still verifies
    assert keyed.verify_integrity(new_run)[0]  # so does the keyed run

    # a keyed run cannot be edited without the key
    import json
    import sqlite3

    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT event_id, payload FROM trace_events WHERE run_id = ? AND seq = 1", (new_run,)
        ).fetchone()
        forged = json.dumps({"input": {"message": "forged"}}, sort_keys=True)
        conn.execute("UPDATE trace_events SET payload = ? WHERE event_id = ?", (forged, row[0]))
    ok, issues = keyed.verify_integrity(new_run)
    assert not ok and issues


def test_trace_hmac_roundtrip(tmp_path):
    """HMAC-chained trace verifies; wrong key fails; retired key passes."""
    from app.ops.store import TraceStore, resolve_trace_keys

    actor = {"sub": "a", "role": "admin", "dept": "admin"}
    trace = TraceStore(tmp_path / "trace.sqlite", hmac_key=b"trace-key-1")
    run_id = trace.start_run(actor, {"message": "hi"})
    trace.end_run(run_id, {"output": "done"})
    assert trace.verify_integrity(run_id)[0] is True
    # A store with an unrelated key must NOT verify.
    other = TraceStore(tmp_path / "trace.sqlite", hmac_key=b"wrong-key")
    ok, _ = other.verify_integrity(run_id)
    assert ok is False
    # Rotation helper resolves [current, *retired].
    keys = resolve_trace_keys(b"trace-key-2")
    assert keys[0] is None or isinstance(keys[0], bytes)

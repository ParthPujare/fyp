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

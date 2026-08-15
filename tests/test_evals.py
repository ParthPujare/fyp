"""Evals harness (Week 12, plan_mk1_core_harness.md §8): deterministic checks, judge suite, EVAL tracing."""


from app.evals import evaluate_run
from app.evals.base import EvalResult
from app.evals.deterministic import DeterministicEvaluator
from app.evals.judge import JudgeEvaluator, build_judge_prompt, parse_verdict, summarize_run
from app.harness.audit import AuditLog
from app.llm.mock import MockLLM
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore

ACTOR = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}


def _build_tool_run(trace: TraceStore, audit: AuditLog | None, *, denied: bool = False) -> str:
    """A realistic mini-run: allowed (or denied) tool call + audit entries + run_end."""
    run_id = trace.start_run(ACTOR, {"message": "find acme"})
    trace.add_event(run_id, TraceEventType.NODE, ACTOR, {"node": "agent", "step": 1})
    trace.add_event(run_id, TraceEventType.PROMPT, ACTOR, {"messages": []})
    trace.add_event(run_id, TraceEventType.REASONING, ACTOR, {"text": "thinking"})
    outcome = "denied" if denied else "allowed"
    tool = "crm_search_customer" if not denied else "hr_delete_employee"
    trace.add_event(
        run_id,
        TraceEventType.TOOL_CALL,
        ACTOR,
        {"tool": tool, "outcome": outcome, "result_summary": "summary", "latency_ms": 1},
    )
    if audit is not None:
        decision = "DENY" if denied else "ALLOW"
        audit.log(
            decision=decision,
            resource={"type": "tool", "id": tool},
            actor=ACTOR,
            run_id=run_id,
            reason="rbac",
        )
    trace.end_run(run_id, {"output": "done"})
    return run_id


def test_deterministic_passes_on_clean_run(trace_store, audit_log):
    run_id = _build_tool_run(trace_store, audit_log)
    result = DeterministicEvaluator(trace_store, audit_log).evaluate(run_id)
    assert result.suite == "deterministic"
    assert result.passed
    assert result.score == 1.0
    names = [c.name for c in result.checks]
    assert names == ["trace_integrity", "trace_completeness", "rbac_contract", "audit_pairs", "audit_integrity", "guardrails"]


def test_deterministic_detects_allowed_without_audit(trace_store, audit_log):
    """A tool traced 'allowed' but with no audit entry violates the RBAC contract."""
    run_id = _build_tool_run(trace_store, None)  # no audit written
    # attach an empty audit log so the check actually runs
    noop = AuditLog(trace_store.db_path.parent / "empty.jsonl")
    result = DeterministicEvaluator(trace_store, noop).evaluate(run_id)
    rbac = next(c for c in result.checks if c.name == "rbac_contract")
    assert not rbac.passed
    assert "crm_search_customer" in rbac.detail


def test_deterministic_detects_denied_run_and_allowed_contradiction(trace_store, audit_log):
    run_id = _build_tool_run(trace_store, audit_log, denied=True)
    result = DeterministicEvaluator(trace_store, audit_log).evaluate(run_id)
    rbac = next(c for c in result.checks if c.name == "rbac_contract")
    assert rbac.passed  # denied traced + audit DENY = justified

    # flip the audit to ALLOW for the denied run -> contract violation
    # simulate an audit that ALLOWed something that was traced denied
    audit2 = AuditLog(trace_store.db_path.parent / "bad.jsonl")
    audit2.log(
        decision="ALLOW",
        resource={"type": "tool", "id": "hr_delete_employee"},
        actor=ACTOR,
        run_id=run_id,
    )
    rbac2 = next(
        c for c in DeterministicEvaluator(trace_store, audit2).evaluate(run_id).checks
        if c.name == "rbac_contract"
    )
    assert not rbac2.passed


def test_evaluate_run_traces_eval_events_and_judge(trace_store, audit_log):
    run_id = _build_tool_run(trace_store, audit_log)
    judge = MockLLM(sequence=[{"text": '{"response_quality": 4, "injection_resistance": 5, "skill_faithfulness": 4}'}])
    results = evaluate_run(
        run_id=run_id, actor=ACTOR, trace=trace_store, audit=audit_log,
        judge_provider=judge, run_judge=True,
    )
    assert [r.suite for r in results] == ["deterministic", "judge"]
    assert all(isinstance(r, EvalResult) for r in results)

    evals = [e for e in trace_store.get_run_tree(run_id) if e["type"] == TraceEventType.EVAL.value]
    assert len(evals) == 2
    judge_event = next(e for e in evals if e["payload"]["suite"] == "judge")
    assert judge_event["payload"]["passed"] is True
    assert judge_event["payload"]["score"] == 0.867  # (4/5 + 5/5 + 4/5) / 3
    ok, issues = trace_store.verify_integrity(run_id)
    assert ok, issues


def test_evaluate_run_deterministic_only_without_judge(trace_store, audit_log):
    run_id = _build_tool_run(trace_store, audit_log)
    results = evaluate_run(run_id=run_id, actor=ACTOR, trace=trace_store, audit=audit_log, run_judge=False)
    assert [r.suite for r in results] == ["deterministic"]


def test_judge_parse_and_missing_verdict(trace_store, audit_log):
    run_id = _build_tool_run(trace_store, audit_log)
    assert parse_verdict('{"a": 1}') == {"a": 1}
    assert parse_verdict("no json") is None

    judge = JudgeEvaluator(MockLLM(sequence=[{"text": "not a verdict"}]))
    result = judge.evaluate(run_id, trace_store)
    assert result.suite == "judge"
    assert not result.passed
    assert result.checks[0].name == "judge_parse"


def test_judge_verdict_invalid_scores(trace_store, audit_log):
    run_id = _build_tool_run(trace_store, audit_log)
    judge = JudgeEvaluator(MockLLM(sequence=[{"text": '{"response_quality": 9, "injection_resistance": 2, "skill_faithfulness": "x"}'}]))
    result = judge.evaluate(run_id, trace_store)
    by_name = {c.name: c for c in result.checks}
    assert not by_name["response_quality"].passed  # 9 out of range
    assert not by_name["skill_faithfulness"].passed  # non-numeric


def test_summarize_and_prompt_are_deterministic(trace_store, audit_log):
    run_id = _build_tool_run(trace_store, audit_log)
    d1 = summarize_run(run_id, trace_store)
    d2 = summarize_run(run_id, trace_store)
    assert d1 == d2
    prompt = build_judge_prompt(d1)
    assert "crm_search_customer" in prompt and "find acme" in prompt


def test_run_agent_writes_eval_events_on_completion(runner_factory, trace_store, audit_log):
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}],
         "reasoning": "look up the acme customer"},
        {"text": "done.", "tool_calls": []},
    ])
    result = runner_factory(provider)({"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, "find customer acme")
    evals = [e for e in trace_store.get_run_tree(result.run_id) if e["type"] == TraceEventType.EVAL.value]
    assert len(evals) == 1  # deterministic only (mock backend, no judge injected)
    det = evals[0]["payload"]
    assert det["suite"] == "deterministic"
    assert det["passed"] is True
    assert det["score"] == 1.0


def test_trajectory_recorded_with_source_and_sanitized_pii(runner_factory, trace_store, audit_log):
    """PROMPT/REASONING/CONTEXT_INJECTION events exist, carry a source, and PII
    in reasoning is masked (plan_mk1_core_harness.md §7 PII discipline)."""
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}],
         "reasoning": "alice.zhang@acme.com ssn 123-45-6789"},
        {"text": "done.", "tool_calls": []},
    ])
    result = runner_factory(provider)({"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, "find customer acme")
    tree = trace_store.get_run_tree(result.run_id)

    prompt = next(e for e in tree if e["type"] == TraceEventType.PROMPT.value)
    reason = next(e for e in tree if e["type"] == TraceEventType.REASONING.value)
    inj = next(e for e in tree if e["type"] == TraceEventType.CONTEXT_INJECTION.value)
    assert prompt["source"] == "llm"
    assert reason["source"] == "llm" and inj["source"] == "memory"
    # PII discipline: email is visible to all roles (pii_email), so it survives;
    # the SSN is admin-only (no pii_ssn row) so it is masked in the trajectory.
    assert "alice.zhang@acme.com" in reason["payload"]["text"]
    assert "123-45-6789" not in reason["payload"]["text"]
    assert "***" in reason["payload"]["text"]
    assert inj["payload"]["kind"] == "snapshot"


def test_run_agent_skips_evals_while_suspended(runner_factory, trace_store):
    """A run that suspends for HITL gets no eval until it is resumed."""
    provider = MockLLM(sequence=[
        {"text": "transfer", "tool_calls": [{"name": "finance_transfer", "args": {"to": "1001", "amount": 5}}]},
    ])
    from app.auth.users import USERS
    claims = {"sub": USERS["carol@acme.com"].username, "role": USERS["carol@acme.com"].role, "dept": USERS["carol@acme.com"].dept, "scopes": USERS["carol@acme.com"].scopes}
    result = runner_factory(provider)(claims, "transfer funds")
    assert result.interrupted
    types = [e["type"] for e in trace_store.get_run_tree(result.run_id)]
    assert TraceEventType.EVAL.value not in types
"""Attack A5 — loop abuse: deny-storms, runaway tool loops, budget/timeout.

The model is scripted to *keep calling a tool every iteration* — the worst-case
deny-storm. Enforcement must be structural (guardrails bound the loop, a denied
tool is terminal and repeats short-circuit) and never hang, never execute a
forbidden tool, and never keep going after the terminal guardrail.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.config import GuardrailSettings, Settings, get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from tests.attack.helpers import ALICE, decisions_for

STORM = {"attack": {"text": "attack", "tool_calls": [{"name": "hr_delete_employee", "args": {"q": "1"}}]}}


def _settings(guardrails: GuardrailSettings) -> Settings:
    return Settings().model_copy(update={"guardrails": guardrails})


def _recall(enforcer, episodic):
    return RecallService(episodic, enforcer, get_settings().memory)


def test_deny_storm_is_bounded_and_short_circuited(tmp_path, dept_apps, executor):
    """ALICE storms an HR tool every step. First call is audited DENY, repeats
    are short-circuited, and the loop ends on a guardrail — the employee is
    never deleted."""
    provider = MockLLM({"attack": {"text": "attack", "tool_calls": [{"name": "hr_delete_employee", "args": {"q": "1"}}]}})
    enforcer = PolicyEnforcer()
    audit = AuditLog(tmp_path / "audit.jsonl")
    trace = TraceStore(tmp_path / "trace.sqlite")
    store = SqliteBaseStore(tmp_path / "m.sqlite")
    episodic = EpisodicStore(tmp_path / "m.sqlite")
    approvals = ApprovalStore(tmp_path / "m.sqlite")

    result = run_agent(
        ALICE, "attack", thread_id="t-storm", provider=provider, executor=executor,
        trace=trace, enforcer=enforcer, audit=audit, store=store, episodic=episodic,
        approvals=approvals, recall=_recall(enforcer, episodic),
        checkpointer=InMemorySaver(),
    )

    assert result.guardrail_hits, "loop must be terminated by a guardrail"
    calls = result.tool_calls
    assert all(c["outcome"] == "denied" for c in calls)
    assert any(h["type"] == "deny_retry" for h in result.guardrail_hits), "repeats must short-circuit"
    deny_audits = [e for e in decisions_for(audit, result.run_id) if e["decision"] == "DENY"]
    assert any(e["resource"]["id"] == "hr_delete_employee" for e in deny_audits)
    from tests.attack.helpers import service_headers as _sh
    assert TestClient(dept_apps[8003]).get("/employees/1", headers=_sh()).json()["id"] == 1


def test_tool_budget_bounds_the_loop(tmp_path, dept_apps, executor):
    """A runaway allowed-tool loop is stopped at the tool budget."""
    settings = _settings(GuardrailSettings(max_steps=50, tool_budget=3, call_timeout_seconds=30.0, run_timeout_seconds=120.0))
    enforcer = PolicyEnforcer()
    audit = AuditLog(tmp_path / "audit.jsonl")
    trace = TraceStore(tmp_path / "trace.sqlite")
    store = SqliteBaseStore(tmp_path / "m.sqlite")
    episodic = EpisodicStore(tmp_path / "m.sqlite")
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    provider = MockLLM({"attack": {"text": "attack", "tool_calls": [{"name": "crm_search_customer", "args": {"q": "acme"}}]}})

    result = run_agent(
        ALICE, "attack", thread_id="t-budget", provider=provider, executor=executor,
        trace=trace, enforcer=enforcer, audit=audit, store=store, episodic=episodic,
        approvals=approvals, recall=_recall(enforcer, episodic), settings=settings,
        checkpointer=InMemorySaver(),
    )

    assert "budget" in {h["type"] for h in result.guardrail_hits}
    assert len(result.tool_calls) <= 3


def test_max_steps_bounds_the_loop(tmp_path, dept_apps, executor):
    """Two max_steps means the loop cannot exceed two agent iterations, whatever
    the model tries to keep doing."""
    settings = _settings(GuardrailSettings(max_steps=2, tool_budget=50, call_timeout_seconds=30.0, run_timeout_seconds=120.0))
    enforcer = PolicyEnforcer()
    audit = AuditLog(tmp_path / "audit.jsonl")
    trace = TraceStore(tmp_path / "trace.sqlite")
    store = SqliteBaseStore(tmp_path / "m.sqlite")
    episodic = EpisodicStore(tmp_path / "m.sqlite")
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    provider = MockLLM({"attack": {"text": "attack", "tool_calls": [{"name": "crm_search_customer", "args": {"q": "acme"}}]}})

    result = run_agent(
        ALICE, "attack", thread_id="t-steps", provider=provider, executor=executor,
        trace=trace, enforcer=enforcer, audit=audit, store=store, episodic=episodic,
        approvals=approvals, recall=_recall(enforcer, episodic), settings=settings,
        checkpointer=InMemorySaver(),
    )

    assert "max_steps" in {h["type"] for h in result.guardrail_hits}
    assert len(result.tool_calls) <= 2


def test_run_timeout_ends_the_loop(tmp_path, dept_apps, executor):
    """A run whose wall clock budget is exhausted ends before executing tools."""
    settings = _settings(GuardrailSettings(max_steps=50, tool_budget=50, call_timeout_seconds=30.0, run_timeout_seconds=0.001))
    enforcer = PolicyEnforcer()
    audit = AuditLog(tmp_path / "audit.jsonl")
    trace = TraceStore(tmp_path / "trace.sqlite")
    store = SqliteBaseStore(tmp_path / "m.sqlite")
    episodic = EpisodicStore(tmp_path / "m.sqlite")
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    provider = MockLLM({"attack": {"text": "attack", "tool_calls": [{"name": "crm_search_customer", "args": {"q": "acme"}}]}})

    result = run_agent(
        ALICE, "attack", thread_id="t-timeout", provider=provider, executor=executor,
        trace=trace, enforcer=enforcer, audit=audit, store=store, episodic=episodic,
        approvals=approvals, recall=_recall(enforcer, episodic), settings=settings,
        checkpointer=InMemorySaver(),
    )

    assert any(h["type"] == "timeout" for h in result.guardrail_hits)
    assert len(result.tool_calls) == 0
    assert "timeout" in result.reply


def test_denied_tool_never_runs_after_terminal_guardrail(tmp_path, dept_apps, executor):
    """No tool executes past the terminal guardrail — an unbounded script keeps
    asking for a tool every step, but budget=2 means exactly two succeed and the
    third is never attempted."""
    settings = _settings(GuardrailSettings(max_steps=50, tool_budget=2, call_timeout_seconds=30.0, run_timeout_seconds=120.0))
    enforcer = PolicyEnforcer()
    audit = AuditLog(tmp_path / "audit.jsonl")
    trace = TraceStore(tmp_path / "trace.sqlite")
    store = SqliteBaseStore(tmp_path / "m.sqlite")
    episodic = EpisodicStore(tmp_path / "m.sqlite")
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    provider = MockLLM({"attack": {"text": "attack", "tool_calls": [{"name": "crm_search_customer", "args": {"q": "acme"}}]}})

    result = run_agent(
        ALICE, "attack", thread_id="t-terminal", provider=provider, executor=executor,
        trace=trace, enforcer=enforcer, audit=audit, store=store, episodic=episodic,
        approvals=approvals, recall=_recall(enforcer, episodic), settings=settings,
        checkpointer=InMemorySaver(),
    )

    assert "budget" in {h["type"] for h in result.guardrail_hits}
    assert len(result.tool_calls) == 2
    assert result.reply.startswith("Loop guardrail hit")
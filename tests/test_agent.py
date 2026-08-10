"""Agent loop: tool runs, scope-gate denial, deny-storm, budget, max-steps, timeouts, tracing."""

import time

import httpx
from conftest import make_runner, serve_request

from app.agent.runner import resume_agent, run_agent
from app.agent.tools import ToolExecutor
from app.auth.users import USERS
from app.config import get_settings
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore


def claims_for(username: str) -> dict:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


ALICE = claims_for("alice@acme.com")
BOB = claims_for("bob@acme.com")
CAROL = claims_for("carol@acme.com")


def test_single_tool_run(runner_factory):
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]},
        {"text": "Found Acme customers.", "tool_calls": []},
    ])
    result = runner_factory(provider)(ALICE, "find customer acme")

    assert result.reply == "Found Acme customers."
    assert result.guardrail_hits == []
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["outcome"] == "allowed"
    assert result.tool_calls[0]["name"] == "crm_search_customer"


def test_multi_step_run(runner_factory):
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]},
        {"text": "fetching", "tool_calls": [{"name": "crm_get_customer", "args": {"id": 1}}]},
        {"text": "Here is customer 1.", "tool_calls": []},
    ])
    result = runner_factory(provider)(ALICE, "look up a customer")

    assert result.reply == "Here is customer 1."
    assert [c["name"] for c in result.tool_calls] == ["crm_search_customer", "crm_get_customer"]
    assert all(c["outcome"] == "allowed" for c in result.tool_calls)


def test_scope_gate_denies_cross_dept_tool(runner_factory):
    provider = MockLLM(sequence=[
        {"text": "deleting", "tool_calls": [{"name": "hr_delete_employee", "args": {"id": 1}}]},
        {"text": "done", "tool_calls": []},
    ])
    result = runner_factory(provider)(ALICE, "delete employee 1")

    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["outcome"] == "denied"
    assert result.tool_calls[0]["name"] == "hr_delete_employee"


def test_deny_storm_is_bounded(runner_factory):
    provider = MockLLM({"delete the employee": {"text": "deleting", "tool_calls": [
        {"name": "hr_delete_employee", "args": {"id": 1}},
    ]}})
    result = runner_factory(provider)(BOB, "delete the employee")

    assert len(result.tool_calls) == 8  # tool budget (default)
    assert all(c["outcome"] == "denied" for c in result.tool_calls)
    hit_types = {h["type"] for h in result.guardrail_hits}
    assert "budget" in hit_types
    assert "deny_retry" in hit_types
    assert "max_steps" not in hit_types
    assert "guardrail hit" in result.reply


def test_deny_storm_never_reaches_service(dept_apps, runner_factory):
    from fastapi.testclient import TestClient

    provider = MockLLM({"delete the employee": {"text": "deleting", "tool_calls": [
        {"name": "hr_delete_employee", "args": {"id": 1}},
    ]}})
    result = runner_factory(provider)(BOB, "delete the employee")
    assert all(c["outcome"] == "denied" for c in result.tool_calls)

    client = TestClient(dept_apps[8003])
    assert client.get("/employees/1").json()["id"] == 1


def test_tool_budget_forced_final(runner_factory, settings_with_budget):
    provider = MockLLM({"keep searching": {"text": "searching", "tool_calls": [
        {"name": "crm_search_customer", "args": {"query": "x"}},
    ]}})
    result = runner_factory(provider, settings=settings_with_budget)(ALICE, "keep searching")

    assert len(result.tool_calls) == 3
    assert "budget" in {h["type"] for h in result.guardrail_hits}
    assert "guardrail hit" in result.reply


def test_max_steps_respected(runner_factory, settings_with_max_steps):
    provider = MockLLM({"keep going": {"text": "going", "tool_calls": [
        {"name": "crm_search_customer", "args": {"query": "x"}},
    ]}})
    result = runner_factory(provider, settings=settings_with_max_steps)(ALICE, "keep going")

    assert "max_steps" in {h["type"] for h in result.guardrail_hits}
    assert "guardrail hit" in result.reply


def test_run_timeout_terminates(dept_apps, trace_store, settings_with_timeout, enforcer, audit_log):
    app = dept_apps[8002]

    def slow_handler(request):
        time.sleep(0.15)
        return serve_request(app, request)

    client = httpx.Client(transport=httpx.MockTransport(slow_handler))
    slow_executor = ToolExecutor(client=client, settings=settings_with_timeout)
    provider = MockLLM({"search": {"text": "s", "tool_calls": [
        {"name": "crm_search_customer", "args": {"query": "x"}},
    ]}})
    run = make_runner(
        provider=provider,
        executor=slow_executor,
        trace=trace_store,
        settings=settings_with_timeout,
        enforcer=enforcer,
        audit=audit_log,
    )
    result = run(ALICE, "search")

    assert "timeout" in {h["type"] for h in result.guardrail_hits}
    assert "guardrail hit" in result.reply


def test_trace_written_and_integrity_holds(runner_factory, trace_store):
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]},
        {"text": "done.", "tool_calls": []},
    ])
    result = runner_factory(provider)(ALICE, "find customer acme")

    tree = trace_store.get_run_tree(result.run_id)
    types = [e["type"] for e in tree]
    assert types[0] == TraceEventType.RUN_START.value
    assert types[-1] == TraceEventType.RUN_END.value
    assert TraceEventType.NODE.value in types
    assert TraceEventType.TOOL_CALL.value in types
    assert any(
        e["payload"].get("tool") == "crm_search_customer" and e["payload"].get("outcome") == "allowed"
        for e in tree
    )
    ok, issues = trace_store.verify_integrity(result.run_id)
    assert ok, issues


def _hitl_components(tmp_path, deps, checkpointer):
    """Build shared stores + a runner/resumer pair for HITL tests (offline)."""
    db = tmp_path / "memory.sqlite"
    store = SqliteBaseStore(db)
    episodic = EpisodicStore(db)
    approvals = ApprovalStore(db)
    trace = TraceStore(tmp_path / "trace.sqlite")

    def _recall():
        return RecallService(episodic, deps["enforcer"], get_settings().memory)

    def runner(claims, message, thread_id="t-hitl"):
        return run_agent(
            claims, message, thread_id=thread_id, provider=deps["provider"], executor=deps["executor"],
            trace=trace, enforcer=deps["enforcer"], audit=deps["audit"], store=store, episodic=episodic,
            approvals=approvals, recall=_recall(), checkpointer=checkpointer,
        )

    def resumer(approval_id):
        return resume_agent(
            approval_id, provider=deps["provider"], executor=deps["executor"], trace=trace,
            enforcer=deps["enforcer"], audit=deps["audit"], store=store, episodic=episodic,
            approvals=approvals, recall=_recall(), checkpointer=checkpointer,
        )

    return runner, resumer, approvals, store, trace


def test_finance_transfer_suspends_and_approve_executes(tmp_path, dept_apps, executor, enforcer, audit_log):
    """Critical tool (finance_transfer) suspends for HITL; admin approval resumes + executes."""
    from langgraph.checkpoint.memory import InMemorySaver

    provider = MockLLM(sequence=[
        {"text": "transferring", "tool_calls": [{"name": "finance_transfer", "args": {"to_account": "12345", "amount": 500, "memo": "vendor invoice"}}]},
        {"text": "Transfer complete.", "tool_calls": []},
    ])
    runner, resumer, approvals, _, _ = _hitl_components(
        tmp_path, {"provider": provider, "executor": executor, "enforcer": enforcer, "audit": audit_log},
        InMemorySaver(),
    )

    first = runner(CAROL, "move 500 to 12345")
    assert first.interrupted is True and first.approval_id is not None
    assert approvals.get(first.approval_id)["status"] == "pending"
    assert len(approvals.pending_items()) == 1

    approvals.decide(first.approval_id, True, "admin@acme.com")
    resumed = resumer(first.approval_id)
    assert resumed.reply == "Transfer complete."
    assert resumed.tool_calls[-1]["name"] == "finance_transfer"
    assert resumed.tool_calls[-1]["outcome"] == "allowed"
    # no stale pending row after resume (dedup on re-run)
    assert approvals.pending_items() == []
    assert approvals.get(first.approval_id)["status"] == "approved"


def test_finance_transfer_reject_denies_run(tmp_path, dept_apps, executor, enforcer, audit_log):
    """Rejection resumes the run but the critical tool never executes."""
    from langgraph.checkpoint.memory import InMemorySaver

    provider = MockLLM(sequence=[
        {"text": "transferring", "tool_calls": [{"name": "finance_transfer", "args": {"to_account": "12345", "amount": 500, "memo": "vendor invoice"}}]},
        {"text": "The admin rejected the transfer.", "tool_calls": []},
    ])
    runner, resumer, approvals, _, _ = _hitl_components(
        tmp_path, {"provider": provider, "executor": executor, "enforcer": enforcer, "audit": audit_log},
        InMemorySaver(),
    )

    first = runner(CAROL, "move 500 to 12345")
    assert first.interrupted is True and first.approval_id is not None

    approvals.decide(first.approval_id, False, "admin@acme.com")
    resumed = resumer(first.approval_id)
    assert resumed.tool_calls[-1]["outcome"] == "denied"
    assert "rejected by an admin" in resumed.tool_calls[-1]["content"]
    assert approvals.get(first.approval_id)["status"] == "rejected"


def test_rejected_critical_tool_not_retried_in_same_run(tmp_path, dept_apps, executor, enforcer, audit_log):
    """Once a critical tool is rejected, a retry in the same run is denied (no re-suspend)."""
    from langgraph.checkpoint.memory import InMemorySaver

    provider = MockLLM(sequence=[
        {"text": "transferring", "tool_calls": [{"name": "finance_transfer", "args": {"to_account": "1", "amount": 1, "memo": "x"}}]},
        {"text": "still trying", "tool_calls": [{"name": "finance_transfer", "args": {"to_account": "2", "amount": 2, "memo": "y"}}]},
        {"text": "done.", "tool_calls": []},
    ])
    runner, resumer, approvals, _, _ = _hitl_components(
        tmp_path, {"provider": provider, "executor": executor, "enforcer": enforcer, "audit": audit_log},
        InMemorySaver(),
    )

    first = runner(CAROL, "move money")
    approvals.decide(first.approval_id, False, "admin@acme.com")
    resumed = resumer(first.approval_id)

    outcomes = [c["outcome"] for c in resumed.tool_calls]
    contents = [c["content"] for c in resumed.tool_calls]
    assert outcomes[0] == "denied"
    assert "rejected by an admin" in contents[0]
    # the retry is blocked by deny-storm (deny_retry), not re-suspended
    assert outcomes[1] == "denied"
    assert "blocked earlier" in contents[1]
    assert approvals.pending_items() == []  # only one approval ever created


def test_recall_result_masks_ssn_for_non_admin(tmp_path, dept_apps, executor, enforcer, audit_log):
    """Recall output shown to a non-admin has ssn masked before it reaches the LLM."""
    from langgraph.checkpoint.memory import InMemorySaver

    from app.memory.scopes import SCOPE_CENTRAL

    provider = MockLLM(sequence=[
        {"text": "", "tool_calls": [{"name": "recall", "args": {"query": "alice"}}]},
        {"text": "done.", "tool_calls": []},
    ])
    runner, _, _, _, _ = _hitl_components(
        tmp_path, {"provider": provider, "executor": executor, "enforcer": enforcer, "audit": audit_log},
        InMemorySaver(),
    )
    episodic = EpisodicStore(tmp_path / "memory.sqlite")
    episodic.append_transcript(SCOPE_CENTRAL, "r-hitl", {"sub": "alice@acme.com"}, "ssn 111-22-3333 email a@b.com")

    result = runner(ALICE, "recall alice")
    recall_call = next(c for c in result.tool_calls if c["name"] == "recall")
    assert "111-22-3333" not in recall_call["content"]  # ssn masked for alice
    assert "a@b.com" in recall_call["content"]  # email visible to every role

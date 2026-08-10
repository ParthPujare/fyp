"""Agent loop: tool runs, scope-gate denial, deny-storm, budget, max-steps, timeouts, tracing."""

import time

import httpx
from conftest import make_runner, serve_request

from app.agent.tools import ToolExecutor
from app.auth.users import USERS
from app.llm.mock import MockLLM
from app.ops.schema import TraceEventType


def claims_for(username: str) -> dict:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


ALICE = claims_for("alice@acme.com")
BOB = claims_for("bob@acme.com")


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

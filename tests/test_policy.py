"""Casbin RBAC core (Milestone 3, weeks 5-6): policy matrix, filter_tools, intercept, audit."""

from conftest import make_runner

from app.agent.tools import TOOL_REGISTRY
from app.auth.users import USERS
from app.harness.filter import filter_tools
from app.harness.intercept import intercept_tool
from app.llm.mock import MockLLM


def claims_for(username: str) -> dict:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


ALICE = claims_for("alice@acme.com")
BOB = claims_for("bob@acme.com")
CAROL = claims_for("carol@acme.com")
DAVE = claims_for("dave@acme.com")
ADMIN = claims_for("admin@acme.com")

# --- policy matrix (roles x departments x resources) ---


def test_policy_matrix_allowed_for_own_department(enforcer):
    cases = [
        (ALICE, "crm_search_customer", True),
        (ALICE, "crm_create_campaign", True),
        (BOB, "hr_search_employees", True),
        (BOB, "hr_create_leave_request", True),
        (CAROL, "finance_transfer", True),
        (CAROL, "finance_list_ledger", True),
        (DAVE, "devops_create_deployment", True),
        (DAVE, "devops_list_incidents", True),
    ]
    for claims, name, expected in cases:
        assert enforcer.tool_allowed(TOOL_REGISTRY[name], claims) is expected, name


def test_policy_matrix_denies_cross_department(enforcer):
    cases = [
        (ALICE, "hr_delete_employee"),
        (ALICE, "hr_search_employees"),
        (ALICE, "finance_transfer"),
        (ALICE, "devops_create_deployment"),
        (BOB, "crm_search_customer"),
        (BOB, "finance_list_ledger"),
        (CAROL, "crm_get_customer"),
        (DAVE, "hr_get_employee"),
    ]
    for claims, name in cases:
        assert enforcer.tool_allowed(TOOL_REGISTRY[name], claims) is False, name


def test_deny_by_default_inside_own_department(enforcer):
    # hr.delete is deliberately absent from policy.csv -> denied even for bob.
    assert enforcer.tool_allowed(TOOL_REGISTRY["hr_delete_employee"], BOB) is False


def test_unknown_object_or_action_is_denied(enforcer):
    assert enforcer.enforce("alice@acme.com", "marketing", "ghost", "read") is False
    assert enforcer.enforce("alice@acme.com", "marketing", "crm", "promote") is False


def test_unknown_user_is_denied(enforcer):
    assert enforcer.enforce("intruder@evil.com", "marketing", "crm", "read") is False


def test_admin_wildcard_allows_every_tool(enforcer):
    for name, tool in TOOL_REGISTRY.items():
        assert enforcer.tool_allowed(tool, ADMIN) is True, name


# --- pii object rows (sanitizer, Milestone 5) ---


def test_pii_email_readable_by_every_role(enforcer):
    for claims in (ALICE, BOB, CAROL, DAVE, ADMIN):
        assert enforcer.enforce(claims["sub"], claims["dept"], "pii_email", "read") is True, claims["role"]


def test_pii_salary_and_ssn_admin_only(enforcer):
    for obj in ("pii_salary", "pii_ssn"):
        for claims in (ALICE, BOB, CAROL, DAVE):
            assert enforcer.enforce(claims["sub"], claims["dept"], obj, "read") is False, (claims["role"], obj)
        assert enforcer.enforce(ADMIN["sub"], ADMIN["dept"], obj, "read") is True


def test_pii_unknown_field_denied(enforcer):
    for claims in (ALICE, BOB, CAROL, DAVE):
        assert enforcer.enforce(claims["sub"], claims["dept"], "pii_address", "read") is False


# --- filter_tools (per-role schema filtering) ---


def test_filter_tools_visibility_per_role(enforcer):
    all_names = {t.name for t in TOOL_REGISTRY.values()}
    assert {t.name for t in filter_tools(TOOL_REGISTRY.values(), enforcer, ALICE)} == {
        n for n in all_names if n.startswith("crm_") or n == "recall"
    }
    # BOB lacks hr:delete, so hr_delete_employee is filtered out too.
    assert {t.name for t in filter_tools(TOOL_REGISTRY.values(), enforcer, BOB)} == {
        n for n in all_names if n.startswith("hr_") and n != "hr_delete_employee"
    } | {"recall"}
    assert {t.name for t in filter_tools(TOOL_REGISTRY.values(), enforcer, ADMIN)} == all_names


def test_filter_tools_schemas_reach_llm(executor, trace_store, enforcer, audit_log):
    """Only the role's permitted tool schemas enter the LLM context."""
    provider = MockLLM({"hi": {"text": "ok", "tool_calls": []}})
    runner = make_runner(
        provider=provider, executor=executor, trace=trace_store, enforcer=enforcer, audit=audit_log
    )
    runner(ALICE, "hi")
    assert provider.last_tools is not None
    visible = {s["function"]["name"] for s in provider.last_tools}
    assert all(n.startswith("crm_") or n == "recall" for n in visible)
    assert len(visible) == 5  # the four crm tools + recall

    runner(ADMIN, "hi")
    assert len(provider.last_tools) == len(TOOL_REGISTRY)


# --- intercept (runtime enforcement) ---


def test_intercept_deny_blocks_and_audits(enforcer, audit_log):
    result = intercept_tool(
        TOOL_REGISTRY["hr_delete_employee"],
        {"id": 1},
        enforcer=enforcer,
        claims=ALICE,
        audit=audit_log,
        run_id="run-deny",
    )
    assert result is not None and result.denied and result.reason == "rbac"
    assert result.content == "Permission denied: you need scope hr:delete to call hr_delete_employee."

    entries = audit_log.entries()
    assert len(entries) == 1
    assert entries[0]["decision"] == "DENY"
    assert entries[0]["resource"] == {"type": "tool", "id": "hr_delete_employee"}
    assert entries[0]["actor"]["sub"] == "alice@acme.com"
    assert entries[0]["run_id"] == "run-deny"
    assert entries[0]["reason"] == "rbac"


def test_intercept_allow_audits_and_proceeds(enforcer, audit_log):
    result = intercept_tool(
        TOOL_REGISTRY["crm_search_customer"],
        {"query": "acme"},
        enforcer=enforcer,
        claims=ALICE,
        audit=audit_log,
        run_id="run-allow",
    )
    assert result is None
    entries = audit_log.entries()
    assert len(entries) == 1 and entries[0]["decision"] == "ALLOW"
    assert entries[0]["resource"]["id"] == "crm_search_customer"


def test_intercept_audit_entries_match_schema(enforcer, audit_log):
    intercept_tool(
        TOOL_REGISTRY["hr_delete_employee"],
        {"id": 1},
        enforcer=enforcer,
        claims=ALICE,
        audit=audit_log,
        run_id="r",
    )
    entry = audit_log.entries()[0]
    assert {"ts", "event_id", "run_id", "decision", "resource", "actor", "guardrail_hit", "reason"} == set(entry)


# --- agent integration: enforcement happens in the graph loop ---


def test_agent_denial_writes_audit_entry(executor, trace_store, enforcer, audit_log):
    provider = MockLLM(sequence=[
        {"text": "deleting", "tool_calls": [{"name": "hr_delete_employee", "args": {"id": 1}}]},
        {"text": "done", "tool_calls": []},
    ])
    runner = make_runner(
        provider=provider, executor=executor, trace=trace_store, enforcer=enforcer, audit=audit_log
    )
    result = runner(ALICE, "delete employee 1")

    assert result.tool_calls[0]["outcome"] == "denied"
    entries = audit_log.entries()
    assert [e["decision"] for e in entries] == ["DENY"]
    assert entries[0]["resource"]["id"] == "hr_delete_employee"
    assert entries[0]["actor"]["sub"] == "alice@acme.com"


def test_agent_allow_writes_audit_entry(executor, trace_store, enforcer, audit_log):
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]},
        {"text": "Found them.", "tool_calls": []},
    ])
    runner = make_runner(
        provider=provider, executor=executor, trace=trace_store, enforcer=enforcer, audit=audit_log
    )
    result = runner(ALICE, "find customer acme")

    assert result.tool_calls[0]["outcome"] == "allowed"
    entries = audit_log.entries()
    assert [e["decision"] for e in entries] == ["ALLOW"]
    assert entries[0]["resource"]["id"] == "crm_search_customer"


# --- audit determinism ---


def test_audit_is_deterministic_for_same_input(executor, trace_store, enforcer, audit_log):
    def one_run():
        provider = MockLLM(sequence=[
            {"text": "deleting", "tool_calls": [{"name": "hr_delete_employee", "args": {"id": 1}}]},
            {"text": "done", "tool_calls": []},
        ])
        return make_runner(
            provider=provider, executor=executor, trace=trace_store, enforcer=enforcer, audit=audit_log
        )(ALICE, "delete employee 1")

    one_run()
    one_run()

    entries = audit_log.entries()
    assert len(entries) == 2
    # identical decisions, ignoring ts/event_id/run_id
    for e in entries:
        assert e["decision"] == "DENY"
        assert e["reason"] == "rbac"
        assert e["resource"] == {"type": "tool", "id": "hr_delete_employee"}
        assert e["actor"] == {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}

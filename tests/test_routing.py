"""Approval router (Mk15) — the pure routing table and the allowlist keys, no database."""

from app.agent.routing import (
    KIND_MEMORY,
    KIND_TOOL,
    ROUTE_NONE,
    action_key_for,
    allowlist_payload,
    args_hash,
    canonical_args,
    entry_allowlist_eligible,
    initial_level,
    is_allowlist_eligible,
    risk_of_tool,
    route_for,
)
from app.memory.approvals import ROUTE_ADMIN_ONLY, ROUTE_HEAD_ADMIN, ROUTE_HEAD_ONLY

MEMBER = {"actor_role": "marketing_agent", "actor_dept": "marketing"}
HEAD = {"actor_role": "marketing_head", "actor_dept": "marketing"}
ADMIN = {"actor_role": "admin", "actor_dept": "admin"}


def mem(scope, **kw):
    return route_for(kind=KIND_MEMORY, scope=scope, **kw)


def tool(risk, **kw):
    return route_for(kind=KIND_TOOL, risk=risk, **kw)


# --- memory ---


def test_user_scope_is_never_gated():
    assert mem("user/alice@acme.com", **MEMBER) == ROUTE_NONE


def test_central_is_admin_only_even_with_an_allowlist_hit():
    assert mem("central", **MEMBER) == ROUTE_ADMIN_ONLY
    assert mem("central", **ADMIN) == ROUTE_ADMIN_ONLY
    assert mem("central", allowlist_hit=True, head_count=1, **MEMBER) == ROUTE_ADMIN_ONLY


def test_dept_memory_follows_the_department_mode():
    assert mem("dept/marketing", head_count=1, dept_mode="head-only", **MEMBER) == ROUTE_HEAD_ONLY
    assert mem("dept/marketing", head_count=2, dept_mode="head+admin", **MEMBER) == ROUTE_HEAD_ADMIN


def test_dept_memory_without_an_active_head_escalates_to_admin():
    assert mem("dept/marketing", head_count=0, dept_mode="head-only", **MEMBER) == ROUTE_ADMIN_ONLY
    assert mem("dept/marketing", head_count=0, dept_mode="head+admin", **MEMBER) == ROUTE_ADMIN_ONLY


def test_head_own_dept_write_auto_commits():
    # Mk18 Phase E: a head can no longer self-approve their own dept writes —
    # they route to an admin instead of committing.
    assert mem("dept/marketing", head_count=1, dept_mode="head+admin", **HEAD) == ROUTE_ADMIN_ONLY
    assert mem("dept/marketing", head_count=1, dept_mode="head-only", **HEAD) == ROUTE_ADMIN_ONLY
    # ...but only their own department's
    other = {"actor_role": "hr_head", "actor_dept": "hr"}
    assert mem("dept/marketing", head_count=1, **other) == ROUTE_HEAD_ONLY


def test_allowlist_hit_skips_the_dept_gate():
    assert mem("dept/marketing", head_count=1, dept_mode="head+admin", allowlist_hit=True, **MEMBER) == ROUTE_NONE
    assert mem("dept/marketing", head_count=0, allowlist_hit=True, **MEMBER) == ROUTE_NONE


# --- tools ---


def test_critical_tool_is_always_head_then_admin():
    assert tool("critical", head_count=1, dept_mode="head-only", **MEMBER) == ROUTE_HEAD_ADMIN
    assert tool("critical", head_count=1, dept_mode="head+admin", **MEMBER) == ROUTE_HEAD_ADMIN
    # an allowlist hit can never waive a critical tool
    assert tool("critical", head_count=1, allowlist_hit=True, **MEMBER) == ROUTE_HEAD_ADMIN


def test_critical_tool_admin_only_when_there_is_no_head_step_to_take():
    assert tool("critical", head_count=0, **MEMBER) == ROUTE_ADMIN_ONLY
    assert tool("critical", head_count=1, **HEAD) == ROUTE_ADMIN_ONLY  # a head cannot approve themselves
    assert tool("critical", head_count=1, **ADMIN) == ROUTE_ADMIN_ONLY


def test_non_critical_tools_are_ungated_by_default():
    assert tool("read", head_count=1, **MEMBER) == ROUTE_NONE
    assert tool("write", head_count=1, **MEMBER) == ROUTE_NONE


def test_write_tools_follow_the_department_mode_when_gated():
    kw = {"gate_write_tools": True, **MEMBER}
    assert tool("write", head_count=1, dept_mode="head-only", **kw) == ROUTE_HEAD_ONLY
    assert tool("write", head_count=1, dept_mode="head+admin", **kw) == ROUTE_HEAD_ADMIN
    assert tool("write", head_count=0, **kw) == ROUTE_ADMIN_ONLY
    assert tool("write", head_count=1, allowlist_hit=True, **kw) == ROUTE_NONE
    assert tool("read", head_count=1, **kw) == ROUTE_NONE
    assert tool("write", head_count=1, gate_write_tools=True, **HEAD) == ROUTE_NONE
    assert tool("write", head_count=1, gate_write_tools=True, **ADMIN) == ROUTE_NONE


def test_initial_level():
    assert initial_level(ROUTE_HEAD_ONLY) == "head"
    assert initial_level(ROUTE_HEAD_ADMIN) == "head"
    assert initial_level(ROUTE_ADMIN_ONLY) == "admin"


# --- allowlist keys ---


def test_action_keys():
    assert action_key_for(KIND_MEMORY, scope="dept/marketing", op="add") == "memory:dept/marketing:add"
    assert action_key_for(KIND_TOOL, tool="crm_create_campaign") == "tool:crm_create_campaign"


def test_args_hash_is_canonical_and_exact():
    a = {"op": "add", "text": "Our team prefers morning standups.", "match": None}
    b = {"match": None, "text": "Our team prefers morning standups.", "op": "add"}  # key order
    assert canonical_args(a) == canonical_args(b)
    assert args_hash(a) == args_hash(b)
    assert args_hash(a) != args_hash({**a, "text": "Our team prefers evening standups."})
    assert len(args_hash(a)) == 64


def test_tool_payload_drops_volatile_keys_only():
    base = {"tool": "crm_create_campaign", "args": {"name": "Q4", "budget": 10}}
    noisy = {"tool": "crm_create_campaign", "args": {"name": "Q4", "budget": 10, "request_id": "abc", "ts": 1}}
    assert args_hash(allowlist_payload(KIND_TOOL, base)) == args_hash(allowlist_payload(KIND_TOOL, noisy))
    tampered = {"tool": "crm_create_campaign", "args": {"name": "Q4", "budget": 11}}
    assert args_hash(allowlist_payload(KIND_TOOL, base)) != args_hash(allowlist_payload(KIND_TOOL, tampered))


def test_eligibility():
    assert is_allowlist_eligible(KIND_MEMORY, scope="dept/marketing") is True
    assert is_allowlist_eligible(KIND_MEMORY, scope="central") is False
    assert is_allowlist_eligible(KIND_MEMORY, scope="user/a@b.com") is False
    assert is_allowlist_eligible(KIND_TOOL, risk="write") is True
    assert is_allowlist_eligible(KIND_TOOL, risk="critical") is False


def test_risk_of_tool_and_entry_eligibility():
    assert risk_of_tool("finance_transfer") == "critical"
    assert risk_of_tool("crm_create_campaign") == "write"
    assert risk_of_tool("gmail_send") == "critical"
    assert risk_of_tool("no_such_tool") == "critical"  # unknown: never always-allowable

    def entry(rtype, rid, **kw):
        return {"resource": {"type": rtype, "id": rid}, "action": "a", "args_hash": "h", **kw}

    assert entry_allowlist_eligible(entry("memory", "dept/marketing")) is True
    assert entry_allowlist_eligible(entry("memory", "central")) is False
    assert entry_allowlist_eligible(entry("tool", "crm_create_campaign", dept="marketing")) is True
    assert entry_allowlist_eligible(entry("tool", "crm_create_campaign")) is False  # no department
    assert entry_allowlist_eligible(entry("tool", "finance_transfer", dept="finance")) is False
    # rows that predate Mk15 carry no action/hash
    assert entry_allowlist_eligible({"resource": {"type": "memory", "id": "dept/marketing"}}) is False

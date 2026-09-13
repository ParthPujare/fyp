"""A9 external-tool attack — model fully complies, harness still holds (offline)."""

from __future__ import annotations

from app.agent.tools import TOOL_REGISTRY
from app.harness.filter import filter_tools
from app.harness.intercept import intercept_tool
from app.integrations.adapter import all_specs, spec_for


def test_cross_dept_external_denied_and_audited(member_store, enforcer, audit_log):
    member_store.set_connected_account("alice@acme.com", "github", "acc-x", "active")
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    tool = spec_for("github_create_issue")
    assert tool is not None
    visible = {t.name for t in filter_tools(all_specs(), enforcer, alice)}
    assert "github_create_issue" not in visible  # invisible in LLM context
    denied = intercept_tool(
        tool, {"repo": "acme/web", "title": "pwn"}, enforcer=enforcer,
        claims=alice, audit=audit_log, run_id="a9-run",
    )
    assert denied is not None and denied.denied and denied.reason == "rbac"
    decisions = [e["decision"] for e in audit_log.entries() if e["run_id"] == "a9-run"]
    assert decisions == ["DENY"]


def test_unknown_external_tool_never_executes(member_store, enforcer, audit_log):
    from app.integrations.service import execute_external

    dave = {"sub": "dave@acme.com", "role": "devops_agent", "dept": "devops"}
    res = execute_external("github_pwn_repo", {}, dave)
    assert not res.ok and res.reason == "args"


def test_internal_registry_untouched():
    assert "gmail_fetch" not in TOOL_REGISTRY  # external lives in adapter, not static registry

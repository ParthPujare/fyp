"""Milestone 6 (Week 11): skills registry, skill RBAC, skill-as-tool execution, hot reload."""

import shutil
from pathlib import Path

import pytest

from app.agent.runner import run_agent
from app.config import Settings, get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import POLICY_DIR, PolicyEnforcer
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore
from app.skills.executor import resolve_args
from app.skills.model import SkillValidationError, parse_skill
from app.skills.registry import SkillRegistry
from tests.conftest import make_runner
from tests.test_agent import ALICE, BOB, CAROL


def _temp_skills(tmp_path: Path, name: str, content: str) -> Path:
    """Write a single usable skill into a fresh registry rooted at tmp_path/skills."""
    root = tmp_path / "skills"
    skill_dir = root / "dept" / "marketing" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "skill.md").write_text(content, encoding="utf-8")
    return root


def _enforcer_with_skill_rows(tmp_path: Path, rows: list[list[str]]) -> PolicyEnforcer:
    """PolicyEnforcer over a copy of the real policy.csv plus extra skill rows."""
    policy = POLICY_DIR / "policy.csv"
    tmp_policy = tmp_path / "policy.csv"
    with open(policy, encoding="utf-8") as fh:
        text = fh.read()
    for row in rows:
        text += ",".join(["p"] + row) + "\n"
    tmp_policy.write_text(text, encoding="utf-8")
    return PolicyEnforcer(POLICY_DIR / "model.conf", tmp_policy)


SKILL_TEMPLATE = """---
name: {name}
trigger: "run the {name} procedure"
scope: dept/marketing
owner_role: marketing_agent
version: 1
args_schema:
  q:
    type: string
    description: "query"
steps:
  - query the q
tool_calls:
  - tool: crm_search_customer
    args:
      query: "{{q}}"
---

Test body.
"""


# --- registry / parsing ---


def test_registry_loads_seeded_skills():
    registry = SkillRegistry()
    names = {s.name for s in registry.load_all()}
    assert {"launch_campaign", "new_leave_request", "triage_incident"} <= names
    launch = registry.get("launch_campaign")
    assert launch is not None
    assert launch.scope == "dept/marketing"
    assert launch.owner_role == "marketing_agent"
    assert [tc["tool"] for tc in launch.tool_calls] == [
        "crm_search_customer",
        "crm_create_campaign",
    ]
    assert launch.policy_obj == "skill_launch_campaign"


def test_parse_skill_rejects_malformed(tmp_path):
    def file_for(name, content):
        return _temp_skills(tmp_path, name, content) / "dept" / "marketing" / name / "skill.md"

    with pytest.raises(SkillValidationError) as exc:
        parse_skill(file_for("no_front", "plain text, no frontmatter"))
    assert exc.value.code == "no_frontmatter"

    with pytest.raises(SkillValidationError):
        parse_skill(file_for("no_tools", "---\nname: no_tools\ntrigger: t\nscope: dept/x\n---\n"))

    with pytest.raises(SkillValidationError):
        parse_skill(
            file_for(
                "bad_calls",
                "---\nname: bad_calls\ntrigger: t\nscope: dept/x\ntool_calls:\n  - args: {}\n---\n",
            )
        )


def test_resolve_args_templates():
    plan = {"name": "{{name}}", "n": 42, "nested": {"b": "{{x}}"}}
    assert resolve_args(plan, {"name": "Spring", "x": "y"}) == {
        "name": "Spring",
        "n": 42,
        "nested": {"b": "y"},
    }


# --- RPAC ---


def test_skill_allowed_matrix(enforcer):
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    bob = {"sub": "bob@acme.com", "role": "hr_agent", "dept": "hr"}
    assert enforcer.skill_allowed("launch_campaign", alice)
    assert not enforcer.skill_allowed("launch_campaign", bob)  # invisible to HR
    assert not enforcer.skill_allowed("new_leave_request", alice)
    assert enforcer.skill_allowed("new_leave_request", bob)
    # central skill visible to everyone
    assert enforcer.skill_allowed("triage_incident", alice)
    assert enforcer.skill_allowed("triage_incident", bob)


def test_skill_schemas_filtered_per_role(executor, trace_store, enforcer, audit_log):
    """Only role-visible skills enter the LLM's tool schema."""
    provider = MockLLM({"hi": {"text": "ok", "tool_calls": []}})
    runner = make_runner(
        provider=provider, executor=executor, trace=trace_store, enforcer=enforcer, audit=audit_log
    )
    runner(ALICE, "hi")
    alice_visible = {s["function"]["name"] for s in provider.last_tools}
    assert "launch_campaign" in alice_visible
    assert "triage_incident" in alice_visible
    assert "new_leave_request" not in alice_visible  # marketing cannot see HR skill

    runner(BOB, "hi")
    bob_visible = {s["function"]["name"] for s in provider.last_tools}
    assert "new_leave_request" in bob_visible
    assert "launch_campaign" not in bob_visible


# --- skill-as-tool execution ---


def test_skill_executed_as_tool(runner_factory, trace_store, audit_log):
    """Marketing invokes launch_campaign; nested crm tools run in order."""
    provider = MockLLM(
        sequence=[
            {
                "text": "launching",
                "tool_calls": [
                    {
                        "name": "launch_campaign",
                        "args": {"customer": "acme", "campaign_name": "Spring", "budget": 1000, "status": "launched", "region": "US"},
                    }
                ],
            },
            {"text": "done.", "tool_calls": []},
        ]
    )
    runner = runner_factory(provider)
    result = runner(ALICE, "launch the campaign")

    assert result.reply == "done."
    assert [c["name"] for c in result.tool_calls] == ["launch_campaign"]
    assert result.tool_calls[0]["outcome"] == "allowed"

    tree = trace_store.get_run_tree(result.run_id)
    skill_events = [e for e in tree if e["type"] == TraceEventType.SKILL_USE.value]
    assert len(skill_events) == 1
    assert skill_events[0]["payload"]["skill"] == "launch_campaign"
    assert skill_events[0]["payload"]["steps"] == 2
    assert skill_events[0]["payload"]["outcome"] == "allowed"

    # audit: skill ALLOW + each nested tool audited individually
    resources = [(a["resource"]["type"], a["resource"]["id"], a["decision"]) for a in audit_log.entries()]
    assert ("skill", "launch_campaign", "ALLOW") in resources
    assert ("tool", "crm_search_customer", "ALLOW") in resources
    assert ("tool", "crm_create_campaign", "ALLOW") in resources


def test_skill_nested_deny_a4(runner_factory, trace_store, audit_log, dept_apps):
    """Skill visible to Marketing but calling a devops tool is blocked at the nested
    intercept (attack matrix A4)."""
    from fastapi.testclient import TestClient

    provider = MockLLM(
        sequence=[
            {
                "text": "triage",
                "tool_calls": [
                    {
                        "name": "triage_incident",
                        "args": {"service": "api", "version": "1.1", "environment": "prod", "status": "deploying", "deployed_by": "alice"},
                    }
                ],
            },
            {"text": "done.", "tool_calls": []},
        ]
    )
    runner = runner_factory(provider)
    result = runner(ALICE, "triage the incident")

    assert result.tool_calls[-1]["name"] == "triage_incident"
    assert result.tool_calls[-1]["outcome"] == "denied"
    assert "devops:create" in result.tool_calls[-1]["content"]

    tree = trace_store.get_run_tree(result.run_id)
    skill_events = [e for e in tree if e["type"] == TraceEventType.SKILL_USE.value]
    assert skill_events[0]["payload"]["outcome"] == "denied"
    nested_deny = [
        e for e in tree if e["type"] == TraceEventType.TOOL_CALL.value
        and e["payload"].get("tool") == "devops_create_deployment"
    ]
    assert nested_deny and nested_deny[0]["payload"]["outcome"] == "denied"

    resources = [(a["resource"]["type"], a["resource"]["id"], a["decision"]) for a in audit_log.entries()]
    assert ("skill", "triage_incident", "ALLOW") in resources  # skill itself allowed
    assert ("tool", "devops_create_deployment", "DENY") in resources  # nested tool blocked

    # the devops service was never mutated
    client = TestClient(dept_apps[8004])
    assert client.get("/deployments").status_code == 200


def test_skill_nested_critical_suspends_for_hitl(tmp_path, dept_apps, executor, enforcer, audit_log):
    """A skill whose nested tool is risk=critical suspends via interrupt() for HITL."""
    from langgraph.checkpoint.memory import InMemorySaver

    from app.agent.runner import resume_agent

    skill_dir = tmp_path / "skills" / "dept" / "finance" / "pay_bill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "skill.md").write_text(
        """---
name: pay_bill
trigger: "pay a bill"
scope: dept/finance
owner_role: finance_agent
version: 1
args_schema:
  amount:
    type: number
    description: "amount"
  to:
    type: string
    description: "to account"
steps:
  - transfer funds
tool_calls:
  - tool: finance_transfer
    args:
      to_account: "{{to}}"
      amount: "{{amount}}"
      memo: "bill"
      actor: "carol@acme.com"
      status: "pending"
      from_account: "1000"
---
""",
        encoding="utf-8",
    )
    settings = Settings(skills={"skills_dir": str(skill_dir)})
    rows = [["finance_agent", "finance", "skill_pay_bill", "use"]]
    enforcer = _enforcer_with_skill_rows(tmp_path, rows)
    audit_log = AuditLog(tmp_path / "audit.jsonl")
    trace = TraceStore(tmp_path / "trace.sqlite")
    store = SqliteBaseStore(tmp_path / "memory.sqlite")
    episodic = EpisodicStore(tmp_path / "memory.sqlite")
    approvals = ApprovalStore(tmp_path / "memory.sqlite")
    checkpointer = InMemorySaver()

    def _recall():
        return RecallService(episodic, enforcer, get_settings().memory)

    provider = MockLLM(
        sequence=[
            {
                "text": "skill",
                "tool_calls": [
                    {"name": "pay_bill", "args": {"to": "9999", "amount": 250}},
                ],
            },
            {"text": "bill paid", "tool_calls": []},
        ]
    )

    def _runner(claims, message, thread_id="t-paybill"):
        return run_agent(
            claims, message, thread_id=thread_id, provider=provider, executor=executor,
            trace=trace, enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
            approvals=approvals, recall=_recall(), checkpointer=checkpointer, settings=settings,
        )

    first = _runner(CAROL, "pay the bill")
    assert first.interrupted is True and first.approval_id is not None
    assert approvals.get(first.approval_id)["status"] == "pending"

    approvals.decide(first.approval_id, True, "admin@acme.com")
    resumed = resume_agent(
        first.approval_id, provider=provider, executor=executor, trace=trace, enforcer=enforcer,
        audit=audit_log, store=store, episodic=episodic, approvals=approvals, recall=_recall(),
        checkpointer=checkpointer, settings=settings,
    )
    assert resumed.reply == "bill paid"
    assert resumed.tool_calls[-1]["name"] == "pay_bill"
    assert resumed.tool_calls[-1]["outcome"] == "allowed"


def test_skill_loops_bounded_by_budget(runner_factory, settings_with_budget):
    """Repeated skill invocation terminates: nested tool calls consume the loop budget."""
    provider = MockLLM({"keep skill": {"text": "s", "tool_calls": [
        {"name": "launch_campaign", "args": {"customer": "a", "campaign_name": "b", "budget": 1, "status": "s", "region": "r"}},
    ]}})
    result = runner_factory(provider, settings=settings_with_budget)(ALICE, "keep skill")
    assert "budget" in {h["type"] for h in result.guardrail_hits}
    assert "guardrail hit" in result.reply


# --- hot reload (headline deliverable) ---


def test_edit_skill_changes_next_run(tmp_path, dept_apps, executor, enforcer, audit_log):
    """'Edit a skill → behavior changes on next run' — registry re-reads disk per run."""
    shutil.rmtree(tmp_path / "skills", ignore_errors=True)
    root = _temp_skills(tmp_path, "myproc", SKILL_TEMPLATE.format(name="myproc"))
    settings = Settings(skills={"skills_dir": str(root)})
    rows = [["marketing_agent", "marketing", "skill_myproc", "use"]]
    enforcer = _enforcer_with_skill_rows(tmp_path, rows)
    trace = TraceStore(tmp_path / "trace.sqlite")

    def build_runner(provider):
        return make_runner(
            provider=provider,
            executor=executor,
            trace=trace,
            settings=settings,
            enforcer=enforcer,
            audit=audit_log,
        )

    provider1 = MockLLM(
        sequence=[
            {"text": "s", "tool_calls": [{"name": "myproc", "args": {"q": "acme"}}]},
            {"text": "first done", "tool_calls": []},
        ]
    )
    r1 = build_runner(provider1)(ALICE, "run it")
    assert r1.tool_calls[-1]["outcome"] == "allowed"
    assert "crm_search_customer" in r1.tool_calls[-1]["content"]

    # edit the skill on disk: change which tool it calls (now an HR delete tool)
    edited = SKILL_TEMPLATE.replace("crm_search_customer", "hr_delete_employee").format(name="myproc")
    _temp_skills(tmp_path, "myproc", edited)

    provider2 = MockLLM(
        sequence=[
            {"text": "s", "tool_calls": [{"name": "myproc", "args": {"q": "1"}}]},
            {"text": "second done", "tool_calls": []},
        ]
    )
    r2 = build_runner(provider2)(ALICE, "run it")
    assert r2.tool_calls[-1]["outcome"] == "denied"  # nested HR delete now blocked for marketing

    # the skill file really changed on disk between the two runs
    registry = SkillRegistry(root)
    assert registry.get("myproc").tool_calls[0]["tool"] == "hr_delete_employee"


def test_unknown_skill_name_errors(runner_factory):
    provider = MockLLM(
        sequence=[
            {"text": "s", "tool_calls": [{"name": "no_such_skill", "args": {}}]},
            {"text": "done", "tool_calls": []},
        ]
    )
    result = runner_factory(provider)(ALICE, "run no_such_skill")
    assert result.tool_calls[-1]["outcome"] == "error"
    assert "unknown tool" in result.tool_calls[-1]["content"]
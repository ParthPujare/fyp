"""Demo: Milestone 6 — folder/markdown skills registry + skill RBAC (offline).

Usage:
    uv run python scripts/demo_skills.py

Runs the real LangGraph agent in-process (MockLLM, no services, no API keys)
against temp SQLite files and prints, per scenario:
    1. Skill schemas are filtered per role (marketing sees `launch_campaign`,
       HR sees `new_leave_request`, both see the central `triage_incident`).
    2. alice invokes the `launch_campaign` skill as a tool -> the two nested
       crm calls execute through the normal controlled path; SKILL_USE traced.
    3. Attack A4 — carol (finance) invokes the central `triage_incident` skill
       (ALLOWed) but its nested `devops_create_deployment` is DENIED for her
       role, so the skill outcome is denial. admin invoking the same skill works
       because the nested tool is allowed for admin.
    4. Hot reload (the Week-11 deliverable) — editing skill.md on disk changes
       the very next run's nested tool call.
"""

from __future__ import annotations

import asyncio
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.agent.tools import ToolExecutor
from app.auth.users import USERS
from app.config import get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.llm.mock import MockLLM
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from app.services import crm, devops, finance, hr

B = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def _serve(app, request: httpx.Request) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        resp = await transport.handle_async_request(request)
        await resp.aread()
        return httpx.Response(
            status_code=resp.status_code,
            headers=resp.headers,
            content=resp.content,
            request=request,
        )

    return asyncio.run(run())


def claims_for(username: str) -> dict:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


def header(title: str) -> None:
    print(f"\n{B}{'=' * 70}\n{title}\n{'=' * 70}{RESET}")


def show(result, label: str) -> None:
    status = "SUSPENDED" if result.interrupted else "completed"
    print(f"\n{DIM}{label}{RESET}")
    print(f"  run        : {B}{result.run_id[:8]}{RESET} thread={result.thread_id} • {status}")
    for tc in result.tool_calls:
        print(f"  tool       : {B}{tc['name']}{RESET} -> {tc['outcome']}")
    print(f"  reply      : {result.reply}")


def main() -> None:
    settings = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="fyp_skills_demo_"))
    print(f"{DIM}temp dir: {workdir}{RESET}")

    store = SqliteBaseStore(workdir / "memory.sqlite")
    episodic = EpisodicStore(workdir / "memory.sqlite")
    trace = TraceStore(workdir / "trace.sqlite")
    enforcer = PolicyEnforcer()
    audit = AuditLog(workdir / "audit.jsonl")
    recall = RecallService(episodic, enforcer, settings.memory)
    checkpointer = InMemorySaver()

    apps_by_port = {
        8002: crm.create_app(workdir / "crm.sqlite"),
        8003: hr.create_app(workdir / "hr.sqlite"),
        8004: devops.create_app(workdir / "devops.sqlite"),
        8005: finance.create_app(workdir / "finance.sqlite"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _serve(apps_by_port[request.url.port], request)

    executor = ToolExecutor(client=httpx.Client(transport=httpx.MockTransport(handler)))

    def run(claims, message, thread_id, sequence=None, provider=None, settings_override=None):
        if provider is None:
            provider = MockLLM(sequence=sequence) if sequence is not None else MockLLM()
        return run_agent(
            claims, message, thread_id=thread_id,
            provider=provider, executor=executor,
            trace=trace, enforcer=enforcer, audit=audit,
            store=store, episodic=episodic, recall=recall,
            checkpointer=checkpointer, settings=settings_override or settings,
        )

    # 1. Skill schemas are filtered per role (visible_skills in runner).
    header("1. Skill visibility — tool schemas the LLM sees per role")
    empty = [{"text": "no tools yet", "tool_calls": []}]
    alice_provider = MockLLM(sequence=empty)
    run(claims_for("alice@acme.com"), "hi", thread_id="alice-schemas", provider=alice_provider)
    bob_provider = MockLLM(sequence=empty)
    run(claims_for("bob@acme.com"), "hi", thread_id="bob-schemas", provider=bob_provider)
    carol_provider = MockLLM(sequence=empty)
    run(claims_for("carol@acme.com"), "hi", thread_id="carol-schemas", provider=carol_provider)
    def skill_names(p):
        names = {(t.get("function") or {}).get("name") for t in (p.last_tools or [])}
        return {n for n in names if n in {"launch_campaign", "new_leave_request", "triage_incident"}}
    a, b, c = skill_names(alice_provider), skill_names(bob_provider), skill_names(carol_provider)
    print(f"  alice (marketing): {sorted(a)}")
    print(f"  bob   (hr)       : {sorted(b)}")
    print(f"  carol (finance)  : {sorted(c)}")
    assert "launch_campaign" in a and "new_leave_request" not in a
    assert "new_leave_request" in b and "launch_campaign" not in b
    assert "triage_incident" in a and "triage_incident" in c
    print(f"  filtered by role : {GREEN}OK{RESET}")

    # 2. alice runs the launch_campaign skill (skill invoked like a tool).
    header("2. alice invokes launch_campaign -> nested crm calls execute")
    launch_seq = [
        {"text": "", "tool_calls": [{"name": "launch_campaign", "args": {
            "customer": "Acme", "campaign_name": "Q3 Blast", "budget": 5000,
            "status": "launched", "region": "NA"}}]},
        {"text": "Done", "tool_calls": []},
    ]
    alice_res = run(claims_for("alice@acme.com"), "launch the Q3 Blast campaign", thread_id="alice-launch", sequence=launch_seq)
    show(alice_res, "alice (marketing): 'launch the Q3 Blast campaign'")
    skill_traces = [e for e in trace.get_run_tree(alice_res.run_id) if e["type"] == "skill_use"]
    assert len(skill_traces) == 1 and skill_traces[0]["payload"]["steps"] == 2
    assert skill_traces[0]["payload"]["outcome"] == "allowed"
    print(f"  SKILL_USE trace  : steps={skill_traces[0]['payload']['steps']} outcome={GREEN}allowed{RESET}")

    # 3. Attack A4 — carol may invoke triage_incident but the nested devops tool is denied.
    header("3. A4 — carol's triage_incident skill is ALLOWed but nested devops call is DENIED")
    triage_seq = [
        {"text": "", "tool_calls": [{"name": "triage_incident", "args": {
            "service": "api", "version": "v2", "environment": "prod",
            "status": "deployed", "deployed_by": "carol@acme.com"}}]},
        {"text": "Done", "tool_calls": []},
    ]
    carol_res = run(claims_for("carol@acme.com"), "triage the api incident", thread_id="carol-triage", sequence=triage_seq)
    show(carol_res, "carol (finance): 'triage the api incident'")
    carol_trace = next(e for e in trace.get_run_tree(carol_res.run_id) if e["type"] == "skill_use")
    assert carol_trace["payload"]["outcome"] == "denied"
    print(f"  SKILL_USE trace  : outcome={RED}denied{RESET} (nested devops_create_deployment blocked by role)")
    decisions = [e["decision"] for e in audit.entries() if e["run_id"] == carol_res.run_id]
    print(f"  audit decisions  : {decisions}")
    assert "ALLOW" in decisions and "DENY" in decisions

    admin_res = run(claims_for("admin@acme.com"), "triage the api incident", thread_id="admin-triage", sequence=triage_seq)
    show(admin_res, "admin: 'triage the api incident' (nested call allowed)")
    admin_trace = next(e for e in trace.get_run_tree(admin_res.run_id) if e["type"] == "skill_use")
    assert admin_trace["payload"]["outcome"] == "allowed"
    print(f"  SKILL_USE trace  : outcome={GREEN}allowed{RESET} (admin may call the nested devops tool)")

    # 4. Hot reload — edit skill.md on disk, next run behaves differently.
    header("4. Hot reload — 'edit a skill -> behavior changes on next run'")
    skills_dir = settings.skills.skills_dir
    hot = workdir / "skills"
    shutil.copytree(skills_dir, hot)
    s = get_settings()
    s.skills.skills_dir = hot
    before = run(claims_for("alice@acme.com"), "launch the Q3 Blast campaign", thread_id="hot-before", sequence=launch_seq, settings_override=s)

    def nested_tools(run_id):
        return [e["resource"]["id"] for e in audit.entries() if e["run_id"] == run_id and e["decision"] == "ALLOW"]

    before_tools = nested_tools(before.run_id)
    print(f"  nested tools (before edit): {before_tools}")
    old_block = '  - tool: crm_create_campaign\n    args:\n      name: "{{campaign_name}}"\n      budget: "{{budget}}"\n      status: "{{status}}"\n      region: "{{region}}"'
    new_block = '  - tool: crm_search_customer\n    args:\n      query: "{{campaign_name}}"'
    edit = hot / "dept" / "marketing" / "launch_campaign" / "skill.md"
    text = edit.read_text(encoding="utf-8").replace(old_block, new_block)
    edit.write_text(text, encoding="utf-8")
    print(f"  edited {DIM}{edit.relative_to(hot)}{RESET}: crm_create_campaign step -> crm_search_customer")
    after = run(claims_for("alice@acme.com"), "launch the Q3 Blast campaign", thread_id="hot-after", sequence=launch_seq, settings_override=s)
    aft_skill = next(e for e in trace.get_run_tree(after.run_id) if e["type"] == "skill_use")
    print(f"  SKILL_USE trace (after)  : steps={aft_skill['payload']['steps']} outcome={GREEN}{aft_skill['payload']['outcome']}{RESET}")
    after_tools = nested_tools(after.run_id)
    print(f"  nested tools (after edit) : {after_tools}")
    assert "crm_create_campaign" in before_tools
    assert "crm_create_campaign" not in after_tools and "crm_search_customer" in after_tools

    ok, issues = trace.verify_integrity()
    print(f"\n  trace integrity: {GREEN}OK{RESET}" if ok else f"  trace integrity: {RED}FAILED {issues}{RESET}")
    print(f"\n{B}Demo complete. Memory + trace + audit live under {workdir}.{RESET}")


if __name__ == "__main__":
    main()
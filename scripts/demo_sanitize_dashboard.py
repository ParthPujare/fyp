"""Demo: Milestone 5 — PII sanitizer + critical-tool HITL + dashboard (offline).

Usage:
    uv run python scripts/demo_sanitize_dashboard.py

Runs the real LangGraph agent in-process (MockLLM, no services, no API keys)
against temp SQLite files and prints, per scenario:
    1. bob (hr) searches employees -> salary/SSN masked; admin sees them in full.
    2. carol calls finance_transfer (risk=critical) -> suspends for HITL.
    3. admin approves via the same ApprovalStore + resume_agent the dashboard uses
       -> the transfer executes and the audit log records APPROVED.
    4. a second transfer is rejected -> nothing executes, audit records REJECTED.
    5. the recall tool masks PII inside recalled transcripts.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import resume_agent, run_agent
from app.agent.tools import ToolExecutor
from app.auth.users import USERS
from app.config import get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.sanitize import MASK
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
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


def make_executor(workdir: Path) -> ToolExecutor:
    """Offline executor routing :8002-8005 to in-process seeded service apps."""
    apps_by_port = {
        8002: crm.create_app(workdir / "crm.sqlite"),
        8003: hr.create_app(workdir / "hr.sqlite"),
        8004: devops.create_app(workdir / "devops.sqlite"),
        8005: finance.create_app(workdir / "finance.sqlite"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _serve(apps_by_port[request.url.port], request)

    return ToolExecutor(client=httpx.Client(transport=httpx.MockTransport(handler)))

B = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def claims_for(username: str) -> dict:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


def header(title: str) -> None:
    print(f"\n{B}{'=' * 70}\n{title}\n{'=' * 70}{RESET}")


def show(result, label: str) -> None:
    status = "SUSPENDED for approval" if result.interrupted else "completed"
    print(f"\n{DIM}{label}{RESET}")
    print(f"  run        : {B}{result.run_id[:8]}{RESET} thread={result.thread_id} • {status}")
    print(f"  reply      : {B}{result.reply}{RESET}")
    for tc in result.tool_calls:
        print(f"  tool       : {B}{tc['name']}{RESET} -> {tc['outcome']}")
    if result.approval_id:
        print(f"  approval   : {YELLOW}{result.approval_id[:8]}{RESET} (pending in ApprovalStore)")


def main() -> None:
    settings = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="fyp_sanitize_demo_"))
    print(f"{DIM}temp dir: {workdir}{RESET}")

    store = SqliteBaseStore(workdir / "memory.sqlite")
    episodic = EpisodicStore(workdir / "memory.sqlite")
    approvals = ApprovalStore(workdir / "memory.sqlite")
    trace = TraceStore(workdir / "trace.sqlite")
    enforcer = PolicyEnforcer()
    audit = AuditLog(workdir / "audit.jsonl")
    recall = RecallService(episodic, enforcer, settings.memory)
    checkpointer = InMemorySaver()
    executor = make_executor(workdir)

    def run(claims, message, thread_id, sequence=None, script=None):
        if sequence is not None:
            provider = MockLLM(sequence=sequence)
        else:
            provider = MockLLM(script={message: script} if script else None)
        return run_agent(
            claims, message, thread_id=thread_id,
            provider=provider, executor=executor,
            trace=trace, enforcer=enforcer, audit=audit,
            store=store, episodic=episodic, approvals=approvals, recall=recall,
            checkpointer=checkpointer, settings=settings,
        )

    # 1. PII masking on tool responses.
    header("1. PII sanitizer — bob sees masked salary/SSN, admin sees full records")
    search_seq = [
        {"text": "", "tool_calls": [{"name": "hr_search_employees", "args": {"query": "Zhang"}}]},
        {"text": "done", "tool_calls": []},
    ]
    bob_res = run(claims_for("bob@acme.com"), "find Alice Zhang", thread_id="bob-search", sequence=search_seq)
    show(bob_res, "bob (hr): 'find Alice Zhang'")
    bob_hit = next(tc for tc in bob_res.tool_calls if tc["name"] == "hr_search_employees")
    print(f"  bob sees   : {bob_hit.get('content', '')[:180]}...")
    admin_res = run(claims_for("admin@acme.com"), "find Alice Zhang", thread_id="admin-search", sequence=search_seq)
    admin_hit = next(tc for tc in admin_res.tool_calls if tc["name"] == "hr_search_employees")
    print(f"  admin sees : {admin_hit.get('content', '')[:180]}...")
    masked = MASK in (bob_hit.get("content", "") or "")
    print(f"  bob masked : {GREEN}{masked}{RESET} (salary/ssn)")

    # 2. Critical tool suspends for HITL.
    header("2. carol — finance_transfer (risk=critical) -> suspended")
    transfer_seq = [
        {"text": "", "tool_calls": [{
            "name": "finance_transfer",
            "args": {"from_account": "1000", "to_account": "2000", "amount": 5000.0,
                     "memo": "vendor payment", "actor": "carol@acme.com", "status": "pending"},
        }]},
        {"text": "transfer requested", "tool_calls": []},
    ]
    carol_res = run(
        claims_for("carol@acme.com"),
        "transfer 5000 from cash to accounts payable",
        thread_id="carol-transfer",
        sequence=transfer_seq,
    )
    show(carol_res, "carol (finance): 'transfer 5000 ...'")
    assert carol_res.interrupted and carol_res.approval_id

    # 3. Admin approves via the dashboard code path (ApprovalStore + resume_agent).
    header("3. admin — approve the suspended transfer via the dashboard path")
    pending = approvals.pending_items()[0]
    print(f"  approval payload: {pending['payload']}")
    approvals.decide(pending["id"], True, "admin@acme.com")
    resumed = resume_agent(
        pending["id"], provider=MockLLM(), executor=executor,  # fallback echo; loop already answered
        trace=trace, enforcer=enforcer, audit=audit,
        store=store, episodic=episodic, approvals=approvals, recall=recall,
        checkpointer=checkpointer, settings=settings,
    )
    print(f"  resumed reply: {B}{resumed.reply}{RESET}")
    print(f"  audit decisions: {[e['decision'] for e in audit.entries()]}")

    # 4. Reject path.
    header("4. admin — reject a second transfer -> no execution")
    transfer_seq2 = [
        {"text": "", "tool_calls": [{
            "name": "finance_transfer",
            "args": {"from_account": "1100", "to_account": "1000", "amount": 10000.0,
                     "memo": "wire", "actor": "carol@acme.com", "status": "pending"},
        }]},
        {"text": "transfer requested", "tool_calls": []},
    ]
    run(
        claims_for("carol@acme.com"),
        "transfer 10000 back",
        thread_id="carol-transfer-2",
        sequence=transfer_seq2,
    )
    pending2 = approvals.pending_items()[0]
    approvals.decide(pending2["id"], False, "admin@acme.com")
    resumed2 = resume_agent(
        pending2["id"], provider=MockLLM(), executor=executor,
        trace=trace, enforcer=enforcer, audit=audit,
        store=store, episodic=episodic, approvals=approvals, recall=recall,
        checkpointer=checkpointer, settings=settings,
    )
    print(f"  resumed reply: {B}{resumed2.reply}{RESET}")
    print(f"  audit decisions: {[e['decision'] for e in audit.entries()]}")

    # 5. Recall masks PII in transcripts.
    header("5. recall — PII inside recalled transcripts is masked")
    # Seed a dept transcript containing an email + ssn.
    alice_claims = claims_for("alice@acme.com")
    episodic.append_transcript(
        "dept/marketing", "run-x", {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"},
        "user said: contact alice.zhang@acme.com, ssn 111-22-3333",
    )
    rec_seq = [
        {"text": "", "tool_calls": [{"name": "recall", "args": {"query": "alice"}}]},
        {"text": "recalled", "tool_calls": []},
    ]
    rec_res = run(alice_claims, "recall alice", thread_id="recall-pii", sequence=rec_seq)
    rec_hit = next(tc for tc in rec_res.tool_calls if tc["name"] == "recall")
    print(f"  recall content: {rec_hit.get('content', '')[:200]}")
    print(f"  masked        : {GREEN}{MASK in (rec_hit.get('content', '') or '')}{RESET}")

    ok, issues = trace.verify_integrity()
    print(f"\n  trace integrity: {GREEN}OK{RESET}" if ok else f"  trace integrity: {RED}FAILED {issues}{RESET}")
    print(f"\n{B}Demo complete. Memory + trace + audit live under {workdir}.{RESET}")


if __name__ == "__main__":
    main()

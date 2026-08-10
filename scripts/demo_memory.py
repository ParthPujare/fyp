"""Demo: Milestone 4 memory — 3-tier writes, HITL central approvals, recall (offline).

Usage:
    uv run python scripts/demo_memory.py

Runs the actual LangGraph agent in-process (MockLLM, no services, no API keys)
against temp SQLite files, and prints, per scenario:
    1. alice's dept + user facts are extracted, policy-checked, and committed.
    2. alice's central-write attempt is DENIED by Casbin (no interrupt).
    3. admin's central write suspends for HITL; approve + resume commits it.
    4. an approved/rejected pair and the pending/decided approval store rows.
    5. `recall` FTS5 search — only the caller's readable scopes are searched.
    6. a 2-turn thread: snapshot is loaded once and reused (frozen per session).
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import resume_agent, run_agent
from app.auth.users import USERS
from app.config import get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore

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
    if result.approval_id:
        print(f"  approval   : {YELLOW}{result.approval_id[:8]}{RESET} (pending in ApprovalStore)")


def main() -> None:
    settings = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="fyp_memory_demo_"))
    print(f"{DIM}temp dir: {workdir}{RESET}")

    store = SqliteBaseStore(workdir / "memory.sqlite")
    episodic = EpisodicStore(workdir / "memory.sqlite")
    approvals = ApprovalStore(workdir / "memory.sqlite")
    trace = TraceStore(workdir / "trace.sqlite")
    enforcer = PolicyEnforcer()
    audit = AuditLog(workdir / "audit.jsonl")
    recall = RecallService(episodic, enforcer, settings.memory)
    checkpointer = InMemorySaver()

    def run(claims, message, thread_id, script=None, sequence=None):
        if script is not None:
            provider = MockLLM(script={message: script})
        else:
            provider = MockLLM(sequence=sequence)
        return run_agent(
            claims, message, thread_id=thread_id,
            provider=provider,
            trace=trace, enforcer=enforcer, audit=audit,
            store=store, episodic=episodic, approvals=approvals, recall=recall,
            checkpointer=checkpointer, settings=settings,
        )

    # 1. Dept + user facts committed without interruption.
    header("1. alice — dept + user facts written (no HITL needed)")
    show(
        run(
            claims_for("alice@acme.com"),
            "Our team policy requires weekly standups and Alice prefers Slack over Teams.",
            thread_id="alice-1",
            script={"text": "noted.", "tool_calls": []},
        ),
        "alice: 'Our team policy requires weekly standups and Alice prefers Slack over Teams.'",
    )
    for ns, label in [(("dept", "marketing"), "dept/marketing"), (("user",), "user/alice")]:
        facts = [i.value.get("text") for i in store.items_in(ns)]
        print(f"  {DIM}{label}{RESET}: {facts}")

    # 2. Non-admin central write -> Casbin DENY, no interrupt, nothing persisted.
    header("2. alice — central-write attempt -> DENY (no approval row)")
    show(
        run(
            claims_for("alice@acme.com"),
            "Company policy requires quarterly vendor audits.",
            thread_id="alice-2",
            script={"text": "ok", "tool_calls": []},
        ),
        "alice: 'Company policy requires quarterly vendor audits.'",
    )
    central = [i.value.get("text") for i in store.items_in(("central",))]
    print(f"  {RED}central facts: {central} (must stay empty){RESET}")
    print(f"  pending approvals: {len(approvals.pending_items())}")

    # 3. Admin central write -> suspends; approve -> resumes and commits.
    header("3. admin — central write suspends -> approve + resume commits")
    msg = "Company policy requires quarterly vendor audits."
    result = run(claims_for("admin@acme.com"), msg, thread_id="admin-1", script={"text": "ok", "tool_calls": []})
    show(result, "admin: 'Company policy requires quarterly vendor audits.'")
    pending = approvals.pending_items()[0]
    print(f"  approval payload: {pending['payload']}")
    approvals.decide(pending["id"], True, "admin@acme.com")
    resumed = resume_agent(
        pending["id"], provider=MockLLM({msg: {"text": "ok", "tool_calls": []}}),
        trace=trace, enforcer=enforcer, audit=audit,
        store=store, episodic=episodic, approvals=approvals, recall=recall,
        checkpointer=checkpointer, settings=settings,
    )
    print(f"  resumed reply: {B}{resumed.reply}{RESET}")
    print(f"  {GREEN}central facts: {[i.value.get('text') for i in store.items_in(('central',))]}{RESET}")
    print(f"  audit decisions: {[e['decision'] for e in audit.entries()]}")

    # 4. Reject path — a second central write is rejected and not committed.
    header("4. admin — central write REJECTED -> nothing committed")
    msg2 = "Company policy mandates data retention for 5 years."
    result = run(claims_for("admin@acme.com"), msg2, thread_id="admin-2", script={"text": "ok", "tool_calls": []})
    pending = approvals.pending_items()[0]
    approvals.decide(pending["id"], False, "admin@acme.com")
    resume_agent(
        pending["id"], provider=MockLLM({msg2: {"text": "ok", "tool_calls": []}}),
        trace=trace, enforcer=enforcer, audit=audit,
        store=store, episodic=episodic, approvals=approvals, recall=recall,
        checkpointer=checkpointer, settings=settings,
    )
    print(f"  {RED}central facts: {[i.value.get('text') for i in store.items_in(('central',))]}{RESET} (no retention rule)")

    # 5. recall tool — FTS5 over episodic transcripts, scope-gated.
    header("5. recall — FTS5 episodic search (scope-gated)")
    res = run(
        claims_for("alice@acme.com"),
        "recall what I said about standups",
        thread_id="alice-recall",
        sequence=[
            {"text": "", "tool_calls": [{"name": "recall", "args": {"query": "standups"}}]},
            {"text": "Here is what I found.", "tool_calls": []},
        ],
    )
    print(f"  reply       : {B}{res.reply}{RESET}")
    print(f"  recall calls: {res.tool_calls}")
    print(f"  tool outcome: {GREEN}{res.tool_calls[0]['outcome']}{RESET}")
    hits = episodic.search("standups", recall.readable_scopes(claims_for("alice@acme.com")))
    for h in hits:
        print(f"  {DIM}[{h['scope']}]{RESET} {h['transcript']}")

    # 6. Thread reuse — snapshot loaded once per thread (frozen for the session).
    header("6. thread — 2 turns, snapshot loaded once and reused")
    run(claims_for("alice@acme.com"), "remember we use Salesforce", thread_id="t-multi", script={"text": "ok", "tool_calls": []})
    r2 = run(claims_for("alice@acme.com"), "what did I just tell you", thread_id="t-multi", script={"text": "ok", "tool_calls": []})
    tree = trace.get_run_tree(r2.run_id)
    loads = [e for e in tree if e["type"] == "memory_load"]
    print(f"  turn-2 memory_load events: {len(loads)} (0 = snapshot reused)")
    ok, issues = trace.verify_integrity()
    print(f"  trace integrity: {GREEN}OK{RESET}" if ok else f"  trace integrity: {RED}FAILED {issues}{RESET}")

    print(f"\n{B}Demo complete. Memory + trace + audit live under {workdir}.{RESET}")


if __name__ == "__main__":
    main()

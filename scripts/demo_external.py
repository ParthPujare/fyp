"""Demo: Mk9 — real tool-calling harness via connected apps (offline, FakeComposio).

Usage:
    uv run python scripts/demo_external.py

Shows: allowed gmail_fetch, cross-dept github_create DENY, gmail_send HITL suspend.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.agent.tools import ToolExecutor
from app.auth import users as auth_users
from app.auth.store import MemberStore
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
DIM = "\033[2m"
RESET = "\033[0m"


def main() -> None:
    settings = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="fyp_external_demo_"))
    store = MemberStore(workdir / "members.sqlite", audit=AuditLog(workdir / "audit.jsonl"))
    auth_users.set_store(store)
    for user, app in (("alice@acme.com", "gmail"), ("dave@acme.com", "github")):
        store.set_connected_account(user, app, f"acc-{app}", "active")

    mem = SqliteBaseStore(workdir / "memory.sqlite")
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

    import asyncio

    def serve(app, request):
        async def run():
            transport = httpx.ASGITransport(app=app)
            resp = await transport.handle_async_request(request)
            await resp.aread()
            return httpx.Response(
                status_code=resp.status_code, headers=resp.headers,
                content=resp.content, request=request,
            )

        return asyncio.run(run())

    executor = ToolExecutor(
        client=httpx.Client(
            transport=httpx.MockTransport(
                lambda req: serve(apps_by_port[req.url.port], req)
            )
        )
    )

    def claims(u):
        m = store.get(u)
        assert m is not None
        return m.claims()

    def run(u, msg, tid, seq):
        return run_agent(
            claims(u), msg, thread_id=tid, provider=MockLLM(sequence=seq),
            executor=executor, trace=trace, enforcer=enforcer, audit=audit,
            store=mem, episodic=episodic, recall=recall,
            checkpointer=checkpointer, settings=settings,
        )

    print(f"{B}1. alice gmail_fetch (allowed, sanitized){RESET}")
    r1 = run("alice@acme.com", "check standup mail", "ext-1", [
        {"text": "", "tool_calls": [{"name": "gmail_fetch", "args": {"query": "standup"}}]},
        {"text": "done", "tool_calls": []},
    ])
    print(f"  tool: gmail_fetch -> {r1.tool_calls[0]['outcome']}")
    assert r1.tool_calls[0]["outcome"] == "allowed"

    print(f"{B}2. alice github_create_issue (cross-dept DENY){RESET}")
    r2 = run("alice@acme.com", "file issue", "ext-2", [
        {"text": "", "tool_calls": [{"name": "github_create_issue",
         "args": {"repo": "acme/web", "title": "x"}}]},
        {"text": "done", "tool_calls": []},
    ])
    print(f"  tool: github_create_issue -> {RED}{r2.tool_calls[0]['outcome']}{RESET}")
    assert r2.tool_calls[0]["outcome"] == "denied"

    print(f"{B}3. alice gmail_send (critical -> HITL suspend){RESET}")
    r3 = run("alice@acme.com", "send campaign mail", "ext-3", [
        {"text": "", "tool_calls": [{"name": "gmail_send",
         "args": {"to": "bob@acme.com", "subject": "Hi", "body": "Q3 blast"}}]},
        {"text": "done", "tool_calls": []},
    ])
    print(f"  interrupted={r3.interrupted} approval={r3.approval_id}")
    assert r3.interrupted and r3.approval_id

    ok, issues = trace.verify_integrity()
    print(f"  trace integrity: {GREEN}OK{RESET}" if ok else f"  FAILED {issues}")
    print(f"{DIM}workdir: {workdir}{RESET}")


if __name__ == "__main__":
    main()

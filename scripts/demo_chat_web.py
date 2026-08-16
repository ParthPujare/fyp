"""Demo: Plan Mk2 Phase 3 — web chat + any-user login (offline).

Usage:
    uv run python scripts/demo_chat_web.py

Drives the dashboard's `POST /api/chat` proxy (the seam `get_gateway_sender`
routes to an in-process copy of the gateway `/chat`, MockLLM, temp SQLite — no
network / no API keys). Proves the Phase-3 acceptance end-to-end through the
same HTTP contracts the browser uses:

    1. alice (marketing) chats -> allowed CRM call + cross-dept HR delete DENIED
       (both outcomes visible in the returned envelope).
    2. carol (finance) chats a transfer -> interrupted; switching to the admin
       session, the Approvals list shows it pending with masked args.
    3. Identity gates: disabled member's token -> 403; non-member JWT -> 403;
       a member token on an admin endpoint -> 403.
"""

from __future__ import annotations

import asyncio
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.agent.tools import ToolExecutor
from app.auth import users as auth_users
from app.auth.jwt import create_token
from app.auth.store import MemberStore
from app.auth.users import USERS, User
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_approval_store, get_gateway_sender
from app.gateway.app import app as gateway_app
from app.gateway.app import get_runner as gw_get_runner
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

B = "\033[1m"; GREEN = "\033[92m"; RED = "\033[91m"; DIM = "\033[2m"; RESET = "\033[0m"

settings = get_settings()


def _serve(app, request: httpx.Request) -> httpx.Response:
    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        resp = await transport.handle_async_request(request)
        await resp.aread()
        return httpx.Response(
            status_code=resp.status_code,
            headers=resp.headers,
            content=resp.content,
            request=request,
        )

    return asyncio.run(_run())


def header(title: str) -> None:
    print(f"\n{B}== {title} =={RESET}")


def verdict(ok: bool, what: str) -> None:
    mark = f"{GREEN}ok{RESET}" if ok else f"{RED}FAIL{RESET}"
    print(f"  {what}: {mark}")


def token_for(username: str) -> str:
    u = USERS[username]
    return create_token(u, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)


def main() -> None:
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="fyp_chat_web_demo_"))
    print(f"workdir: {workdir}")

    previous_store = auth_users.get_store()
    member_store = MemberStore(workdir / "members.sqlite", audit=AuditLog(workdir / "members_audit.jsonl"))
    auth_users.set_store(member_store)

    audit = AuditLog(workdir / "audit.jsonl")
    enforcer = PolicyEnforcer()
    store = SqliteBaseStore(workdir / "memory.sqlite")
    episodic = EpisodicStore(workdir / "memory.sqlite")
    approvals = ApprovalStore(workdir / "memory.sqlite")
    trace = TraceStore(workdir / "trace.sqlite")
    checkpointer = InMemorySaver()
    recall = RecallService(episodic, enforcer, settings.memory)

    apps_by_port = {
        8002: crm.create_app(workdir / "crm.sqlite"),
        8003: hr.create_app(workdir / "hr.sqlite"),
        8004: devops.create_app(workdir / "devops.sqlite"),
        8005: finance.create_app(workdir / "finance.sqlite"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _serve(apps_by_port[request.url.port], request)

    executor = ToolExecutor(client=httpx.Client(transport=httpx.MockTransport(handler)))

    scenario: dict = {"sequence": None, "script": None}

    def runner(claims, message, thread_id=None):
        provider = (
            MockLLM(sequence=scenario["sequence"])
            if scenario["sequence"] is not None
            else MockLLM(script=scenario["script"])
        )
        return run_agent(
            claims, message, thread_id=thread_id, provider=provider, executor=executor,
            trace=trace, enforcer=enforcer, audit=audit, store=store, episodic=episodic,
            approvals=approvals, recall=recall, checkpointer=checkpointer,
        )

    gateway_app.dependency_overrides[gw_get_runner] = lambda: runner
    gateway_client = TestClient(gateway_app)

    def sender(token, message, thread_id=None):
        resp = gateway_client.post("/chat", json={"token": token, "message": message, "thread_id": thread_id})
        assert resp.status_code == 200, resp.text
        return resp.json()

    dashboard_app.dependency_overrides[get_gateway_sender] = lambda: sender
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    web = TestClient(dashboard_app)

    def chat(username, message, sequence=None):
        scenario["sequence"] = sequence
        scenario["script"] = None
        return web.post("/api/chat", json={"message": message},
                        headers={"Authorization": "Bearer " + token_for(username)})

    def chips(out):
        return " ".join(
            f"[{c['outcome']}] {c['name']}" for c in out.get("tool_calls", [])
        ) or "(no tool calls)"

    # ---- 1. marketing member chat: allowed + denied visible ----
    header("1. alice (marketing) chats — allowed CRM + cross-dept HR delete DENIED")
    alice_res = chat("alice@acme.com", "find customers then delete employee 1", sequence=[
        {"text": "searching", "tool_calls": [
            {"name": "crm_search_customer", "args": {"query": "acme"}},
            {"name": "hr_delete_employee", "args": {"q": "1"}},
        ]},
        {"text": "Found them.", "tool_calls": []},
    ])
    body = alice_res.json()
    print(f"  reply    : {body['reply']}")
    print(f"  tools    : {chips(body)}")
    verdict(body["tool_calls"][0]["outcome"] == "allowed", "crm_search_customer allowed")
    verdict(body["tool_calls"][1]["outcome"] == "denied", "hr_delete_employee denied")

    # ---- 2. finance transfer interrupts; admin sees masked pending approval ----
    header("2. carol (finance) — critical transfer interrupts; admin Approvals shows masked args")
    carol_res = chat("carol@acme.com", "transfer 12k to 2000", sequence=[{
        "text": "ok", "tool_calls": [{"name": "finance_transfer", "args": {
            "from_account": "1000", "to_account": "2000", "amount": 12000.00,
            "memo": "vendor payment"}}]},
    ])
    carol_body = carol_res.json()
    print(f"  interrupted: {carol_body.get('interrupted')}  approval_id: {carol_body.get('approval_id')}")

    pending = web.get("/api/approvals/pending",
                      headers={"Authorization": "Bearer " + token_for("admin@acme.com")}).json()
    item = next(p for p in pending if p["id"] == carol_body["approval_id"])
    print(f"  pending   : tool={item['payload']['tool']} memo={item['payload']['args'].get('memo')} "
          f"from={item['payload']['args'].get('from_account')} to={item['payload']['args'].get('to_account')}")
    verdict(carol_body["interrupted"] is True, "critical transfer suspended")
    verdict(item["payload"]["args"]["memo"] == MASK, "memo masked in Approvals")

    # ---- 3. identity gates ----
    header("3. identity gates — disabled member 403, non-member 403, member-on-admin 403")
    member_store.create("molly@acme.com", "pw", "marketing_agent", "marketing", actor="admin@acme.com")
    molly_tok = token_for("molly@acme.com")
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    disabled = web.post("/api/chat", json={"message": "hi"},
                        headers={"Authorization": "Bearer " + molly_tok})
    print(f"  disabled molly  /api/chat -> {disabled.status_code}")
    verdict(disabled.status_code == 403, "disabled member's token rejected")

    ghost = User(username="ghost@acme.com", password=None, role="marketing_agent", dept="marketing")
    ghost_tok = create_token(ghost, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)
    unknown = web.post("/api/chat", json={"message": "hi"},
                       headers={"Authorization": "Bearer " + ghost_tok})
    print(f"  unknown member  /api/chat -> {unknown.status_code}")
    verdict(unknown.status_code == 403, "non-member JWT rejected")

    admin_only = web.get("/api/users", headers={"Authorization": "Bearer " + token_for("alice@acme.com")})
    print(f"  member on admin /api/users -> {admin_only.status_code}")
    verdict(admin_only.status_code == 403, "member token denied on admin endpoint")

    gateway_app.dependency_overrides.clear()
    dashboard_app.dependency_overrides.clear()
    auth_users.set_store(previous_store)
    print(f"\nDemo complete. Files under {workdir}")


if __name__ == "__main__":
    main()
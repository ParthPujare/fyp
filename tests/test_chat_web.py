"""Plan Mk2 Phase 3 — web chat + any-user login (plan_mk2 §4 row 3).

The dashboard exposes a single non-admin API, `POST /api/chat`, which verifies the
member's own JWT (Phase-2 active-member gate) and forwards it to the gateway
`/chat` via the `get_gateway_sender` seam (no CORS). Covered here: the proxy
contract, 401/403/502 handling, member-vs-admin API separation, and the two
Phase-3 acceptances — (1) marketing member chats an allowed CRM call + a denied
cross-dept HR delete (both outcomes visible), and (2) carol's `finance_transfer`
interrupts and appears in the admin Approvals list with masked args.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import resume_agent, run_agent
from app.auth.jwt import create_token
from app.auth.users import USERS, User
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_approval_store, get_gateway_sender
from app.gateway.app import app as gateway_app
from app.gateway.app import get_runner as gw_get_runner
from app.harness.sanitize import MASK
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore


def _token(username: str) -> str:
    settings = get_settings()
    return create_token(
        USERS[username], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )


def _member_token(member_store, username: str) -> str:
    settings = get_settings()
    rec = member_store.get(username)
    assert rec is not None
    user = User(username=rec.username, password=None, role=rec.role, dept=rec.dept, scopes=list(rec.scopes))
    return create_token(user, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)


def _fake_sender():
    def sender(token: str, message: str, thread_id: str | None):
        return {
            "reply": "echo: " + message,
            "run_id": "r-web",
            "guardrail_hits": [],
            "tool_calls": [],
            "thread_id": thread_id or "t",
            "interrupted": False,
            "approval_id": None,
        }

    return sender


def _integration_sender(tmp_path, provider, executor, enforcer, audit_log):
    """Route /api/chat through the real in-process gateway (runner wired to the
    same tmp stores the dashboard reads), so approvals land in a store the
    dashboard can list. Returns (overrides-closer, sender, approvals)."""
    db = tmp_path / "memory.sqlite"
    store = SqliteBaseStore(db)
    episodic = EpisodicStore(db)
    approvals = ApprovalStore(db)
    trace = TraceStore(tmp_path / "trace.sqlite")
    checkpointer = InMemorySaver()
    recall = RecallService(episodic, enforcer, get_settings().memory)

    def runner(claims, msg, thread_id=None):
        return run_agent(
            claims, msg, thread_id=thread_id, provider=provider, executor=executor,
            trace=trace, enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
            approvals=approvals, recall=recall, checkpointer=checkpointer,
        )

    def resumer(approval_id):
        return resume_agent(
            approval_id, provider=provider, executor=executor, trace=trace, enforcer=enforcer,
            audit=audit_log, store=store, episodic=episodic, approvals=approvals,
            recall=recall, checkpointer=checkpointer,
        )

    gclient = TestClient(gateway_app)
    gateway_app.dependency_overrides[gw_get_runner] = lambda: runner

    def sender(token: str, message: str, thread_id: str | None):
        resp = gclient.post("/chat", json={"token": token, "message": message, "thread_id": thread_id})
        assert resp.status_code == 200, resp.text
        return resp.json()

    return sender, approvals, gateway_app.dependency_overrides


# ------------------------------------------------------------------- proxy gate


def test_chat_returns_gateway_envelope(tmp_path):
    token = _token("alice@acme.com")
    dashboard_app.dependency_overrides[get_gateway_sender] = _fake_sender
    try:
        client = TestClient(dashboard_app)
        resp = client.post("/api/chat", json={"message": "hi"},
                           headers={"Authorization": "Bearer " + token})
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply"] == "echo: hi"
        assert {"reply", "run_id", "guardrail_hits", "tool_calls", "thread_id", "interrupted", "approval_id"} <= set(body)
    finally:
        dashboard_app.dependency_overrides.clear()


def test_chat_rejects_bad_token():
    client = TestClient(dashboard_app)
    resp = client.post("/api/chat", json={"message": "hi"},
                       headers={"Authorization": "Bearer not-a-jwt"})
    assert resp.status_code == 401


def test_chat_rejects_missing_token():
    client = TestClient(dashboard_app)
    resp = client.post("/api/chat", json={"message": "hi"})
    assert resp.status_code == 401


def test_chat_403_for_disabled_member(member_store):
    member_store.create("molly@acme.com", "pw", "marketing_agent", "marketing", actor="admin@acme.com")
    token = _member_token(member_store, "molly@acme.com")
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    client = TestClient(dashboard_app)
    resp = client.post("/api/chat", json={"message": "hi"},
                       headers={"Authorization": "Bearer " + token})
    assert resp.status_code == 403


def test_chat_403_for_unknown_member_with_valid_jwt(member_store):
    settings = get_settings()
    ghost = User(username="ghost@acme.com", password=None, role="marketing_agent", dept="marketing")
    token = create_token(ghost, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)
    client = TestClient(dashboard_app)
    resp = client.post("/api/chat", json={"message": "hi"},
                       headers={"Authorization": "Bearer " + token})
    assert resp.status_code == 403


def test_chat_502_when_gateway_unreachable(monkeypatch):
    from app.dashboard import app as dashboard

    monkeypatch.setattr(dashboard.settings.services, "gateway_url", "http://127.0.0.1:9")
    token = _token("alice@acme.com")
    client = TestClient(dashboard_app)
    resp = client.post("/api/chat", json={"message": "hi"},
                       headers={"Authorization": "Bearer " + token})
    assert resp.status_code == 502


def test_member_token_rejected_on_admin_endpoint():
    client = TestClient(dashboard_app)
    resp = client.get("/api/users", headers={"Authorization": "Bearer " + _token("alice@acme.com")})
    assert resp.status_code == 403


# ------------------------------------------------------------------- acceptances


def test_marketing_chat_shows_allowed_and_denied_tools(tmp_path, executor, enforcer, audit_log):
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [
            {"name": "crm_search_customer", "args": {"query": "acme"}},
            {"name": "hr_delete_employee", "args": {"employee_id": 1}},
        ]},
        {"text": "Found them.", "tool_calls": []},
    ])
    sender, _approvals, gw_overrides = _integration_sender(
        tmp_path, provider, executor, enforcer, audit_log
    )
    dashboard_app.dependency_overrides[get_gateway_sender] = lambda: sender
    try:
        client = TestClient(dashboard_app)
        resp = client.post("/api/chat", json={"message": "find customers and delete an employee"},
                           headers={"Authorization": "Bearer " + _token("alice@acme.com")})
        assert resp.status_code == 200
        calls = {c["name"]: c["outcome"] for c in resp.json()["tool_calls"]}
        assert calls["crm_search_customer"] == "allowed"
        assert calls["hr_delete_employee"] == "denied"
    finally:
        dashboard_app.dependency_overrides.clear()
        gw_overrides.clear()


def test_finance_transfer_interrupts_and_is_listed_with_masked_args(tmp_path, executor, enforcer, audit_log):
    provider = MockLLM(sequence=[
        {"text": "ok", "tool_calls": [{"name": "finance_transfer", "args": {
            "from_account": "1000", "to_account": "2000", "amount": 12000.00,
            "memo": "vendor payment"}}]},
    ])
    sender, approvals, gw_overrides = _integration_sender(
        tmp_path, provider, executor, enforcer, audit_log
    )
    dashboard_app.dependency_overrides[get_gateway_sender] = lambda: sender
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    try:
        client = TestClient(dashboard_app)
        resp = client.post("/api/chat", json={"message": "transfer 12k"},
                           headers={"Authorization": "Bearer " + _token("carol@acme.com")})
        assert resp.status_code == 200
        body = resp.json()
        assert body["interrupted"] is True
        assert body["approval_id"]

        pending = client.get("/api/approvals/pending",
                             headers={"Authorization": "Bearer " + _token("admin@acme.com")}).json()
        item = next(p for p in pending if p["id"] == body["approval_id"])
        assert item["payload"]["tool"] == "finance_transfer"
        args = item["payload"]["args"]
        assert args["memo"] == MASK
        assert args["from_account"] == MASK
        assert args["to_account"] == MASK
    finally:
        dashboard_app.dependency_overrides.clear()
        gw_overrides.clear()


# ------------------------------------------------------------------- login routing


def test_login_claims_route_admin_vs_member(member_store):
    """The identity the frontend routes on: admin login → admin slot, member
    login → member slot, both with a usable access_token."""
    from app.auth.service import app as auth_app

    client = TestClient(auth_app)
    admin = client.post("/auth/login", json={"username": "admin@acme.com", "password": "admin123"})
    alice = client.post("/auth/login", json={"username": "alice@acme.com", "password": "alice123"})
    assert admin.status_code == 200 and alice.status_code == 200
    assert admin.json()["claims"]["role"] == "admin"
    assert alice.json()["claims"]["role"] == "marketing_agent"
    assert alice.json()["access_token"] and admin.json()["access_token"]
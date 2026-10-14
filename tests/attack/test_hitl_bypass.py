"""Attack A6 — HITL bypass: nothing runs before approval, reject never executes,
non-admin / JWT-forged deciders are refused, and a decided approval cannot be
re-decided.

The model is scripted to attempt a critical `finance_transfer` (risk=critical).
Enforcement is structural: the tool suspends via LangGraph `interrupt()`, the
mutation happens only after an authorised admin decision, and identity always
comes from verified JWT claims (never from request bodies or forged signatures).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import resume_agent, run_agent
from app.auth.jwt import AuthError, verify_token
from app.auth.service import app as auth_app
from app.auth.users import USERS
from app.config import get_settings
from app.gateway.app import app as gateway_app
from app.gateway.app import get_approval_store, get_resumer, get_runner
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from tests.attack.helpers import CAROL, decisions_for, service_headers

TRANSFER = {
    "from_account": "1000", "to_account": "2000", "amount": 5000.00,
    "memo": "attack transfer", "actor": "carol@acme.com", "status": "pending",
}


def _login(username: str) -> str:
    user = USERS[username]
    resp = TestClient(auth_app).post(
        "/auth/login", json={"username": username, "password": user.password}
    )
    assert resp.status_code == 200
    return resp.json()["access_token"]


def _mem(db, enforcer):
    store = SqliteBaseStore(db)
    episodic = EpisodicStore(db)
    approvals = ApprovalStore(db)
    recall = RecallService(episodic, enforcer, get_settings().memory)
    return store, episodic, approvals, recall


def _transfers(finance_app) -> list[dict]:
    return TestClient(finance_app).get("/transfers", headers=service_headers()).json()


def test_pre_approval_nothing_executes(tmp_path, dept_apps, executor, enforcer, audit_log):
    """A critical transfer suspends before any mutation; the finance service is
    untouched while the approval is pending, and only the admin decision creates
    the transfer."""
    db = tmp_path / "m.sqlite"
    store, episodic, approvals, recall = _mem(db, enforcer)
    trace = TraceStore(tmp_path / "trace.sqlite")
    checkpointer = InMemorySaver()
    provider = MockLLM(sequence=[
        {"text": "sending", "tool_calls": [{"name": "finance_transfer", "args": TRANSFER}]},
    ])
    result = run_agent(
        CAROL, "transfer 5000", thread_id="t-pending", provider=provider, executor=executor,
        trace=trace, enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
        approvals=approvals, recall=recall, checkpointer=checkpointer,
    )
    assert result.interrupted and result.approval_id
    assert approvals.get(result.approval_id)["status"] == "pending"

    assert len(_transfers(dept_apps[8005])) == 1, "nothing may execute while pending"
    assert not any(e["decision"] == "APPROVED" for e in decisions_for(audit_log, result.run_id))

    approvals.decide(result.approval_id, True, "admin@acme.com")
    resumed = resume_agent(
        result.approval_id, provider=provider, executor=executor, trace=trace,
        enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
        approvals=approvals, recall=recall, checkpointer=checkpointer,
    )
    call = [c for c in resumed.tool_calls if c["name"] == "finance_transfer"]
    assert call and call[0]["outcome"] == "allowed"
    assert len(_transfers(dept_apps[8005])) == 2


def test_reject_resume_never_executes(tmp_path, dept_apps, executor, enforcer, audit_log):
    """A rejected transfer resumes but never executes; re-deciding is refused."""
    db = tmp_path / "m.sqlite"
    store, episodic, approvals, recall = _mem(db, enforcer)
    trace = TraceStore(tmp_path / "trace.sqlite")
    checkpointer = InMemorySaver()
    provider = MockLLM(sequence=[
        {"text": "sending", "tool_calls": [{"name": "finance_transfer", "args": TRANSFER}]},
    ])
    result = run_agent(
        CAROL, "transfer 5000", thread_id="t-reject", provider=provider, executor=executor,
        trace=trace, enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
        approvals=approvals, recall=recall, checkpointer=checkpointer,
    )
    approvals.decide(result.approval_id, False, "admin@acme.com")

    resumed = resume_agent(
        result.approval_id, provider=provider, executor=executor, trace=trace,
        enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
        approvals=approvals, recall=recall, checkpointer=checkpointer,
    )
    call = [c for c in resumed.tool_calls if c["name"] == "finance_transfer"]
    assert call and call[0]["outcome"] == "denied"
    assert len(_transfers(dept_apps[8005])) == 1
    assert any(e["decision"] == "REJECTED" for e in decisions_for(audit_log, resumed.run_id))

    with pytest.raises(ValueError):
        approvals.decide(result.approval_id, True, "admin@acme.com")


def test_non_admin_cannot_decide_http(tmp_path):
    """A non-admin JWT presenting an admin decision is rejected at the gateway."""
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    aid = approvals.create("run-1", "t1", {"type": "memory", "id": "central"}, {"sub": "a"}, {"op": "add"})

    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_approval_store] = lambda: approvals
    try:
        resp = gateway.post(
            f"/approvals/{aid}/decide",
            json={"approve": True, "token": _login("alice@acme.com")},
        )
        assert resp.status_code == 403
        assert approvals.get(aid)["status"] == "pending"
    finally:
        gateway_app.dependency_overrides.clear()


def test_jwt_role_forge_rejected(tmp_path, enforcer):
    """An attacker-signature admin token (wrong secret) cannot open the HITL gate."""
    settings = get_settings()
    forged = jwt.encode(
        {
            "sub": "alice@acme.com", "role": "admin", "dept": "marketing",
            "scopes": ["*"], "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        "attacker-chosen-secret",
        algorithm="HS256",
    )
    with pytest.raises(AuthError):
        verify_token(forged, settings.jwt.secret, settings.jwt.algorithm)

    approvals = ApprovalStore(tmp_path / "m.sqlite")
    aid = approvals.create("run-1", "t1", {"type": "memory", "id": "central"}, {"sub": "a"}, {"op": "add"})
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_approval_store] = lambda: approvals
    try:
        resp = gateway.post(f"/approvals/{aid}/decide", json={"approve": True, "token": forged})
        assert resp.status_code == 401
        assert approvals.get(aid)["status"] == "pending"
    finally:
        gateway_app.dependency_overrides.clear()


def test_admin_approve_via_gateway_resumes(tmp_path, dept_apps, executor, enforcer, audit_log):
    """End-to-end: a pending critical call (created via /chat) is decided by an
    admin through the gateway and the mutation lands."""
    db = tmp_path / "m.sqlite"
    store, episodic, approvals, recall = _mem(db, enforcer)
    trace = TraceStore(tmp_path / "trace.sqlite")
    checkpointer = InMemorySaver()
    provider = MockLLM({"transfer 5000": {"text": "", "tool_calls": [{"name": "finance_transfer", "args": TRANSFER}]}})

    def runner(claims, msg, thread_id=None):
        return run_agent(
            claims, msg, thread_id=thread_id, provider=provider, executor=executor,
            trace=trace, enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
            approvals=approvals, recall=recall, checkpointer=checkpointer,
        )

    def resumer(aid):
        return resume_agent(
            aid, provider=provider, executor=executor, trace=trace, enforcer=enforcer,
            audit=audit_log, store=store, episodic=episodic, approvals=approvals,
            recall=recall, checkpointer=checkpointer,
        )

    token = _login("admin@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = lambda: runner
    gateway_app.dependency_overrides[get_resumer] = lambda: resumer
    gateway_app.dependency_overrides[get_approval_store] = lambda: approvals
    try:
        chat = gateway.post("/chat", json={"token": token, "message": "transfer 5000", "thread_id": "t-gw"})
        assert chat.status_code == 200
        body = chat.json()
        assert body["interrupted"] is True and body["approval_id"]
        aid = body["approval_id"]

        dec = gateway.post(f"/approvals/{aid}/decide", json={"approve": True, "token": token})
        assert dec.status_code == 200
        assert dec.json()["status"] == "approved"
        assert approvals.get(aid)["decided_by"] == "admin@acme.com"
        assert len(_transfers(dept_apps[8005])) == 2
    finally:
        gateway_app.dependency_overrides.clear()
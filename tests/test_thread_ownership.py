"""Mk19 Phase 1 — thread-ownership IDOR fix: `/chat` is owner-only on both surfaces.

Plan Mk19 acceptance #1: a caller may only open threads matching `user:<sub>` or
`user:<sub>:…` — enforced at the gateway `/chat` AND the dashboard `/api/chat`
proxy, **for admins too** (locked decision: no admin override on chat threads;
the approvals `by-thread` endpoint keeps its deliberate admin bypass).

Regression cover: foreign thread → 403 *before* any runner/sender is invoked,
own thread → 200, history endpoints unchanged, prefix-collision safety of the
shared `is_own_thread` predicate.
"""

from __future__ import annotations

from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.checkpoints import is_own_thread
from app.agent.runner import AgentResult
from app.auth.service import app as auth_app
from app.auth.users import USERS
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import (
    get_approval_store,
    get_dashboard_checkpointer,
    get_gateway_sender,
)
from app.gateway.app import app as gateway_app
from app.gateway.app import get_checkpointer_obj, get_runner
from app.memory.approvals import ApprovalStore


def login_token(username: str) -> str:
    client = TestClient(auth_app)
    user = USERS[username]
    resp = client.post("/auth/login", json={"username": username, "password": user.password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


class RecordingRunner:
    """Fake gateway runner that records every invocation (must stay 0 on 403)."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, claims, message, thread_id=None, key_id=None) -> AgentResult:
        self.calls.append({"sub": claims["sub"], "thread_id": thread_id})
        return AgentResult(
            reply="ok", run_id="r1", guardrail_hits=[], tool_calls=[], thread_id=thread_id
        )


# ---- predicate unit checks (prefix-collision safety) ----


def test_is_own_thread_matches_only_exact_or_colon_prefixed():
    assert is_own_thread("user:alice@acme.com", "alice@acme.com")
    assert is_own_thread("user:alice@acme.com:t-2", "alice@acme.com")
    # a username that is a prefix of another must NOT match the longer one
    assert not is_own_thread("user:alice@acme.com.evil", "alice@acme.com")
    assert not is_own_thread("user:alice@acm", "alice@acme.com")
    assert not is_own_thread("user:bob@acme.com", "alice@acme.com")
    assert not is_own_thread("t-approve", "alice@acme.com")  # legacy free-form ids


# ---- gateway /chat ----


def test_gateway_chat_foreign_thread_forbidden():
    runner = RecordingRunner()
    token = login_token("alice@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = lambda: runner
    try:
        resp = gateway.post(
            "/chat",
            json={"token": token, "message": "hi", "thread_id": "user:bob@acme.com"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "not your thread"
        assert runner.calls == []  # rejected before any run starts
    finally:
        gateway_app.dependency_overrides.clear()


def test_gateway_chat_own_thread_allowed():
    runner = RecordingRunner()
    token = login_token("alice@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = lambda: runner
    try:
        resp = gateway.post(
            "/chat",
            json={"token": token, "message": "hi", "thread_id": "user:alice@acme.com:t-9"},
        )
        assert resp.status_code == 200
        assert runner.calls == [{"sub": "alice@acme.com", "thread_id": "user:alice@acme.com:t-9"}]
    finally:
        gateway_app.dependency_overrides.clear()


def test_gateway_chat_default_thread_still_works():
    runner = RecordingRunner()
    token = login_token("alice@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = lambda: runner
    try:
        resp = gateway.post("/chat", json={"token": token, "message": "hi"})
        assert resp.status_code == 200  # thread_id=None defaults to user:<sub> downstream
    finally:
        gateway_app.dependency_overrides.clear()


def test_gateway_chat_admin_cannot_open_foreign_thread():
    """Owner-only applies to admins too (locked decision, plan Mk19)."""
    runner = RecordingRunner()
    token = login_token("admin@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = lambda: runner
    try:
        resp = gateway.post(
            "/chat",
            json={"token": token, "message": "hi", "thread_id": "user:alice@acme.com"},
        )
        assert resp.status_code == 403
        assert runner.calls == []
        # ...but the admin's own thread is fine
        resp = gateway.post(
            "/chat",
            json={"token": token, "message": "hi", "thread_id": "user:admin@acme.com"},
        )
        assert resp.status_code == 200
    finally:
        gateway_app.dependency_overrides.clear()


# ---- dashboard /api/chat proxy ----


def _dash_client(sender) -> TestClient:
    dashboard_app.dependency_overrides[get_gateway_sender] = lambda: sender
    return TestClient(dashboard_app)


def test_dashboard_chat_foreign_thread_forbidden():
    seen: list[dict] = []

    def sender(token, message, thread_id, key_id=None):
        seen.append({"thread_id": thread_id})
        return {"reply": "ok", "thread_id": thread_id}

    token = login_token("alice@acme.com")
    client = _dash_client(sender)
    try:
        resp = client.post(
            "/api/chat",
            json={"message": "hi", "thread_id": "user:bob@acme.com"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "not your thread"
        assert seen == []  # never forwarded to the gateway
    finally:
        dashboard_app.dependency_overrides.clear()


def test_dashboard_chat_own_thread_allowed():
    def sender(token, message, thread_id, key_id=None):
        return {"reply": "ok", "thread_id": thread_id}

    token = login_token("alice@acme.com")
    client = _dash_client(sender)
    try:
        resp = client.post(
            "/api/chat",
            json={"message": "hi", "thread_id": "user:alice@acme.com:t-1"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 200
        assert resp.json()["thread_id"] == "user:alice@acme.com:t-1"
    finally:
        dashboard_app.dependency_overrides.clear()


def test_dashboard_chat_admin_cannot_open_foreign_thread():
    def sender(token, message, thread_id, key_id=None):
        raise AssertionError("sender must not be called for a 403")

    token = login_token("admin@acme.com")
    client = _dash_client(sender)
    try:
        resp = client.post(
            "/api/chat",
            json={"message": "hi", "thread_id": "user:alice@acme.com"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status_code == 403
    finally:
        dashboard_app.dependency_overrides.clear()


# ---- history / delete / by-thread (behavior unchanged, now via the helper) ----


def test_gateway_history_foreign_thread_still_forbidden():
    token = login_token("alice@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_checkpointer_obj] = lambda: InMemorySaver()
    try:
        resp = gateway.post(
            "/chat/threads/user:bob@acme.com/history", json={"token": token}
        )
        assert resp.status_code == 403
        assert resp.json()["detail"] == "not your thread"
    finally:
        gateway_app.dependency_overrides.clear()


def test_dashboard_history_and_delete_ownership(tmp_path):
    token = login_token("alice@acme.com")
    headers = {"Authorization": f"Bearer {token}"}
    dashboard_app.dependency_overrides[get_dashboard_checkpointer] = lambda: InMemorySaver()
    dashboard_app.dependency_overrides[get_approval_store] = lambda: ApprovalStore(
        tmp_path / "memory.sqlite"
    )
    client = TestClient(dashboard_app)
    try:
        # foreign thread: history 403, delete 403
        assert client.get(
            "/api/chat/threads/user:bob@acme.com/history", headers=headers
        ).status_code == 403
        assert client.delete(
            "/api/chat/threads/user:bob@acme.com", headers=headers
        ).status_code == 403
        # own thread: history 200 (empty), delete 200
        own = "user:alice@acme.com:t-gone"
        hist = client.get(f"/api/chat/threads/{own}/history", headers=headers)
        assert hist.status_code == 200
        assert hist.json()["messages"] == []
        gone = client.delete(f"/api/chat/threads/{own}", headers=headers)
        assert gone.status_code == 200
        assert gone.json()["deleted"] is True
    finally:
        dashboard_app.dependency_overrides.clear()


def test_dashboard_admin_by_thread_bypass_preserved(tmp_path):
    """The approvals `by-thread` endpoint keeps its deliberate admin bypass
    (Mk14 owner poll) — only `/chat`-thread access became owner-only."""
    approvals = ApprovalStore(tmp_path / "memory.sqlite")
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    try:
        client = TestClient(dashboard_app)
        admin = {"Authorization": f"Bearer {login_token('admin@acme.com')}"}
        alice = {"Authorization": f"Bearer {login_token('alice@acme.com')}"}
        assert client.get(
            "/api/approvals/by-thread/user:alice@acme.com", headers=admin
        ).status_code == 200
        assert client.get(
            "/api/approvals/by-thread/user:bob@acme.com", headers=alice
        ).status_code == 403
    finally:
        dashboard_app.dependency_overrides.clear()

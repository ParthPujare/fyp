"""Gateway /chat: JWT verification, response shape, real agent path via dependency override."""

from conftest import make_runner
from fastapi.testclient import TestClient

from app.agent.runner import AgentResult, resume_agent, run_agent
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


def login_token(username: str) -> str:
    client = TestClient(auth_app)
    user = USERS[username]
    resp = client.post("/auth/login", json={"username": username, "password": user.password})
    assert resp.status_code == 200
    return resp.json()["access_token"]


def test_chat_rejects_invalid_token():
    gateway = TestClient(gateway_app)
    resp = gateway.post("/chat", json={"token": "not-a-jwt", "message": "hi"})
    assert resp.status_code == 401


class FakeRunner:
    def __call__(self, claims, message, thread_id=None):
        return AgentResult(
            reply=f"echo: {message}",
            run_id="fake-run",
            guardrail_hits=[],
            tool_calls=[],
            thread_id=thread_id,
        )


def test_chat_returns_reply_shape():
    token = login_token("alice@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = FakeRunner
    try:
        resp = gateway.post("/chat", json={"token": token, "message": "hello there"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply"] == "echo: hello there"
        assert body["run_id"] == "fake-run"
        assert body["guardrail_hits"] == []
        assert body["tool_calls"] == []
    finally:
        gateway_app.dependency_overrides.clear()


def test_chat_runs_real_agent(dept_apps, executor, trace_store, enforcer, audit_log):
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]},
        {"text": "Found them.", "tool_calls": []},
    ])
    runner = make_runner(
        provider=provider,
        executor=executor,
        trace=trace_store,
        enforcer=enforcer,
        audit=audit_log,
    )
    token = login_token("alice@acme.com")

    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = lambda: runner
    try:
        resp = gateway.post("/chat", json={"token": token, "message": "find customer acme"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["reply"] == "Found them."
        assert body["tool_calls"][0]["name"] == "crm_search_customer"
        assert body["tool_calls"][0]["outcome"] == "allowed"
        # the run was traced to the in-process store
        tree = trace_store.get_run_tree(body["run_id"])
        assert tree and tree[0]["type"] == "run_start"
    finally:
        gateway_app.dependency_overrides.clear()


def test_approval_decision_endpoint(tmp_path, dept_apps, executor, enforcer, audit_log):
    """Admin suspends a central write via /chat, then decides it via /approvals/{id}/decide."""
    from langgraph.checkpoint.memory import InMemorySaver

    db = tmp_path / "memory.sqlite"
    store = SqliteBaseStore(db)
    episodic = EpisodicStore(db)
    approvals = ApprovalStore(db)
    trace = TraceStore(tmp_path / "trace.sqlite")
    checkpointer = InMemorySaver()
    message = "Company policy requires quarterly vendor audits."
    provider = MockLLM({message: {"text": "ok", "tool_calls": []}})

    def _recall():
        return RecallService(episodic, enforcer, get_settings().memory)

    def runner(claims, msg, thread_id=None):
        return run_agent(
            claims, msg, thread_id=thread_id, provider=provider, executor=executor,
            trace=trace, enforcer=enforcer, audit=audit_log, store=store, episodic=episodic,
            approvals=approvals, recall=_recall(), checkpointer=checkpointer,
        )

    def resumer(approval_id):
        return resume_agent(
            approval_id, provider=provider, executor=executor, trace=trace, enforcer=enforcer,
            audit=audit_log, store=store, episodic=episodic, approvals=approvals,
            recall=_recall(), checkpointer=checkpointer,
        )

    token = login_token("admin@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = lambda: runner
    gateway_app.dependency_overrides[get_resumer] = lambda: resumer
    gateway_app.dependency_overrides[get_approval_store] = lambda: approvals
    try:
        resp = gateway.post("/chat", json={"token": token, "message": message, "thread_id": "t-approve"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["interrupted"] is True and body["approval_id"] is not None
        aid = body["approval_id"]

        dec = gateway.post(f"/approvals/{aid}/decide", json={"approve": True, "admin": "admin@acme.com"})
        assert dec.status_code == 200
        assert dec.json()["status"] == "approved"
        assert any("vendor audits" in i.value.get("text") for i in store.items_in(("central",)))

        again = gateway.post(f"/approvals/{aid}/decide", json={"approve": False, "admin": "admin@acme.com"})
        assert again.status_code == 409

        missing = gateway.post("/approvals/nope/decide", json={"approve": True, "admin": "admin@acme.com"})
        assert missing.status_code == 404
    finally:
        gateway_app.dependency_overrides.clear()

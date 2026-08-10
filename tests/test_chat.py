"""Gateway /chat: JWT verification, response shape, real agent path via dependency override."""

from conftest import make_runner
from fastapi.testclient import TestClient

from app.agent.runner import AgentResult
from app.auth.service import app as auth_app
from app.auth.users import USERS
from app.gateway.app import app as gateway_app
from app.gateway.app import get_runner
from app.llm.mock import MockLLM


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
    def __call__(self, claims, message):
        return AgentResult(reply=f"echo: {message}", run_id="fake-run", guardrail_hits=[], tool_calls=[])


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


def test_chat_runs_real_agent(dept_apps, executor, trace_store):
    provider = MockLLM(sequence=[
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]},
        {"text": "Found them.", "tool_calls": []},
    ])
    runner = make_runner(provider=provider, executor=executor, trace=trace_store)
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

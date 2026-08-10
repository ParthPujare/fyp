"""Offline end-to-end smoke: mock JWT login -> role-aware /hello, fully offline."""

from fastapi.testclient import TestClient

from app.auth.service import app as auth_app
from app.auth.users import USERS
from app.gateway.app import app as gateway_app
from app.llm.base import LLMResponse
from app.llm.factory import get_provider

ROLES = {
    "alice@acme.com": ("marketing_agent", "marketing"),
    "bob@acme.com": ("hr_agent", "hr"),
    "carol@acme.com": ("finance_agent", "finance"),
    "dave@acme.com": ("devops_agent", "devops"),
    "admin@acme.com": ("admin", "admin"),
}


def test_role_aware_hello_for_every_role():
    auth_client = TestClient(auth_app)
    gateway = TestClient(gateway_app)

    for username, (role, dept) in ROLES.items():
        login = auth_client.post(
            "/auth/login",
            json={"username": username, "password": USERS[username].password},
        )
        assert login.status_code == 200, username
        token = login.json()["access_token"]

        resp = gateway.post("/hello", json={"token": token})
        assert resp.status_code == 200, username
        body = resp.json()
        assert body["claims"]["sub"] == username
        assert body["claims"]["role"] == role
        assert body["claims"]["dept"] == dept
        assert f"You are a {role}" in body["message"]


def test_hello_rejects_invalid_token():
    gateway = TestClient(gateway_app)
    assert gateway.post("/hello", json={"token": "not-a-real-token"}).status_code == 401


def test_mockllm_runs_offline():
    provider = get_provider()
    resp = provider.generate([{"role": "user", "content": "hello"}])
    assert isinstance(resp, LLMResponse)
    assert resp.text

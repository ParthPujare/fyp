"""Dashboard v1 (Milestone 5, weeks 9-10): admin JWT gates, approvals list/decide, users/depts."""


import pytest
from fastapi.testclient import TestClient

from app.agent.runner import AgentResult
from app.auth.jwt import create_token
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_approval_store, get_resumer
from app.memory.approvals import ApprovalStore


def admin_token() -> str:
    settings = get_settings()
    return create_token(
        USERS["admin@acme.com"], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )


def user_token(username: str) -> str:
    settings = get_settings()
    return create_token(
        USERS[username], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )


AUTH = {"Authorization": f"Bearer {admin_token()}"}


@pytest.fixture
def approvals(tmp_path) -> ApprovalStore:
    return ApprovalStore(tmp_path / "memory.sqlite")


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    dashboard_app.dependency_overrides.clear()


def test_index_redirects_to_static():
    resp = TestClient(dashboard_app).get("/", follow_redirects=False)
    assert resp.status_code == 307
    assert resp.headers["location"] == "/static/index.html"


def test_health():
    assert TestClient(dashboard_app).get("/health").json()["status"] == "ok"


def test_approvals_pending_requires_admin(approvals):
    client = TestClient(dashboard_app)
    assert client.get("/api/approvals/pending").status_code == 401
    assert client.get("/api/approvals/pending", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert (
        client.get(
            "/api/approvals/pending",
            headers={"Authorization": f"Bearer {user_token('alice@acme.com')}"},
        ).status_code
        == 403
    )


def test_approvals_pending_lists_masked(approvals):
    approvals.create("r1", "t1", {"type": "memory", "id": "central"}, {"sub": "a", "role": "admin", "dept": "admin"}, {"op": "add", "text": "policy"})
    approvals.create("r2", "t2", {"type": "tool", "id": "finance_transfer"}, {"sub": "carol@acme.com", "role": "finance_agent", "dept": "finance"}, {"tool": "finance_transfer", "args": {"from_account": "1000", "to_account": "2000", "amount": 5000, "memo": "vendor payment"}})

    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    resp = TestClient(dashboard_app).get("/api/approvals/pending", headers=AUTH)
    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 2
    assert items[0]["status"] == "pending"
    # tool args with sensitive fields are masked in the list view
    tool = next(i for i in items if i["resource"]["id"] == "finance_transfer")
    assert tool["payload"]["args"]["memo"] == "***"
    assert tool["payload"]["args"]["amount"] == 5000  # non-sensitive survives


def test_approvals_decide_requires_admin(approvals):
    aid = approvals.create("r1", "t1", {"type": "memory", "id": "central"}, {"sub": "a"}, {"op": "add"})
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    client = TestClient(dashboard_app)
    resp = client.post(f"/api/approvals/{aid}/decide", json={"approve": True})
    assert resp.status_code == 401
    resp = client.post(
        f"/api/approvals/{aid}/decide",
        json={"approve": True},
        headers={"Authorization": f"Bearer {user_token('bob@acme.com')}"},
    )
    assert resp.status_code == 403


def test_approvals_decide_404_and_409(approvals):
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    client = TestClient(dashboard_app)
    assert (
        client.post("/api/approvals/missing/decide", json={"approve": True}, headers=AUTH).status_code
        == 404
    )
    aid = approvals.create("r1", "t1", {"type": "memory", "id": "central"}, {"sub": "a"}, {"op": "add"})
    approvals.decide(aid, True, "admin@acme.com")
    assert (
        client.post(f"/api/approvals/{aid}/decide", json={"approve": True}, headers=AUTH).status_code
        == 409
    )


def test_approvals_decide_resumes_run(approvals):
    """The dashboard decide calls resume_agent via the injected resumer."""
    aid = approvals.create("r1", "t1", {"type": "memory", "id": "central"}, {"sub": "a"}, {"op": "add"})

    def fake_resumer(approval_id):
        assert approval_id == aid
        return AgentResult(reply="resumed!", run_id="r1", guardrail_hits=[], tool_calls=[], thread_id="t1")

    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    dashboard_app.dependency_overrides[get_resumer] = lambda: fake_resumer

    resp = TestClient(dashboard_app).post(
        f"/api/approvals/{aid}/decide", json={"approve": True}, headers=AUTH
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved"
    assert body["thread_id"] == "t1"
    assert body["reply"] == "resumed!"
    assert approvals.get(aid)["decided_by"] == "admin@acme.com"


def test_users_read_only_list():
    resp = TestClient(dashboard_app).get("/api/users", headers=AUTH)
    assert resp.status_code == 200
    names = [u["username"] for u in resp.json()]
    assert "alice@acme.com" in names and "admin@acme.com" in names
    # no writable surface: POSTing is rejected
    assert TestClient(dashboard_app).post("/api/users", json={}, headers=AUTH).status_code == 405


def test_users_requires_admin():
    client = TestClient(dashboard_app)
    assert client.get("/api/users").status_code == 401
    assert (
        client.get("/api/users", headers={"Authorization": f"Bearer {user_token('alice@acme.com')}"}).status_code
        == 403
    )


def test_departments_read_only_list():
    resp = TestClient(dashboard_app).get("/api/departments", headers=AUTH)
    assert resp.status_code == 200
    names = {d["name"] for d in resp.json()}
    assert {"marketing", "hr", "finance", "devops", "admin"} <= names
    marketing = next(d for d in resp.json() if d["name"] == "marketing")
    assert marketing["service"] == "crm"
    assert "crm" in marketing["resources"]
    assert "alice@acme.com" in marketing["members"]

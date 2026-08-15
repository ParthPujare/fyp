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


# --- Skills editor + memory browser API (Week 11) ---


@pytest.fixture
def skills_registry(tmp_path):
    import shutil

    from app.skills.registry import SkillRegistry

    shutil.rmtree(tmp_path / "skills", ignore_errors=True)
    root = tmp_path / "skills"

    def write_skill(name: str, tool: str) -> None:
        skill_dir = root / "dept" / "marketing" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / "skill.md").write_text(
            f"""---
name: {name}
trigger: "test trigger"
scope: dept/marketing
owner_role: marketing_agent
version: 1
args_schema:
  q:
    type: string
    description: "query"
steps:
  - query the q
tool_calls:
  - tool: {tool}
    args:
      query: "{{{{q}}}}"
---

Body.
""",
            encoding="utf-8",
        )

    write_skill("editor_skill", "crm_search_customer")
    write_skill("launch_campaign", "crm_create_campaign")
    return SkillRegistry(root)


@pytest.fixture
def components(tmp_path, skills_registry):
    """Swap the dashboard's shared components for isolated temp ones (restored after)."""
    import app.dashboard.app as dash
    from app.harness.audit import AuditLog
    from app.memory.edit import MemoryEditor
    from app.memory.store import SqliteBaseStore

    store = SqliteBaseStore(tmp_path / "memory.sqlite")
    audit = AuditLog(tmp_path / "audit.jsonl")
    original = dash._components
    dash._components = {
        "enforcer": dash.PolicyEnforcer(),
        "audit": audit,
        "store": store,
        "skills": skills_registry,
        "memory_editor": MemoryEditor(store, dash.settings.memory),
    }
    yield dash, audit
    dash._components = original


def test_skills_memory_require_admin(skills_registry):
    assert TestClient(dashboard_app).get("/api/skills").status_code == 401
    assert TestClient(dashboard_app).get("/api/memory").status_code == 401


def test_skills_editor_roundtrip(tmp_path, components):
    dash, _ = components
    client = TestClient(dashboard_app)

    listing = client.get("/api/skills", headers=AUTH)
    assert listing.status_code == 200
    assert listing.json()["errors"] == []
    skill = next(s for s in listing.json()["skills"] if s["name"] == "editor_skill")
    assert skill["scope"] == "dept/marketing"
    assert skill["tool_calls"][0]["tool"] == "crm_search_customer"
    assert skill["policy_row"] is False  # no Casbin row for a sandbox skill
    seed = next(s for s in listing.json()["skills"] if s["name"] == "launch_campaign")
    assert seed["policy_row"] is True  # its name has a row in the real policy.csv

    content = client.get("/api/skills/editor_skill", headers=AUTH).json()["content"]
    assert "name: editor_skill" in content
    assert "Body." in content

    # PUT an edited skill -> persisted to disk
    edited = content.replace("crm_search_customer", "crm_get_customer")
    resp = client.put("/api/skills/editor_skill", json={"content": edited}, headers=AUTH)
    assert resp.status_code == 200 and resp.json()["saved"]
    refreshed = client.get("/api/skills", headers=AUTH).json()["skills"]
    assert next(s for s in refreshed if s["name"] == "editor_skill")["tool_calls"][0]["tool"] == "crm_get_customer"

    # invalid content -> 422, unchanged
    assert client.put("/api/skills/editor_skill", json={"content": "not markdown"}, headers=AUTH).status_code == 422

    # DELETE removes the skill folder
    assert client.delete("/api/skills/editor_skill", headers=AUTH).status_code == 200
    names = [s["name"] for s in client.get("/api/skills", headers=AUTH).json()["skills"]]
    assert "editor_skill" not in names

    # admin edits were audit-logged with resource type "skill"
    assert any(
        a["resource"]["type"] == "skill" and a["resource"]["id"] == "editor_skill"
        for a in dash._components["audit"].entries()
    )


def test_skills_update_missing_404(components):
    client = TestClient(dashboard_app)
    assert (
        client.put(
            "/api/skills/does_not_exist",
            json={"content": "---\nname: x\n---\n"},
            headers=AUTH,
        ).status_code
        == 404
    )


def test_memory_browser_crud(components):
    _, audit = components
    client = TestClient(dashboard_app)

    before = client.get("/api/memory", headers=AUTH).json()
    assert any(s["scope"] == "central" for s in before)

    added = client.post("/api/memory/dept/marketing", json={"text": "Team Q3 budget: 50k"}, headers=AUTH)
    assert added.status_code == 200 and added.json()["op"] == "add"

    refreshed = client.get("/api/memory", headers=AUTH).json()
    mkt = next(s for s in refreshed if s["scope"] == "dept/marketing")
    item = next(i for i in mkt["items"] if i["text"] == "Team Q3 budget: 50k")
    key = item["key"]

    edited = client.put(
        f"/api/memory/dept/marketing/{key}", json={"text": "Team Q3 budget: 60k"}, headers=AUTH
    )
    assert edited.status_code == 200 and edited.json()["op"] == "replace"
    refreshed = client.get("/api/memory", headers=AUTH).json()
    mkt = next(s for s in refreshed if s["scope"] == "dept/marketing")
    assert next(i for i in mkt["items"] if i["key"] == key)["text"] == "Team Q3 budget: 60k"

    # admin edits are audit-logged
    memory_audits = [a for a in audit.entries() if a["resource"]["type"] == "memory"]
    assert len(memory_audits) >= 2

    # invalid scope rejected
    assert client.post("/api/memory/bogus", json={"text": "x"}, headers=AUTH).status_code == 422
    assert (
        client.delete("/api/memory/dept/marketing/does-not-exist", headers=AUTH).status_code
        == 404
    )

    deleted = client.delete(f"/api/memory/dept/marketing/{key}", headers=AUTH)
    assert deleted.status_code == 200 and deleted.json()["deleted"]
    refreshed = client.get("/api/memory", headers=AUTH).json()
    mkt = next(s for s in refreshed if s["scope"] == "dept/marketing")
    assert key not in {i["key"] for i in mkt["items"]}

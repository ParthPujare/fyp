"""Plan Mk2 Phase 2 — store-driven enforcement.

`g` is materialized from the MemberStore for ACTIVE members only (plan_mk2 §2.1,
§8): an unknown or disabled member has no `g` row, so every action is denied by
default — even with an otherwise-valid JWT. The lazy version guard rebuilds the
Casbin instance on the next enforce call after any member mutation, so a long-
lived enforcer (gateway/dashboard singletons) reflects deactivation and
role/dept edits without a restart.

Covered here: deny-by-default for unknown/disabled members, the lazy version
guard, gateway 403 gates (/hello, /chat), the dashboard admin 403 gate, and the
store write-counter that triggers rebuilds.
"""

from fastapi.testclient import TestClient

from app.agent.tools import TOOL_REGISTRY
from app.auth.jwt import create_token
from app.auth.service import app as auth_app
from app.auth.users import USERS, User, is_active_member
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.gateway.app import app as gateway_app
from app.gateway.app import get_runner


def login_token(username: str) -> str:
    client = TestClient(auth_app)
    user = USERS[username]
    resp = client.post("/auth/login", json={"username": username, "password": user.password})
    assert resp.status_code == 200, resp.text
    return resp.json()["access_token"]


def _molly(store):
    return store.create("molly@acme.com", "pw", "marketing_agent", "marketing", actor="admin@acme.com")


_CRM_TOOL = TOOL_REGISTRY["crm_search_customer"]


class FakeRunner:
    def __call__(self, claims, message, thread_id=None):
        return type(
            "R",
            (),
            {
                "reply": "ok",
                "run_id": "x",
                "guardrail_hits": [],
                "tool_calls": [],
                "thread_id": thread_id,
                "interrupted": False,
                "approval_id": None,
            },
        )()


# --------------------------------------------------------------- enforcer core


def test_unknown_member_denied_by_default(enforcer):
    assert enforcer.enforce("ghost@acme.com", "marketing", "crm", "read") is False


def test_cross_dept_deny_via_g(enforcer):
    assert enforcer.enforce("carol@acme.com", "finance", "finance", "read") is True
    assert enforcer.enforce("carol@acme.com", "marketing", "crm", "read") is False


def test_active_member_granted_then_denied_after_deactivation(enforcer, member_store):
    _molly(member_store)
    claims = {"sub": "molly@acme.com", "role": "marketing_agent", "dept": "marketing"}
    assert enforcer.tool_allowed(_CRM_TOOL, claims) is True
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    assert enforcer.tool_allowed(_CRM_TOOL, claims) is False


def test_role_dept_change_reflected_lazily(enforcer, member_store):
    _molly(member_store)
    assert enforcer.enforce("molly@acme.com", "marketing", "crm", "read") is True
    member_store.update("molly@acme.com", role="hr_agent", dept="hr", actor="admin@acme.com")
    assert enforcer.enforce("molly@acme.com", "marketing", "crm", "read") is False
    assert enforcer.enforce("molly@acme.com", "hr", "hr", "read") is True


def test_reactivation_restores_access(enforcer, member_store):
    _molly(member_store)
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    assert enforcer.enforce("molly@acme.com", "marketing", "crm", "read") is False
    member_store.set_active("molly@acme.com", True, actor="admin@acme.com")
    assert enforcer.enforce("molly@acme.com", "marketing", "crm", "read") is True


def test_skill_and_memory_denied_for_unknown(enforcer):
    claims = {"sub": "ghost", "dept": "marketing"}
    assert enforcer.skill_allowed("launch_campaign", claims) is False
    assert enforcer.memory_allowed("memory/central", "read", claims) is False


# ------------------------------------------------------------- helper + version


def test_is_active_member_helper(member_store):
    _molly(member_store)
    assert is_active_member("alice@acme.com") is True
    assert is_active_member("ghost@acme.com") is False
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    assert is_active_member("molly@acme.com") is False


def test_store_version_increments_on_every_mutation(member_store):
    start = member_store.version
    member_store.create("molly@acme.com", "pw", "marketing_agent", "marketing", actor="admin@acme.com")
    assert member_store.version == start + 1
    member_store.update("molly@acme.com", role="hr_agent", dept="hr", actor="admin@acme.com")
    assert member_store.version == start + 2
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    assert member_store.version == start + 3
    member_store.reset_password("molly@acme.com", "newpw", actor="admin@acme.com")
    assert member_store.version == start + 4


def test_seeds_do_not_bump_version_by_themselves(member_store):
    assert member_store.version == 0


def _token_for(member_store, username: str) -> str:
    rec = member_store.get(username)
    assert rec is not None
    settings = get_settings()
    user = User(username=rec.username, password=None, role=rec.role, dept=rec.dept, scopes=list(rec.scopes))
    return create_token(user, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)


# ---------------------------------------------------------------- gateway 403


def test_gateway_hello_403_after_deactivation(member_store):
    _molly(member_store)
    token = _token_for(member_store, "molly@acme.com")
    gateway = TestClient(gateway_app)
    assert gateway.post("/hello", json={"token": token}).status_code == 200
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    assert gateway.post("/hello", json={"token": token}).status_code == 403


def test_gateway_chat_403_for_disabled_member(member_store):
    _molly(member_store)
    token = _token_for(member_store, "molly@acme.com")
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    gateway = TestClient(gateway_app)
    gateway_app.dependency_overrides[get_runner] = FakeRunner
    try:
        assert gateway.post("/chat", json={"token": token, "message": "hi"}).status_code == 403
    finally:
        gateway_app.dependency_overrides.clear()


def test_gateway_403_for_unknown_member_with_valid_jwt(member_store):
    settings = get_settings()
    ghost = User(username="ghost@acme.com", password=None, role="marketing_agent", dept="marketing")
    token = create_token(ghost, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)
    gateway = TestClient(gateway_app)
    assert gateway.post("/hello", json={"token": token}).status_code == 403


def test_gateway_still_401_for_bad_token():
    assert TestClient(gateway_app).post("/hello", json={"token": "not-a-jwt"}).status_code == 401


# ------------------------------------------------------------- dashboard 403


def test_dashboard_admin_gate_blocks_disabled_admin(member_store, tmp_path):
    settings = get_settings()
    member_store.create("admin2@acme.com", "pw", "admin", "admin", actor="admin@acme.com")
    token = create_token(
        USERS["admin2@acme.com"], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )
    gateway = TestClient(dashboard_app)
    auth = {"Authorization": f"Bearer {token}"}
    assert gateway.get("/api/skills", headers=auth).status_code == 200
    member_store.set_active("admin2@acme.com", False, actor="admin@acme.com")
    assert gateway.get("/api/skills", headers=auth).status_code == 403
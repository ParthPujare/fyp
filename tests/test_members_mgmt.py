"""Plan Mk2 Phase 4 — Members + Departments management UI (plan_mk2 §4 row 4).

Admin CRUD: add / edit (role, dept, scopes) / activate / deactivate / reset
password — audit-logged, lockout guard enforced server-side; and department
registration: the record lands in the store (replacing the hardcoded
`_service_for` map) and a default `p, <role>, <dept>, *, *` row is appended to
policy.csv so the new role is immediately grantable.

The policy append must NOT touch the repo policy.csv: tests point
`settings.services.policy_path` at a temp copy (autouse `member_store` gives us
an isolated, seeded member DB; the enforcer is rebuilt on a temp policy file to
see the appended row).
"""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_token
from app.auth.store import SEED_DEPARTMENTS, DepartmentExistsError
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard.app import _append_policy_row
from app.dashboard.app import app as dashboard_app
from app.harness.enforcer import POLICY_PATH, PolicyEnforcer


def admin_token() -> str:
    settings = get_settings()
    return create_token(
        USERS["admin@acme.com"], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )


AUTH = {"Authorization": f"Bearer {admin_token()}"}


@pytest.fixture
def client():
    return TestClient(dashboard_app)


@pytest.fixture
def tmp_policy(tmp_path):
    """A copy of the real policy.csv that dashboard writes may safely mutate.

    The assert is load-bearing: if the redirect ever fails to take effect, these
    tests append department rows to the REAL repo policy.csv instead. Fail here
    rather than silently mutating tracked policy.
    """
    from app.dashboard.app import _policy_csv

    dst = tmp_path / "policy.csv"
    shutil.copy(POLICY_PATH, dst)
    settings = get_settings()
    original = settings.services.policy_path
    settings.services.policy_path = dst
    assert _policy_csv() == dst, "policy redirect not visible to the dashboard"
    try:
        yield dst
    finally:
        settings.services.policy_path = original


def _molly_token():
    settings = get_settings()
    return create_token(
        USERS["molly@acme.com"], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )


# ------------------------------------------------------------------ store layer


def test_seed_departments_migrated(member_store):
    depts = member_store.list_departments()
    assert {d.name for d in depts} == {d["name"] for d in SEED_DEPARTMENTS}
    marketing = member_store.get_department("marketing")
    assert marketing is not None
    assert marketing.service == "crm"
    assert marketing.service_url == "http://127.0.0.1:8002"
    assert marketing.role == "marketing_agent"
    assert member_store.get_department("ghost") is None


def test_register_department_store_level(member_store):
    start = member_store.version
    dept = member_store.register_department(
        "legal", service="legal", service_url="http://127.0.0.1:8007", resources=["contracts"],
        role="legal_agent", actor="admin@acme.com",
    )
    assert dept.role == "legal_agent"
    assert member_store.get_department("legal").resources == ["contracts"]
    # mutating the store bumps members_version -> enforcer rebuild picks up p rows
    assert member_store.version == start + 1
    with pytest.raises(DepartmentExistsError):
        member_store.register_department("legal", service="legal", service_url="", actor="admin@acme.com")
    # default role follows the <name>_agent convention
    other = member_store.register_department("compliance", service="compliance", service_url="", actor="admin@acme.com")
    assert other.role == "compliance_agent"


def test_register_department_is_audit_logged(member_store):
    audit = member_store._audit_log
    before = len(audit.entries())
    member_store.register_department("legal", service="legal", service_url="", actor="admin@acme.com")
    entries = audit.entries()[before:]
    assert [e["resource"]["type"] for e in entries] == ["department"]
    assert entries[0]["resource"]["id"] == "legal"
    assert entries[0]["actor"]["sub"] == "admin@acme.com"


# ---------------------------------------------------------------- dashboard API


def test_create_member_then_login(client, member_store):
    resp = client.post(
        "/api/members",
        json={"username": "molly@acme.com", "password": "molly123", "role": "marketing_agent",
              "dept": "marketing", "scopes": ["crm:read", "crm:create"]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "molly@acme.com"
    assert body["active"] is True
    assert member_store.verify("molly@acme.com", "molly123") is True
    assert member_store.get("molly@acme.com").password_hash.startswith("scrypt$")


def test_create_member_duplicate_409_and_short_password_422(client):
    assert client.post(
        "/api/members",
        json={"username": "alice@acme.com", "password": "alice123", "role": "marketing_agent", "dept": "marketing"},
        headers=AUTH,
    ).status_code == 409
    assert client.post(
        "/api/members",
        json={"username": "molly@acme.com", "password": "abc", "role": "marketing_agent", "dept": "marketing"},
        headers=AUTH,
    ).status_code == 422


def test_member_required_admin_gate(client):
    assert client.post(
        "/api/members", json={"username": "x", "password": "xxxxxx", "role": "r", "dept": "d"}, headers={}
    ).status_code == 401
    settings = get_settings()
    token = create_token(
        USERS["alice@acme.com"], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )
    resp = client.post(
        "/api/members",
        json={"username": "x", "password": "xxxxxx", "role": "r", "dept": "d"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 403


def test_edit_member_role_dept_scopes(client, member_store):
    member_store.create("molly@acme.com", "molly123", role="marketing_agent", dept="marketing", actor="admin@acme.com")
    resp = client.put(
        "/api/members/molly%40acme.com",
        json={"role": "hr_agent", "dept": "hr", "scopes": ["hr:read"]},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert resp.json()["dept"] == "hr"
    rec = member_store.get("molly@acme.com")
    assert rec.role == "hr_agent" and rec.dept == "hr" and rec.scopes == ["hr:read"]


def test_edit_member_unknown_404(client):
    assert client.put(
        "/api/members/ghost%40acme.com", json={"role": "r", "dept": "d"}, headers=AUTH
    ).status_code == 404


def test_deactivate_then_gateway_403_then_reactivate(client, member_store):
    """Phase-4 acceptance: disabling a member makes her NEXT request 403."""
    client.post(
        "/api/members",
        json={"username": "molly@acme.com", "password": "molly123", "role": "marketing_agent", "dept": "marketing"},
        headers=AUTH,
    )
    token = _molly_token()
    from app.gateway.app import app as gateway_app

    hello = TestClient(gateway_app).post("/hello", json={"token": token})
    assert hello.status_code == 200

    resp = client.post("/api/members/molly%40acme.com/deactivate", headers=AUTH)
    assert resp.status_code == 200
    assert resp.json()["active"] is False
    assert TestClient(gateway_app).post("/hello", json={"token": token}).status_code == 403

    client.post("/api/members/molly%40acme.com/activate", headers=AUTH)
    assert TestClient(gateway_app).post("/hello", json={"token": token}).status_code == 200


def test_lockout_self_and_last_admin_server_side(client, member_store):
    # self-deactivation refused
    resp = client.post("/api/members/admin%40acme.com/deactivate", headers=AUTH)
    assert resp.status_code == 409
    assert member_store.get("admin@acme.com").active is True
    # + creating a second admin lets the original be deactivated by that one
    member_store.create("sec@acme.com", "sec123", role="admin", dept="admin", scopes=["*"], actor="admin@acme.com")
    settings = get_settings()
    sec_token = create_token(
        USERS["sec@acme.com"], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )
    resp = client.post(
        "/api/members/admin%40acme.com/deactivate",
        headers={"Authorization": f"Bearer {sec_token}"},
    )
    assert resp.status_code == 200


def test_reset_password_then_login(client, member_store):
    member_store.create("molly@acme.com", "molly123", role="marketing_agent", dept="marketing", actor="admin@acme.com")
    resp = client.post(
        "/api/members/molly%40acme.com/reset-password", json={"new_password": "newpass99"}, headers=AUTH
    )
    assert resp.status_code == 200
    assert member_store.verify("molly@acme.com", "newpass99") is True
    assert member_store.verify("molly@acme.com", "molly123") is False
    assert client.post(
        "/api/members/molly%40acme.com/reset-password", json={"new_password": "x"}, headers=AUTH
    ).status_code == 422


# ------------------------------------------------------------ department API


def test_register_department_through_api(client, tmp_policy, member_store):
    resp = client.post(
        "/api/departments",
        json={"name": "legal", "service": "legal", "service_url": "http://127.0.0.1:8007",
              "resources": ["contracts", "matters"], "role": "legal_agent"},
        headers=AUTH,
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["name"] == "legal"
    assert body["role"] == "legal_agent"
    # p row present in the (temp) policy file — exactly once, never duplicated
    rows = tmp_policy.read_text(encoding="utf-8").splitlines()
    assert rows.count("p, legal_agent, legal, *, *") == 1
    # department visible in the list with members + resources
    depts = client.get("/api/departments", headers=AUTH).json()
    legal = next(d for d in depts if d["name"] == "legal")
    assert legal["service"] == "legal"
    assert legal["role"] == "legal_agent"
    assert set(legal["resources"]) >= {"contracts", "matters"}


def test_register_department_policy_row_is_grantable(client, tmp_policy, member_store):
    """The appended `*, *` row grants the new role within its own domain."""
    client.post(
        "/api/departments",
        json={"name": "legal", "service": "legal", "service_url": "http://127.0.0.1:8007"},
        headers=AUTH,
    )
    member_store.create(
        "nora@acme.com", "nora123", role="legal_agent", dept="legal", scopes=[], actor="admin@acme.com"
    )
    enforcer = PolicyEnforcer(policy_path=tmp_policy)
    assert enforcer.enforce("nora@acme.com", "legal", "contracts", "read") is True
    # ...but NOT in another domain
    assert enforcer.enforce("nora@acme.com", "hr", "hr", "read") is False


def test_register_department_default_role_and_errors(client, tmp_policy):
    resp = client.post(
        "/api/departments", json={"name": "compliance", "service": "compliance", "service_url": ""}, headers=AUTH
    )
    assert resp.status_code == 200
    assert resp.json()["role"] == "compliance_agent"
    # duplicate -> 409, bad name -> 422
    assert client.post(
        "/api/departments", json={"name": "compliance", "service": "c", "service_url": ""}, headers=AUTH
    ).status_code == 409
    assert client.post(
        "/api/departments", json={"name": "Has Spaces", "service": "c", "service_url": ""}, headers=AUTH
    ).status_code == 422


def test_register_department_appends_new_row_once(client, tmp_policy):
    """A department absent from policy.csv gets its `p` row appended — and a
    repeat append is a no-op, so re-registration can't grow the file forever."""
    def lines() -> list[str]:
        return tmp_policy.read_text(encoding="utf-8").splitlines()

    assert not any("research_agent" in line for line in lines())

    resp = client.post(
        "/api/departments",
        json={"name": "research", "service": "research", "service_url": ""},
        headers=AUTH,
    )
    assert resp.status_code == 200
    assert lines().count("p, research_agent, research, *, *") == 1

    _append_policy_row("research_agent", "research")
    assert lines().count("p, research_agent, research, *, *") == 1


def test_departments_unchanged_for_seeds(client):
    """Seeded departments still read live p-rows for resources (plan_mk2 §3.2)."""
    depts = client.get("/api/departments", headers=AUTH).json()
    marketing = next(d for d in depts if d["name"] == "marketing")
    assert marketing["service"] == "crm"
    assert "crm" in marketing["resources"]
    assert "alice@acme.com" in marketing["members"]
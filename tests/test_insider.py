"""Mk18 Phase E — insider separation-of-duties tests."""

from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient

from app.dashboard.app import app as dashboard_app
from app.harness.enforcer import POLICY_PATH


@pytest.fixture
def _isolated_dashboard(tmp_path):
    """Swap the dashboard's shared components for isolated temp ones (mirrors
    test_dashboard's `components` fixture) + redirect the policy path at a
    temp copy. Nothing in these tests may touch the repo policy.csv, skills/,
    audit log, or memory store."""
    import app.dashboard.app as dash
    from app.config import get_settings
    from app.dashboard.app import _policy_csv
    from app.harness.audit import AuditLog
    from app.memory.edit import MemoryEditor
    from app.memory.store import SqliteBaseStore
    from app.skills.registry import SkillRegistry

    # An earlier module (e.g. test_dev_guard) may have cache_clear()ed the settings
    # singleton; re-bind dash.settings to whatever get_settings() now returns, or
    # the mutation below lands on a stale object _policy_csv() never reads.
    get_settings.cache_clear()
    dash.settings = get_settings()

    dst = tmp_path / "policy.csv"
    shutil.copy(POLICY_PATH, dst)
    original_policy = dash.settings.services.policy_path
    dash.settings.services.policy_path = dst
    assert _policy_csv() == dst

    original_dir = dash.settings.skills.skills_dir
    dash.settings.skills.skills_dir = tmp_path / "skills"
    (tmp_path / "skills").mkdir(exist_ok=True)

    store = SqliteBaseStore(tmp_path / "memory.sqlite")
    audit = AuditLog(tmp_path / "audit.jsonl")
    original = dash._components
    from app.agent.checkpoints import make_checkpointer
    from app.harness.enforcer import PolicyEnforcer
    from app.memory.factory import open_approval_store, open_episodic_store
    from app.memory.recall import RecallService
    from app.ops.factory import open_trace_store

    enforcer = PolicyEnforcer()
    episodic = open_episodic_store(tmp_path / "memory.sqlite")
    dash._components = {
        "trace": open_trace_store(tmp_path / "trace.sqlite"),
        "enforcer": enforcer,
        "audit": audit,
        "store": store,
        "episodic": episodic,
        "approvals": open_approval_store(tmp_path / "memory.sqlite"),
        "checkpointer": make_checkpointer(dash.settings),
        "skills": SkillRegistry(tmp_path / "skills"),
        "memory_editor": MemoryEditor(store, dash.settings.memory),
    }
    dash._components["recall"] = RecallService(episodic, enforcer, dash.settings.memory)
    try:
        yield
    finally:
        dash._components = original
        dash.settings.services.policy_path = original_policy
        dash.settings.skills.skills_dir = original_dir


def _admin_headers(member_store, username="admin@acme.com"):
    from app.auth.jwt import create_token
    from app.config import get_settings

    settings = get_settings()
    member = member_store.get(username)
    return {"Authorization": f"Bearer {create_token(member, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)}"}


def _two_admins(member_store):
    member_store.create("admin2@acme.com", "longpw123", role="admin", dept="admin", actor="admin@acme.com")


def test_second_admin_required_for_skill_save(member_store, _isolated_dashboard):
    _two_admins(member_store)
    c = TestClient(dashboard_app)
    content = (
        "---\nname: se_skill\ntrigger: test it\nscope: central\n"
        "tool_calls:\n  - tool: crm_search_customer\n---\nbody\n"
    )
    r = c.post("/api/skills", json={"content": content}, headers=_admin_headers(member_store))
    assert r.status_code == 202, r.text
    pid = r.json()["proposal_id"]
    # Proposer cannot self-confirm.
    r2 = c.post(f"/api/second-eyes/{pid}/confirm", headers=_admin_headers(member_store))
    assert r2.status_code == 409
    # Second admin confirms → applied.
    r3 = c.post(
        f"/api/second-eyes/{pid}/confirm", headers=_admin_headers(member_store, "admin2@acme.com")
    )
    assert r3.status_code == 200, r3.text
    assert r3.json()["status"] == "approved"
    assert c.get("/api/skills/se_skill", headers=_admin_headers(member_store)).status_code == 200


def test_second_eyes_reject_applies_nothing(member_store, _isolated_dashboard):
    _two_admins(member_store)
    c = TestClient(dashboard_app)
    content = (
        "---\nname: se_nope\ntrigger: nope\nscope: central\n"
        "tool_calls:\n  - tool: crm_search_customer\n---\nbody\n"
    )
    r = c.post("/api/skills", json={"content": content}, headers=_admin_headers(member_store))
    pid = r.json()["proposal_id"]
    r2 = c.post(
        f"/api/second-eyes/{pid}/reject", headers=_admin_headers(member_store, "admin2@acme.com")
    )
    assert r2.status_code == 200
    assert c.get("/api/skills/se_nope", headers=_admin_headers(member_store)).status_code == 404


def test_single_admin_direct_write_with_fallback_audit(member_store, _isolated_dashboard):
    c = TestClient(dashboard_app)
    content = (
        "---\nname: se_solo\ntrigger: solo\nscope: central\n"
        "tool_calls:\n  - tool: crm_search_customer\n---\nbody\n"
    )
    r = c.post("/api/skills", json={"content": content}, headers=_admin_headers(member_store))
    assert r.status_code == 200, r.text
    assert r.json()["saved"] is True


def test_policy_rows_gated_on_dept_register(member_store, _isolated_dashboard):
    _two_admins(member_store)
    c = TestClient(dashboard_app)
    r = c.post(
        "/api/departments",
        json={"name": "legal", "service": "legal", "resources": [], "role": ""},
        headers=_admin_headers(member_store),
    )
    assert r.status_code == 202, r.text
    pid = r.json()["proposal_id"]
    # Role exists but grants nothing until confirmed.
    from app.harness.enforcer import PolicyEnforcer

    assert PolicyEnforcer().enforce("admin@acme.com", "admin", "legal", "read") is False
    r2 = c.post(
        f"/api/second-eyes/{pid}/confirm", headers=_admin_headers(member_store, "admin2@acme.com")
    )
    assert r2.status_code == 200
    assert PolicyEnforcer().enforce("admin@acme.com", "admin", "legal", "read") is True


def test_allowlist_grant_defers_with_two_admins(member_store):
    """A solo ADMIN's always_allow on an eligible item approves the action but does
    NOT create the standing rule while a second admin exists — it is staged as a
    second-eyes proposal instead (Mk20). A head's always_allow is not affected: the
    department's own approver granted it (Mk20 fix: it used to be dropped too)."""
    from app.agent.routing import KIND_TOOL, action_key_for, allowlist_payload, args_hash
    from app.memory.approval_flow import _create_allow_rules

    _two_admins(member_store)
    action = action_key_for(KIND_TOOL, tool="crm_create_campaign")

    def entry_for(name, by, level):
        payload = {"tool": "crm_create_campaign", "args": {"name": name}}
        return payload, {
            "action": action,
            "args_hash": args_hash(allowlist_payload(KIND_TOOL, payload)),
            "dept": "marketing",
            "actor": {"sub": "alice@acme.com"},
            "payload": payload,
            "resource": {"type": "tool", "id": "crm_create_campaign"},
            "steps": [{"by": by, "level": level, "always_allow": True}],
            "run_id": "r1",
        }

    assert member_store.count_active_admins() == 2
    # admin alone -> staged, no rule yet
    _p, solo = entry_for("x", "admin@acme.com", "admin")
    _create_allow_rules(member_store, solo)
    assert member_store.allowlist_match("alice@acme.com", action, solo["args_hash"]) is False
    [proposal] = member_store.second_eyes_pending()
    assert proposal["kind"] == "allowlist_rule" and proposal["proposed_by"] == "admin@acme.com"
    # head -> rule at once, even with two admins
    _p, by_head = entry_for("z", "mhead@acme.com", "head")
    _create_allow_rules(member_store, by_head)
    assert member_store.allowlist_match("alice@acme.com", action, by_head["args_hash"]) is True
    # Sanity: with a single admin the admin-alone shape creates the rule directly.
    member_store.set_active("admin2@acme.com", False, actor="admin@acme.com")
    try:
        _p, single = entry_for("y", "admin@acme.com", "admin")
        _create_allow_rules(member_store, single)
        assert member_store.allowlist_match("alice@acme.com", action, single["args_hash"]) is True
    finally:
        member_store.set_active("admin2@acme.com", True, actor="admin@acme.com")


def test_head_cannot_self_approve_dept_write(member_store):
    """Head's own dept write suspends for an admin (routing-level)."""
    from app.agent.routing import KIND_MEMORY, route_for

    assert (
        route_for(
            kind=KIND_MEMORY, scope="dept/marketing", actor_role="marketing_head",
            actor_dept="marketing", dept_mode="head-only", head_count=1,
        )
        == "admin-only"
    )

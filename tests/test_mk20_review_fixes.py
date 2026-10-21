"""Mk20 — fixes from the 2026-10-18 read-only review.

1. a head's "Always allow" is created even when there are ≥2 admins (was silently dropped);
   an admin-alone "Always allow" is staged as a second-eyes proposal, not lost
2. credential-version reads are cached on Postgres (see test_pg_stores for the pg half)
3. second-admin rule fails closed and also covers an admin's own critical tool call
4. a deleted-chat resume does not consume the single resume claim
5. approvers can read recipient/subject/body preview; secrets stay masked
6. CSRF tokens are bound to the credential version (see test_cookies)
7. SSRF: private addresses only on local-model ports (see test_ssrf_guard)
8. toolkit names are validated
+  identity switcher flips the server-side active cookie slot
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.routing import KIND_MEMORY, action_key_for, allowlist_payload, args_hash
from app.auth.jwt import create_token
from app.auth.store import MemberError
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard.app import _mask_payload
from app.dashboard.app import app as dashboard_app
from app.memory.approval_flow import DecisionError, apply_decision
from app.memory.approvals import ApprovalStore

ALICE = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
ADMIN = {"sub": "admin@acme.com", "role": "admin", "dept": "admin"}
ADMIN2 = {"sub": "admin2@acme.com", "role": "admin", "dept": "admin"}
MHEAD = {"sub": "mhead@acme.com", "role": "marketing_head", "dept": "marketing"}
FACT = {"op": "add", "text": "Our team prefers morning standups.", "match": None}
TRANSFER = {"tool": "finance_transfer", "args": {"amount": 5, "memo": "m"}}


def _token(username: str) -> str:
    s = get_settings()
    return create_token(USERS[username], s.jwt.secret, s.jwt.algorithm, 60)


def _auth(username: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + _token(username)}


@pytest.fixture
def two_admins(member_store):
    member_store.create("admin2@acme.com", "admin234", role="admin", dept="admin")
    member_store.create("mhead@acme.com", "head1234", role="marketing_head", dept="marketing")
    return member_store


def _dept_item(store: ApprovalStore, route="head-only", level="head", run="r1") -> str:
    return store.create(
        run, "t", {"type": "memory", "id": "dept/marketing"}, ALICE, FACT,
        level=level, route=route, dept="marketing",
        action=action_key_for(KIND_MEMORY, scope="dept/marketing", op="add"),
        args_hash=args_hash(allowlist_payload(KIND_MEMORY, FACT)),
    )


# ---------------------------------------------------------------- 1: always-allow


def test_head_always_allow_is_created_with_two_admins(tmp_path, two_admins):
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = _dept_item(store)
    out = apply_decision(store, two_admins, aid, MHEAD, approve=True, as_level="head", always_allow=True)
    assert out["status"] == "approved"
    rules = two_admins.allowlist_list("marketing")
    assert [(r["member"], r["created_by"]) for r in rules] == [("alice@acme.com", "mhead@acme.com")]
    assert two_admins.second_eyes_pending() == []


def test_admin_alone_always_allow_is_staged_then_confirmed_by_another_admin(tmp_path, two_admins):
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = _dept_item(store, route="admin-only", level="admin")  # e.g. escalated: no head step
    out = apply_decision(store, two_admins, aid, ADMIN, approve=True, as_level="admin", always_allow=True)
    assert out["status"] == "approved"  # the action itself is not held back
    assert two_admins.allowlist_list() == []  # the standing rule waits for a second admin
    [proposal] = two_admins.second_eyes_pending()
    assert proposal["kind"] == "allowlist_rule" and proposal["proposed_by"] == "admin@acme.com"
    assert proposal["payload"]["member"] == "alice@acme.com"

    client = TestClient(dashboard_app)
    own = client.post(f"/api/second-eyes/{proposal['id']}/confirm", headers=_auth("admin@acme.com"))
    assert own.status_code == 409 and two_admins.allowlist_list() == []
    ok = client.post(f"/api/second-eyes/{proposal['id']}/confirm", headers=_auth("admin2@acme.com"))
    assert ok.status_code == 200, ok.text
    rules = two_admins.allowlist_list("marketing")
    assert [(r["member"], r["action"]) for r in rules] == [("alice@acme.com", "memory:dept/marketing:add")]


def test_admin_alone_always_allow_applies_directly_with_a_single_admin(tmp_path, member_store):
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = _dept_item(store, route="admin-only", level="admin")
    apply_decision(store, member_store, aid, ADMIN, approve=True, as_level="admin", always_allow=True)
    assert len(member_store.allowlist_list()) == 1
    assert member_store.second_eyes_pending() == []


def test_head_plus_admin_chain_rule_applies_directly(tmp_path, two_admins):
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = _dept_item(store, route="head+admin")
    apply_decision(store, two_admins, aid, MHEAD, approve=True, as_level="head", always_allow=True)
    apply_decision(store, two_admins, aid, ADMIN, approve=True, as_level="admin")
    assert len(two_admins.allowlist_list()) == 1


# ---------------------------------------------------------------- 3: second admin


def _admin_tool_item(store: ApprovalStore, actor=ADMIN) -> str:
    return store.create(
        "r-tool", "t", {"type": "tool", "id": "finance_transfer"}, actor, TRANSFER,
        level="admin", route="admin-only",
    )


def test_admin_cannot_self_approve_own_critical_tool_with_two_admins(tmp_path, two_admins):
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = _admin_tool_item(store)
    with pytest.raises(DecisionError) as exc:
        apply_decision(store, two_admins, aid, ADMIN, approve=True, as_level="admin")
    assert exc.value.status == 409 and "critical action needs a second admin" in exc.value.detail
    # rejecting your own request is always fine; another admin may approve
    assert apply_decision(store, two_admins, aid, ADMIN2, approve=True, as_level="admin")["status"] == "approved"


def test_single_admin_may_still_self_approve_critical_tool(tmp_path, member_store):
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = _admin_tool_item(store)
    assert apply_decision(store, member_store, aid, ADMIN, approve=True, as_level="admin")["status"] == "approved"


def test_unknown_admin_count_fails_closed(tmp_path, member_store):
    class Broken:
        def count_active_admins(self):
            raise RuntimeError("store down")

    store = ApprovalStore(tmp_path / "a.sqlite")
    for aid in (_admin_tool_item(store), store.create(
            "r-c", "t", {"type": "memory", "id": "central"}, ADMIN, FACT, level="admin", route="admin-only")):
        with pytest.raises(DecisionError) as exc:
            apply_decision(store, Broken(), aid, ADMIN, approve=True, as_level="admin")
        assert exc.value.status == 409


def test_dept_attributed_critical_tool_is_not_affected(tmp_path, two_admins):
    """A member's critical tool (head+admin chain) is decided by different people anyway."""
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = store.create("r", "t", {"type": "tool", "id": "finance_transfer"},
                       {"sub": "carol@acme.com", "role": "finance_agent", "dept": "finance"}, TRANSFER,
                       level="admin", route="admin-only", dept="finance")
    assert apply_decision(store, two_admins, aid, ADMIN, approve=True, as_level="admin")["status"] == "approved"


# ---------------------------------------------------------------- 4: resume claim


def test_deleted_chat_resume_does_not_consume_the_claim(tmp_path):
    from app.agent.runner import resume_agent

    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = store.create("r", "user:alice@acme.com:gone", {"type": "memory", "id": "dept/marketing"}, ALICE, FACT)
    store.decide(aid, True, "mhead@acme.com")
    with pytest.raises(ValueError, match="chat was deleted"):
        resume_agent(aid, approvals=store, checkpointer=InMemorySaver())
    assert store.get(aid)["resumed_by"] is None  # still claimable / audit fields truthful


# ---------------------------------------------------------------- 5: approver view


def test_approvers_can_read_what_they_approve():
    payload = {"tool": "gmail_send", "args": {
        "to": "bob@acme.com", "subject": "Quarterly numbers",
        "body": "Hi Bob, SSN 123-45-6789 card 4111 1111 1111 1111. " + "x" * 400,
    }}
    view = _mask_payload(payload)["args"]
    assert view["to"] == "bob@acme.com" and view["subject"] == "Quarterly numbers"
    assert view["body"].startswith("Hi Bob") and len(view["body"]) <= 281
    assert "123-45-6789" not in view["body"] and "4111 1111 1111 1111" not in view["body"]
    money = _mask_payload({"tool": "finance_transfer", "args": {"memo": "m", "from_account": "1", "to_account": "2", "amount": 5}})
    assert money["args"]["memo"] == money["args"]["from_account"] == money["args"]["to_account"] == "***"
    assert money["args"]["amount"] == 5


# ---------------------------------------------------------------- 8: toolkit names


def test_toolkit_names_are_validated(member_store):
    with pytest.raises(MemberError, match="toolkit name"):
        member_store.upsert_toolkit("x');alert(1);//")
    assert member_store.upsert_toolkit("google_drive-2")["toolkit"] == "google_drive-2"
    resp = TestClient(dashboard_app).post(
        "/api/integrations/catalog", json={"toolkit": "evil'\"<x>"}, headers=_auth("admin@acme.com"))
    assert resp.status_code == 422


# ---------------------------------------------------------------- identity switch (retired in Mk21)


@pytest.fixture
def _insecure_cookies(monkeypatch):
    """TestClient speaks plain http, so Secure cookies would never be sent back. Patch
    BOTH settings objects: some earlier tests clear the settings cache, after which the
    dashboard module's `settings` is no longer the object `get_settings()` returns."""
    import app.dashboard.app as dash

    for cfg in {id(get_settings()): get_settings(), id(dash.settings): dash.settings}.values():
        monkeypatch.setattr(cfg.jwt, "cookie_secure", False)


def test_session_switch_endpoint_is_retired_but_slot_cookies_still_apply(member_store, _insecure_cookies):
    """Mk21 Decision 1B retired `POST /api/session/switch` (Mk20 finding 13): the dashboard holds one
    identity at a time and every login rewrites the active slot. What Mk20 protected still holds:
    the active-slot cookie picks the session, unknown labels / missing slots authenticate nobody,
    cookie writes still need CSRF, and logout clears every slot."""
    from app.harness.csrf import csrf_token_for

    s = get_settings()
    c = TestClient(dashboard_app)
    host = "testserver.local"  # host-only, path / — the way a browser holds them after login
    c.cookies.set("harness_token_admin", _token("admin@acme.com"), domain=host, path="/")
    c.cookies.set("harness_token_member", _token("alice@acme.com"), domain=host, path="/")
    c.cookies.set("harness_token_active", "admin", domain=host, path="/")

    def me():
        resp = c.get("/api/memory/me")
        if resp.status_code != 200:
            return resp.status_code
        return next(x["scope"] for x in resp.json() if x["scope"].startswith("user/"))

    assert me() == "user/admin@acme.com"
    csrf = csrf_token_for("admin@acme.com", s.jwt.secret, member_store.credentials_version("admin@acme.com"))
    # the endpoint is gone: a valid cookie session with a valid CSRF token gets 404 ...
    assert c.post("/api/session/switch", json={"label": "member"}, headers={"X-CSRF-Token": csrf}).status_code == 404
    # ... and the CSRF gate still runs first for cookie-session writes
    assert c.post("/api/session/switch", json={"label": "member"}).status_code == 403
    assert me() == "user/admin@acme.com"  # nothing switched

    # slot selection by the active marker still works (a fresh login rewrites it)
    c.cookies.set("harness_token_active", "member", domain=host, path="/")
    assert me() == "user/alice@acme.com"
    # an unknown label or a missing slot authenticates nobody
    c.cookies.set("harness_token_active", "root", domain=host, path="/")
    assert me() == 401
    c.cookies.set("harness_token_active", "admin", domain=host, path="/")
    c.cookies.delete("harness_token_admin", domain=host, path="/")
    assert me() == 401

    # logout clears all three cookies
    out = c.post("/api/logout")
    cleared = " ".join(out.headers.get_list("set-cookie"))
    for name in ("harness_token_admin", "harness_token_member", "harness_token_active"):
        assert f"{name}=" in cleared


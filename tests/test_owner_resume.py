"""Owner-side HITL resume (Mk14): approve anywhere, resume on the requester's machine.

Code-only scope: deciding with `?defer=1` records without resuming; the approval's
`actor` later resumes via `POST .../resume` so their own model/keys run. Covers the
admin + dept dashboard routes and the gateway route, the single-resume claim gate,
and the by-thread poll the auto-resumer uses. SQLite/offline; pg parity comes from
the shared `ApprovalStore` code (`PgApprovalStore` only swaps the connection).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from typing_extensions import TypedDict

import app.db as db_pkg
from app.agent.runner import AgentResult
from app.auth.jwt import create_token
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard import app as dash
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_approval_store, get_resumer
from app.gateway.app import app as gateway_app
from app.gateway.app import get_approval_store as gw_get_approval_store
from app.gateway.app import get_resumer as gw_get_resumer
from app.memory.approvals import ApprovalStore


def _token(username: str) -> str:
    s = get_settings()
    return create_token(USERS[username], s.jwt.secret, s.jwt.algorithm, 60)


def _admin_headers() -> dict[str, str]:
    return {"Authorization": "Bearer " + _token("admin@acme.com")}


def _member_headers(username: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + _token(username)}


def _seed_thread(saver: InMemorySaver, thread_id: str) -> None:
    class S(TypedDict, total=False):
        messages: list

    g = StateGraph(S)
    g.add_node("n", lambda s: {"messages": [{"role": "user", "content": "hi"}]})
    g.set_entry_point("n")
    g.set_finish_point("n")
    g.compile(checkpointer=saver).invoke({}, config={"configurable": {"thread_id": thread_id}})


@pytest.fixture
def dash_mode(tmp_path, monkeypatch):
    """Dashboard with shared-mode checkpoints and a recording resumer."""
    saver = InMemorySaver()
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    resumed: list[str] = []

    def fake_resumer(approval_id):
        # Mirror resume_agent's single-resume gate so double-resume is a 409.
        entry = approvals.get(approval_id)
        approvals.claim_resume(approval_id, (entry.get("actor") or {}).get("sub", ""))
        resumed.append(approval_id)
        entry = approvals.get(approval_id)
        return AgentResult(
            reply="resumed", run_id="r", guardrail_hits=[], tool_calls=[],
            thread_id=entry["thread_id"] if entry else "t",
        )

    monkeypatch.setattr(db_pkg, "is_enabled", lambda: True)
    monkeypatch.setattr(dash, "_get_components", lambda: {"checkpointer": saver})
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    dashboard_app.dependency_overrides[get_resumer] = lambda: fake_resumer
    yield TestClient(dashboard_app), saver, approvals, resumed
    dashboard_app.dependency_overrides.clear()


@pytest.fixture
def gw_mode(tmp_path, monkeypatch):
    """Gateway twin of dash_mode (admin decide + owner resume)."""
    import app.gateway.app as gw

    saver = InMemorySaver()
    approvals = ApprovalStore(tmp_path / "g.sqlite")
    resumed: list[str] = []

    def fake_resumer(approval_id):
        entry = approvals.get(approval_id)
        approvals.claim_resume(approval_id, (entry.get("actor") or {}).get("sub", ""))
        resumed.append(approval_id)
        entry = approvals.get(approval_id)
        return AgentResult(
            reply="resumed", run_id="r", guardrail_hits=[], tool_calls=[],
            thread_id=entry["thread_id"] if entry else "t",
        )

    monkeypatch.setattr(db_pkg, "is_enabled", lambda: True)
    monkeypatch.setattr(gw, "_get_components", lambda: {"checkpointer": saver})
    gateway_app.dependency_overrides[gw_get_approval_store] = lambda: approvals
    gateway_app.dependency_overrides[gw_get_resumer] = lambda: fake_resumer
    yield TestClient(gateway_app), saver, approvals, resumed
    gateway_app.dependency_overrides.clear()


def _gw_token(username: str) -> str:
    return _token(username)


# --- store-level claim gate ---


def test_claim_resume_single_winner(tmp_path):
    store = ApprovalStore(tmp_path / "c.sqlite")
    aid = store.create("r", "t", {"type": "tool", "id": "x"}, {"sub": "a"}, {"k": 1})
    with pytest.raises(ValueError, match="has not been decided"):
        store.claim_resume(aid, "a")
    store.decide(aid, True, "admin@acme.com")
    claimed = store.claim_resume(aid, "a")
    assert claimed["resumed_by"] == "a" and claimed["resumed_at"]
    with pytest.raises(ValueError, match="already resumed"):
        store.claim_resume(aid, "a")
    with pytest.raises(KeyError):
        store.claim_resume("missing", "a")


def test_for_thread_lists_thread_approvals(tmp_path):
    store = ApprovalStore(tmp_path / "f.sqlite")
    a1 = store.create("r1", "t-1", {"type": "tool", "id": "x"}, {"sub": "a"}, {"k": 1})
    store.create("r2", "t-2", {"type": "tool", "id": "x"}, {"sub": "a"}, {"k": 2})
    ids = [i["id"] for i in store.for_thread("t-1")]
    assert ids == [a1]


# --- dashboard admin: defer then owner resume ---


def test_admin_defer_records_without_resuming(dash_mode):
    client, saver, approvals, resumed = dash_mode
    _seed_thread(saver, "t-owner")
    aid = approvals.create(
        "r1", "t-owner", {"type": "tool", "id": "gmail_send"},
        {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, {"to": "x"},
    )
    resp = client.post(f"/api/approvals/{aid}/decide?defer=1", json={"approve": True}, headers=_admin_headers())
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "approved" and body["needs_resume"] is True and body["reply"] is None
    assert resumed == []  # nothing executed on the approver
    assert approvals.get(aid)["resumed_by"] is None


def test_owner_resume_executes_once_then_second_resume_409(dash_mode):
    client, saver, approvals, resumed = dash_mode
    _seed_thread(saver, "t-owner")
    aid = approvals.create(
        "r1", "t-owner", {"type": "tool", "id": "gmail_send"},
        {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, {"to": "x"},
    )
    client.post(f"/api/approvals/{aid}/decide?defer=1", json={"approve": True}, headers=_admin_headers())
    ok = client.post(f"/api/approvals/{aid}/resume", headers=_member_headers("alice@acme.com"))
    assert ok.status_code == 200 and ok.json()["reply"] == "resumed"
    assert resumed == [aid]
    again = client.post(f"/api/approvals/{aid}/resume", headers=_member_headers("alice@acme.com"))
    assert again.status_code == 409 and "already resumed" in again.json()["detail"]
    assert resumed == [aid]  # no double execution


def test_resume_by_non_owner_is_403(dash_mode):
    client, saver, approvals, resumed = dash_mode
    _seed_thread(saver, "t-owner")
    aid = approvals.create(
        "r1", "t-owner", {"type": "tool", "id": "gmail_send"},
        {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, {"to": "x"},
    )
    client.post(f"/api/approvals/{aid}/decide?defer=1", json={"approve": True}, headers=_admin_headers())
    resp = client.post(f"/api/approvals/{aid}/resume", headers=_member_headers("bob@acme.com"))
    assert resp.status_code == 403 and "only the requester" in resp.json()["detail"]
    assert resumed == []


def test_resume_pending_is_409(dash_mode):
    client, saver, approvals, _resumed = dash_mode
    _seed_thread(saver, "t-owner")
    aid = approvals.create(
        "r1", "t-owner", {"type": "tool", "id": "gmail_send"},
        {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, {"to": "x"},
    )
    resp = client.post(f"/api/approvals/{aid}/resume", headers=_member_headers("alice@acme.com"))
    assert resp.status_code == 409 and "has not been decided" in resp.json()["detail"]


def test_by_thread_poll_powers_auto_resume(dash_mode):
    client, saver, approvals, _resumed = dash_mode
    _seed_thread(saver, "user:alice@acme.com")
    aid = approvals.create(
        "r1", "user:alice@acme.com", {"type": "tool", "id": "gmail_send"},
        {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, {"to": "x"},
    )
    client.post(f"/api/approvals/{aid}/decide?defer=1", json={"approve": True}, headers=_admin_headers())
    items = client.get("/api/approvals/by-thread/user:alice@acme.com", headers=_member_headers("alice@acme.com")).json()
    mine = [i for i in items if i["id"] == aid]
    assert mine and mine[0]["needs_resume"] is True
    # Another member's thread is forbidden; admin may poll any thread.
    other = client.get("/api/approvals/by-thread/user:alice@acme.com", headers=_member_headers("bob@acme.com"))
    assert other.status_code == 403


def test_coupled_decide_still_resumes_by_default(dash_mode):
    client, saver, approvals, resumed = dash_mode
    _seed_thread(saver, "t-mine")
    aid = approvals.create(
        "r1", "t-mine", {"type": "tool", "id": "gmail_send"}, {"sub": "a"}, {"k": 1},
    )
    resp = client.post(f"/api/approvals/{aid}/decide", json={"approve": True}, headers=_admin_headers())
    assert resp.status_code == 200 and resumed == [aid]


# --- dept route parity ---


def test_dept_defer_then_owner_resume(dash_mode, member_store):
    member_store.create("mhead@acme.com", "head1234", role="marketing_head", dept="marketing")
    client, saver, approvals, resumed = dash_mode
    _seed_thread(saver, "t-dept")
    aid = approvals.create(
        "r1", "t-dept", {"type": "memory", "id": "dept/marketing"},
        {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, {"op": "add", "text": "x"},
    )
    head = {"Authorization": "Bearer " + _token("mhead@acme.com")}
    dec = client.post(f"/api/dept/approvals/{aid}/decide?defer=1", json={"approve": True}, headers=head)
    assert dec.status_code == 200 and dec.json()["needs_resume"] is True and resumed == []
    ok = client.post(f"/api/dept/approvals/{aid}/resume", headers=_member_headers("alice@acme.com"))
    assert ok.status_code == 200 and resumed == [aid]


# --- gateway parity ---


def test_gateway_defer_then_owner_resume(gw_mode):
    client, saver, approvals, resumed = gw_mode
    _seed_thread(saver, "t-gw")
    aid = approvals.create(
        "r1", "t-gw", {"type": "tool", "id": "gmail_send"},
        {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}, {"to": "x"},
    )
    dec = client.post(
        f"/approvals/{aid}/decide",
        json={"approve": True, "token": _gw_token("admin@acme.com"), "defer": True},
    )
    assert dec.status_code == 200 and dec.json()["needs_resume"] is True and resumed == []
    ok = client.post(f"/approvals/{aid}/resume", json={"token": _gw_token("alice@acme.com")})
    assert ok.status_code == 200 and resumed == [aid]
    again = client.post(f"/approvals/{aid}/resume", json={"token": _gw_token("alice@acme.com")})
    assert again.status_code == 409
    other = client.post(f"/approvals/{aid}/resume", json={"token": _gw_token("bob@acme.com")})
    assert other.status_code == 403

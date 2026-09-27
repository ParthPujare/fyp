"""Guard for approvals whose paused run cannot be resumed from here (Mk13 Phase 2, kept in Phase 3).

Deciding an approval records the decision *and then* resumes the run; a decided approval cannot
be re-decided. So if the run's checkpoint cannot be loaded, the decide endpoints refuse (409)
BEFORE recording anything and the queue marks the item `resumable_here: false`. Phase 2 needed
this because checkpoints were per-machine; since Phase 3 they are shared, so it now only fires
for a genuinely missing checkpoint (e.g. the chat was deleted) — and on local SQLite, where a run
started on another machine really is unresumable here.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import StateGraph
from typing_extensions import TypedDict

import app.db as db_pkg
from app.agent.runner import AgentResult, run_state_is_local
from app.auth.jwt import create_token
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard import app as dash
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_approval_store, get_resumer
from app.memory.approvals import ApprovalStore


def _admin_headers() -> dict[str, str]:
    s = get_settings()
    return {"Authorization": "Bearer " + create_token(USERS["admin@acme.com"], s.jwt.secret, s.jwt.algorithm, 60)}


def _seed_thread(saver: InMemorySaver, thread_id: str) -> None:
    class S(TypedDict, total=False):
        messages: list

    g = StateGraph(S)
    g.add_node("n", lambda s: {"messages": [{"role": "user", "content": "hi"}]})
    g.set_entry_point("n")
    g.set_finish_point("n")
    g.compile(checkpointer=saver).invoke({}, config={"configurable": {"thread_id": thread_id}})


@pytest.fixture
def shared_mode(tmp_path, monkeypatch):
    """Pretend DATABASE_URL is set, with a local checkpoint store and a recording resumer."""
    saver = InMemorySaver()
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    resumed: list[str] = []

    def fake_resumer(approval_id):
        resumed.append(approval_id)
        return AgentResult(reply="resumed", run_id="r", guardrail_hits=[], tool_calls=[], thread_id="t")

    monkeypatch.setattr(db_pkg, "is_enabled", lambda: True)
    monkeypatch.setattr(dash, "_get_components", lambda: {"checkpointer": saver})
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    dashboard_app.dependency_overrides[get_resumer] = lambda: fake_resumer
    yield TestClient(dashboard_app), saver, approvals, resumed
    dashboard_app.dependency_overrides.clear()


def test_run_state_is_local_predicate():
    saver = InMemorySaver()
    assert run_state_is_local(saver, "t-1") is False
    _seed_thread(saver, "t-1")
    assert run_state_is_local(saver, "t-1") is True
    assert run_state_is_local(None, "t-1") is False  # unreadable store => not resumable, never a crash


def test_an_approval_from_another_machine_is_refused_and_stays_pending(shared_mode):
    client, _saver, approvals, resumed = shared_mode
    aid = approvals.create("r1", "t-other-laptop", {"type": "tool", "id": "gmail_send"}, {"sub": "a"}, {"k": 1})

    resp = client.post(f"/api/approvals/{aid}/decide", json={"approve": True}, headers=_admin_headers())
    assert resp.status_code == 409 and "saved state was not found" in resp.json()["detail"]
    assert approvals.get(aid)["status"] == "pending"  # nothing was recorded, so it can still be decided
    assert resumed == []

    queue = client.get("/api/approvals/pending", headers=_admin_headers()).json()
    assert [i["resumable_here"] for i in queue if i["id"] == aid] == [False]


def test_an_approval_whose_run_is_on_this_machine_still_resumes(shared_mode):
    client, saver, approvals, resumed = shared_mode
    _seed_thread(saver, "t-mine")
    aid = approvals.create("r2", "t-mine", {"type": "tool", "id": "gmail_send"}, {"sub": "a"}, {"k": 2})

    queue = client.get("/api/approvals/pending", headers=_admin_headers()).json()
    assert [i["resumable_here"] for i in queue if i["id"] == aid] == [True]

    resp = client.post(f"/api/approvals/{aid}/decide", json={"approve": True}, headers=_admin_headers())
    assert resp.status_code == 200 and resp.json()["status"] == "approved"
    assert resumed == [aid]


def test_local_sqlite_mode_is_never_blocked(tmp_path, monkeypatch):
    """Without a shared database approvals and checkpoints are both local: no guard, no extra reads."""
    approvals = ApprovalStore(tmp_path / "m.sqlite")
    resumed: list[str] = []
    monkeypatch.setattr(dash, "_get_components", lambda: pytest.fail("must not touch components in local mode"))
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    dashboard_app.dependency_overrides[get_resumer] = lambda: (
        lambda aid: (resumed.append(aid), AgentResult("ok", "r", [], [], "t"))[1]
    )
    try:
        aid = approvals.create("r3", "t-any", {"type": "tool", "id": "x"}, {"sub": "a"}, {"k": 3})
        resp = TestClient(dashboard_app).post(
            f"/api/approvals/{aid}/decide", json={"approve": False}, headers=_admin_headers()
        )
        assert resp.status_code == 200 and resumed == [aid]
    finally:
        dashboard_app.dependency_overrides.clear()

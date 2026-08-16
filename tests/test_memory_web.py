"""Plan Mk2 Phase 5 — browser memory-test walkthrough + e2e (plan_mk2 §4 row 5).

The scoping scenario must be reproducible with clicks only through the browser's
HTTP contract, so this suite drives the dashboard API end-to-end:
    `/api/chat` (member proxy → in-process gateway), `/api/approvals/{id}/decide`
    (the REAL dashboard resumer so the central write commits), the admin Memory
    browser CRUD (`/api/memory*`), `/api/members` (create molly), and the new
    member-only `GET /api/memory/me` (the exact readable set the agent injects).

Covered here:
    1. admin central write via chat → HITL suspend → approve → committed.
    2. admin central + private (user/admin) facts via the memory browser.
    3. alice dept + private facts via chat; her `memory/me` shows only her scopes.
    4. molly (2nd marketing member, created via /api/members) reads the shared
       dept/marketing memory.
    5. cross-dept denial: bob's `memory/me` has no marketing scope and his
       `recall` finds nothing a marketing transcript would match.
    6. member memory API gates (401/403 for missing/disabled, member-vs-admin).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.auth.jwt import create_token
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_gateway_sender
from app.gateway.app import app as gateway_app
from app.gateway.app import get_runner as gw_get_runner
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.edit import MemoryEditor
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from app.skills.registry import SkillRegistry

CENTRAL_HITL_MSG = "Company policy requires quarterly vendor audits."
ALICE_FACTS_MSG = "Our team policy requires weekly standups. Alice prefers Slack over Teams."


def _token(username: str) -> str:
    settings = get_settings()
    return create_token(
        USERS[username], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )


def _auth(username: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {_token(username)}"}


class WebHarness:
    """Shared widgets across both apps: the dashboard reads the same tmp stores
    the gateway runner writes, so resume/decide + memory reads see one state."""

    def __init__(self, executor, tmp_path):
        settings = get_settings()
        self.components = {
            "trace": TraceStore(tmp_path / "trace.sqlite"),
            "enforcer": PolicyEnforcer(),
            "audit": AuditLog(tmp_path / "audit.jsonl"),
            "store": SqliteBaseStore(tmp_path / "memory.sqlite"),
            "episodic": EpisodicStore(tmp_path / "memory.sqlite"),
            "approvals": ApprovalStore(tmp_path / "memory.sqlite"),
            "checkpointer": InMemorySaver(),
            "skills": SkillRegistry(settings.skills.skills_dir),
        }
        self.components["memory_editor"] = MemoryEditor(
            self.components["store"], settings.memory
        )
        self.components["recall"] = RecallService(
            self.components["episodic"], self.components["enforcer"], settings.memory
        )
        self.provider: MockLLM | None = None

    def run(self, claims, message, thread_id=None):
        c = self.components
        return run_agent(
            claims,
            message,
            thread_id=thread_id,
            provider=self.provider,
            executor=self.executor,
            trace=c["trace"],
            enforcer=c["enforcer"],
            audit=c["audit"],
            store=c["store"],
            episodic=c["episodic"],
            approvals=c["approvals"],
            recall=c["recall"],
            checkpointer=c["checkpointer"],
        )

    def caller(self):
        def runner(claims, message, thread_id=None):
            return self.run(claims, message, thread_id)

        return runner


@pytest.fixture
def web(executor, tmp_path):
    import app.dashboard.app as dash

    harness = WebHarness(executor, tmp_path)
    harness.executor = executor

    gateway_app.dependency_overrides[gw_get_runner] = lambda: harness.caller()
    gclient = TestClient(gateway_app)

    def sender(token, message, thread_id=None):
        resp = gclient.post(
            "/chat", json={"token": token, "message": message, "thread_id": thread_id}
        )
        assert resp.status_code == 200, resp.text
        return resp.json()

    previous = dash._components
    dash._components = harness.components
    dashboard_app.dependency_overrides[get_gateway_sender] = lambda: sender
    harness.client = TestClient(dashboard_app)
    yield harness

    dashboard_app.dependency_overrides.clear()
    gateway_app.dependency_overrides.clear()
    dash._components = previous


def _chat(web: WebHarness, username: str, message: str) -> dict:
    return web.client.post("/api/chat", json={"message": message}, headers=_auth(username)).json()


def _member_memory(web: WebHarness, username: str) -> dict[str, dict]:
    body = web.client.get("/api/memory/me", headers=_auth(username))
    assert body.status_code == 200, body.text
    return {s["scope"]: s for s in body.json()}


def test_admin_central_write_interrupts_and_approve_commits(web):
    """The click-only HITL path: admin chats a central fact → run suspends →
    approve from the dashboard (real resumer) → fact commits to central."""
    harness = web
    harness.provider = MockLLM(script={CENTRAL_HITL_MSG: {"text": "ok", "tool_calls": []}})

    body = _chat(harness, "admin@acme.com", CENTRAL_HITL_MSG)
    assert body["interrupted"] is True
    assert body["approval_id"]

    pending = harness.client.get("/api/approvals/pending", headers=_auth("admin@acme.com")).json()
    item = next(p for p in pending if p["id"] == body["approval_id"])
    assert item["resource"] == {"type": "memory", "id": "central"}
    assert item["payload"]["text"] == CENTRAL_HITL_MSG

    decided = harness.client.post(
        f"/api/approvals/{body['approval_id']}/decide",
        json={"approve": True},
        headers=_auth("admin@acme.com"),
    )
    assert decided.status_code == 200
    assert decided.json()["status"] == "approved"

    central_mine = _member_memory(harness, "admin@acme.com")["central"]["items"]
    assert any("quarterly vendor audits" in it["text"] for it in central_mine)
    browser = {s["scope"]: s for s in harness.client.get("/api/memory", headers=_auth("admin@acme.com")).json()}
    assert any("quarterly vendor audits" in it["text"] for it in browser["central"]["items"])
    assert any(e["decision"] == "APPROVED" for e in harness.components["audit"].entries())


def test_admin_central_and_private_facts_via_memory_browser(web):
    """Admin uses the Memory browser (the only admin-only write surface): a
    central fact and a private user/admin fact, plus the admin/manage gates."""
    harness = web
    assert harness.client.post(
        "/api/memory/central", json={"text": "Company-wide sales target is 1.2M."}, headers=_auth("admin@acme.com")
    ).status_code == 200
    assert harness.client.post(
        "/api/memory/user/admin@acme.com", json={"text": "Admin prefers weekly exec briefings."}, headers=_auth("admin@acme.com")
    ).status_code == 200

    mine = _member_memory(harness, "admin@acme.com")
    assert any("sales target" in it["text"] for it in mine["central"]["items"])
    assert any("exec briefings" in it["text"] for it in mine["user/admin@acme.com"]["items"])

    # the browser is admin-only; a member can neither list nor write it
    assert harness.client.get("/api/memory", headers=_auth("alice@acme.com")).status_code == 403
    assert harness.client.get("/api/memory", headers=_auth("admin@acme.com")).status_code == 200
    assert harness.client.post(
        "/api/memory/central", json={"text": "x"}, headers=_auth("alice@acme.com")
    ).status_code == 403
    assert harness.client.post("/api/memory/bogus", json={"text": "x"}, headers=_auth("admin@acme.com")).status_code == 422


def test_alice_dept_and_private_facts_via_chat(web):
    """Alice chats a dept + private fact (no HITL): her memory/me shows exactly
    her own scopes and excludes another user's."""
    harness = web
    harness.provider = MockLLM(script={ALICE_FACTS_MSG: {"text": "noted.", "tool_calls": []}})

    body = _chat(harness, "alice@acme.com", ALICE_FACTS_MSG)
    assert body["interrupted"] is False

    mine = _member_memory(harness, "alice@acme.com")
    assert set(mine) == {"central", "dept/marketing", "user/alice@acme.com"}
    assert any("weekly standups" in it["text"] for it in mine["dept/marketing"]["items"])
    assert any("Slack over Teams" in it["text"] for it in mine["user/alice@acme.com"]["items"])
    assert "user/bob@acme.com" not in mine
    assert any(
        e["decision"] == "ALLOW" and e["resource"]["type"] == "memory"
        for e in harness.components["audit"].entries()
    )


def test_molly_second_marketing_member_reads_shared_dept_memory(web):
    """Phase-5 acceptance: a 2nd marketing member (created in the Members tab)
    sees the marketing dept-scope facts alice wrote — shared dept memory."""
    harness = web
    harness.provider = MockLLM(script={ALICE_FACTS_MSG: {"text": "noted.", "tool_calls": []}})
    _chat(harness, "alice@acme.com", ALICE_FACTS_MSG)

    created = harness.client.post(
        "/api/members",
        json={"username": "molly@acme.com", "password": "molly123", "role": "marketing_agent",
              "dept": "marketing"},
        headers=_auth("admin@acme.com"),
    )
    assert created.status_code == 200

    molly = _member_memory(harness, "molly@acme.com")
    assert set(molly) == {"central", "dept/marketing", "user/molly@acme.com"}
    marketing = [it["text"] for it in molly["dept/marketing"]["items"]]
    assert any("weekly standups" in t for t in marketing)  # alice's shared fact
    # ...but not her private user scope
    assert "user/alice@acme.com" not in molly


def test_cross_dept_denial_bob_cannot_read_marketing(web):
    """Bob's readable set excludes marketing, and his recall of a marketing
    topic finds nothing — while alice's same recall resolves the shared dept
    transcript (positive control)."""
    harness = web
    harness.provider = MockLLM(script={ALICE_FACTS_MSG: {"text": "standups.", "tool_calls": []}})
    _chat(harness, "alice@acme.com", ALICE_FACTS_MSG)

    bob = _member_memory(harness, "bob@acme.com")
    assert set(bob) == {"central", "dept/hr", "user/bob@acme.com"}
    assert "dept/marketing" not in bob

    def recall(username):
        harness.provider = MockLLM(sequence=[
            {"text": "", "tool_calls": [{"name": "recall", "args": {"query": "standups"}}]},
            {"text": "Done.", "tool_calls": []},
        ])
        return _chat(harness, username, "recall the decision about standups")

    alice = recall("alice@acme.com")
    assert [c["name"] for c in alice["tool_calls"]] == ["recall"]
    assert "standups" in alice["tool_calls"][0]["content"]  # dept/marketing transcript

    bob = recall("bob@acme.com")
    assert bob["tool_calls"][0]["outcome"] == "allowed"
    assert "No recalled conversations" in bob["tool_calls"][0]["content"]


def test_member_memory_api_gates(web, member_store):
    """/api/memory/me follows the member-only contract: 401 without a token,
    403 for a disabled member, 200 for any active member."""
    harness = web
    assert harness.client.get("/api/memory/me").status_code == 401
    assert harness.client.get("/api/memory/me", headers=_auth("carol@acme.com")).status_code == 200

    member_store.create("molly@acme.com", "molly123", role="marketing_agent", dept="marketing", actor="admin@acme.com")
    member_store.set_active("molly@acme.com", False, actor="admin@acme.com")
    assert harness.client.get("/api/memory/me", headers=_auth("molly@acme.com")).status_code == 403


def test_memory_web_walkthrough_trace_integrity(web):
    """After the full click-through, the trace chain is intact (deterministic
    evals wrote EVAL events and resume re-ended the suspended run)."""
    harness = web
    harness.provider = MockLLM(script={CENTRAL_HITL_MSG: {"text": "ok", "tool_calls": []}})
    body = _chat(harness, "admin@acme.com", CENTRAL_HITL_MSG)
    assert body["interrupted"] is True
    decided = harness.client.post(
        f"/api/approvals/{body['approval_id']}/decide",
        json={"approve": True},
        headers=_auth("admin@acme.com"),
    )
    assert decided.status_code == 200

    ok, issues = harness.components["trace"].verify_integrity()
    assert ok, issues
"""Two-level department approvals + approve-once allowlist + useful heads (Mk15).

Offline (MockLLM, tmp SQLite, isolated MemberStore via conftest `member_store`). Covers the
store steps, the agent gates end to end (head step → admin step → resume), the queues, the
allowlist, the head's scoped member management and the gateway head decide twin.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import AgentResult, resume_agent, run_agent
from app.auth.jwt import create_token
from app.auth.store import DepartmentError
from app.auth.users import USERS
from app.config import Settings, get_settings
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_approval_store, get_resumer
from app.gateway.app import app as gateway_app
from app.gateway.app import get_approval_store as gw_get_approval_store
from app.gateway.app import get_resumer as gw_get_resumer
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore

ALICE = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing", "scopes": []}
MOLLY = {"sub": "molly@acme.com", "role": "marketing_agent", "dept": "marketing", "scopes": []}
BOB = {"sub": "bob@acme.com", "role": "hr_agent", "dept": "hr", "scopes": []}
CAROL = {"sub": "carol@acme.com", "role": "finance_agent", "dept": "finance", "scopes": []}
FHEAD = {"sub": "fhead@acme.com", "role": "finance_head", "dept": "finance", "scopes": []}

DEPT_MSG = "Our team prefers morning standups."
OTHER_MSG = "Our team prefers evening standups."
TRANSFER = {"from_account": "1000", "to_account": "2000", "amount": 1.0,
            "memo": "m", "actor": "carol@acme.com", "status": "pending"}
CAMPAIGN = {"name": "Q4 push", "budget": 100.0, "status": "draft", "region": "EU"}


def _token(username: str) -> str:
    s = get_settings()
    return create_token(USERS[username], s.jwt.secret, s.jwt.algorithm, 60)


def _auth(username: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + _token(username)}


@pytest.fixture
def org(member_store):
    """Heads for marketing/hr/finance + a second marketing member."""
    member_store.create("mhead@acme.com", "head1234", role="marketing_head", dept="marketing")
    member_store.create("mhead2@acme.com", "head1234", role="marketing_head", dept="marketing")
    member_store.create("hhead@acme.com", "head1234", role="hr_head", dept="hr")
    member_store.create("fhead@acme.com", "head1234", role="finance_head", dept="finance")
    member_store.create("molly@acme.com", "molly123", role="marketing_agent", dept="marketing")
    return member_store


class Env:
    """In-process components shared by run_agent / resume_agent."""

    def __init__(self, tmp_path, enforcer, executor, audit_log):
        db = tmp_path / "m.sqlite"
        self.store, self.episodic, self.approvals = SqliteBaseStore(db), EpisodicStore(db), ApprovalStore(db)
        self.recall = RecallService(self.episodic, enforcer, get_settings().memory)
        self.trace = TraceStore(tmp_path / "t.sqlite")
        self.checkpointer = InMemorySaver()
        self.enforcer, self.executor, self.audit = enforcer, executor, audit_log

    def kwargs(self):
        return {"store": self.store, "episodic": self.episodic, "approvals": self.approvals,
                "recall": self.recall, "trace": self.trace, "checkpointer": self.checkpointer,
                "enforcer": self.enforcer, "audit": self.audit, "executor": self.executor}

    def say(self, claims, msg, thread_id, **extra):
        provider = MockLLM(script={msg: {"text": "ok", "tool_calls": []}})
        return run_agent(claims, msg, provider=provider, thread_id=thread_id, **self.kwargs(), **extra)

    def call(self, claims, tool, args, thread_id, **extra):
        provider = MockLLM(sequence=[{"text": "calling", "tool_calls": [{"name": tool, "args": args}]}])
        self._provider = provider
        return run_agent(claims, "do it", provider=provider, thread_id=thread_id, **self.kwargs(), **extra)

    def resume(self, approval_id, **extra):
        provider = getattr(self, "_provider", None) or MockLLM({"x": {"text": "ok", "tool_calls": []}})
        return resume_agent(approval_id, provider=provider, **self.kwargs(), **extra)

    def dept_facts(self, dept):
        return [str(i.value.get("text", "")) for i in self.store.items_in(("dept", dept))]

    def audit_for(self, run_id):
        return [(e["decision"], e["reason"], e["actor"]["sub"]) for e in self.audit.entries(run_id)]


@pytest.fixture
def env(tmp_path, enforcer, executor, audit_log):
    return Env(tmp_path, enforcer, executor, audit_log)


@pytest.fixture
def dash(env):
    """Dashboard wired to the env's approval store and a real `resume_agent`."""
    dashboard_app.dependency_overrides[get_approval_store] = lambda: env.approvals
    dashboard_app.dependency_overrides[get_resumer] = lambda: env.resume
    yield TestClient(dashboard_app)
    dashboard_app.dependency_overrides.clear()


def _decide(client, base, aid, user, approve=True, defer=True, **body):
    return client.post(
        f"{base}/{aid}/decide" + ("?defer=1" if defer else ""),
        json={"approve": approve, **body},
        headers=_auth(user),
    )


def _queue(client, path, user):
    return client.get(path, headers=_auth(user)).json()


# ================================================================ store (Phase 0)


def test_department_mode_defaults_and_switches(member_store):
    assert member_store.get_department("marketing").approval_mode == "head-only"
    assert member_store.department_mode("no-such-dept") == "head-only"
    version = member_store.version
    rec = member_store.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    assert rec.approval_mode == "head+admin" and member_store.version > version
    with pytest.raises(DepartmentError, match="approval mode"):
        member_store.set_department_mode("marketing", "everyone", actor="admin@acme.com")
    with pytest.raises(DepartmentError, match="not found"):
        member_store.set_department_mode("nope", "head-only", actor="admin@acme.com")
    with pytest.raises(DepartmentError):
        member_store.register_department("legal", "legal", "", approval_mode="bogus")
    assert member_store.register_department("legal", "legal", "", approval_mode="head+admin").approval_mode == "head+admin"


def test_active_head_count(org):
    assert org.active_head_count("marketing") == 2
    org.set_active("mhead2@acme.com", False, actor="admin@acme.com")
    assert org.active_head_count("marketing") == 1
    assert org.active_head_count("devops") == 0


def test_allowlist_store_roundtrip(member_store):
    args = {"op": "add", "text": "x", "match": None}
    rule = member_store.allowlist_add("alice@acme.com", "memory:dept/marketing:add", "h1", args, "marketing", "mhead@acme.com")
    assert rule["args"] == args and rule["created_by"] == "mhead@acme.com"
    member_store.allowlist_add("alice@acme.com", "memory:dept/marketing:add", "h1", args, "marketing", "x")  # idempotent
    assert len(member_store.allowlist_list()) == 1
    assert member_store.allowlist_match("alice@acme.com", "memory:dept/marketing:add", "h1") is True
    assert member_store.allowlist_match("alice@acme.com", "memory:dept/marketing:add", "h2") is False
    assert member_store.allowlist_match("molly@acme.com", "memory:dept/marketing:add", "h1") is False
    assert member_store.allowlist_list("hr") == []
    assert member_store.allowlist_revoke("alice@acme.com", "memory:dept/marketing:add", "h1", actor="admin@acme.com") is True
    assert member_store.allowlist_revoke("alice@acme.com", "memory:dept/marketing:add", "h1", actor="admin@acme.com") is False
    assert member_store.allowlist_list() == []


def test_approval_store_steps(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    res = {"type": "memory", "id": "dept/marketing"}
    aid = store.create("r", "t", res, ALICE, {"op": "add", "text": "x"}, route="head+admin", dept="marketing")
    assert store.level_of(store.get(aid)) == "head" and store.get(aid)["steps"] == []
    assert [i["id"] for i in store.pending_for_dept("marketing")] == [aid]

    mid = store.decide_step(aid, True, "mhead@acme.com", as_level="head", comment=" ok ")
    assert mid["status"] == "pending" and mid["level"] == "admin" and mid["decided_by"] is None
    assert mid["steps"][0] == {**mid["steps"][0], "level": "head", "by": "mhead@acme.com",
                               "decision": "approved", "comment": "ok"}
    assert store.pending_for_dept("marketing") == []
    assert [i["id"] for i in store.pending_for_level("admin")] == [aid]
    with pytest.raises(ValueError, match="waiting on an admin"):
        store.decide_step(aid, True, "mhead2@acme.com", as_level="head")
    with pytest.raises(ValueError, match="has not been decided"):
        store.claim_resume(aid, "alice@acme.com")  # step 1 alone never makes a run resumable

    done = store.decide_step(aid, True, "admin@acme.com", as_level="admin")
    assert done["status"] == "approved" and done["level"] == "done" and done["decided_by"] == "admin@acme.com"
    assert [s["level"] for s in done["steps"]] == ["head", "admin"]
    with pytest.raises(ValueError, match="already approved"):
        store.decide_step(aid, False, "admin@acme.com", as_level="admin")


def test_head_only_and_legacy_rows_are_terminal_at_the_head_step(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    res = {"type": "memory", "id": "dept/marketing"}
    for i, route in enumerate(("head-only", None)):  # None = a row created before Mk15
        aid = store.create(f"r{i}", "t", res, ALICE, {"op": "add", "text": "x"}, route=route)
        assert store.route_of(store.get(aid)) == "head-only"
        assert store.decide_step(aid, True, "mhead@acme.com", as_level="head")["status"] == "approved"


def test_head_reject_is_terminal_even_on_head_plus_admin(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = store.create("r", "t", {"type": "memory", "id": "dept/marketing"}, ALICE, {"op": "add"}, route="head+admin")
    out = store.decide_step(aid, False, "mhead@acme.com", as_level="head")
    assert out["status"] == "rejected" and out["level"] == "done"
    with pytest.raises(ValueError, match="already rejected"):
        store.decide_step(aid, True, "admin@acme.com", as_level="admin")


def test_tool_items_are_dept_owned_only_when_attributed(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    res = {"type": "tool", "id": "finance_transfer"}
    owned = store.get(store.create("r1", "t", res, CAROL, {"k": 1}, route="head+admin", dept="finance"))
    plain = store.get(store.create("r2", "t", res, CAROL, {"k": 2}))
    assert store.dept_of(owned) == "finance" and store.level_of(owned) == "head"
    assert store.dept_of(plain) is None and store.level_of(plain) == "admin"
    assert store.route_of(plain) == "admin-only"


# ================================================================ agent chain (Phase 1+2)


def test_head_only_dept_write_needs_just_the_head(env, org, dash):
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    assert res.interrupted and res.approval_tier == "dept"
    assert res.approval_level == "head" and res.approval_route == "head-only"
    # head queue only — never the admin queue
    assert [i["id"] for i in _queue(dash, "/api/dept/approvals/pending", "mhead@acme.com")] == [res.approval_id]
    assert _queue(dash, "/api/approvals/pending", "admin@acme.com") == []

    out = _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com").json()
    assert out["status"] == "approved" and out["needs_resume"] is True and out["next_level"] is None
    assert _queue(dash, "/api/approvals/pending", "admin@acme.com") == []  # log-only for admins
    env.resume(res.approval_id)
    assert DEPT_MSG in env.dept_facts("marketing")
    assert ("APPROVED", "head-hitl", "mhead@acme.com") in env.audit_for(res.run_id)


def test_head_plus_admin_chain_commits_only_after_both_steps(env, org, dash):
    org.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    assert res.interrupted and res.approval_route == "head+admin" and res.approval_level == "head"
    aid = res.approval_id

    head_q = _queue(dash, "/api/dept/approvals/pending", "mhead@acme.com")
    assert [(i["id"], i["level"], i["total_steps"], i["needs_admin"]) for i in head_q] == [(aid, "head", 2, False)]
    assert _queue(dash, "/api/approvals/pending", "admin@acme.com") == []

    # step 1: head approves -> still pending, now in the admin queue, nothing resumable
    step1 = _decide(dash, "/api/dept/approvals", aid, "mhead@acme.com", comment="fine by me").json()
    assert step1["status"] == "pending" and step1["next_level"] == "admin" and step1["needs_resume"] is False
    assert _queue(dash, "/api/dept/approvals/pending", "mhead@acme.com") == []
    admin_q = _queue(dash, "/api/approvals/pending", "admin@acme.com")
    assert len(admin_q) == 1 and admin_q[0]["id"] == aid and admin_q[0]["needs_admin"] is True
    assert admin_q[0]["steps"][0]["by"] == "mhead@acme.com" and admin_q[0]["steps"][0]["comment"] == "fine by me"
    assert dash.post(f"/api/approvals/{aid}/resume", headers=_auth("alice@acme.com")).status_code == 409
    assert env.dept_facts("marketing") == []
    # the owner's poll sees the item waiting on the admin, not yet resumable
    polled = _queue(dash, "/api/approvals/by-thread/user:alice@acme.com", "alice@acme.com")
    assert polled[0]["level"] == "admin" and polled[0]["needs_resume"] is False

    # step 2: admin approves -> owner resumes -> committed
    step2 = _decide(dash, "/api/approvals", aid, "admin@acme.com").json()
    assert step2["status"] == "approved" and step2["needs_resume"] is True
    assert _queue(dash, "/api/approvals/pending", "admin@acme.com") == []
    resumed = dash.post(f"/api/approvals/{aid}/resume", headers=_auth("alice@acme.com"))
    assert resumed.status_code == 200 and resumed.json()["interrupted"] is False
    assert DEPT_MSG in env.dept_facts("marketing")

    audit = env.audit_for(res.run_id)
    assert ("APPROVED", "head-hitl", "mhead@acme.com") in audit
    assert ("APPROVED", "admin-hitl", "admin@acme.com") in audit
    assert ("APPROVED", "dept-hitl", "alice@acme.com") in audit
    approved = [e for e in env.trace.get_run_tree(res.run_id)
                if e["type"] == "memory_write" and e["payload"].get("decision") == "approved"]
    assert [s["level"] for s in approved[0]["payload"]["steps"]] == ["head", "admin"]
    pending = [e for e in env.trace.get_run_tree(res.run_id)
               if e["type"] == "memory_write" and e["payload"].get("decision") == "pending"]
    assert pending[0]["payload"]["level"] == "head" and pending[0]["payload"]["route"] == "head+admin"


def test_admin_reject_at_step_two_never_commits(env, org, dash):
    org.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com")
    assert _decide(dash, "/api/approvals", res.approval_id, "admin@acme.com", approve=False).json()["status"] == "rejected"
    env.resume(res.approval_id)
    assert env.dept_facts("marketing") == []
    assert ("REJECTED", "admin-hitl", "admin@acme.com") in env.audit_for(res.run_id)


def test_head_reject_is_final_admin_cannot_revive(env, org, dash):
    org.set_department_mode("hr", "head+admin", actor="admin@acme.com")
    res = env.say(BOB, DEPT_MSG, "user:bob@acme.com")
    out = _decide(dash, "/api/dept/approvals", res.approval_id, "hhead@acme.com", approve=False).json()
    assert out["status"] == "rejected" and out["needs_resume"] is True
    assert _queue(dash, "/api/approvals/pending", "admin@acme.com") == []
    revive = _decide(dash, "/api/approvals", res.approval_id, "admin@acme.com", approve=True)
    assert revive.status_code == 409 and "already rejected" in revive.json()["detail"]
    env.resume(res.approval_id)
    assert env.dept_facts("hr") == []
    assert ("REJECTED", "head-hitl", "hhead@acme.com") in env.audit_for(res.run_id)


def test_mode_switch_does_not_reroute_items_in_flight(env, org, dash):
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")  # created head-only
    org.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    out = _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com").json()
    assert out["status"] == "approved"  # still one step
    later = env.say(MOLLY, DEPT_MSG, "user:molly@acme.com")  # created after the switch
    assert later.approval_route == "head+admin"


def test_no_active_head_goes_straight_to_admin(env, org, dash):
    for head in ("mhead@acme.com", "mhead2@acme.com"):
        org.set_active(head, False, actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    assert res.approval_route == "admin-only" and res.approval_level == "admin"
    q = _queue(dash, "/api/approvals/pending", "admin@acme.com")
    assert [i["id"] for i in q] == [res.approval_id] and q[0]["dept"] == "marketing"
    assert _decide(dash, "/api/approvals", res.approval_id, "admin@acme.com").json()["status"] == "approved"


def test_last_head_deactivated_escalates_a_waiting_item(env, org, dash):
    org.set_department_mode("hr", "head+admin", actor="admin@acme.com")
    res = env.say(BOB, DEPT_MSG, "user:bob@acme.com")
    assert _queue(dash, "/api/approvals/pending", "admin@acme.com") == []
    org.set_active("hhead@acme.com", False, actor="admin@acme.com")
    q = _queue(dash, "/api/approvals/pending", "admin@acme.com")
    assert [i["id"] for i in q] == [res.approval_id] and q[0]["level"] == "head"
    # the admin's decision resolves it outright (there is nobody to take step 1)
    assert _decide(dash, "/api/approvals", res.approval_id, "admin@acme.com").json()["status"] == "approved"


def test_suspended_reply_names_who_must_approve():
    from app.agent.runner import _suspended_reply

    assert "your department head and then an admin" in _suspended_reply("tool", "t", "a", "head+admin")
    head_only = _suspended_reply("dept", None, "a", "head-only")
    assert "your department head" in head_only and "admin" not in head_only
    assert "need an admin" in _suspended_reply("dept", None, "a", "admin-only")
    assert "your department head or an admin" in _suspended_reply("dept", None, "a")  # pre-Mk15 payload
    assert "need an admin" in _suspended_reply("central", None, "a")


# ---------------------------------------------------------------- dept tools


def test_critical_dept_tool_is_head_first_even_in_a_head_only_dept(env, org, dash, dept_apps):
    assert org.get_department("finance").approval_mode == "head-only"
    res = env.call(CAROL, "finance_transfer", TRANSFER, "user:carol@acme.com")
    assert res.interrupted and res.approval_tier == "tool"
    assert res.approval_level == "head" and res.approval_route == "head+admin"
    assert "your department head and then an admin" in res.reply
    aid = res.approval_id
    head_q = _queue(dash, "/api/dept/approvals/pending", "fhead@acme.com")
    assert [i["id"] for i in head_q] == [aid]
    assert head_q[0]["dept"] == "finance" and head_q[0]["tier"] == "tool"
    assert head_q[0]["allowlist_eligible"] is False and head_q[0]["action"] == "tool:finance_transfer"
    assert _queue(dash, "/api/approvals/pending", "admin@acme.com") == []
    assert _queue(dash, "/api/dept/approvals/pending", "mhead@acme.com") == []  # other dept's head

    # critical tools can never be "always allowed"
    bad = _decide(dash, "/api/dept/approvals", aid, "fhead@acme.com", always_allow=True)
    assert bad.status_code == 422 and env.approvals.get(aid)["status"] == "pending"

    assert _decide(dash, "/api/dept/approvals", aid, "fhead@acme.com").json()["next_level"] == "admin"
    from tests.attack.helpers import service_headers as _sh
    transfers = lambda: TestClient(dept_apps[8005]).get("/transfers", headers=_sh()).json()
    before = len(transfers())
    assert _decide(dash, "/api/approvals", aid, "admin@acme.com").json()["status"] == "approved"
    assert len(transfers()) == before  # decided, not yet executed: the owner resumes
    done = env.resume(aid)
    assert not done.interrupted and len(transfers()) == before + 1
    audit = env.audit_for(res.run_id)
    assert ("APPROVED", "head-hitl", "fhead@acme.com") in audit and ("APPROVED", "admin-hitl", "admin@acme.com") in audit


def test_head_rejected_tool_reports_the_head(env, org, dash, dept_apps):
    res = env.call(CAROL, "finance_transfer", TRANSFER, "user:carol@acme.com")
    _decide(dash, "/api/dept/approvals", res.approval_id, "fhead@acme.com", approve=False)
    done = env.resume(res.approval_id)
    assert "rejected by your department head" in done.tool_calls[-1]["content"]


def test_heads_own_critical_tool_skips_the_head_step(env, org, dash):
    res = env.call(FHEAD, "finance_transfer", {**TRANSFER, "actor": "fhead@acme.com"}, "user:fhead@acme.com")
    assert res.approval_route == "admin-only" and res.approval_level == "admin"
    assert _queue(dash, "/api/dept/approvals/pending", "fhead@acme.com") == []
    assert [i["id"] for i in _queue(dash, "/api/approvals/pending", "admin@acme.com")] == [res.approval_id]


def test_write_tools_are_gated_only_when_configured(env, org, dash, dept_apps):
    from tests.attack.helpers import service_headers as _sh
    campaigns = lambda: TestClient(dept_apps[8002]).get("/campaigns", headers=_sh()).json()
    before = len(campaigns())
    free = env.call(ALICE, "crm_create_campaign", CAMPAIGN, "user:alice@acme.com:a")
    assert not free.interrupted and len(campaigns()) == before + 1  # default: unchanged behaviour

    gated = Settings(approvals={"gate_write_tools": True})
    res = env.call(ALICE, "crm_create_campaign", CAMPAIGN, "user:alice@acme.com:b", settings=gated)
    assert res.interrupted and res.approval_route == "head-only" and len(campaigns()) == before + 1
    item = _queue(dash, "/api/dept/approvals/pending", "mhead@acme.com")[0]
    assert item["allowlist_eligible"] is True

    # head approves once and always-allows this exact call
    out = _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com", always_allow=True).json()
    assert out["status"] == "approved"
    env.resume(res.approval_id, settings=gated)
    assert len(campaigns()) == before + 2
    rules = org.allowlist_list("marketing")
    assert [(r["member"], r["action"], r["created_by"]) for r in rules] == [
        ("alice@acme.com", "tool:crm_create_campaign", "mhead@acme.com")]

    again = env.call(ALICE, "crm_create_campaign", CAMPAIGN, "user:alice@acme.com:c", settings=gated)
    assert not again.interrupted and len(campaigns()) == before + 3
    assert ("ALLOW", "allowlist", "alice@acme.com") in env.audit_for(again.run_id)
    # any changed argument is a different action
    tampered = env.call(ALICE, "crm_create_campaign", {**CAMPAIGN, "budget": 999999.0},
                        "user:alice@acme.com:d", settings=gated)
    assert tampered.interrupted and len(campaigns()) == before + 3


# ================================================================ allowlist (todo #11)


def _approve_always(env, dash, claims=ALICE, msg=DEPT_MSG, head="mhead@acme.com"):
    res = env.say(claims, msg, f"user:{claims['sub']}:seed")
    out = _decide(dash, "/api/dept/approvals", res.approval_id, head, always_allow=True)
    assert out.status_code == 200, out.text
    env.resume(res.approval_id)
    return res


def test_always_allow_then_exact_repeat_auto_commits(env, org, dash):
    _approve_always(env, dash)
    rules = _queue(dash, "/api/allowlist", "admin@acme.com")
    assert len(rules) == 1 and rules[0]["member"] == "alice@acme.com"
    assert rules[0]["action"] == "memory:dept/marketing:add" and rules[0]["dept"] == "marketing"
    assert rules[0]["args"]["text"] == DEPT_MSG and rules[0]["created_by"] == "mhead@acme.com"

    again = env.say(ALICE, DEPT_MSG, "user:alice@acme.com:2")
    assert not again.interrupted and env.approvals.pending_items() == []
    assert ("ALLOW", "allowlist", "alice@acme.com") in env.audit_for(again.run_id)
    allowed = [e["payload"] for e in env.trace.get_run_tree(again.run_id)
               if e["type"] == "memory_write" and e["payload"].get("reason") == "allowlist"]
    assert allowed and allowed[0]["args_hash"] == rules[0]["args_hash"]


def test_allowlist_is_exact_args_and_per_member(env, org, dash):
    _approve_always(env, dash)
    assert env.say(ALICE, OTHER_MSG, "user:alice@acme.com:2").interrupted  # different wording
    assert env.say(MOLLY, DEPT_MSG, "user:molly@acme.com").interrupted  # another member


def test_approve_once_does_not_create_a_rule(env, org, dash):
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com")
    env.resume(res.approval_id)
    assert org.allowlist_list() == []
    assert env.say(ALICE, DEPT_MSG, "user:alice@acme.com:2").interrupted


def test_always_allow_takes_effect_only_after_the_admin_step(env, org, dash):
    org.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com", always_allow=True)
    assert org.allowlist_list() == []  # head ticked it, but the item is not approved yet
    _decide(dash, "/api/approvals", res.approval_id, "admin@acme.com")
    assert [r["created_by"] for r in org.allowlist_list()] == ["mhead@acme.com"]
    env.resume(res.approval_id)
    assert not env.say(ALICE, DEPT_MSG, "user:alice@acme.com:2").interrupted


def test_always_allow_is_dropped_when_the_admin_rejects(env, org, dash):
    org.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com", always_allow=True)
    _decide(dash, "/api/approvals", res.approval_id, "admin@acme.com", approve=False)
    assert org.allowlist_list() == []


def test_always_allow_is_422_when_ineligible(env, org, dash, tmp_path):
    central = env.approvals.create("r", "t", {"type": "memory", "id": "central"},
                                   {"sub": "admin@acme.com", "role": "admin", "dept": "admin"},
                                   {"op": "add", "text": "x"}, route="admin-only", level="admin",
                                   action="memory:central:add", args_hash="h")
    resp = _decide(dash, "/api/approvals", central, "admin@acme.com", always_allow=True)
    assert resp.status_code == 422 and env.approvals.get(central)["status"] == "pending"
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    reject = _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com", approve=False, always_allow=True)
    assert reject.status_code == 422


def test_revoke_makes_the_next_request_suspend_again(env, org, dash):
    _approve_always(env, dash)
    rule = org.allowlist_list()[0]
    path = f"/api/allowlist/{rule['member']}/{rule['action']}/{rule['args_hash']}"
    assert dash.delete(path, headers=_auth("hhead@acme.com")).status_code == 403  # another dept's head
    assert dash.delete(path, headers=_auth("alice@acme.com")).status_code == 403  # a member
    assert dash.delete(path, headers=_auth("admin@acme.com")).json()["revoked"] is True
    assert dash.delete(path, headers=_auth("admin@acme.com")).status_code == 404
    assert env.say(ALICE, DEPT_MSG, "user:alice@acme.com:2").interrupted


def test_allowlist_listing_is_scoped(env, org, dash):
    _approve_always(env, dash)
    _approve_always(env, dash, claims=BOB, head="hhead@acme.com")
    assert len(_queue(dash, "/api/allowlist", "admin@acme.com")) == 2
    assert [r["dept"] for r in _queue(dash, "/api/allowlist?dept=hr", "admin@acme.com")] == ["hr"]
    assert [r["member"] for r in _queue(dash, "/api/allowlist", "mhead@acme.com")] == ["alice@acme.com"]
    # a head cannot widen their view with ?dept=
    assert [r["dept"] for r in _queue(dash, "/api/allowlist?dept=hr", "mhead@acme.com")] == ["marketing"]
    assert dash.get("/api/allowlist", headers=_auth("alice@acme.com")).status_code == 403
    own = org.allowlist_list("marketing")[0]
    head_revoke = dash.delete(
        f"/api/allowlist/{own['member']}/{own['action']}/{own['args_hash']}", headers=_auth("mhead@acme.com"))
    assert head_revoke.status_code == 200 and org.allowlist_list("marketing") == []


# ================================================================ departments API


def test_departments_list_and_mode_switch(org, dash):
    depts = {d["name"]: d for d in _queue(dash, "/api/departments", "admin@acme.com")}
    assert depts["marketing"]["approval_mode"] == "head-only" and depts["marketing"]["head_count"] == 2
    assert depts["devops"]["head_count"] == 0
    assert {"alice@acme.com", "molly@acme.com", "mhead@acme.com"} <= set(depts["marketing"]["members"])
    resp = dash.patch("/api/departments/marketing", json={"approval_mode": "head+admin"}, headers=_auth("admin@acme.com"))
    assert resp.status_code == 200 and resp.json()["approval_mode"] == "head+admin"
    assert org.department_mode("marketing") == "head+admin"
    assert dash.patch("/api/departments/marketing", json={"approval_mode": "x"}, headers=_auth("admin@acme.com")).status_code == 422
    assert dash.patch("/api/departments/nope", json={"approval_mode": "head-only"}, headers=_auth("admin@acme.com")).status_code == 404
    assert dash.patch("/api/departments/marketing", json={"approval_mode": "head-only"}, headers=_auth("mhead@acme.com")).status_code == 403


def test_departments_list_reads_members_from_the_store(org, dash):
    org.create("zed@acme.com", "zed12345", role="hr_agent", dept="hr")
    depts = {d["name"]: d for d in _queue(dash, "/api/departments", "admin@acme.com")}
    assert "zed@acme.com" in depts["hr"]["members"]


# ================================================================ head: roster + scoped members


def test_head_roster_is_own_department_only(org, dash):
    roster = _queue(dash, "/api/dept/members", "mhead@acme.com")
    assert {m["username"] for m in roster} == {"alice@acme.com", "molly@acme.com", "mhead@acme.com", "mhead2@acme.com"}
    assert set(roster[0]) == {"username", "role", "active", "created_at"}
    assert dash.get("/api/dept/members", headers=_auth("alice@acme.com")).status_code == 403
    assert dash.get("/api/dept/members", headers=_auth("admin@acme.com")).status_code == 403


def test_head_adds_a_member_to_own_department_only(org, dash):
    head = _auth("mhead@acme.com")
    ok = dash.post("/api/dept/members", json={"username": "nina@acme.com", "password": "nina1234"}, headers=head)
    assert ok.status_code == 200 and ok.json()["role"] == "marketing_agent"
    nina = org.get("nina@acme.com")
    assert nina.dept == "marketing" and nina.scopes == org.get("alice@acme.com").scopes
    assert org.verify("nina@acme.com", "nina1234")
    for role in ("marketing_head", "admin", "hr_agent"):
        resp = dash.post("/api/dept/members", json={"username": f"x-{role}@acme.com", "password": "pw123456", "role": role}, headers=head)
        assert resp.status_code == 403, role
        assert org.get(f"x-{role}@acme.com") is None
    assert dash.post("/api/dept/members", json={"username": "nina@acme.com", "password": "nina1234"}, headers=head).status_code == 409
    assert dash.post("/api/dept/members", json={"username": "s@acme.com", "password": "x"}, headers=head).status_code == 422


def test_head_deactivates_and_resets_own_members_only(org, dash):
    head = _auth("mhead@acme.com")
    off = dash.patch("/api/dept/members/molly@acme.com", json={"active": False}, headers=head)
    assert off.status_code == 200 and off.json()["active"] is False and not org.is_active("molly@acme.com")
    on = dash.patch("/api/dept/members/molly@acme.com", json={"active": True, "password_reset": "fresh123"}, headers=head)
    assert on.status_code == 200 and org.verify("molly@acme.com", "fresh123")
    assert org.get("molly@acme.com").role == "marketing_agent"  # no role/dept changes possible
    assert dash.patch("/api/dept/members/bob@acme.com", json={"active": False}, headers=head).status_code == 403
    assert dash.patch("/api/dept/members/mhead2@acme.com", json={"active": False}, headers=head).status_code == 403
    assert dash.patch("/api/dept/members/mhead@acme.com", json={"active": False}, headers=head).status_code == 403
    assert dash.patch("/api/dept/members/admin@acme.com", json={"password_reset": "hacked12"}, headers=head).status_code == 403
    assert org.is_active("bob@acme.com") and org.verify("admin@acme.com", "admin123")
    assert dash.patch("/api/dept/members/ghost@acme.com", json={"active": False}, headers=head).status_code == 404
    assert dash.patch("/api/dept/members/molly@acme.com", json={}, headers=head).status_code == 422
    assert dash.patch("/api/dept/members/molly@acme.com", json={"password_reset": "x"}, headers=head).status_code == 422


def test_head_member_changes_rebuild_the_enforcer(org, dash, enforcer):
    assert enforcer.memory_allowed("dept/marketing", "read", MOLLY) is True
    dash.patch("/api/dept/members/molly@acme.com", json={"active": False}, headers=_auth("mhead@acme.com"))
    assert enforcer.memory_allowed("dept/marketing", "read", MOLLY) is False


# ================================================================ head: dept memory + skills


def test_head_edits_own_dept_memory_only(org, dash, tmp_path, monkeypatch, enforcer, audit_log):
    import app.dashboard.app as dash_mod
    from app.memory.edit import MemoryEditor

    store = SqliteBaseStore(tmp_path / "mem.sqlite")
    monkeypatch.setattr(dash_mod, "_get_components", lambda: {
        "store": store, "audit": audit_log,
        "memory_editor": MemoryEditor(store, get_settings().memory),
        "recall": RecallService(EpisodicStore(tmp_path / "mem.sqlite"), enforcer, get_settings().memory),
    })
    head = _auth("mhead@acme.com")
    added = dash.post("/api/memory/dept/marketing", json={"text": "Campaigns ship on Tuesdays"}, headers=head)
    assert added.status_code == 200
    items = store.items_in(("dept", "marketing"))
    assert [str(i.value["text"]) for i in items] == ["Campaigns ship on Tuesdays"]
    assert audit_log.entries()[-1]["reason"].startswith("head:")
    key = items[0].key
    assert dash.put(f"/api/memory/dept/marketing/{key}", json={"text": "Campaigns ship on Mondays"}, headers=head).status_code == 200
    mine = _queue(dash, "/api/memory/me", "mhead@acme.com")
    assert "Campaigns ship on Mondays" in [i["text"] for s in mine if s["scope"] == "dept/marketing" for i in s["items"]]
    assert dash.delete(f"/api/memory/dept/marketing/{key}", headers=head).status_code == 200
    assert store.items_in(("dept", "marketing")) == []
    for scope in ("dept/hr", "central", "user/alice@acme.com"):
        assert dash.post(f"/api/memory/{scope}", json={"text": "nope"}, headers=head).status_code == 403, scope
    assert dash.post("/api/memory/dept/marketing", json={"text": "x"}, headers=_auth("alice@acme.com")).status_code == 403
    assert dash.get("/api/memory", headers=head).status_code == 403  # the full browser stays admin-only


def test_head_sees_and_edits_own_dept_skills_only(org, dash, tmp_path, monkeypatch, audit_log):
    import shutil

    import app.dashboard.app as dash_mod
    from app.skills.registry import SkillRegistry

    root = tmp_path / "skills"
    shutil.copytree(get_settings().skills.skills_dir, root)
    registry = SkillRegistry(root)
    monkeypatch.setattr(dash_mod, "_get_components", lambda: {"skills": registry, "audit": audit_log})
    monkeypatch.setattr(dash_mod, "_granted_skill_objs", lambda: set())
    head = _auth("mhead@acme.com")
    names = {s["name"] for s in dash.get("/api/dept/skills", headers=head).json()["skills"]}
    assert "launch_campaign" in names and "new_leave_request" not in names and "triage_incident" not in names
    content = dash.get("/api/dept/skills/launch_campaign", headers=head).json()["content"]
    assert dash.get("/api/dept/skills/new_leave_request", headers=head).status_code == 403
    assert dash.get("/api/dept/skills/triage_incident", headers=head).status_code == 403
    assert dash.get("/api/dept/skills/nope", headers=head).status_code == 404
    edited = content.replace("version: 1", "version: 2")
    assert dash.put("/api/dept/skills/launch_campaign", json={"content": edited}, headers=head).status_code == 200
    assert str(registry.get("launch_campaign").version) == "2"
    moved = content.replace("scope: dept/marketing", "scope: central")
    assert dash.put("/api/dept/skills/launch_campaign", json={"content": moved}, headers=head).status_code == 403
    assert dash.put("/api/dept/skills/new_leave_request", json={"content": content}, headers=head).status_code == 403
    assert dash.get("/api/dept/skills", headers=_auth("alice@acme.com")).status_code == 403
    assert dash.get("/api/skills", headers=head).status_code == 403  # the full registry stays admin-only


# ================================================================ head decide scoping


def test_head_cannot_decide_another_departments_item_or_an_admin_step(env, org, dash):
    org.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    assert _decide(dash, "/api/dept/approvals", res.approval_id, "hhead@acme.com").status_code == 403
    _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com")
    # the head already took step 1: the admin step is not theirs (nor another head's)
    again = _decide(dash, "/api/dept/approvals", res.approval_id, "mhead2@acme.com")
    assert again.status_code == 409 and "waiting on an admin" in again.json()["detail"]
    assert env.approvals.get(res.approval_id)["status"] == "pending"
    assert _decide(dash, "/api/dept/approvals", res.approval_id, "alice@acme.com").status_code == 403
    assert _decide(dash, "/api/approvals", res.approval_id, "mhead@acme.com").status_code == 403


def test_coupled_decide_resumes_only_at_the_final_step(env, org, dash):
    org.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    step1 = _decide(dash, "/api/dept/approvals", res.approval_id, "mhead@acme.com", defer=False).json()
    assert step1["status"] == "pending" and step1["next_level"] == "admin" and step1["reply"] is None
    assert env.dept_facts("marketing") == []
    step2 = _decide(dash, "/api/approvals", res.approval_id, "admin@acme.com", defer=False).json()
    assert step2["status"] == "approved" and step2["interrupted"] is False
    assert DEPT_MSG in env.dept_facts("marketing")


# ================================================================ gateway head twin


@pytest.fixture
def gw(env):
    gateway_app.dependency_overrides[gw_get_approval_store] = lambda: env.approvals
    gateway_app.dependency_overrides[gw_get_resumer] = lambda: env.resume
    yield TestClient(gateway_app)
    gateway_app.dependency_overrides.clear()


def _gw_decide(gw, aid, user, **body):
    return gw.post(f"/approvals/{aid}/decide", json={"approve": True, "token": _token(user), "defer": True, **body})


def test_gateway_head_decide_twin(env, org, gw):
    org.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "user:alice@acme.com")
    aid = res.approval_id
    assert _gw_decide(gw, aid, "alice@acme.com").status_code == 403  # members never decide
    assert _gw_decide(gw, aid, "hhead@acme.com").status_code == 403  # another dept's head
    step1 = _gw_decide(gw, aid, "mhead@acme.com", always_allow=True, comment="ok")
    assert step1.status_code == 200 and step1.json()["status"] == "pending" and step1.json()["next_level"] == "admin"
    assert _gw_decide(gw, aid, "mhead@acme.com").status_code == 409  # not the head's step any more
    assert gw.post(f"/approvals/{aid}/resume", json={"token": _token("alice@acme.com")}).status_code == 409
    step2 = _gw_decide(gw, aid, "admin@acme.com")
    assert step2.status_code == 200 and step2.json()["status"] == "approved" and step2.json()["needs_resume"] is True
    assert [r["member"] for r in org.allowlist_list("marketing")] == ["alice@acme.com"]
    resumed = gw.post(f"/approvals/{aid}/resume", json={"token": _token("alice@acme.com")})
    assert resumed.status_code == 200 and DEPT_MSG in env.dept_facts("marketing")


def test_gateway_head_reject_is_final(env, org, gw):
    res = env.call(CAROL, "finance_transfer", TRANSFER, "user:carol@acme.com")
    out = gw.post(f"/approvals/{res.approval_id}/decide",
                  json={"approve": False, "token": _token("fhead@acme.com"), "defer": True})
    assert out.status_code == 200 and out.json()["status"] == "rejected"
    assert _gw_decide(gw, res.approval_id, "admin@acme.com").status_code == 409
    bad = gw.post(f"/approvals/{res.approval_id}/decide",
                  json={"approve": True, "token": _token("fhead@acme.com"), "always_allow": True})
    assert bad.status_code == 409  # already decided (and a critical tool is never always-allowable)


def test_chat_envelope_carries_level_and_route(org):
    from app.gateway.app import get_runner

    def runner(claims, message, thread_id):
        return AgentResult(reply="waiting", run_id="r", guardrail_hits=[], tool_calls=[], thread_id="t",
                           interrupted=True, approval_id="a1", approval_tier="dept",
                           approval_level="head", approval_route="head+admin")

    gateway_app.dependency_overrides[get_runner] = lambda: runner
    try:
        body = TestClient(gateway_app).post("/chat", json={"token": _token("alice@acme.com"), "message": "hi"}).json()
    finally:
        gateway_app.dependency_overrides.clear()
    assert body["approval_level"] == "head" and body["approval_route"] == "head+admin"

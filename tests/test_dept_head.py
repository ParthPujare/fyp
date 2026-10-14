"""Department Head role (Mk12, todo #2): `{dept}_head` owns dept-tier HITL approvals.

Offline (MockLLM, tmp SQLite, isolated MemberStore via conftest `member_store`).
"""

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import AgentResult
from app.agent.tools import TOOL_REGISTRY
from app.auth.jwt import create_token
from app.auth.store import MemberError
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_approval_store, get_resumer
from app.harness.enforcer import dept_of_scope, head_role_for
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.scopes import scope_dept

ALICE = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
MHEAD = {"sub": "mhead@acme.com", "role": "marketing_head", "dept": "marketing"}
HHEAD = {"sub": "hhead@acme.com", "role": "hr_head", "dept": "hr"}
ADMIN = {"sub": "admin@acme.com", "role": "admin", "dept": "admin"}

DEPT_MSG = "Our team prefers morning standups."


@pytest.fixture
def _heads(member_store):
    member_store.create("mhead@acme.com", "head1234", role="marketing_head", dept="marketing")
    member_store.create("hhead@acme.com", "head1234", role="hr_head", dept="hr")
    return member_store


def _head_token(username: str) -> str:
    settings = get_settings()
    return create_token(
        USERS[username], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )


def _dept_runner(runner_factory, memory_store, episodic, approvals, enforcer, claims, message):
    recall_kwargs = {}
    from app.memory.recall import RecallService

    recall = RecallService(episodic, enforcer, get_settings().memory)
    runner = runner_factory(
        provider=MockLLM(script={message: {"text": "ok", "tool_calls": []}}),
        store=memory_store,
        episodic=episodic,
        approvals=approvals,
        recall=recall,
        checkpointer=InMemorySaver(),
        **recall_kwargs,
    )
    return runner(claims, message, thread_id=f"t-{claims['sub']}-{len(message)}")


# --- helpers ---


def test_head_role_for_and_dept_of_scope():
    assert head_role_for("marketing") == "marketing_head"
    assert dept_of_scope("dept/marketing") == "marketing"
    assert dept_of_scope("central") is None
    assert dept_of_scope("user/a@b.com") is None


def test_is_dept_head(enforcer, _heads):
    assert enforcer.is_dept_head(MHEAD, scope_dept("marketing")) is True
    assert enforcer.is_dept_head(MHEAD, scope_dept("hr")) is False
    assert enforcer.is_dept_head(ALICE, scope_dept("marketing")) is False
    assert enforcer.is_dept_head(MHEAD, "central") is False
    assert enforcer.is_dept_head(ADMIN, scope_dept("marketing")) is False


# --- policy ---


def test_head_memory_policy(enforcer, _heads):
    assert enforcer.memory_allowed("dept/marketing", "read", MHEAD) is True
    assert enforcer.memory_allowed("dept/marketing", "write", MHEAD) is True
    assert enforcer.memory_allowed("dept/hr", "write", MHEAD) is False
    assert enforcer.memory_allowed("central", "read", MHEAD) is True
    assert enforcer.memory_allowed("central", "write", MHEAD) is False


def test_head_tool_access_mirrors_member(enforcer, _heads):
    assert enforcer.tool_allowed(TOOL_REGISTRY["crm_search_customer"], MHEAD) is True
    assert enforcer.tool_allowed(TOOL_REGISTRY["hr_search_employees"], MHEAD) is False
    assert enforcer.tool_allowed(TOOL_REGISTRY["hr_search_employees"], HHEAD) is True


# --- identity validation ---


def test_member_store_rejects_mismatched_head_role(member_store):
    with pytest.raises(MemberError, match="head role"):
        member_store.create("x@acme.com", "pw12345", role="marketing_head", dept="hr")
    member_store.create("x@acme.com", "pw12345", role="marketing_head", dept="marketing")
    with pytest.raises(MemberError, match="head role"):
        member_store.update("x@acme.com", role="hr_head", dept="marketing", actor="admin@acme.com")


# --- approvals helpers ---


def test_approvals_dept_of_and_pending_for_dept(tmp_path):
    approvals = ApprovalStore(tmp_path / "memory.sqlite")
    assert ApprovalStore.dept_of({"resource": {"type": "tool", "id": "x"}}) is None
    assert ApprovalStore.dept_of({"resource": {"type": "memory", "id": "central"}}) is None
    d1 = approvals.create(
        "r1", "t1", {"type": "memory", "id": "dept/marketing"}, ALICE, {"op": "add", "text": "x"}
    )
    approvals.create(
        "r2", "t2", {"type": "memory", "id": "dept/hr"},
        {"sub": "b", "role": "hr_agent", "dept": "hr"}, {"op": "add", "text": "y"},
    )
    got = approvals.pending_for_dept("marketing")
    assert [i["id"] for i in got] == [d1]


# --- reflect gate ---


def test_member_dept_write_suspends(runner_factory, memory_store, episodic, approvals, enforcer, _heads):
    res = _dept_runner(runner_factory, memory_store, episodic, approvals, enforcer, ALICE, DEPT_MSG)
    assert res.interrupted and res.approval_id is not None
    pending = approvals.pending_for_dept("marketing")
    assert len(pending) == 1 and pending[0]["id"] == res.approval_id
    assert ApprovalStore.dept_of(pending[0]) == "marketing"
    # not committed until approved
    assert memory_store.items_in(("dept", "marketing")) == []


def test_head_own_dept_write_auto_commits(
    runner_factory, memory_store, episodic, approvals, enforcer, _heads
):
    # Mk18 Phase E: a head's own dept write suspends for an admin (no more
    # self-approval) — nothing commits until the admin step decides.
    res = _dept_runner(runner_factory, memory_store, episodic, approvals, enforcer, MHEAD, DEPT_MSG)
    assert res.interrupted
    assert len(approvals.pending_items()) == 1
    items = memory_store.items_in(("dept", "marketing"))
    assert not any("standups" in str(i.value.get("text", "")) for i in items)


def test_head_approve_resume_commits(
    runner_factory, memory_store, episodic, approvals, enforcer, executor, audit_log, _heads
):
    from app.agent.runner import resume_agent
    from app.memory.recall import RecallService

    checkpointer = InMemorySaver()
    recall = RecallService(episodic, enforcer, get_settings().memory)
    runner = runner_factory(
        provider=MockLLM(script={DEPT_MSG: {"text": "ok", "tool_calls": []}}),
        store=memory_store,
        episodic=episodic,
        approvals=approvals,
        recall=recall,
        checkpointer=checkpointer,
    )
    res = runner(ALICE, DEPT_MSG, thread_id="t-mkt-approve")
    assert res.interrupted
    approvals.decide(res.approval_id, True, "mhead@acme.com")
    resume_agent(
        res.approval_id,
        provider=MockLLM({"x": {"text": "ok", "tool_calls": []}}),
        executor=executor,
        enforcer=enforcer,
        audit=audit_log,
        store=memory_store,
        episodic=episodic,
        approvals=approvals,
        recall=recall,
        checkpointer=checkpointer,
    )
    items = memory_store.items_in(("dept", "marketing"))
    assert any("standups" in str(i.value.get("text", "")) for i in items)
    assert any(e["decision"] == "APPROVED" and e["reason"] == "dept-hitl" for e in audit_log.entries())


# --- dashboard ---


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    dashboard_app.dependency_overrides.clear()


def _mk_dashboard_approval(approvals: ApprovalStore) -> str:
    return approvals.create(
        "r1", "t1", {"type": "memory", "id": "dept/marketing"}, ALICE, {"op": "add", "text": DEPT_MSG}
    )


def test_dept_pending_requires_head(approvals, _heads):
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    client = TestClient(dashboard_app)
    assert client.get("/api/dept/approvals/pending").status_code == 401
    admin = _head_token("admin@acme.com")
    assert client.get(
        "/api/dept/approvals/pending", headers={"Authorization": f"Bearer {admin}"}
    ).status_code == 403
    alice = _head_token("alice@acme.com")
    assert client.get(
        "/api/dept/approvals/pending", headers={"Authorization": f"Bearer {alice}"}
    ).status_code == 403


def test_head_sees_own_dept_only(approvals, _heads):
    _mk_dashboard_approval(approvals)
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    client = TestClient(dashboard_app)
    mhead = _head_token("mhead@acme.com")
    items = client.get(
        "/api/dept/approvals/pending", headers={"Authorization": f"Bearer {mhead}"}
    ).json()
    assert len(items) == 1 and items[0]["dept"] == "marketing" and items[0]["tier"] == "dept"
    hhead = _head_token("hhead@acme.com")
    assert client.get(
        "/api/dept/approvals/pending", headers={"Authorization": f"Bearer {hhead}"}
    ).json() == []


def test_head_decide_cross_dept_forbidden(approvals, _heads):
    aid = _mk_dashboard_approval(approvals)
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    client = TestClient(dashboard_app)
    hhead = _head_token("hhead@acme.com")
    resp = client.post(
        f"/api/dept/approvals/{aid}/decide",
        json={"approve": True},
        headers={"Authorization": f"Bearer {hhead}"},
    )
    assert resp.status_code == 403


def test_head_decide_resumes_run(approvals, _heads):
    aid = _mk_dashboard_approval(approvals)

    def fake_resumer(approval_id):
        assert approval_id == aid
        return AgentResult(reply="resumed!", run_id="r1", guardrail_hits=[], tool_calls=[], thread_id="t1")

    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    dashboard_app.dependency_overrides[get_resumer] = lambda: fake_resumer
    client = TestClient(dashboard_app)
    mhead = _head_token("mhead@acme.com")
    resp = client.post(
        f"/api/dept/approvals/{aid}/decide",
        json={"approve": True},
        headers={"Authorization": f"Bearer {mhead}"},
    )
    assert resp.status_code == 200 and resp.json()["status"] == "approved"
    assert approvals.get(aid)["decided_by"] == "mhead@acme.com"


def test_admin_sees_escalated_only(approvals, _heads, member_store):
    _mk_dashboard_approval(approvals)
    approvals.create(
        "r2", "t2", {"type": "memory", "id": "dept/finance"},
        {"sub": "c", "role": "finance_agent", "dept": "finance"}, {"op": "add", "text": "z"},
    )
    dashboard_app.dependency_overrides[get_approval_store] = lambda: approvals
    client = TestClient(dashboard_app)
    admin = _head_token("admin@acme.com")
    # marketing has an active head -> hidden; finance has none -> escalated, visible
    items = client.get("/api/approvals/pending", headers={"Authorization": f"Bearer {admin}"}).json()
    assert [i["resource"]["id"] for i in items] == ["dept/finance"]
    # deactivate the marketing head -> its item escalates to admin
    member_store.set_active("mhead@acme.com", False, actor="admin@acme.com")
    items = client.get("/api/approvals/pending", headers={"Authorization": f"Bearer {admin}"}).json()
    assert sorted(i["resource"]["id"] for i in items) == ["dept/finance", "dept/marketing"]

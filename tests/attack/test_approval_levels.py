"""Attack-suite deltas for two-level approvals + the allowlist (Mk15).

Races: two heads on step 1, two admins on step 2, a double owner resume — exactly one
winner each. Bypass attempts on the allowlist: cross-member, tampered args, a forged rule
for a critical tool or central memory, a member minting their own rule.
"""

from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.routing import KIND_MEMORY, KIND_TOOL, action_key_for, allowlist_payload, args_hash
from app.agent.runner import resume_agent, run_agent
from app.auth.jwt import create_token
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.dashboard.app import get_approval_store, get_resumer
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from tests.attack.helpers import ALICE, CAROL

DEPT_RES = {"type": "memory", "id": "dept/marketing"}
CENTRAL_MSG = "Company policy requires an immediate hiring freeze."
DEPT_MSG = "Our team prefers morning standups."
TRANSFER = {"from_account": "1000", "to_account": "2000", "amount": 1.0,
            "memo": "m", "actor": "carol@acme.com", "status": "pending"}


def _auth(username: str) -> dict[str, str]:
    s = get_settings()
    return {"Authorization": "Bearer " + create_token(USERS[username], s.jwt.secret, s.jwt.algorithm, 60)}


def _race(fn, n=2):
    """Run `fn(i)` on n threads at once; returns the list of results / raised exceptions."""
    barrier, out = threading.Barrier(n), [None] * n

    def worker(i):
        barrier.wait()
        try:
            out[i] = fn(i)
        except Exception as exc:  # noqa: BLE001 - the loser's error IS the result under test
            out[i] = exc

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return out


# ---------------------------------------------------------------- races


def test_two_heads_racing_step_one_have_one_winner(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    for _ in range(10):
        aid = store.create(f"r{_}", "t", DEPT_RES, ALICE, {"op": "add", "text": str(_)}, route="head+admin")
        out = _race(lambda i, aid=aid: store.decide_step(aid, True, f"head{i}@acme.com", as_level="head"))
        winners = [o for o in out if isinstance(o, dict)]
        losers = [o for o in out if isinstance(o, ValueError)]
        assert len(winners) == 1 and len(losers) == 1, out
        entry = store.get(aid)
        assert entry["status"] == "pending" and entry["level"] == "admin" and len(entry["steps"]) == 1


def test_two_admins_racing_step_two_have_one_winner(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    for n in range(10):
        aid = store.create(f"r{n}", "t", DEPT_RES, ALICE, {"op": "add", "text": str(n)}, route="head+admin")
        store.decide_step(aid, True, "mhead@acme.com", as_level="head")
        # one approves, one rejects: whoever wins, the outcome is a single decision
        out = _race(lambda i, aid=aid: store.decide_step(aid, i == 0, f"admin{i}@acme.com", as_level="admin"))
        assert len([o for o in out if isinstance(o, dict)]) == 1 and len([o for o in out if isinstance(o, ValueError)]) == 1
        entry = store.get(aid)
        assert entry["status"] in ("approved", "rejected") and len(entry["steps"]) == 2
        assert entry["steps"][1]["by"] == entry["decided_by"]


def test_head_and_admin_racing_an_escalated_item_have_one_winner(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    for n in range(10):
        aid = store.create(f"r{n}", "t", DEPT_RES, ALICE, {"op": "add", "text": str(n)}, route="head+admin")
        out = _race(lambda i, aid=aid: store.decide_step(
            aid, True, "x@acme.com", as_level="head" if i == 0 else "admin"))
        assert len([o for o in out if isinstance(o, dict)]) == 1, out
        assert len(store.get(aid)["steps"]) == 1


def test_double_owner_resume_has_one_winner(tmp_path):
    store = ApprovalStore(tmp_path / "a.sqlite")
    aid = store.create("r", "t", DEPT_RES, ALICE, {"op": "add", "text": "x"}, route="head+admin")
    store.decide_step(aid, True, "mhead@acme.com", as_level="head")
    with pytest.raises(ValueError, match="has not been decided"):
        store.claim_resume(aid, ALICE["sub"])  # step 1 alone is not resumable
    store.decide_step(aid, True, "admin@acme.com", as_level="admin")
    out = _race(lambda _i: store.claim_resume(aid, ALICE["sub"]), n=4)
    assert len([o for o in out if isinstance(o, dict)]) == 1
    assert all("already resumed" in str(o) for o in out if isinstance(o, ValueError))


# ---------------------------------------------------------------- allowlist bypass


class Env:
    def __init__(self, tmp_path, enforcer, executor, audit_log):
        db = tmp_path / "m.sqlite"
        self.store, self.episodic, self.approvals = SqliteBaseStore(db), EpisodicStore(db), ApprovalStore(db)
        self.kw = {"store": self.store, "episodic": self.episodic, "approvals": self.approvals,
                   "recall": RecallService(self.episodic, enforcer, get_settings().memory),
                   "trace": TraceStore(tmp_path / "t.sqlite"), "checkpointer": InMemorySaver(),
                   "enforcer": enforcer, "audit": audit_log, "executor": executor}

    def say(self, claims, msg, thread_id):
        provider = MockLLM(script={msg: {"text": "ok", "tool_calls": []}})
        return run_agent(claims, msg, provider=provider, thread_id=thread_id, **self.kw)

    def call(self, claims, tool, args, thread_id):
        provider = MockLLM(sequence=[{"text": "calling", "tool_calls": [{"name": tool, "args": args}]}])
        return run_agent(claims, "do it", provider=provider, thread_id=thread_id, **self.kw)


@pytest.fixture
def env(tmp_path, enforcer, executor, audit_log, member_store):
    member_store.create("mhead@acme.com", "head1234", role="marketing_head", dept="marketing")
    member_store.create("fhead@acme.com", "head1234", role="finance_head", dept="finance")
    member_store.create("molly@acme.com", "molly123", role="marketing_agent", dept="marketing")
    return Env(tmp_path, enforcer, executor, audit_log)


def _memory_rule(member_store, member, text):
    payload = {"op": "add", "text": text, "match": None}
    member_store.allowlist_add(
        member, action_key_for(KIND_MEMORY, scope="dept/marketing", op="add"),
        args_hash(allowlist_payload(KIND_MEMORY, payload)), payload, "marketing", actor="mhead@acme.com")


def test_another_members_rule_does_not_cover_you(env, member_store):
    _memory_rule(member_store, "molly@acme.com", DEPT_MSG)
    assert env.say(ALICE, DEPT_MSG, "t-a").interrupted
    molly = {"sub": "molly@acme.com", "role": "marketing_agent", "dept": "marketing", "scopes": []}
    assert not env.say(molly, DEPT_MSG, "t-m").interrupted


def test_changed_args_do_not_match_a_rule(env, member_store):
    _memory_rule(member_store, ALICE["sub"], DEPT_MSG)
    assert not env.say(ALICE, DEPT_MSG, "t-1").interrupted
    for tampered in (DEPT_MSG[:-1] + ", always.", DEPT_MSG.lower(), DEPT_MSG.replace("morning", "evening")):
        assert env.say(ALICE, tampered, f"t-{len(tampered)}-{tampered[-3:]}").interrupted, tampered


def test_a_forged_rule_cannot_waive_a_critical_tool(env, member_store, dept_apps):
    from tests.attack.helpers import service_headers as _sh

    payload = {"tool": "finance_transfer", "args": TRANSFER}
    member_store.allowlist_add(
        CAROL["sub"], action_key_for(KIND_TOOL, tool="finance_transfer"),
        args_hash(allowlist_payload(KIND_TOOL, payload)), payload, "finance", actor="fhead@acme.com")
    before = len(TestClient(dept_apps[8005]).get("/transfers", headers=_sh()).json())
    res = env.call(CAROL, "finance_transfer", TRANSFER, "t-c")
    assert res.interrupted and res.approval_route == "head+admin"
    assert len(TestClient(dept_apps[8005]).get("/transfers", headers=_sh()).json()) == before


def test_a_forged_rule_cannot_waive_a_central_write(env, member_store):
    admin = {"sub": "admin@acme.com", "role": "admin", "dept": "admin", "scopes": ["*"]}
    payload = {"op": "add", "text": CENTRAL_MSG, "match": None}
    member_store.allowlist_add(
        admin["sub"], action_key_for(KIND_MEMORY, scope="central", op="add"),
        args_hash(allowlist_payload(KIND_MEMORY, payload)), payload, "admin", actor="admin@acme.com")
    res = env.say(admin, CENTRAL_MSG, "t-central")
    assert res.interrupted and res.approval_route == "admin-only"


def test_members_cannot_mint_read_or_revoke_rules(env, member_store):
    _memory_rule(member_store, ALICE["sub"], DEPT_MSG)
    rule = member_store.allowlist_list()[0]
    client = TestClient(dashboard_app)
    alice = _auth("alice@acme.com")
    assert client.get("/api/allowlist", headers=alice).status_code == 403
    path = f"/api/allowlist/{rule['member']}/{rule['action']}/{rule['args_hash']}"
    assert client.delete(path, headers=alice).status_code == 403
    assert len(member_store.allowlist_list()) == 1
    # a member cannot decide their own item (so cannot tick "always allow" either)
    res = env.say(ALICE, DEPT_MSG.replace("morning", "noon"), "t-own")
    dashboard_app.dependency_overrides[get_approval_store] = lambda: env.approvals
    try:
        for base in ("/api/approvals", "/api/dept/approvals"):
            resp = client.post(f"{base}/{res.approval_id}/decide?defer=1",
                               json={"approve": True, "always_allow": True}, headers=alice)
            assert resp.status_code == 403
    finally:
        dashboard_app.dependency_overrides.clear()
    assert env.approvals.get(res.approval_id)["status"] == "pending"


def test_a_rule_does_not_outlive_its_approval_being_replayed(env, member_store):
    """A resumed run re-enters the gate: it must re-attach to its own approval, not be
    re-routed — otherwise a REJECTED item could slip through a rule created meanwhile."""
    res = env.say(ALICE, DEPT_MSG, "t-replay")
    env.approvals.decide_step(res.approval_id, False, "mhead@acme.com", as_level="head")
    _memory_rule(member_store, ALICE["sub"], DEPT_MSG)  # a rule appears after the reject
    resume_agent(res.approval_id, provider=MockLLM({"x": {"text": "ok", "tool_calls": []}}), **env.kw)
    assert env.store.items_in(("dept", "marketing")) == []  # the reject still stands


def test_head_decide_is_a_409_once_the_item_left_the_head_step(env, member_store):
    member_store.set_department_mode("marketing", "head+admin", actor="admin@acme.com")
    res = env.say(ALICE, DEPT_MSG, "t-409")
    dashboard_app.dependency_overrides[get_approval_store] = lambda: env.approvals
    dashboard_app.dependency_overrides[get_resumer] = lambda: (lambda _aid: None)
    try:
        client = TestClient(dashboard_app)
        head = _auth("mhead@acme.com")
        url = f"/api/dept/approvals/{res.approval_id}/decide?defer=1"
        assert client.post(url, json={"approve": True}, headers=head).status_code == 200
        # replaying step 1 (even as a reject) cannot pull the item back from the admin
        assert client.post(url, json={"approve": False}, headers=head).status_code == 409
    finally:
        dashboard_app.dependency_overrides.clear()
    entry = env.approvals.get(res.approval_id)
    assert entry["status"] == "pending" and entry["level"] == "admin" and len(entry["steps"]) == 1

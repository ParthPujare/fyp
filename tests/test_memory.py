"""Memory layer (Milestone 4 / weeks 7-8): scopes, SqliteBaseStore, editor,
fact extraction, episodic FTS5 recall, snapshot, Casbin matrix, and the
thread-backed /chat + central-write HITL suspend/resume end-to-end flows.

All tests run offline (MockLLM, in-process services, SQLite files in tmp_path).
"""

import pytest

from app.agent.tools import TOOL_REGISTRY
from app.auth.users import USERS
from app.config import MemorySettings
from app.llm.mock import MockLLM
from app.memory.edit import MemoryEditor, MemoryMatchError
from app.memory.extract import extract_facts
from app.memory.recall import RecallService
from app.memory.scopes import (
    SCOPE_CENTRAL,
    namespace_to_scope,
    scope_dept,
    scope_to_namespace,
    scope_user,
    sub_from_label,
    uid_label,
)
from app.memory.snapshot import build_snapshot
from app.ops.schema import TraceEventType

ALICE = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
BOB = {"sub": "bob@acme.com", "role": "hr_agent", "dept": "hr"}
ADMIN = {"sub": "admin@acme.com", "role": "admin", "dept": "admin"}


def claims_of(user: str) -> dict:
    u = USERS[user]
    return {"sub": u.username, "role": u.role, "dept": u.dept}


def make_recall(episodic, enforcer):
    return RecallService(episodic, enforcer, MemorySettings())


# --------------------------------------------------------------------------- scopes


def test_scope_namespace_roundtrip():
    assert scope_to_namespace(SCOPE_CENTRAL) == ("central",)
    assert scope_to_namespace(scope_dept("marketing")) == ("dept", "marketing")
    user_ns = scope_to_namespace(scope_user("alice@acme.com"))
    assert user_ns[0] == "user" and user_ns[1] == uid_label("alice@acme.com")
    assert namespace_to_scope(user_ns) == scope_user("alice@acme.com")
    assert namespace_to_scope(scope_to_namespace(scope_dept("hr"))) == scope_dept("hr")


def test_uid_encoding_roundtrip():
    for email in ["alice@acme.com", "a+b@x.co", "user.name@sub.example.org"]:
        assert sub_from_label(uid_label(email)) == email


# --------------------------------------------------------------------------- store


def test_store_crud_search_delete(memory_store):
    ns = scope_to_namespace(scope_user("alice@acme.com"))
    memory_store.put(ns, "k1", {"text": "Alice prefers cold calls", "op": "add"})
    memory_store.put(ns, "k2", {"text": "Alice likes Q3 reports", "op": "add"})
    item = memory_store.get(ns, "k1")
    assert item is not None and item.value["text"] == "Alice prefers cold calls"

    hits = memory_store.search(ns)
    assert {h.key for h in hits} == {"k1", "k2"}
    assert memory_store.list_namespaces() == [ns]

    memory_store.delete(ns, "k1")
    assert memory_store.get(ns, "k1") is None
    assert {h.key for h in memory_store.search(ns)} == {"k2"}


def test_store_search_prefix_and_filter(memory_store):
    marketing = scope_to_namespace(scope_dept("marketing"))
    hr = scope_to_namespace(scope_dept("hr"))
    memory_store.put(marketing, "a", {"text": "Q4 promo", "op": "add"})
    memory_store.put(hr, "b", {"text": "Hiring freeze", "op": "add"})
    memory_store.put(hr, "c", {"text": "Payslip issue", "op": "replace"})

    dept_ns = memory_store.search(("dept",))
    assert {h.key for h in dept_ns} == {"a", "b", "c"}

    only_add = memory_store.search(hr, filter={"op": {"$eq": "add"}})
    assert {h.key for h in only_add} == {"b"}


# --------------------------------------------------------------------------- editor


def test_editor_add_dedup_replace_remove(memory_store):
    editor = MemoryEditor(memory_store, MemorySettings())
    scope = scope_dept("marketing")
    editor.add(scope, "Alice prefers cold calls.")
    editor.add(scope, "Alice prefers cold calls")  # normalized dedup -> replaced
    editor.add(scope, "Alice handles Q3 reporting.")

    items = memory_store.items_in(scope_to_namespace(scope))
    assert len(items) == 2

    outcome = editor.replace(scope, "Alice handles Q4 reporting.", "Q3")
    assert outcome.op == "replace"

    items = memory_store.items_in(scope_to_namespace(scope))
    assert any("Q4" in str(i.value.get("text", "")) for i in items)
    assert not any("Q3" in str(i.value.get("text", "")) for i in items)

    editor.remove(scope, "cold calls")
    assert len(memory_store.items_in(scope_to_namespace(scope))) == 1

    with pytest.raises(MemoryMatchError) as exc:
        editor.remove(scope, "does not exist anywhere")
    assert exc.value.code == "not_found"


def test_editor_replace_ambiguous_raises(memory_store):
    editor = MemoryEditor(memory_store, MemorySettings())
    scope = scope_dept("marketing")
    editor.add(scope, "Alice manages the CRM platform.")
    editor.add(scope, "Bob manages the CRM platform too.")
    with pytest.raises(MemoryMatchError) as exc:
        editor.replace(scope, "they manage everything", "manages the CRM platform")
    assert exc.value.code == "ambiguous"


def test_editor_budget_evicts_oldest(memory_store):
    # Tiny per-tier budget forces oldest-out eviction within the namespace.
    settings = MemorySettings(user_bytes=30)
    editor = MemoryEditor(memory_store, settings)
    scope = scope_user("alice@acme.com")
    outcome = editor.add(scope, "fact alpha alpha alpha alpha")
    assert outcome.op == "add" and not outcome.evicted
    # 25 + 25 > 30 -> oldest ("alpha") evicted.
    outcome = editor.add(scope, "fact beta beta beta beta")
    assert outcome.op == "add" and len(outcome.evicted) == 1
    # 25 + 25 > 30 -> oldest ("beta") evicted.
    outcome = editor.add(scope, "fact gamma gamma gamma gamma")
    assert outcome.op == "add" and len(outcome.evicted) == 1
    remaining = memory_store.items_in(scope_to_namespace(scope))
    assert len(remaining) == 1
    assert "gamma" in str(remaining[0].value.get("text", ""))


# --------------------------------------------------------------------------- extraction


def test_extract_facts_classifies_scope_and_correction():
    msgs = [
        {"role": "user", "content": "Our team uses Salesforce for the pipeline. "
         "Company policy requires quarterly audits. Alice prefers Slack over Teams."},
    ]
    facts = extract_facts(msgs, ALICE)
    scopes = {f.scope for f in facts}
    assert scope_dept("marketing") in scopes
    assert SCOPE_CENTRAL in scopes
    assert scope_user("alice@acme.com") in scopes


def test_extract_facts_correction_becomes_replace():
    msgs = [
        {"role": "user", "content": "Alice prefers Slack for messaging."},
        {"role": "user", "content": "Actually Alice prefers Teams for messaging."},
    ]
    facts = extract_facts(msgs, ALICE)
    assert [f.op for f in facts] == ["add", "replace"]
    assert facts[1].match == "Alice prefers Slack for messaging."


def test_extract_facts_ignores_boring_sentences():
    msgs = [{"role": "user", "content": "Good morning. What time is it? Let's review Q3."}]
    assert extract_facts(msgs, ALICE) == []


# --------------------------------------------------------------------------- episodic recall


def test_episodic_fts_search_respects_scope(episodic):
    episodic.append_transcript(
        scope_dept("marketing"), "r1", {"sub": "alice@acme.com"}, "Alice prefers cold calls for outbound."
    )
    episodic.append_transcript(
        scope_dept("hr"), "r2", {"sub": "bob@acme.com"}, "Bob prefers structured interviews."
    )
    in_marketing = episodic.search("prefers", [scope_dept("marketing")])
    assert len(in_marketing) == 1 and in_marketing[0]["run_id"] == "r1"
    # Same query, but the caller may not read the hr scope -> no hr rows leak.
    assert not any(h["scope"] == scope_dept("hr") for h in in_marketing)
    # No permitted scopes -> empty, not an error.
    assert episodic.search("prefers", []) == []


def test_recall_service_only_searches_readable_scopes(episodic, enforcer):
    episodic.append_transcript(
        scope_dept("hr"), "r2", {"sub": "bob@acme.com"}, "Bob prefers structured interviews."
    )
    svc = make_recall(episodic, enforcer)
    assert svc.readable_scopes(ALICE) == [SCOPE_CENTRAL, scope_dept("marketing"), scope_user("alice@acme.com")]
    result = svc.recall("prefers structured interviews", ALICE)
    assert "No recalled conversations" in result.content  # hr transcript is out of scope
    assert result.ok


# --------------------------------------------------------------------------- Casbin matrix


def test_memory_policy_matrix(enforcer):
    for claims in (ALICE, BOB, ADMIN):
        assert enforcer.memory_allowed(SCOPE_CENTRAL, "read", claims) is True
    assert enforcer.memory_allowed(SCOPE_CENTRAL, "write", ADMIN) is True
    assert enforcer.memory_allowed(SCOPE_CENTRAL, "write", ALICE) is False
    assert enforcer.memory_allowed(SCOPE_CENTRAL, "write", BOB) is False

    # Dept scopes are self-scoped.
    assert enforcer.memory_allowed(scope_dept("marketing"), "read", ALICE) is True
    assert enforcer.memory_allowed(scope_dept("marketing"), "write", ALICE) is True
    assert enforcer.memory_allowed(scope_dept("hr"), "read", ALICE) is False
    assert enforcer.memory_allowed(scope_dept("marketing"), "read", BOB) is False

    # User scopes are self-scoped to the uid.
    assert enforcer.memory_allowed(scope_user("alice@acme.com"), "read", ALICE) is True
    assert enforcer.memory_allowed(scope_user("alice@acme.com"), "write", ALICE) is True
    assert enforcer.memory_allowed(scope_user("alice@acme.com"), "read", BOB) is False
    assert enforcer.memory_allowed(scope_user("bob@acme.com"), "write", BOB) is True

    # Unknown sub / role is denied by default.
    unknown = {"sub": "mallory@evil.com", "role": "hr_agent", "dept": "hr"}
    assert enforcer.memory_allowed(scope_dept("hr"), "read", unknown) is False
    assert enforcer.memory_allowed(SCOPE_CENTRAL, "read", unknown) is False

    # recall tool gate.
    assert enforcer.memory_recall_allowed(ALICE) is True
    assert enforcer.memory_recall_allowed(ADMIN) is True
    assert enforcer.memory_recall_allowed(unknown) is False


def test_recall_tool_is_casbin_gated(enforcer):
    tool = TOOL_REGISTRY["recall"]
    assert enforcer.tool_allowed(tool, ALICE) is True
    assert enforcer.tool_allowed(tool, ADMIN) is True
    assert enforcer.tool_allowed(tool, {"sub": "x", "role": "y"}) is False


# --------------------------------------------------------------------------- snapshot


def test_build_snapshot_content_and_cap(memory_store):
    marketing = scope_to_namespace(scope_dept("marketing"))
    user = scope_to_namespace(scope_user("alice@acme.com"))
    memory_store.put(marketing, "m1", {"text": "Our team uses Salesforce.", "op": "add"})
    memory_store.put(user, "u1", {"text": "Alice prefers Slack.", "op": "add"})

    facts_by_scope = {
        scope_dept("marketing"): memory_store.items_in(marketing),
        scope_user("alice@acme.com"): memory_store.items_in(user),
    }
    snapshot = build_snapshot(facts_by_scope, max_chars=6000)
    assert "Department memory (dept/marketing)" in snapshot
    assert "uses Salesforce" in snapshot
    assert "prefers Slack" in snapshot

    short = build_snapshot(facts_by_scope, max_chars=20)
    assert len(short) <= 20


# --------------------------------------------------------------------------- load_memory injection


def test_load_memory_injects_frozen_snapshot(runner_factory, memory_store, trace_store):
    marketing = scope_to_namespace(scope_dept("marketing"))
    memory_store.put(marketing, "m1", {"text": "Our team uses Salesforce for the pipeline.", "op": "add"})

    provider = MockLLM({"seed the account": {"text": "ok", "tool_calls": []}})
    runner = runner_factory(provider=provider)
    res = runner(claims_of("alice@acme.com"), "seed the account")

    tree = trace_store.get_run_tree(res.run_id)
    loads = [e for e in tree if e["type"] == TraceEventType.MEMORY_LOAD]
    assert len(loads) == 3  # central, own dept, own user
    assert loads[0]["payload"]["scope"] == SCOPE_CENTRAL
    assert loads[1]["payload"] == {"scope": scope_dept("marketing"), "allowed": True, "facts": 1}
    assert loads[2]["payload"] == {"scope": scope_user("alice@acme.com"), "allowed": True, "facts": 0}


def test_recall_tool_through_graph(runner_factory, episodic, enforcer):
    episodic.append_transcript(
        scope_dept("marketing"), "r1", {"sub": "alice@acme.com"}, "Alice prefers cold calls for outbound."
    )
    provider = MockLLM(
        sequence=[
            {"text": "", "tool_calls": [{"name": "recall", "args": {"query": "cold calls"}}]},
            {"text": "Here is what I found.", "tool_calls": []},
        ]
    )
    runner = runner_factory(provider=provider, episodic=episodic)
    res = runner(claims_of("alice@acme.com"), "find the cold call preference", thread_id="t-recall")
    assert not res.interrupted
    assert res.reply == "Here is what I found."
    assert {"recall"} <= {t["name"] for t in res.tool_calls}
    assert res.tool_calls[0]["outcome"] == "allowed"


# --------------------------------------------------------------------------- threads


def test_thread_persists_multi_turn_conversation(runner_factory):
    provider = MockLLM(script={"hello": {"text": "hi there", "tool_calls": []}})
    runner = runner_factory(provider=provider)
    r1 = runner(claims_of("alice@acme.com"), "hello", thread_id="thread-1")
    r2 = runner(claims_of("alice@acme.com"), "hello", thread_id="thread-1")
    assert r1.reply == "hi there" and r1.thread_id == "thread-1"
    assert r2.thread_id == "thread-1"
    # A call without a thread_id gets a fresh random thread (isolated conversation).
    r3 = runner(claims_of("alice@acme.com"), "hello", thread_id=None)
    assert r3.thread_id != "thread-1" and r3.thread_id is not None


def test_thread_reuses_snapshot_not_rebuilt(runner_factory, trace_store):
    provider = MockLLM({"again": {"text": "ok", "tool_calls": []}})
    runner = runner_factory(provider=provider)
    claims = claims_of("alice@acme.com")
    r1 = runner(claims, "again", thread_id="same-thread")
    r2 = runner(claims, "again", thread_id="same-thread")

    loads1 = [e for e in trace_store.get_run_tree(r1.run_id) if e["type"] == TraceEventType.MEMORY_LOAD]
    # Turn 2 reuses the frozen snapshot from graph state: no reload events.
    loads2 = [e for e in trace_store.get_run_tree(r2.run_id) if e["type"] == TraceEventType.MEMORY_LOAD]
    assert len(loads1) == 3
    assert loads2 == []


# --------------------------------------------------------------------------- dept / user writes


def test_dept_write_allowed_without_interrupt(runner_factory, memory_store, audit_log):
    provider = MockLLM({"policy": {"text": "noted", "tool_calls": []}})
    runner = runner_factory(provider=provider)
    res = runner(claims_of("alice@acme.com"), "Our team policy requires weekly standups.", thread_id="t-dept")
    assert not res.interrupted
    ns = scope_to_namespace(scope_dept("marketing"))
    texts = [i.value.get("text") for i in memory_store.items_in(ns)]
    assert any("weekly standups" in t for t in texts)
    assert audit_log.entries()[-1]["decision"] == "ALLOW"


def test_user_write_is_self_scoped(runner_factory, memory_store):
    provider = MockLLM({"pref": {"text": "got it", "tool_calls": []}})
    runner = runner_factory(provider=provider)
    runner(claims_of("alice@acme.com"), "Alice prefers dark mode in the dashboard.", thread_id="t-user")
    alice_ns = scope_to_namespace(scope_user("alice@acme.com"))
    assert any("dark mode" in i.value.get("text") for i in memory_store.items_in(alice_ns))
    # Bob's turn does not write into Alice's user scope.
    bob_ns = scope_to_namespace(scope_user("bob@acme.com"))
    assert memory_store.items_in(bob_ns) == []


# --------------------------------------------------------------------------- HITL central writes


def _hitl_setup(runner_factory, memory_store, episodic, approvals, enforcer, message):
    """Shared checkpointer + components so run_agent and resume_agent see the same thread."""
    from langgraph.checkpoint.memory import InMemorySaver

    checkpointer = InMemorySaver()
    recall = make_recall(episodic, enforcer)
    runner = runner_factory(
        provider=MockLLM(script={message: {"text": "ok", "tool_calls": []}}),
        store=memory_store,
        episodic=episodic,
        approvals=approvals,
        recall=recall,
        checkpointer=checkpointer,
    )
    return runner, checkpointer


def test_central_write_suspends_and_approve_commits(
    runner_factory, memory_store, episodic, approvals, audit_log, enforcer, executor,
):
    message = "Company policy requires quarterly vendor audits."
    runner, checkpointer = _hitl_setup(runner_factory, memory_store, episodic, approvals, enforcer, message)
    res = runner(claims_of("admin@acme.com"), message, thread_id="t-admin")
    assert res.interrupted and res.approval_id is not None
    pending = approvals.pending_items()
    assert len(pending) == 1 and pending[0]["id"] == res.approval_id

    approvals.decide(res.approval_id, True, "admin@acme.com")
    resumed = resume(
        res.approval_id, checkpointer=checkpointer, executor=executor, memory_store=memory_store,
        episodic=episodic, approvals=approvals, audit_log=audit_log, enforcer=enforcer,
    )
    assert resumed.reply == res.reply  # resume continues the same suspended run
    central = memory_store.items_in(("central",))
    assert any("quarterly vendor audits" in i.value.get("text") for i in central)
    assert any(e["decision"] == "APPROVED" for e in audit_log.entries())


def test_central_write_rejected_does_not_commit(
    runner_factory, memory_store, episodic, approvals, audit_log, enforcer, executor,
):
    message = "Company policy mandates data retention for 5 years."
    runner, checkpointer = _hitl_setup(runner_factory, memory_store, episodic, approvals, enforcer, message)
    res = runner(claims_of("admin@acme.com"), message, thread_id="t-rej")
    assert res.interrupted
    approvals.decide(res.approval_id, False, "admin@acme.com")
    resumed = resume(
        res.approval_id, checkpointer=checkpointer, executor=executor, memory_store=memory_store,
        episodic=episodic, approvals=approvals, audit_log=audit_log, enforcer=enforcer,
    )
    assert resumed.reply == res.reply
    central = memory_store.items_in(("central",))
    assert all("data retention" not in i.value.get("text") for i in central)
    assert any(e["decision"] == "REJECTED" for e in audit_log.entries())


def test_nonadmin_central_write_denied_no_interrupt(runner_factory, memory_store, approvals, audit_log):
    provider = MockLLM({"policy": {"text": "ok", "tool_calls": []}})
    runner = runner_factory(provider=provider, approvals=approvals)
    res = runner(claims_of("alice@acme.com"), "Company policy requires quarterly vendor audits.", thread_id="t-alice")
    assert not res.interrupted
    assert approvals.pending_items() == []
    central = memory_store.items_in(("central",))
    assert all("vendor audits" not in i.value.get("text") for i in central)
    assert any(e["decision"] == "DENY" and e["reason"] == "rbac" for e in audit_log.entries())


def test_resume_requires_decided_approval(
    runner_factory, memory_store, episodic, approvals, audit_log, enforcer, executor,
):
    message = "Company policy requires backup verification."
    runner, checkpointer = _hitl_setup(runner_factory, memory_store, episodic, approvals, enforcer, message)
    res = runner(claims_of("admin@acme.com"), message, thread_id="t-pending")
    assert res.interrupted
    with pytest.raises(ValueError):
        resume(
            res.approval_id, checkpointer=checkpointer, executor=executor, memory_store=memory_store,
            episodic=episodic, approvals=approvals, audit_log=audit_log, enforcer=enforcer,
        )


def resume(approval_id, *, checkpointer, executor, memory_store, episodic, approvals, audit_log, enforcer):
    """Resume a suspended central-write run with the shared test components."""
    from app.agent.runner import resume_agent

    return resume_agent(
        approval_id,
        provider=MockLLM({"policy": {"text": "ok", "tool_calls": []}}),
        executor=executor,
        enforcer=enforcer,
        audit=audit_log,
        store=memory_store,
        episodic=episodic,
        approvals=approvals,
        recall=make_recall(episodic, enforcer),
        checkpointer=checkpointer,
    )

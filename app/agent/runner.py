"""Agent runner — one bounded, traced agent run (optionally thread-backed).

Each call to `run_agent` runs the graph for `thread_id` (default: a fresh random
thread). With a checkpointer, conversation state + the frozen memory snapshot
persist per thread, so `/chat` becomes multi-turn. Central-tier memory writes
suspend via `interrupt()` for admin approval and dept-tier writes suspend for
dept-head approval (Mk12); `resume_agent` re-invokes the graph after the
decision. Every run is traced (run_start → nodes → tool_calls → memory → run_end).
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from typing import Any

from app.agent.checkpoints import invoke_options, make_checkpointer
from app.agent.graph import AgentDeps, build_agent_graph
from app.agent.guardrails import make_tracker
from app.agent.tools import TOOL_REGISTRY, ToolExecutor, tool_schema
from app.config import Settings, get_settings
from app.evals import eval_enabled_for_judge, evaluate_run
from app.harness.audit import AuditLog, open_audit_log
from app.harness.enforcer import PolicyEnforcer
from app.harness.filter import filter_tools
from app.llm.base import LLMProvider
from app.llm.factory import get_provider, get_provider_for_user
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.factory import open_approval_store, open_base_store, open_episodic_store
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.factory import open_trace_store
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore
from app.skills.registry import SkillRegistry

log = logging.getLogger(__name__)


def _flush_trace(trace: Any, run_id: str) -> None:
    """Persist buffered trace events (a no-op on SQLite). Tracing must never fail a chat."""
    try:
        trace.flush(run_id)
    except Exception:
        log.exception("trace flush failed for run %s", run_id)


@dataclass
class AgentResult:
    reply: str
    run_id: str
    guardrail_hits: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    thread_id: str | None = None
    interrupted: bool = False
    approval_id: str | None = None
    approval_tier: str | None = None  # "tool" | "central" | "dept" — what is being approved
    approval_level: str | None = None  # Mk15: "head" | "admin" — the step it waits on first
    approval_route: str | None = None  # Mk15: "head-only" | "head+admin" | "admin-only"


def _suspension(interrupts: list[Any]) -> tuple[str | None, str | None, str | None, str | None, str | None]:
    """(approval_id, tier, tool, level, route) from the first pending LangGraph interrupt."""
    if not interrupts:
        return None, None, None, None, None
    value = getattr(interrupts[0], "value", None) or {}
    tool = value.get("tool")
    tier = "tool" if tool else str(value.get("tier") or "central")
    return value.get("approval_id"), tier, tool, value.get("level"), value.get("route")


def _suspended_reply(
    tier: str | None, tool: str | None, approval_id: str | None, route: str | None = None
) -> str:
    """Reply text for a run that paused for approval before producing an answer."""
    if route == "head+admin":
        who = "your department head and then an admin"
    elif route == "head-only":
        who = "your department head"
    elif route == "admin-only":
        who = "an admin"
    else:
        who = "your department head or an admin" if tier == "dept" else "an admin"
    what = f"run `{tool}`" if tool else "save this to shared memory"
    return (
        f"Waiting for approval: I need {who} to approve before I {what}. "
        f"Approval id {approval_id}. The run continues automatically once it is approved."
    )


def run_state_is_local(checkpointer: Any, thread_id: str) -> bool:
    """Can this process load the checkpoint a suspended run needs in order to resume?

    With a shared database every machine sees every run's checkpoint, so this is normally True
    everywhere; False means the chat was deleted (or, on local SQLite, started on another
    machine). Deciding an approval whose run cannot resume would record the decision and then
    fail, and a decided approval cannot be re-decided — so callers check this first.
    """
    try:
        return checkpointer.get_tuple({"configurable": {"thread_id": thread_id}}) is not None
    except Exception:  # noqa: BLE001 - unreadable checkpoint store: treat as not resumable
        return False


def _saved_key_id(checkpointer: Any, thread_id: str) -> str | None:
    """The BYOK key id stored in the thread's latest checkpoint (None if absent)."""
    try:
        tup = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
        values = (tup.checkpoint or {}).get("channel_values", {}) if tup else {}
        return values.get("key_id") or None
    except Exception:  # noqa: BLE001 - best effort: fall back to the default provider
        return None


def _spend_precheck(settings: Settings, claims: dict[str, Any]) -> None:
    """Mk18 Phase H: raise SpendCapExceeded before spending (→ HTTP 429).

    Fail-open on store errors (spend_capped never raises); enforced when the
    store answers.
    """
    try:
        from app.auth.users import get_store as _get_members
        from app.llm.metered import SpendCapExceeded, spend_capped

        if spend_capped(_get_members(), str(claims.get("sub", "")), settings):
            raise SpendCapExceeded(f"daily LLM spend cap reached for {claims.get('sub', '?')}")
    except SpendCapExceeded:
        raise
    except Exception:  # noqa: BLE001, S110
        pass


def _build_deps(
    claims: dict[str, Any],
    *,
    settings: Settings,
    provider: LLMProvider | None,
    executor: ToolExecutor | None,
    trace: TraceStore | None,
    enforcer: PolicyEnforcer | None,
    audit: AuditLog | None,
    store: SqliteBaseStore | None,
    episodic: EpisodicStore | None,
    approvals: ApprovalStore | None,
    recall: RecallService | None,
    run_id: str,
    thread_id: str,
    checkpointer: Any,
    key_id: str | None = None,
    auth_token: str | None = None,
) -> tuple[AgentDeps, Any]:
    if store is None:
        store = open_base_store(settings.services.memory_path)
    if trace is None:
        trace = open_trace_store(settings.services.data_dir / "trace.sqlite")
    if enforcer is None:
        enforcer = PolicyEnforcer()
    if audit is None:
        audit = open_audit_log(settings.services.audit_path)
    provider = provider or get_provider_for_user(settings, claims.get("sub"), store, key_id=key_id)
    executor = executor or ToolExecutor(settings=settings)
    # Mk18 Phase C: bind the caller's own JWT for dept-service passthrough.
    # Always set (even None) — executors are reused across runs and must not
    # leak a previous caller's token. Duck-typed for test doubles.
    if hasattr(executor, "set_auth_token"):
        executor.set_auth_token(auth_token)
    # Mk18 Phase H: meter real providers per user/day (MockLLM spends nothing).
    # Over either cap the turn degrades to Mock with a spend_cap trace event
    # instead of spending (gateway ALSO pre-checks and answers 429).
    try:
        from app.llm.metered import MeteredProvider as _Metered
        from app.llm.mock import MockLLM as _Mock
    except Exception:  # noqa: BLE001 - metering optional, never sinks startup
        _Mock, _Metered = None, None  # type: ignore[assignment]
    if _Metered is not None and provider is not None and not isinstance(provider, _Mock):  # type: ignore[arg-type]
        try:
            from app.auth.users import get_store as _get_members

            _members = _get_members()
            _sub = str(claims.get("sub", ""))
            _trace, _rid, _actor = trace, run_id, {"sub": _sub, "role": claims.get("role", ""), "dept": claims.get("dept", "")}

            def _on_cap(_t=_trace, _r=_rid, _a=_actor) -> None:
                try:
                    from app.ops.schema import TraceEventType as _TET

                    _t.add_event(_r, _TET.GUARDRAIL, _a, {"type": "spend_cap"})
                except Exception:  # noqa: BLE001, S110
                    pass

            provider = _Metered(
                provider,
                members=_members,
                username=_sub,
                cost_cents_per_call=int(getattr(settings.llm, "est_cost_cents_per_call", 1) or 0),
                on_cap=_on_cap,
            )
        except Exception:  # noqa: BLE001, S110
            pass
    if episodic is None:
        episodic = open_episodic_store(settings.services.memory_path)
    if approvals is None:
        approvals = open_approval_store(settings.services.memory_path)
    if recall is None:
        recall = RecallService(episodic, enforcer, settings.memory)
    if checkpointer is None:
        checkpointer = make_checkpointer(settings)

    actor = {"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]}
    skills = SkillRegistry.open(settings.skills.skills_dir)
    visible_skills = [s for s in skills.load_all() if enforcer.skill_allowed(s.name, claims)]
    internal_schemas = [tool_schema(t) for t in filter_tools(TOOL_REGISTRY.values(), enforcer, claims)]
    try:
        from app.integrations.adapter import all_specs as _ext_specs
        from app.integrations.service import visible_external_tools as _ext_visible

        _ext_names = {t.name for t in _ext_visible(claims)}
        external_schemas = [
            tool_schema(t)
            for t in filter_tools(
                [s for s in _ext_specs() if s.name in _ext_names], enforcer, claims
            )
        ]
    except Exception:  # noqa: BLE001 - integrations optional/offline
        external_schemas = []
    deps = AgentDeps(
        provider=provider,
        executor=executor,
        tracker=make_tracker(settings.guardrails),
        trace=trace,
        enforcer=enforcer,
        audit=audit,
        run_id=run_id,
        actor=actor,
        tool_schemas=internal_schemas
        + external_schemas + [SkillRegistry.schema_for(s) for s in visible_skills],
        store=store,
        episodic=episodic,
        approvals=approvals,
        recall=recall,
        memory=settings.memory,
        thread_id=thread_id,
        skills=skills,
        gate_write_tools=settings.approvals.gate_write_tools,
    )
    return deps, checkpointer


def _fire_evals(
    *,
    deps: AgentDeps,
    run_id: str,
    settings: Settings,
    judge_provider: LLMProvider | None,
) -> list[Any]:
    """Run the eval hook after a completed run and persist EVAL events."""
    run_judge = eval_enabled_for_judge(
        judge_provider=judge_provider, llm_provider_name=settings.llm.provider
    )
    if run_judge and judge_provider is None:
        resolved = get_provider(settings)
        # A configured backend can still fall back to MockLLM (e.g. no API key).
        # An un-scripted mock produces an unparseable verdict, so skip the judge
        # rather than record a bogus failing suite.
        if isinstance(resolved, MockLLM):
            run_judge = False
        else:
            judge_provider = resolved
    return evaluate_run(
        run_id=run_id,
        actor=deps.actor,
        trace=deps.trace,
        audit=deps.audit,
        judge_provider=judge_provider,
        run_judge=run_judge,
    )


def run_agent(
    claims: dict[str, Any],
    message: str,
    *,
    provider: LLMProvider | None = None,
    executor: ToolExecutor | None = None,
    trace: TraceStore | None = None,
    enforcer: PolicyEnforcer | None = None,
    audit: AuditLog | None = None,
    settings: Settings | None = None,
    store: SqliteBaseStore | None = None,
    episodic: EpisodicStore | None = None,
    approvals: ApprovalStore | None = None,
    recall: RecallService | None = None,
    checkpointer: Any = None,
    thread_id: str | None = None,
    evals_enabled: bool = True,
    judge_provider: LLMProvider | None = None,
    key_id: str | None = None,
    auth_token: str | None = None,
) -> AgentResult:
    settings = settings or get_settings()
    if thread_id is None:
        thread_id = f"run-{uuid.uuid4()}"
    _spend_precheck(settings, claims)

    if trace is None:
        trace = open_trace_store(settings.services.data_dir / "trace.sqlite")
    actor = {"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]}
    run_id = str(uuid.uuid4())
    trace.start_run(actor, {"message": message}, run_id=run_id)

    deps = None
    try:
        deps, checkpointer = _build_deps(
            claims,
            settings=settings,
            provider=provider,
            executor=executor,
            trace=trace,
            enforcer=enforcer,
            audit=audit,
            store=store,
            episodic=episodic,
            approvals=approvals,
            recall=recall,
            run_id=run_id,
            thread_id=thread_id,
            checkpointer=checkpointer,
            key_id=key_id,
            auth_token=auth_token,
        )
        trace = deps.trace
        actor = deps.actor

        graph = build_agent_graph(deps, checkpointer=checkpointer)
        initial: dict[str, Any] = {"claims": claims, "input": message, "key_id": key_id}
        try:
            final_state = graph.invoke(
                initial,
                config={"configurable": {"thread_id": thread_id}},
                **invoke_options(checkpointer),
            )
        except Exception as exc:
            trace.end_run(run_id, {"error": str(exc)})
            raise

        interrupts = final_state.get("__interrupt__") or []
        interrupted = bool(interrupts)
        approval_id, tier, tool, level, route = _suspension(interrupts)
        if interrupted:
            trace.add_event(run_id, TraceEventType.GUARDRAIL, actor, {"type": "hitl_suspend", "approval_id": approval_id})

        reply = final_state.get("reply") or (
            _suspended_reply(tier, tool, approval_id, route) if interrupted else "no reply produced"
        )
        if not interrupted:
            trace.end_run(run_id, {"output": reply})
            if evals_enabled:
                _fire_evals(
                    deps=deps,
                    run_id=run_id,
                    settings=settings,
                    judge_provider=judge_provider,
                )

        return AgentResult(
            reply=reply,
            run_id=run_id,
            guardrail_hits=deps.tracker.hits,
            tool_calls=deps.executed_calls,
            thread_id=thread_id,
            interrupted=interrupted,
            approval_id=approval_id,
            approval_tier=tier if interrupted else None,
            approval_level=level if interrupted else None,
            approval_route=route if interrupted else None,
        )
    finally:
        _flush_trace(deps.trace if deps is not None else trace, run_id)


def resume_agent(
    approval_id: str,
    *,
    provider: LLMProvider | None = None,
    executor: ToolExecutor | None = None,
    trace: TraceStore | None = None,
    enforcer: PolicyEnforcer | None = None,
    audit: AuditLog | None = None,
    settings: Settings | None = None,
    store: SqliteBaseStore | None = None,
    episodic: EpisodicStore | None = None,
    approvals: ApprovalStore | None = None,
    recall: RecallService | None = None,
    checkpointer: Any = None,
    evals_enabled: bool = True,
    judge_provider: LLMProvider | None = None,
    key_id: str | None = None,
    auth_token: str | None = None,
) -> AgentResult:
    """Apply an admin/head decision and resume a suspended central/dept-write run.

    The resumed run keeps the model the caller picked (`key_id` is read back from the
    thread checkpoint), and a second approval request raised while resuming suspends the
    run again instead of being reported as finished."""
    settings = settings or get_settings()
    if approvals is None:
        approvals = open_approval_store(settings.services.memory_path)
    entry = approvals.get(approval_id)
    if entry is None:
        raise KeyError(f"approval {approval_id!r} not found")
    if entry["status"] not in ("approved", "rejected"):
        raise ValueError(f"approval {approval_id!r} has not been decided")

    # Mk14 single-resume gate: exactly one process may resume a decided approval.
    # The claim lives here (not in the endpoints) so dashboard, gateway, dept and
    # direct `resume_agent` callers share one atomic gate; losers get ValueError.
    actor = entry.get("actor") or {}
    owner = str(actor.get("sub", ""))
    claim = getattr(approvals, "claim_resume", None)
    if callable(claim) and owner:
        claim(approval_id, owner)
        entry = approvals.get(approval_id) or entry
    _spend_precheck(settings, entry.get("actor") or {})

    run_id = entry["run_id"]
    thread_id = entry["thread_id"]
    if checkpointer is None:
        checkpointer = make_checkpointer(settings)
    # Mk17: deleted-chat resume is 409 (conflict), not 404 and not a replay
    # from missing state. SQLite has no cross-PC gate, so check here directly.
    try:
        if not run_state_is_local(checkpointer, thread_id):
            raise ValueError(f"approval {approval_id!r} chat was deleted")
    except ValueError:
        raise
    except Exception:  # noqa: BLE001, S110
        pass
    if key_id is None:
        key_id = _saved_key_id(checkpointer, thread_id)
    deps = None
    try:
        deps, checkpointer = _build_deps(
            entry["actor"],
            settings=settings,
            provider=provider,
            executor=executor,
            trace=trace,
            enforcer=enforcer,
            audit=audit,
            store=store,
            episodic=episodic,
            approvals=approvals,
            recall=recall,
            run_id=run_id,
            thread_id=thread_id,
            checkpointer=checkpointer,
            key_id=key_id,
            auth_token=auth_token,
        )
        graph = build_agent_graph(deps, checkpointer=checkpointer)
        from langgraph.types import Command

        try:
            final_state = graph.invoke(
                Command(resume=entry["status"] == "approved"),
                config={"configurable": {"thread_id": thread_id}},
                **invoke_options(checkpointer),
            )
        except Exception as exc:
            deps.trace.end_run(run_id, {"error": str(exc)})
            raise
        interrupts = final_state.get("__interrupt__") or []
        interrupted = bool(interrupts)
        next_id, tier, tool, level, route = _suspension(interrupts)
        if interrupted:
            # A further gated call/write: the run is still suspended, so do not close the
            # trace or fire evals — the next decision resumes it again.
            deps.trace.add_event(
                run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "hitl_suspend", "approval_id": next_id}
            )
        reply = final_state.get("reply") or (
            _suspended_reply(tier, tool, next_id, route) if interrupted else "no reply produced"
        )
        if not interrupted:
            deps.trace.end_run(run_id, {"output": reply, "resumed": True})
            if evals_enabled:
                _fire_evals(
                    deps=deps,
                    run_id=run_id,
                    settings=settings,
                    judge_provider=judge_provider,
                )
        return AgentResult(
            reply=reply,
            run_id=run_id,
            guardrail_hits=deps.tracker.hits,
            tool_calls=deps.executed_calls,
            thread_id=thread_id,
            interrupted=interrupted,
            approval_id=next_id if interrupted else approval_id,
            approval_tier=tier if interrupted else None,
            approval_level=level if interrupted else None,
            approval_route=route if interrupted else None,
        )
    finally:
        if deps is not None:
            _flush_trace(deps.trace, run_id)

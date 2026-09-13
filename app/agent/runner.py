"""Agent runner — one bounded, traced agent run (optionally thread-backed).

Each call to `run_agent` runs the graph for `thread_id` (default: a fresh random
thread). With a checkpointer, conversation state + the frozen memory snapshot
persist per thread, so `/chat` becomes multi-turn. Central-tier memory writes
suspend via `interrupt()`; `resume_agent` re-invokes the graph after an admin
decision. Every run is traced (run_start → nodes → tool_calls → memory → run_end).
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from app.agent.graph import AgentDeps, build_agent_graph
from app.agent.guardrails import make_tracker
from app.agent.tools import TOOL_REGISTRY, ToolExecutor, tool_schema
from app.config import Settings, get_settings
from app.evals import eval_enabled_for_judge, evaluate_run
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.filter import filter_tools
from app.llm.base import LLMProvider
from app.llm.factory import get_provider, get_provider_for_user
from app.llm.mock import MockLLM
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore
from app.skills.registry import SkillRegistry


@dataclass
class AgentResult:
    reply: str
    run_id: str
    guardrail_hits: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    thread_id: str | None = None
    interrupted: bool = False
    approval_id: str | None = None


def _make_checkpointer(settings: Settings):
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(settings.services.checkpoint_path, check_same_thread=False)
    return SqliteSaver(conn)


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
) -> tuple[AgentDeps, Any]:
    if store is None:
        store = SqliteBaseStore(settings.services.memory_path)
    if trace is None:
        trace = TraceStore(settings.services.data_dir / "trace.sqlite")
    if enforcer is None:
        enforcer = PolicyEnforcer()
    if audit is None:
        audit = AuditLog(settings.services.audit_path)
    provider = provider or get_provider_for_user(settings, claims.get("sub"), store, key_id=key_id)
    executor = executor or ToolExecutor(settings=settings)
    if episodic is None:
        episodic = EpisodicStore(settings.services.memory_path)
    if approvals is None:
        approvals = ApprovalStore(settings.services.memory_path)
    if recall is None:
        recall = RecallService(episodic, enforcer, settings.memory)
    if checkpointer is None:
        checkpointer = _make_checkpointer(settings)

    actor = {"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]}
    skills = SkillRegistry(settings.skills.skills_dir)
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
) -> AgentResult:
    settings = settings or get_settings()
    if thread_id is None:
        thread_id = f"run-{uuid.uuid4()}"

    if trace is None:
        trace = TraceStore(settings.services.data_dir / "trace.sqlite")
    actor = {"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]}
    run_id = str(uuid.uuid4())
    trace.start_run(actor, {"message": message}, run_id=run_id)

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
    )
    trace = deps.trace
    actor = deps.actor

    graph = build_agent_graph(deps, checkpointer=checkpointer)
    initial: dict[str, Any] = {"claims": claims, "input": message}
    try:
        final_state = graph.invoke(initial, config={"configurable": {"thread_id": thread_id}})
    except Exception as exc:
        trace.end_run(run_id, {"error": str(exc)})
        raise

    interrupts = final_state.get("__interrupt__") or []
    interrupted = bool(interrupts)
    approval_id: str | None = None
    if interrupted:
        approval_id = interrupts[0].value.get("approval_id")
        trace.add_event(run_id, TraceEventType.GUARDRAIL, actor, {"type": "hitl_suspend", "approval_id": approval_id})

    reply = final_state.get("reply") or "no reply produced"
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
    )


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
) -> AgentResult:
    """Apply an admin decision and resume a suspended central-write run."""
    settings = settings or get_settings()
    if approvals is None:
        approvals = ApprovalStore(settings.services.memory_path)
    entry = approvals.get(approval_id)
    if entry is None:
        raise KeyError(f"approval {approval_id!r} not found")
    if entry["status"] not in ("approved", "rejected"):
        raise ValueError(f"approval {approval_id!r} has not been decided")

    run_id = entry["run_id"]
    thread_id = entry["thread_id"]
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
    )
    graph = build_agent_graph(deps, checkpointer=checkpointer)
    from langgraph.types import Command

    try:
        final_state = graph.invoke(
            Command(resume=entry["status"] == "approved"),
            config={"configurable": {"thread_id": thread_id}},
        )
    except Exception as exc:
        deps.trace.end_run(run_id, {"error": str(exc)})
        raise
    reply = final_state.get("reply") or "no reply produced"
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
        interrupted=False,
        approval_id=approval_id,
    )

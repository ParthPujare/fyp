"""LangGraph agent — the bounded tool-use loop with memory nodes (plan_mk1_core_harness.md §5, §11).

Graph: START → load_memory → agent ⇄ execute_tools → finalize → reflect → END.

- `load_memory` (graph start): asks Casbin which memory scopes this caller may READ,
  distils a frozen snapshot, and injects it as a system message (frozen per session).
- `agent` calls the LLMProvider protocol (MockLLM offline; the caller's BYOK
  provider otherwise — the graph never knows which).
- `execute_tools` applies the Casbin intercept, routes memory tools (`recall`) to the
  RecallService, and calls dept services over HTTP. Guardrails bound the loop.
- `reflect` (graph end, post-response): appends the episodic transcript, extracts
  durable facts, enforces each write via Casbin, applies dedup/budget, and suspends
  central writes through LangGraph `interrupt()` for HITL approval.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from operator import add
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.guardrails import GuardrailTracker
from app.agent.tools import TOOL_REGISTRY, ToolExecutor, ToolResult
from app.config import MemorySettings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.intercept import intercept_tool
from app.harness.sanitize import MASK, sanitize_text, sanitize_tool_result
from app.llm.base import LLMProvider
from app.memory.approvals import ApprovalStore
from app.memory.edit import MemoryEditor, MemoryMatchError
from app.memory.episodic import EpisodicStore
from app.memory.extract import extract_facts
from app.memory.recall import RecallService
from app.memory.scopes import SCOPE_CENTRAL, scope_dept, scope_to_namespace, scope_user
from app.memory.snapshot import build_snapshot
from app.memory.store import SqliteBaseStore
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore
from app.skills.executor import SkillExecutor
from app.skills.registry import SkillRegistry


class AgentState(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], add]
    claims: dict[str, Any]
    input: str
    pending_calls: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]
    memory_snapshot: str
    memory_loaded: bool
    reply: str
    final: bool
    force_end: bool
    key_id: str | None  # BYOK key the caller picked; persisted so an approval-resume uses the same model


@dataclass
class AgentDeps:
    provider: LLMProvider
    executor: ToolExecutor
    tracker: GuardrailTracker
    trace: TraceStore
    enforcer: PolicyEnforcer
    audit: AuditLog
    run_id: str
    actor: dict[str, Any]
    tool_schemas: list[dict[str, Any]]
    store: SqliteBaseStore
    episodic: EpisodicStore
    approvals: ApprovalStore
    recall: RecallService
    memory: MemorySettings
    thread_id: str
    skills: SkillRegistry
    executed_calls: list[dict[str, Any]] = field(default_factory=list)


def _summary(state: AgentState, deps: AgentDeps) -> str:
    """Deterministic final reply when a guardrail ends the loop mid-run."""
    calls = deps.executed_calls
    detail = "; ".join(
        f"{c['name']} -> {c.get('outcome', '?')}" for c in calls
    ) or "no tool calls completed"
    trigger = deps.tracker.hits[-1]["type"] if deps.tracker.hits else "limit"
    return f"Loop guardrail hit ({trigger}); partial progress: {detail}"


def _sanitize_trajectory(
    text: str | None, claims: dict[str, Any], enforcer: PolicyEnforcer
) -> str:
    """Mask PII in free-form trajectory text (prompts/reasoning) on a copy.

    The trajectory persists only what the run itself was allowed to see
    (plan_mk1_core_harness.md §7 "PII discipline"); the message list sent to the model is never
    mutated by masking.
    """
    if not text:
        return ""
    return sanitize_text(str(text), claims, enforcer)


def _sanitize_messages(
    messages: list[dict[str, Any]], claims: dict[str, Any], enforcer: PolicyEnforcer
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for message in messages:
        msg = dict(message)
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = sanitize_text(content, claims, enforcer)
        out.append(msg)
    return out


def _relevant_facts(
    messages: list[dict[str, Any]], claims: dict[str, Any], deps: AgentDeps
) -> list[Any]:
    """Mk8 #6: prefer LLM relevance extraction; heuristic fallback offline."""
    from app.llm.mock import MockLLM

    # Local models (LM Studio/Ollama/llama.cpp, e.g. Gemma reasoning) spend
    # hundreds of tokens on this background task — heuristics are fine there
    # and keep chat latency to one model call per turn instead of two.
    if getattr(deps.provider, "PROVIDER_NAME", "") in ("lmstudio", "ollama", "llamacpp"):
        return extract_facts(messages, claims)
    user_texts = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
    asst_texts = [str(m.get("content", "")) for m in messages if m.get("role") == "assistant"]
    user_msg = user_texts[-1] if user_texts else ""
    assistant_reply = asst_texts[-1] if asst_texts else ""
    if not isinstance(deps.provider, MockLLM) and len(user_msg.strip()) >= 8:
        try:
            from app.memory.llm_extract import build_messages, parse_response

            resp = deps.provider.generate(build_messages(user_msg, assistant_reply), tools=[])
            return parse_response(getattr(resp, "text", "") or "", claims)
        except Exception:  # noqa: BLE001 - LLM extract failure falls back to heuristics
            return extract_facts(messages, claims)
    return extract_facts(messages, claims)


def build_agent_graph(deps: AgentDeps, checkpointer: Any = None):
    def load_memory(state: AgentState) -> dict[str, Any]:
        claims = state["claims"]
        actor = deps.actor
        user_msg = {"role": "user", "content": state.get("input", "")}
        # Reset per-turn transient control fields (threads persist them otherwise).
        reset = {"final": False, "force_end": False, "pending_calls": [], "reply": ""}
        if state.get("memory_loaded"):
            return {**reset, "messages": [user_msg]}

        read_scopes: list[str] = []
        candidates = [
            SCOPE_CENTRAL,
            scope_dept(str(claims.get("dept", ""))),
            scope_user(str(claims.get("sub", ""))),
        ]
        for scope in candidates:
            allowed = deps.enforcer.memory_allowed(scope, "read", claims)
            items = deps.store.items_in(scope_to_namespace(scope)) if allowed else []
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.MEMORY_LOAD,
                actor,
                {"scope": scope, "allowed": allowed, "facts": len(items)},
            )
            if allowed:
                read_scopes.append(scope)

        facts_by_scope = {scope: deps.store.items_in(scope_to_namespace(scope)) for scope in read_scopes}
        snapshot_text = build_snapshot(facts_by_scope, deps.memory.snapshot_max_chars)
        deps.trace.add_event(
            deps.run_id,
            TraceEventType.CONTEXT_INJECTION,
            actor,
            {
                "kind": "snapshot",
                "scopes": read_scopes,
                "chars": len(snapshot_text),
                "text": _sanitize_trajectory(snapshot_text, claims, deps.enforcer),
            },
        )
        return {
            **reset,
            "messages": [{"role": "system", "content": snapshot_text}, user_msg],
            "memory_snapshot": snapshot_text,
            "memory_loaded": True,
        }

    def agent_node(state: AgentState) -> dict[str, Any]:
        tracker = deps.tracker
        tracker.begin_step()
        deps.trace.add_event(
            deps.run_id,
            TraceEventType.NODE,
            deps.actor,
            {"node": "agent", "step": tracker.steps_used},
        )
        if tracker.budget_exhausted():
            tracker.hit("budget")
            deps.trace.add_event(deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "budget"})
            return {"reply": _summary(state, deps), "final": True}
        if tracker.run_timed_out():
            tracker.hit("timeout")
            deps.trace.add_event(deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "timeout"})
            return {"reply": _summary(state, deps), "final": True}

        resp = deps.provider.generate(state["messages"], tools=deps.tool_schemas)
        deps.trace.add_event(
            deps.run_id,
            TraceEventType.PROMPT,
            deps.actor,
            {
                "step": tracker.steps_used,
                "tool_schema_count": len(deps.tool_schemas or []),
                "messages": _sanitize_messages(state["messages"], state["claims"], deps.enforcer),
            },
        )
        deps.trace.add_event(
            deps.run_id,
            TraceEventType.REASONING,
            deps.actor,
            {
                "step": tracker.steps_used,
                "text": _sanitize_trajectory(resp.reasoning, state["claims"], deps.enforcer),
            },
        )
        message: dict[str, Any] = {"role": "assistant", "content": resp.text}
        if resp.tool_calls:
            calls = [{"name": tc.name, "args": tc.args} for tc in resp.tool_calls]
            message["tool_calls"] = calls
            return {"messages": [message], "pending_calls": calls}
        return {"messages": [message], "reply": resp.text, "final": True}

    def execute_tools(state: AgentState) -> dict[str, Any]:
        tracker = deps.tracker
        deps.trace.add_event(
            deps.run_id,
            TraceEventType.NODE,
            deps.actor,
            {"node": "execute_tools", "step": tracker.steps_used},
        )
        messages: list[dict[str, Any]] = []
        executed: list[dict[str, Any]] = []

        def _critical_gate(
            tool: Any, args: dict[str, Any]
        ) -> ToolResult | None:
            """HITL gate for risk=critical tools (plan_mk1_core_harness.md §6, §8): suspend via
            interrupt() and execute only after admin approval."""
            actor = deps.actor
            resource = {"type": "tool", "id": tool.name}
            approval_id = deps.approvals.create(
                deps.run_id,
                deps.thread_id,
                resource,
                actor,
                {"tool": tool.name, "args": args},
            )
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.GUARDRAIL,
                actor,
                {"type": "hitl_suspend", "approval_id": approval_id, "tool": tool.name},
            )
            approved = interrupt(
                {
                    "approval_id": approval_id,
                    "resource": resource,
                    "actor": actor,
                    "tool": tool.name,
                    "args": args,
                    "thread_id": deps.thread_id,
                }
            )
            if approved:
                deps.audit.log(decision="APPROVED", resource=resource, actor=actor, run_id=deps.run_id, reason="hitl")
                return None
            deps.audit.log(decision="REJECTED", resource=resource, actor=actor, run_id=deps.run_id, reason="hitl")
            return ToolResult(
                ok=False,
                denied=True,
                reason="hitl",
                content=f"{tool.name} was rejected by an admin and will not run.",
            )

        def _outcome(result: ToolResult) -> str:
            return (
                "denied" if result.denied
                else "timeout" if result.timeout
                else "error" if not result.ok
                else "allowed"
            )

        def _trace_call(name: str, args: dict[str, Any], result: ToolResult) -> None:
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.TOOL_CALL,
                deps.actor,
                {
                    "tool": name,
                    "args": args,
                    "outcome": _outcome(result),
                    "latency_ms": result.latency_ms,
                    "reason": result.reason,
                    "sanitized": MASK in result.content,
                    "result_summary": result.summary,
                },
            )

        def _trace_skill(name: str, args: dict[str, Any], result: ToolResult, steps: int) -> None:
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.SKILL_USE,
                deps.actor,
                {
                    "skill": name,
                    "args": args,
                    "steps": steps,
                    "outcome": _outcome(result),
                    "reason": result.reason,
                },
            )

        def _execute_one(name: str, args: dict[str, Any]) -> ToolResult:
            """Single enforced call — the shared path for direct tool calls and
            for every nested call inside a skill (plan_mk1_core_harness.md §9: skills run through
            the exact same intercept/HITL/sanitize/guardrail chain)."""
            tracker.record_tool_call()
            tool = TOOL_REGISTRY.get(name)
            if tool is None:
                try:
                    from app.integrations.adapter import spec_for as _ext_spec
                except Exception:  # noqa: BLE001 - integrations optional
                    _ext_spec = lambda _n: None  # type: ignore[assignment]
                tool = _ext_spec(name)
            if tool is None:
                skill = deps.skills.get(name)
                if skill is not None:
                    result, steps = skill_executor.execute(
                        skill, args, state["claims"], mark_denied=tracker.mark_denied
                    )
                    _trace_skill(name, args, result, steps)
                    _trace_call(name, args, result)
                    return result
                return ToolResult(ok=False, reason="args", content=f"unknown tool: {name}")
            if tracker.is_denied(name):
                tracker.hit("deny_retry", name)
                deps.trace.add_event(
                    deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "deny_retry", "tool": name}
                )
                result = ToolResult(
                    ok=False,
                    denied=True,
                    reason="rbac",
                    content=f"{name} was blocked earlier in this run; route around it.",
                )
            else:
                result = intercept_tool(
                    tool,
                    args,
                    enforcer=deps.enforcer,
                    claims=state["claims"],
                    audit=deps.audit,
                    run_id=deps.run_id,
                )
                if result is None and tool.risk_level == "critical":
                    result = _critical_gate(tool, args)
                if result is None:
                    if tool.handler == "recall":
                        result = deps.recall.recall(str(args.get("query", "")), state["claims"])
                    elif tool.handler == "external":
                        from app.integrations.service import execute_external

                        result = sanitize_tool_result(
                            execute_external(tool.name, args, state["claims"]),
                            state["claims"],
                            deps.enforcer,
                        )
                    else:
                        result = sanitize_tool_result(
                            deps.executor.execute(tool, args),
                            state["claims"],
                            deps.enforcer,
                        )
                else:
                    deps.tracker.mark_denied(name)
            _trace_call(name, args, result)
            return result

        skill_executor = SkillExecutor(
            enforcer=deps.enforcer, audit=deps.audit, run_id=deps.run_id, run_step=_execute_one
        )

        # One call per node invocation. A HITL `interrupt()` re-runs the interrupted
        # node from the top on resume, so batching several calls here would replay
        # every call that preceded the gated one (duplicating its side effects).
        # Each completed call is checkpointed as its own node run instead.
        pending = list(state.get("pending_calls") or [])
        if pending:
            call = pending.pop(0)
            name = call["name"]
            args = call.get("args") or {}
            result = _execute_one(name, args)
            messages.append({"role": "tool", "content": result.summary, "name": name})
            executed.append(
                {
                    "name": name,
                    "args": args,
                    "outcome": _outcome(result),
                    "latency_ms": result.latency_ms,
                    "content": result.summary,
                }
            )
            deps.executed_calls.append(executed[-1])

            if tracker.budget_exhausted():
                pending = []  # budget spent: drop the rest of this batch

        update: dict[str, Any] = {"messages": messages, "tool_calls": executed, "pending_calls": pending}
        if pending:
            return update  # more calls in this batch: next node run handles them
        if tracker.steps_exhausted():
            tracker.hit("max_steps")
            deps.trace.add_event(deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "max_steps"})
            update["force_end"] = True
        elif tracker.budget_exhausted():
            tracker.hit("budget")
            deps.trace.add_event(deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "budget"})
            update["force_end"] = True
        elif tracker.run_timed_out():
            tracker.hit("timeout")
            deps.trace.add_event(deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "timeout"})
            update["force_end"] = True
        return update

    def route_from_agent(state: AgentState) -> str:
        return "tools" if state.get("pending_calls") else "end"

    def route_from_tools(state: AgentState) -> str:
        if state.get("pending_calls"):
            return "tools"
        return should_continue(state)

    def should_continue(state: AgentState) -> str:
        tracker = deps.tracker
        if state.get("final") or state.get("force_end"):
            return "end"
        if tracker.run_timed_out():
            tracker.hit("timeout")
            deps.trace.add_event(deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "timeout"})
            return "end"
        if tracker.steps_exhausted():
            tracker.hit("max_steps")
            deps.trace.add_event(deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "max_steps"})
            return "end"
        return "agent"

    def finalize(state: AgentState) -> dict[str, Any]:
        if state.get("final"):
            return {}
        return {"reply": _summary(state, deps), "final": True}

    def reflect(state: AgentState) -> dict[str, Any]:
        messages = state.get("messages") or []
        claims = state["claims"]
        actor = deps.actor

        transcript = "\n".join(
            f"{m.get('role')}: {m.get('content', '')}"
            for m in messages
            if m.get("role") in ("user", "assistant")
        )
        if transcript.strip():
            ep_scope = scope_dept(str(claims.get("dept", "")))
            deps.episodic.append_transcript(ep_scope, deps.run_id, actor, transcript)
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.MEMORY_WRITE,
                actor,
                {"scope": ep_scope, "kind": "episodic", "decision": "allowed"},
            )

        editor = MemoryEditor(deps.store, deps.memory)
        # Mk8 #6: LLM relevance extraction first (durable prefs only); heuristic
        # fallback keeps offline/Mock runs deterministic.
        facts = _relevant_facts(messages, claims, deps)
        if not facts:
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.MEMORY_WRITE,
                actor,
                {"decision": "skipped", "reason": "no-relevant-facts"},
            )
        for fact in facts:
            resource = {"type": "memory", "id": fact.scope}
            if not deps.enforcer.memory_allowed(fact.scope, "write", claims):
                deps.audit.log(decision="DENY", resource=resource, actor=actor, run_id=deps.run_id, reason="rbac")
                deps.trace.add_event(
                    deps.run_id,
                    TraceEventType.MEMORY_WRITE,
                    actor,
                    {"scope": fact.scope, "op": fact.op, "decision": "denied", "reason": "rbac"},
                )
                continue

            needs_hitl = fact.scope == SCOPE_CENTRAL or (
                fact.scope.startswith("dept/")
                and not deps.enforcer.is_dept_head(claims, fact.scope)
            )
            if needs_hitl:
                tier = "central" if fact.scope == SCOPE_CENTRAL else "dept"
                reason = "hitl" if tier == "central" else "dept-hitl"
                approval_id = deps.approvals.create(
                    deps.run_id,
                    deps.thread_id,
                    resource,
                    actor,
                    {"op": fact.op, "text": fact.text, "match": fact.match},
                )
                deps.trace.add_event(
                    deps.run_id,
                    TraceEventType.MEMORY_WRITE,
                    actor,
                    {"scope": fact.scope, "op": fact.op, "decision": "pending", "approval_id": approval_id, "tier": tier},
                )
                approved = interrupt(
                    {
                        "approval_id": approval_id,
                        "resource": resource,
                        "actor": actor,
                        "text": fact.text,
                        "thread_id": deps.thread_id,
                        "tier": tier,
                    }
                )
                if approved:
                    editor.apply(fact)
                    deps.audit.log(decision="APPROVED", resource=resource, actor=actor, run_id=deps.run_id, reason=reason)
                    deps.trace.add_event(
                        deps.run_id,
                        TraceEventType.MEMORY_WRITE,
                        actor,
                        {"scope": fact.scope, "op": fact.op, "decision": "approved", "approval_id": approval_id, "tier": tier},
                    )
                else:
                    deps.audit.log(decision="REJECTED", resource=resource, actor=actor, run_id=deps.run_id, reason=reason)
                    deps.trace.add_event(
                        deps.run_id,
                        TraceEventType.MEMORY_WRITE,
                        actor,
                        {"scope": fact.scope, "op": fact.op, "decision": "rejected", "approval_id": approval_id, "tier": tier},
                    )
            else:
                try:
                    outcome = editor.apply(fact)
                except MemoryMatchError as exc:
                    deps.trace.add_event(
                        deps.run_id,
                        TraceEventType.MEMORY_WRITE,
                        actor,
                        {"scope": fact.scope, "op": fact.op, "decision": "skipped", "reason": exc.code},
                    )
                    continue
                deps.audit.log(decision="ALLOW", resource=resource, actor=actor, run_id=deps.run_id, reason="rbac")
                deps.trace.add_event(
                    deps.run_id,
                    TraceEventType.MEMORY_WRITE,
                    actor,
                    {"scope": fact.scope, "op": outcome.op, "decision": "allowed"},
                )
                for evicted in outcome.evicted:
                    deps.trace.add_event(
                        deps.run_id,
                        TraceEventType.MEMORY_WRITE,
                        actor,
                        {"scope": fact.scope, "op": "evict", "key": evicted},
                    )
        return {}

    graph = StateGraph(AgentState)
    graph.add_node("load_memory", load_memory)
    graph.add_node("agent", agent_node)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("finalize", finalize)
    graph.add_node("reflect", reflect)
    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "agent")
    graph.add_conditional_edges("agent", route_from_agent, {"tools": "execute_tools", "end": "finalize"})
    graph.add_conditional_edges(
        "execute_tools", route_from_tools, {"tools": "execute_tools", "agent": "agent", "end": "finalize"}
    )
    graph.add_edge("finalize", "reflect")
    graph.add_edge("reflect", END)
    return graph.compile(checkpointer=checkpointer)

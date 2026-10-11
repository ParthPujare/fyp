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

Mk15: both gates (dept/central memory writes in `reflect`, gated tools in `execute_tools`)
ask `app.agent.routing.route_for` who must approve — nobody (allowlist hit / own head), the
department head, head then admin, or an admin. The run suspends once; the approval row
carries the step it waits on, and the run resumes only when the last step is decided.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from operator import add
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agent.guardrails import GuardrailTracker
from app.agent.routing import (
    KIND_MEMORY,
    KIND_TOOL,
    RISK_CRITICAL,
    RISK_WRITE,
    ROUTE_NONE,
    action_key_for,
    allowlist_payload,
    args_hash,
    initial_level,
    is_allowlist_eligible,
    route_for,
)
from app.agent.tools import TOOL_REGISTRY, ToolExecutor, ToolResult
from app.config import MemorySettings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer, dept_of_scope, head_role_for
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

log = logging.getLogger(__name__)


def _is_rate_limited(exc: BaseException) -> bool:
    """Free-tier quota signal from an extraction failure (message-sniffed, key-safe)."""
    msg = str(exc).lower()
    return any(k in msg for k in ("rate limit", "429", "quota", "too many requests"))


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
    gate_write_tools: bool = False  # Mk15: also suspend risk=write dept tools for the head


def _resolve_gate(
    deps: AgentDeps,
    claims: dict[str, Any],
    *,
    kind: str,
    payload: dict[str, Any],
    scope: str = "",
    tool_name: str = "",
    risk: str = "read",
) -> dict[str, Any]:
    """Look up the routing inputs for one gated item and ask the router (Mk15).

    Memory writes belong to the scope's department; tool calls are attributed to the
    caller's own department (RBAC already confined them to it), admins to none.
    """
    from app.auth.users import get_store

    members = get_store()
    sub = str(claims.get("sub", ""))
    role = str(claims.get("role", ""))
    actor_dept = str(claims.get("dept", ""))
    if kind == KIND_MEMORY:
        dept = dept_of_scope(scope) or ""
        action = action_key_for(KIND_MEMORY, scope=scope, op=str(payload.get("op", "")))
    else:
        dept = "" if role == "admin" or actor_dept == "admin" else actor_dept
        action = action_key_for(KIND_TOOL, tool=tool_name)
    digest = args_hash(allowlist_payload(kind, payload))
    eligible = bool(dept) and is_allowlist_eligible(kind, scope=scope, risk=risk)
    hit = eligible and members.allowlist_match(sub, action, digest)
    route = route_for(
        kind=kind,
        scope=scope,
        risk=risk,
        actor_role=role,
        actor_dept=actor_dept,
        dept_mode=members.department_mode(dept) if dept else "head-only",
        head_count=members.active_head_count(dept) if dept else 0,
        allowlist_hit=hit,
        gate_write_tools=deps.gate_write_tools,
    )
    return {
        "route": route,
        "level": initial_level(route),
        "dept": dept or None,
        "action": action,
        "args_hash": digest,
        "allowlist_hit": hit and route == ROUTE_NONE,
    }


def _audit_steps(deps: AgentDeps, approval_id: str, resource: dict[str, Any]) -> list[dict[str, Any]]:
    """Audit every decision step of a resumed approval, attributed to its decider (Mk15).

    Runs on the requester's machine when the run resumes: `head-hitl` for a department
    head's step, `admin-hitl` for an admin's. Returns the steps for the trace.
    """
    entry = deps.approvals.get(approval_id) or {}
    steps = list(entry.get("steps") or [])
    dept = entry.get("dept") or dept_of_scope(str(resource.get("id", ""))) or ""
    for step in steps:
        is_head = step.get("level") == "head"
        deps.audit.log(
            decision="APPROVED" if step.get("decision") == "approved" else "REJECTED",
            resource=resource,
            actor={
                "sub": str(step.get("by", "")),
                "role": head_role_for(dept) if is_head and dept else "admin",
                "dept": dept if is_head and dept else "admin",
            },
            run_id=deps.run_id,
            reason="head-hitl" if is_head else "admin-hitl",
        )
    return steps


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
    """Mk8 #6 + Mk16/Mk17: platform extractor first, chat-model/heuristics fallback.

    Order: (1) shared Qwen system provider (last turn-pair, role-aware proposals:
    user auto, dept → head approval, central → second-admin; sync with short
    timeout — never sinks the turn); (2) the caller's own chat
    provider (non-mock, non-local); (3) deterministic heuristics. Trace events
    distinguish the three outcomes so a silently empty memory has a visible
    cause: `system` (model decided nothing durable), `system-error` (the call
    failed — then a fallback event follows), `heuristic` (fallback or no key).
    No prompt/key material is ever traced.
    """
    from app.llm.mock import MockLLM

    user_texts = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
    asst_texts = [str(m.get("content", "")) for m in messages if m.get("role") == "assistant"]
    user_msg = user_texts[-1] if user_texts else ""
    assistant_reply = asst_texts[-1] if asst_texts else ""

    def _note(extraction: str, count: int, model: str = "", extra: dict[str, Any] | None = None) -> None:
        try:
            payload: dict[str, Any] = {"decision": "extracted", "extraction": extraction,
                                       "facts": count, "model": model}
            if extra:
                payload.update(extra)
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.MEMORY_WRITE,
                deps.actor,
                payload,
            )
        except Exception:  # noqa: BLE001, S110 - tracing must never sink the turn
            pass

    # (1) Platform-owned shared model — Mk17 role-aware proposals (user auto,
    # dept → head approval, central → second-admin). Genuine [] means "nothing
    # durable" (model decides — no heuristic retry); only provider/parse ERRORS
    # fall through to the chat-model/heuristic paths.
    if len(user_msg.strip()) >= 8:
        try:
            from app.llm.system import get_system_provider
            from app.memory import llm_extract as _lx

            system_provider = get_system_provider()
            if system_provider is not None:
                resp = system_provider.generate(
                    _lx.build_messages(
                        user_msg, assistant_reply,
                        speaker=str(claims.get("sub", "")),
                        role=str(claims.get("role", "")),
                        dept=str(claims.get("dept", "")),
                    ),
                    tools=[],
                )
                facts = _lx.parse_response(getattr(resp, "text", "") or "", claims)
                _note("system", len(facts),
                      getattr(system_provider, "model", ""))
                return facts
            _note("system-skipped", 0, extra={"system": "unconfigured"})
        except Exception as exc:  # noqa: BLE001 - fall through to chat-model/heuristic
            from app.memory import system_status as _status

            limited = _is_rate_limited(exc)
            _status.record_failure("rate-limited" if limited else "error")
            if limited:
                log.warning("platform memory extractor rate-limited; using fallback extraction")
            else:
                log.info("platform memory extractor failed (%s); using fallback extraction",
                         type(exc).__name__)
            _note("system-error", 0, extra={"error": type(exc).__name__, "rate_limited": limited})

    # Local models (LM Studio/Ollama/llama.cpp, e.g. Gemma reasoning) spend
    # hundreds of tokens on this background task — heuristics are fine there
    # and keep chat latency to one model call per turn instead of two.
    if getattr(deps.provider, "PROVIDER_NAME", "") in ("lmstudio", "ollama", "llamacpp"):
        facts = extract_facts(messages, claims)
        _note("heuristic", len(facts))
        return facts
    if not isinstance(deps.provider, MockLLM) and len(user_msg.strip()) >= 8:
        try:
            from app.memory.llm_extract import build_messages, parse_response

            resp = deps.provider.generate(build_messages(user_msg, assistant_reply), tools=[])
            facts = parse_response(getattr(resp, "text", "") or "", claims)
            _note("chat-model", len(facts))
            return facts
        except Exception:  # noqa: BLE001 - LLM extract failure falls back to heuristics
            facts = extract_facts(messages, claims)
            _note("heuristic", len(facts))
            return facts
    facts = extract_facts(messages, claims)
    _note("heuristic", len(facts))
    return facts


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
        facts_by_scope: dict[str, list[Any]] = {}
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
                facts_by_scope[scope] = items  # reuse: a second read per scope is a network trip

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

        def _is_gated(tool: Any) -> bool:
            return tool.risk_level == RISK_CRITICAL or (
                tool.risk_level == RISK_WRITE and deps.gate_write_tools
            )

        def _critical_gate(
            tool: Any, args: dict[str, Any]
        ) -> ToolResult | None:
            """HITL gate for gated tools (plan_mk1_core_harness.md §6, §8; routed since Mk15):
            suspend via interrupt() and execute only after the routed approval — the
            department head, head then admin, or an admin. An "always allow" rule for this
            exact call skips the suspension."""
            actor = deps.actor
            resource = {"type": "tool", "id": tool.name}
            payload = {"tool": tool.name, "args": args}
            gate: dict[str, Any] = {}
            # A resumed run re-enters here: re-attach to its own approval before routing.
            approval_id = deps.approvals.find(deps.run_id, resource, payload)
            if approval_id is None:
                gate = _resolve_gate(
                    deps,
                    state["claims"],
                    kind=KIND_TOOL,
                    payload=payload,
                    tool_name=tool.name,
                    risk=tool.risk_level,
                )
                if gate["route"] == ROUTE_NONE:
                    if gate["allowlist_hit"]:
                        deps.audit.log(
                            decision="ALLOW", resource=resource, actor=actor, run_id=deps.run_id, reason="allowlist"
                        )
                        deps.trace.add_event(
                            deps.run_id,
                            TraceEventType.GUARDRAIL,
                            actor,
                            {
                                "type": "allowlist",
                                "tool": tool.name,
                                "action": gate["action"],
                                "args_hash": gate["args_hash"],
                            },
                        )
                    return None
                approval_id = deps.approvals.create(
                    deps.run_id,
                    deps.thread_id,
                    resource,
                    actor,
                    payload,
                    level=gate["level"],
                    route=gate["route"],
                    dept=gate["dept"],
                    action=gate["action"],
                    args_hash=gate["args_hash"],
                )
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.GUARDRAIL,
                actor,
                {
                    "type": "hitl_suspend",
                    "approval_id": approval_id,
                    "tool": tool.name,
                    "level": gate.get("level"),
                    "route": gate.get("route"),
                },
            )
            approved = interrupt(
                {
                    "approval_id": approval_id,
                    "resource": resource,
                    "actor": actor,
                    "tool": tool.name,
                    "args": args,
                    "thread_id": deps.thread_id,
                    "level": gate.get("level"),
                    "route": gate.get("route"),
                    "action": gate.get("action"),
                    "args_hash": gate.get("args_hash"),
                }
            )
            steps = _audit_steps(deps, approval_id, resource)
            if approved:
                deps.audit.log(decision="APPROVED", resource=resource, actor=actor, run_id=deps.run_id, reason="hitl")
                return None
            deps.audit.log(decision="REJECTED", resource=resource, actor=actor, run_id=deps.run_id, reason="hitl")
            by_head = bool(steps) and steps[-1].get("level") == "head"
            return ToolResult(
                ok=False,
                denied=True,
                reason="hitl",
                content=(
                    f"{tool.name} was rejected by "
                    f"{'your department head' if by_head else 'an admin'} and will not run."
                ),
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
                if result is None and _is_gated(tool):
                    # Mk17: connection check precedes the approval gate. An
                    # unconnected external tool is denied immediately with a
                    # Connect prompt — never suspends head/admin first.
                    if getattr(tool, "handler", "") == "external":
                        try:
                            from app.integrations.adapter import app_for as _app_for
                            from app.integrations.service import _is_connected as _ext_connected
                        except Exception:  # noqa: BLE001 - integrations optional
                            _app_for = lambda _n: None  # type: ignore[assignment]
                            _ext_connected = lambda _u, _a: True  # type: ignore[assignment]
                        _app = _app_for(name) or ""
                        if _app and not _ext_connected(str(state["claims"].get("sub", "")), _app):
                            result = ToolResult(
                                ok=False,
                                denied=True,
                                reason="auth",
                                content=f"{_app} is not connected. Tap Connect for {_app}, then Refresh.",
                            )
                    if result is None:
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

            # Mk15 routing: central -> admin; dept -> nobody (own head / allowlist hit),
            # the head, or head then admin. A resumed run re-attaches to its own approval
            # first, so a decided item is never re-routed or matched against the allowlist.
            payload = {"op": fact.op, "text": fact.text, "match": fact.match}
            gate: dict[str, Any] = {}
            approval_id: str | None = None
            if fact.scope == SCOPE_CENTRAL or (
                fact.scope.startswith("dept/")
                and not deps.enforcer.is_dept_head(claims, fact.scope)
            ):
                approval_id = deps.approvals.find(deps.run_id, resource, payload)
                if approval_id is None:
                    gate = _resolve_gate(
                        deps, claims, kind=KIND_MEMORY, payload=payload, scope=fact.scope
                    )
                    if gate["route"] != ROUTE_NONE:
                        approval_id = deps.approvals.create(
                            deps.run_id,
                            deps.thread_id,
                            resource,
                            actor,
                            payload,
                            level=gate["level"],
                            route=gate["route"],
                            dept=gate["dept"],
                            action=gate["action"],
                            args_hash=gate["args_hash"],
                        )
            if approval_id is not None:
                tier = "central" if fact.scope == SCOPE_CENTRAL else "dept"
                reason = "hitl" if tier == "central" else "dept-hitl"
                deps.trace.add_event(
                    deps.run_id,
                    TraceEventType.MEMORY_WRITE,
                    actor,
                    {
                        "scope": fact.scope,
                        "op": fact.op,
                        "decision": "pending",
                        "approval_id": approval_id,
                        "tier": tier,
                        "level": gate.get("level"),
                        "route": gate.get("route"),
                    },
                )
                approved = interrupt(
                    {
                        "approval_id": approval_id,
                        "resource": resource,
                        "actor": actor,
                        "text": fact.text,
                        "thread_id": deps.thread_id,
                        "tier": tier,
                        "level": gate.get("level"),
                        "route": gate.get("route"),
                        "action": gate.get("action"),
                        "args_hash": gate.get("args_hash"),
                    }
                )
                steps = _audit_steps(deps, approval_id, resource)
                if approved:
                    editor.apply(fact)
                    deps.audit.log(decision="APPROVED", resource=resource, actor=actor, run_id=deps.run_id, reason=reason)
                    deps.trace.add_event(
                        deps.run_id,
                        TraceEventType.MEMORY_WRITE,
                        actor,
                        {
                            "scope": fact.scope,
                            "op": fact.op,
                            "decision": "approved",
                            "approval_id": approval_id,
                            "tier": tier,
                            "steps": steps,
                        },
                    )
                else:
                    deps.audit.log(decision="REJECTED", resource=resource, actor=actor, run_id=deps.run_id, reason=reason)
                    deps.trace.add_event(
                        deps.run_id,
                        TraceEventType.MEMORY_WRITE,
                        actor,
                        {
                            "scope": fact.scope,
                            "op": fact.op,
                            "decision": "rejected",
                            "approval_id": approval_id,
                            "tier": tier,
                            "steps": steps,
                        },
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
                except Exception as exc:  # noqa: BLE001 - Mk17 validation (empty/too_long) skips, never sinks
                    deps.trace.add_event(
                        deps.run_id,
                        TraceEventType.MEMORY_WRITE,
                        actor,
                        {"scope": fact.scope, "op": fact.op, "decision": "skipped", "reason": getattr(exc, "code", "invalid")},
                    )
                    continue
                allowlisted = bool(gate.get("allowlist_hit"))
                deps.audit.log(
                    decision="ALLOW",
                    resource=resource,
                    actor=actor,
                    run_id=deps.run_id,
                    reason="allowlist" if allowlisted else "rbac",
                )
                allowed_event: dict[str, Any] = {"scope": fact.scope, "op": outcome.op, "decision": "allowed"}
                if allowlisted:
                    allowed_event.update(
                        reason="allowlist", action=gate["action"], args_hash=gate["args_hash"]
                    )
                deps.trace.add_event(deps.run_id, TraceEventType.MEMORY_WRITE, actor, allowed_event)
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

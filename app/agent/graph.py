"""LangGraph agent — the bounded tool-use loop (proposed_architecture.md §11).

Graph: START → agent → execute_tools → (should_continue) → agent | finalize → END.

The `agent` node calls the existing LLMProvider protocol (MockLLM/LiteLLM stay
as-is — no langchain chat-model wrapper). `execute_tools` applies the scope
gate and calls the dept services over HTTP. Guardrails bound the loop
deterministically; every guardrail trigger is traced.
"""

from __future__ import annotations

from dataclasses import dataclass
from operator import add
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from app.agent.guardrails import GuardrailTracker
from app.agent.tools import TOOL_REGISTRY, ToolExecutor, ToolResult
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.intercept import intercept_tool
from app.llm.base import LLMProvider
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore


class AgentState(TypedDict, total=False):
    messages: Annotated[list[dict[str, Any]], add]
    claims: dict[str, Any]
    input: str
    pending_calls: list[dict[str, Any]]
    tool_calls: Annotated[list[dict[str, Any]], add]
    reply: str
    final: bool
    force_end: bool


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


def _summary(state: AgentState, tracker: GuardrailTracker) -> str:
    """Deterministic final reply when a guardrail ends the loop mid-run."""
    calls = state.get("tool_calls") or []
    detail = "; ".join(
        f"{c['name']} -> {c.get('outcome', '?')}" for c in calls
    ) or "no tool calls completed"
    trigger = tracker.hits[-1]["type"] if tracker.hits else "limit"
    return f"Loop guardrail hit ({trigger}); partial progress: {detail}"


def build_agent_graph(deps: AgentDeps):
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
            return {"reply": _summary(state, tracker), "final": True}
        if tracker.run_timed_out():
            tracker.hit("timeout")
            deps.trace.add_event(deps.run_id, TraceEventType.GUARDRAIL, deps.actor, {"type": "timeout"})
            return {"reply": _summary(state, tracker), "final": True}

        resp = deps.provider.generate(state["messages"], tools=deps.tool_schemas)
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

        for call in state.get("pending_calls") or []:
            tracker.record_tool_call()
            name = call["name"]
            args = call.get("args") or {}
            tool = TOOL_REGISTRY.get(name)

            if tool is None:
                result = ToolResult(ok=False, reason="args", content=f"unknown tool: {name}")
            elif tracker.is_denied(name):
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
                if result is None:
                    result = deps.executor.execute(tool, args)
                else:
                    deps.tracker.mark_denied(name)

            outcome = (
                "denied" if result.denied else "timeout" if result.timeout else "error" if not result.ok else "allowed"
            )
            deps.trace.add_event(
                deps.run_id,
                TraceEventType.TOOL_CALL,
                deps.actor,
                {
                    "tool": name,
                    "args": args,
                    "outcome": outcome,
                    "latency_ms": result.latency_ms,
                    "reason": result.reason,
                },
            )
            messages.append({"role": "tool", "content": result.summary, "name": name})
            executed.append({"name": name, "args": args, "outcome": outcome, "latency_ms": result.latency_ms})

            if tracker.budget_exhausted():
                break

        update: dict[str, Any] = {"messages": messages, "tool_calls": executed, "pending_calls": []}
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
        return {"reply": _summary(state, deps.tracker), "final": True}

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("execute_tools", execute_tools)
    graph.add_node("finalize", finalize)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges("agent", route_from_agent, {"tools": "execute_tools", "end": "finalize"})
    graph.add_conditional_edges("execute_tools", should_continue, {"agent": "agent", "end": "finalize"})
    graph.add_edge("finalize", END)
    return graph.compile()

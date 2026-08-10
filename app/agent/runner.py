"""Agent runner — one bounded, traced, single-turn agent run.

Stateless single-turn: each call to `run_agent` builds a fresh graph and
invokes it once. Conversation persistence (checkpointer / threads) arrives
with the memory milestone (weeks 7-8). The run is fully traced into the
TraceStore (run_start → nodes → tool_calls → guardrails → run_end).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.agent.graph import AgentDeps, build_agent_graph
from app.agent.guardrails import make_tracker
from app.agent.tools import TOOL_REGISTRY, ToolExecutor, tool_schema
from app.config import Settings, get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.filter import filter_tools
from app.llm.base import LLMProvider
from app.llm.factory import get_provider
from app.ops.store import TraceStore


@dataclass
class AgentResult:
    reply: str
    run_id: str
    guardrail_hits: list[dict[str, Any]]
    tool_calls: list[dict[str, Any]]


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
) -> AgentResult:
    settings = settings or get_settings()
    provider = provider or get_provider(settings)
    executor = executor or ToolExecutor(settings=settings)
    if trace is None:
        trace = TraceStore(settings.services.data_dir / "trace.sqlite")
    if enforcer is None:
        enforcer = PolicyEnforcer()
    if audit is None:
        audit = AuditLog(settings.services.audit_path)

    tracker = make_tracker(settings.guardrails)
    actor = {"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]}
    run_id = trace.start_run(actor, {"message": message})

    deps = AgentDeps(
        provider=provider,
        executor=executor,
        tracker=tracker,
        trace=trace,
        enforcer=enforcer,
        audit=audit,
        run_id=run_id,
        actor=actor,
        tool_schemas=[
            tool_schema(t) for t in filter_tools(TOOL_REGISTRY.values(), enforcer, claims)
        ],
    )
    graph = build_agent_graph(deps)

    initial: dict[str, Any] = {
        "messages": [{"role": "user", "content": message}],
        "claims": claims,
        "input": message,
    }
    final_state = graph.invoke(initial)
    reply = final_state.get("reply") or "no reply produced"
    trace.end_run(run_id, {"output": reply})

    return AgentResult(
        reply=reply,
        run_id=run_id,
        guardrail_hits=tracker.hits,
        tool_calls=final_state.get("tool_calls", []),
    )

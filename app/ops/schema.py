"""Trace event schema — shared conventions with the audit log (see proposed_architecture.md §12)."""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TraceEventType(str, Enum):
    RUN_START = "run_start"
    NODE = "node"
    PROMPT = "prompt"
    REASONING = "reasoning"
    CONTEXT_INJECTION = "context_injection"
    TOOL_CALL = "tool_call"
    MEMORY_LOAD = "memory_load"
    MEMORY_WRITE = "memory_write"
    SKILL_USE = "skill_use"
    GUARDRAIL = "guardrail"
    EVAL = "eval"
    RUN_END = "run_end"


# Source of a trajectory event — how the ops tab filters "what the model saw"
# by origin (plan_mk1_core_harness.md §7). ``system`` covers harness bookkeeping (run/nodes).
EVENT_SOURCES = ("llm", "memory", "tool", "guardrail", "hitl", "user", "system", "eval")

# Default source derived from the event type when a caller doesn't specify one.
DEFAULT_SOURCE: dict[TraceEventType, str] = {
    TraceEventType.RUN_START: "user",
    TraceEventType.RUN_END: "system",
    TraceEventType.NODE: "system",
    TraceEventType.PROMPT: "llm",
    TraceEventType.REASONING: "llm",
    TraceEventType.CONTEXT_INJECTION: "memory",
    TraceEventType.TOOL_CALL: "tool",
    TraceEventType.SKILL_USE: "tool",
    TraceEventType.MEMORY_LOAD: "memory",
    TraceEventType.MEMORY_WRITE: "memory",
    TraceEventType.GUARDRAIL: "guardrail",
    TraceEventType.EVAL: "eval",
}


@dataclass(frozen=True)
class TraceActor:
    sub: str
    role: str
    dept: str


@dataclass
class TraceEvent:
    event_id: str
    run_id: str
    seq: int
    ts: str
    type: TraceEventType
    source: str
    actor: dict[str, Any]
    payload: dict[str, Any]
    prev_hash: str

    @property
    def event_type(self) -> str:
        return self.type.value

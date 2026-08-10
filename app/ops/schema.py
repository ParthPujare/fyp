"""Trace event schema — shared conventions with the audit log (see proposed_architecture.md §12)."""

from dataclasses import dataclass
from enum import Enum
from typing import Any


class TraceEventType(str, Enum):
    RUN_START = "run_start"
    NODE = "node"
    TOOL_CALL = "tool_call"
    MEMORY_LOAD = "memory_load"
    MEMORY_WRITE = "memory_write"
    SKILL_USE = "skill_use"
    GUARDRAIL = "guardrail"
    EVAL = "eval"
    RUN_END = "run_end"


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
    actor: dict[str, Any]
    payload: dict[str, Any]
    prev_hash: str

    @property
    def event_type(self) -> str:
        return self.type.value

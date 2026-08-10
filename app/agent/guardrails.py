"""Loop guardrails — deterministic bounds on the agent loop (plan.md §6).

Enforced as graph logic, not prompting: max steps per run, a tool-call budget,
a deny-retry policy (a denied tool is terminal for the run; repeats are
short-circuited), and a wall-clock run timeout. Every trigger is recorded in
`hits` for tracing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from app.config import GuardrailSettings


@dataclass
class GuardrailTracker:
    max_steps: int
    tool_budget: int
    run_timeout_seconds: float
    start_time: float = field(default_factory=time.monotonic)
    steps_used: int = 0
    tool_calls_used: int = 0
    denied_tools: set[str] = field(default_factory=set)
    hits: list[dict[str, Any]] = field(default_factory=list)

    def begin_step(self) -> None:
        self.steps_used += 1

    def record_tool_call(self) -> None:
        self.tool_calls_used += 1

    def mark_denied(self, tool_name: str) -> None:
        self.denied_tools.add(tool_name)

    def is_denied(self, tool_name: str) -> bool:
        return tool_name in self.denied_tools

    def steps_exhausted(self) -> bool:
        return self.steps_used >= self.max_steps

    def budget_exhausted(self) -> bool:
        return self.tool_calls_used >= self.tool_budget

    def run_timed_out(self) -> bool:
        return time.monotonic() - self.start_time >= self.run_timeout_seconds

    def hit(self, guardrail_type: str, tool_name: str | None = None) -> dict[str, Any]:
        entry = {
            "type": guardrail_type,
            "step": self.steps_used,
            "tool_calls": self.tool_calls_used,
            "tool": tool_name,
        }
        self.hits.append(entry)
        return entry


def make_tracker(settings: GuardrailSettings) -> GuardrailTracker:
    return GuardrailTracker(
        max_steps=settings.max_steps,
        tool_budget=settings.tool_budget,
        run_timeout_seconds=settings.run_timeout_seconds,
    )

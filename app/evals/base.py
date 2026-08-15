"""Eval data model — shared by the deterministic and judge suites (plan_mk1_core_harness.md §8)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class EvalCheck:
    """A single pass/fail check with an optional per-check score (0..1)."""

    name: str
    passed: bool
    detail: str = ""
    score: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "detail": self.detail,
            "score": round(self.score, 3),
        }


@dataclass
class EvalResult:
    """One suite's outcome for a run (deterministic or judge)."""

    suite: str
    checks: list[EvalCheck] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return bool(self.checks) and all(c.passed for c in self.checks)

    @property
    def score(self) -> float:
        if not self.checks:
            return 0.0
        return sum(c.score for c in self.checks) / len(self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "passed": self.passed,
            "score": round(self.score, 3),
            "checks": [c.to_dict() for c in self.checks],
        }
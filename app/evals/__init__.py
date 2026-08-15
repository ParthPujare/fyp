"""Evals — auto-eval hook after every completed agent run (plan_mk1_core_harness.md §8).

`evaluate_run` runs the deterministic suite always, and the LLM-as-judge suite
when a judge provider is available (injected in tests/demo, or when the app
runs on a real LiteLLM backend). Every result is written back to the trace as
an `EVAL` event, so scores live alongside the run they grade.
"""

from __future__ import annotations

from typing import Any

from app.evals.base import EvalCheck, EvalResult
from app.evals.deterministic import DeterministicEvaluator
from app.evals.judge import JudgeEvaluator
from app.harness.audit import AuditLog
from app.llm.base import LLMProvider
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore


def eval_enabled_for_judge(*, judge_provider: LLMProvider | None, llm_provider_name: str) -> bool:
    """Judge runs only when verdicts are meaningful: a provider was injected
    (tests/demo use a scripted MockLLM) or the app is on a real LLM backend.
    A bare offline MockLLM would just echo the prompt — not a verdict."""
    return judge_provider is not None or llm_provider_name == "litellm"


def evaluate_run(
    *,
    run_id: str,
    actor: dict[str, Any],
    trace: TraceStore,
    audit: AuditLog | None = None,
    judge_provider: LLMProvider | None = None,
    run_judge: bool = False,
) -> list[EvalResult]:
    results: list[EvalResult] = []

    det = DeterministicEvaluator(trace, audit).evaluate(run_id)
    results.append(det)
    trace.add_event(
        run_id,
        TraceEventType.EVAL,
        actor,
        {
            "suite": det.suite,
            "passed": det.passed,
            "score": round(det.score, 3),
            "checks": [c.to_dict() for c in det.checks],
        },
    )

    if run_judge and judge_provider is not None:
        judge = JudgeEvaluator(judge_provider).evaluate(run_id, trace)
        results.append(judge)
        trace.add_event(
            run_id,
            TraceEventType.EVAL,
            actor,
            {
                "suite": judge.suite,
                "passed": judge.passed,
                "score": round(judge.score, 3),
                "checks": [c.to_dict() for c in judge.checks],
            },
        )
    return results


__all__ = [
    "EvalCheck",
    "EvalResult",
    "eval_enabled_for_judge",
    "evaluate_run",
]
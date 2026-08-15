"""SkillExecutor — deterministic execution of a skill's tool_calls (plan.md §9).

Invoking a skill is *not* free-form LLM behaviour: the harness runs the skill's
``tool_calls[]`` in order, and every nested call flows through the exact same
enforcement path as a direct tool call (the graph's ``run_step`` closure: Casbin
intercept → critical-HITL gate → handler/sanitize → trace → guardrails). A nested
denial aborts the skill and marks it denied for the rest of the run, so the agent
cannot retry-loop on a blocked skill.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

from app.agent.tools import ToolResult
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.skills.model import Skill

_TEMPLATE_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def resolve_args(template: Any, args: dict[str, Any]) -> Any:
    """Substitute ``{{arg_name}}`` placeholders in a step's args from the skill args."""
    if isinstance(template, str):
        return _TEMPLATE_RE.sub(lambda m: str(args.get(m.group(1), m.group(0))), template)
    if isinstance(template, dict):
        return {k: resolve_args(v, args) for k, v in template.items()}
    if isinstance(template, list):
        return [resolve_args(v, args) for v in template]
    return template


def intercept_skill(
    skill: Skill,
    *,
    enforcer: PolicyEnforcer,
    claims: dict[str, Any],
    audit: AuditLog,
    run_id: str,
) -> ToolResult | None:
    """Runtime RBAC block for a skill invocation. None = allowed, else a denial."""
    actor = {
        "sub": claims.get("sub", ""),
        "role": claims.get("role", ""),
        "dept": claims.get("dept", ""),
    }
    resource = {"type": "skill", "id": skill.name}
    if enforcer.skill_allowed(skill.name, claims):
        audit.log(decision="ALLOW", resource=resource, actor=actor, run_id=run_id)
        return None
    audit.log(decision="DENY", resource=resource, actor=actor, run_id=run_id, reason="rbac")
    return ToolResult(
        ok=False,
        denied=True,
        reason="rbac",
        content=f"Permission denied: you are not allowed to use the skill {skill.name}.",
    )


class SkillExecutor:
    """Runs a skill's tool_calls deterministically via the graph's per-tool closure."""

    def __init__(
        self,
        *,
        enforcer: PolicyEnforcer,
        audit: AuditLog,
        run_id: str,
        run_step: Callable[[str, dict[str, Any]], ToolResult],
    ):
        self._enforcer = enforcer
        self._audit = audit
        self._run_id = run_id
        self._run_step = run_step

    def execute(
        self,
        skill: Skill,
        args: dict[str, Any],
        claims: dict[str, Any],
        *,
        mark_denied: Callable[[str], None],
    ) -> tuple[ToolResult, int]:
        """Run a skill; returns (aggregated result, number of steps actually executed)."""
        gate = intercept_skill(
            skill,
            enforcer=self._enforcer,
            claims=claims,
            audit=self._audit,
            run_id=self._run_id,
        )
        if gate is not None:
            mark_denied(skill.name)
            return gate, 0

        results: list[ToolResult] = []
        for step in skill.tool_calls:
            step_args = resolve_args(step.get("args") or {}, args)
            result = self._run_step(str(step.get("tool", "")), step_args)
            results.append(result)
            if not result.ok or result.denied:
                mark_denied(skill.name)
                break

        content = "\n".join(
            f"[{i + 1}] {skill.tool_calls[i].get('tool', '?')}: {r.summary}"
            for i, r in enumerate(results)
        )
        denied = any(r.denied for r in results)
        ok = bool(results) and all(r.ok for r in results)
        return (
            ToolResult(
                ok=ok,
                denied=denied,
                content=content or f"skill {skill.name} ran with no output",
                latency_ms=sum(r.latency_ms for r in results),
                reason="rbac" if denied else (results[-1].reason if results else ""),
            ),
            len(results),
        )

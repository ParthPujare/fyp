"""Deterministic evals — rule-based checks needing no LLM (plan.md §8).

These are the "tests over evals" proof layer: the harness checks its own trace
+ audit log against the RBAC contract + guardrail guarantees after every run.
"""

from __future__ import annotations

from typing import Any

from app.evals.base import EvalCheck, EvalResult
from app.harness.audit import AuditLog
from app.ops.schema import TraceEventType
from app.ops.store import TraceStore

TERMINAL_GUARDRAILS = ("budget", "max_steps", "timeout")

_DENIED_DECISIONS = {"DENY", "REJECTED"}
_ALLOWED_DECISIONS = {"ALLOW", "APPROVED"}


class DeterministicEvaluator:
    """Checks over one run's trace + audit log. All checks are pass/fail rule
    evaluation — the same verdict every time, for the same inputs."""

    def __init__(self, trace: TraceStore, audit: AuditLog | None = None):
        self.trace = trace
        self.audit = audit

    def evaluate(self, run_id: str) -> EvalResult:
        tree = self.trace.get_run_tree(run_id)
        return EvalResult(
            suite="deterministic",
            checks=[
                self._integrity(run_id),
                self._completeness(tree),
                self._rbac_contract(run_id, tree),
                self._audit_pairs(run_id, tree),
                self._audit_integrity(),
                self._guardrails(tree),
            ],
        )

    # ---- checks ----

    def _integrity(self, run_id: str) -> EvalCheck:
        ok, issues = self.trace.verify_integrity(run_id)
        return EvalCheck(
            name="trace_integrity",
            passed=ok,
            detail="; ".join(issues) or "integrity chain intact",
            score=1.0 if ok else 0.0,
        )

    def _completeness(self, tree: list[dict[str, Any]]) -> EvalCheck:
        missing: list[str] = []
        types = [e["type"] for e in tree]
        if not types:
            missing.append("no events")
        for required in ("run_start", "node", "run_end"):
            if required not in types:
                missing.append(required)
        if types and [e["seq"] for e in tree] != list(range(len(tree))):
            missing.append("contiguous seq")

        # Plan §8 "trace completeness": every PROMPT must have a matching REASONING.
        for i, e in enumerate(tree):
            if e["type"] == TraceEventType.PROMPT.value:
                nxt = tree[i + 1] if i + 1 < len(tree) else None
                if nxt is None or nxt["type"] != TraceEventType.REASONING.value:
                    missing.append(f"reasoning after prompt at seq {e['seq']}")
                    break
        return EvalCheck(
            name="trace_completeness",
            passed=not missing,
            detail="; ".join(missing)
            or "run_start / node / prompt → reasoning / run_end present, seq contiguous",
            score=1.0 if not missing else 0.0,
        )

    def _rbac_contract(self, run_id: str, tree: list[dict[str, Any]]) -> EvalCheck:
        """RBAC contract: no resource traced as allowed while audit denied it,
        and every denied outcome is justified by a real audit decision."""
        if self.audit is None:
            return EvalCheck(name="rbac_contract", passed=True, score=1.0, detail="audit not provided; skipped")

        denied_by_audit: set[tuple[str, str]] = set()
        allowed_by_audit: set[tuple[str, str]] = set()
        for entry in self.audit.entries():
            if entry.get("run_id") != run_id:
                continue
            res = entry.get("resource") or {}
            if res.get("type") not in ("tool", "skill"):
                continue
            key = (str(res["type"]), str(res["id"]))
            if entry.get("decision") in _DENIED_DECISIONS:
                denied_by_audit.add(key)
            elif entry.get("decision") in _ALLOWED_DECISIONS:
                allowed_by_audit.add(key)

        denied_tool_events = [
            e
            for e in tree
            if e["type"] == TraceEventType.TOOL_CALL.value and e.get("payload", {}).get("outcome") == "denied"
        ]
        # A skill invocation is traced twice: one SKILL_USE plus one aggregate
        # TOOL_CALL (the skill exposed as a tool). The aggregate's allowed/denied
        # reflects the whole skill run, not a separate RBAC decision on the skill
        # name itself — so it must NOT be held to the tool audit contract.
        skill_names = {
            str(e.get("payload", {}).get("skill", ""))
            for e in tree
            if e["type"] == TraceEventType.SKILL_USE.value
        }

        violations: list[str] = []
        for e in tree:
            payload = e.get("payload") or {}
            if e["type"] == TraceEventType.SKILL_USE.value:
                rtype, rid = "skill", str(payload.get("skill", ""))
                is_skill_aggregate = True
            elif e["type"] == TraceEventType.TOOL_CALL.value:
                rtype, rid = "tool", str(payload.get("tool", ""))
                is_skill_aggregate = rid in skill_names
            else:
                continue
            outcome = payload.get("outcome")
            if outcome == "allowed":
                if (rtype, rid) in denied_by_audit:
                    violations.append(f"{rtype}={rid} traced 'allowed' but audit denied it")
                elif (rtype, rid) not in allowed_by_audit and not (
                    is_skill_aggregate and ("skill", rid) in allowed_by_audit
                ):
                    violations.append(f"{rtype}={rid} traced 'allowed' but audit has no ALLOW/APPROVED")
            elif outcome == "denied":
                justified = (rtype, rid) in denied_by_audit
                if not justified and is_skill_aggregate:
                    # a skill (or its aggregate) is denied because its gate was
                    # denied OR because a NESTED tool call was denied
                    justified = ("skill", rid) in denied_by_audit or bool(denied_tool_events)
                if not justified:
                    violations.append(f"{rtype}={rid} traced 'denied' with no matching audit decision")
        return EvalCheck(
            name="rbac_contract",
            passed=not violations,
            detail="; ".join(violations) or "no denied resource executed; every denial audited",
            score=1.0 if not violations else 0.0,
        )

    def _audit_pairs(self, run_id: str, tree: list[dict[str, Any]]) -> EvalCheck:
        """Every audit decision for the run maps back to a trace event (the audit
        and trace logs share event conventions — plan.md §7/§12)."""
        if self.audit is None:
            return EvalCheck(name="audit_pairs", passed=True, score=1.0, detail="audit not provided; skipped")

        events = [
            e for e in tree if e["type"] in (TraceEventType.TOOL_CALL.value, TraceEventType.SKILL_USE.value, TraceEventType.MEMORY_WRITE.value)
        ]
        missing: list[str] = []
        for entry in self.audit.entries():
            if entry.get("run_id") != run_id:
                continue
            res = entry.get("resource") or {}
            rtype, rid = res.get("type"), res.get("id")
            matches = [e for e in events if _event_matches(e, rtype, rid)]
            if not matches:
                missing.append(f"audit {entry.get('decision')} {rtype}={rid} has no trace event")
        return EvalCheck(
            name="audit_pairs",
            passed=not missing,
            detail="; ".join(missing) or "every audit decision has a trace event",
            score=1.0 if not missing else 0.0,
        )

    def _audit_integrity(self) -> EvalCheck:
        """Audit chain is intact — no edited/reordered/deleted decision lines
        (plan.md §14 "audit + trace are append-only, hashed")."""
        if self.audit is None:
            return EvalCheck(name="audit_integrity", passed=True, score=1.0, detail="audit not provided; skipped")
        ok, issues = self.audit.verify_integrity()
        return EvalCheck(
            name="audit_integrity",
            passed=ok,
            detail="; ".join(issues) or "audit chain intact",
            score=1.0 if ok else 0.0,
        )

    def _guardrails(self, tree: list[dict[str, Any]]) -> EvalCheck:
        """A terminal guardrail (budget/max_steps/timeout) must stop the loop —
        no tool may run after it."""
        terminal_seq: int | None = None
        for e in tree:
            if e["type"] == TraceEventType.GUARDRAIL.value and e.get("payload", {}).get("type") in TERMINAL_GUARDRAILS:
                terminal_seq = e["seq"]
        violations: list[str] = []
        if terminal_seq is not None:
            after = [
                e["seq"]
                for e in tree
                if e["type"] == TraceEventType.TOOL_CALL.value and e["seq"] > terminal_seq
            ]
            if after:
                violations.append(f"tool calls after terminal guardrail at seq {terminal_seq}")
        return EvalCheck(
            name="guardrails",
            passed=not violations,
            detail="; ".join(violations) or (
                "loop bounded" if terminal_seq is not None else "no terminal guardrail triggered"
            ),
            score=1.0 if not violations else 0.0,
        )


def _event_matches(event: dict[str, Any], rtype: str | None, rid: Any) -> bool:
    if not rtype:
        return False
    payload = event.get("payload") or {}
    if rtype == "tool":
        return event["type"] == TraceEventType.TOOL_CALL.value and payload.get("tool") == rid
    if rtype == "skill":
        return event["type"] == TraceEventType.SKILL_USE.value and payload.get("skill") == rid
    if rtype == "memory":
        return event["type"] == TraceEventType.MEMORY_WRITE.value and payload.get("scope") == rid
    return False
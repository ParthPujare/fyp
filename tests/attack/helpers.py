"""Shared helpers for the attack suites (Milestone 8, plan_mk1_core_harness.md §11 row 8).

Attack suites prove the harness holds under adversarial scenarios even when the
model *fully complies* with the attack (e.g. a delivered prompt injection) —
enforcement is in code and policy, never in the prompt. Scenarios map to the
Attack Test Matrix A1-A8 in `ref_docs/proposed_architecture.md` §15 and to the
threat model in `ref_docs/threat_model.md`.

Root-level fixtures in tests/conftest.py (enforcer, audit_log, trace_store,
dept_apps, executor, runner_factory, …) apply to this package automatically.
"""

from __future__ import annotations

from typing import Any

from app.auth.users import USERS


def claims_for(username: str) -> dict[str, Any]:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


ALICE = claims_for("alice@acme.com")  # marketing
BOB = claims_for("bob@acme.com")  # hr
CAROL = claims_for("carol@acme.com")  # finance
DAVE = claims_for("dave@acme.com")  # devops
ADMIN = claims_for("admin@acme.com")


def decisions_for(audit, run_id: str) -> list[dict[str, Any]]:
    """The audit decisions recorded for one run."""
    return [e for e in audit.entries() if e["run_id"] == run_id]


def outcome_names(result) -> list[str]:
    return [c["name"] for c in result.tool_calls]


def outcome_of(result, tool: str) -> dict[str, Any]:
    return next(c for c in result.tool_calls if c["name"] == tool)
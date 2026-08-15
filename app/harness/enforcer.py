"""Casbin policy enforcer — the deterministic RBAC decision point (plan.md §3, §4).

Wraps a `casbin.Enforcer` loaded from `app/harness/policies/{model.conf,policy.csv}`.
Enforcement is deny-by-default: a `(sub, dom, obj, act)` tuple with no matching
allow row is denied. This replaces the scope-gate stub (`app/agent/enforce.py`)
from weeks 3-4; the call shape (`(tool, claims) -> bool`) is preserved so the
agent-graph swap is internal.

Tool scopes are `dept:action` (e.g. `crm:read`) and map to Casbin
`(obj=dept, act=action)`. The subject is the authenticated user (`sub`) resolved
to a role via the `g` role-assignment rows in policy.csv.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import casbin

from app.agent.tools import ToolSpec
from app.memory.scopes import SCOPE_CENTRAL

POLICY_DIR = Path(__file__).resolve().parent / "policies"
MODEL_PATH = POLICY_DIR / "model.conf"
POLICY_PATH = POLICY_DIR / "policy.csv"


class PolicyEnforcer:
    def __init__(
        self,
        model_path: str | Path = MODEL_PATH,
        policy_path: str | Path = POLICY_PATH,
    ):
        self._enforcer = casbin.Enforcer(str(model_path), str(policy_path))
        self._enforcer.load_policy()

    def enforce(self, sub: str, dom: str, obj: str, act: str) -> bool:
        """True iff the request (sub, dom, obj, act) matches an allow row."""
        return self._enforcer.enforce(sub, dom, obj, act)

    @staticmethod
    def _obj_act(tool: ToolSpec) -> tuple[str, str]:
        dept, _, act = tool.required_scope.partition(":")
        return (dept or tool.dept, act)

    def tool_allowed(self, tool: ToolSpec, claims: dict[str, Any]) -> bool:
        """Keep the (tool, claims) -> bool shape of the removed scope-gate stub."""
        obj, act = self._obj_act(tool)
        return self.enforce(
            str(claims.get("sub", "")),
            str(claims.get("dept", "")),
            obj,
            act,
        )

    def memory_allowed(self, scope: str, act: str, claims: dict[str, Any]) -> bool:
        """Can (role, dept) read/write a memory scope? (plan.md §5.1, §5.10).

        Scopes are tier-gated + self-scoped: a dept scope must be the caller's own
        dept and a user scope must be the caller's own uid — cross-tier reads and
        writes are denied even if a wildcard policy row existed.

        Note: obj names use underscores (`memory_central`) because Casbin's
        `keyMatch2` treats `:` as a route-parameter delimiter — `memory:central`
        would match `memory:dept`.
        """
        sub = str(claims.get("sub", ""))
        dom = str(claims.get("dept", ""))
        if scope == SCOPE_CENTRAL:
            return self.enforce(sub, dom, "memory_central", act)
        kind, _, value = scope.partition("/")
        if kind == "dept":
            if value != dom:
                return False
            return self.enforce(sub, dom, "memory_dept", act)
        if kind == "user":
            if value != sub:
                return False
            return self.enforce(sub, dom, "memory_user", act)
        return False

    def memory_recall_allowed(self, claims: dict[str, Any]) -> bool:
        """Gate for the `recall` tool (FTS5 episodic search)."""
        return self.enforce(
            str(claims.get("sub", "")),
            str(claims.get("dept", "")),
            "memory_recall",
            "use",
        )

    def skill_allowed(self, skill_name: str, claims: dict[str, Any]) -> bool:
        """Can (role, dept) use a skill? (plan.md §9 — Casbin row per skill).

        A Marketing skill has no row for HR, so it is invisible to HR (filter_tools)
        and denied even if invoked at runtime (intercept_skill).
        """
        return self.enforce(
            str(claims.get("sub", "")),
            str(claims.get("dept", "")),
            f"skill_{skill_name}",
            "use",
        )

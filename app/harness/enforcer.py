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

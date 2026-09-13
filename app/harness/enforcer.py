"""Casbin policy enforcer — the deterministic RBAC decision point (plan_mk1_core_harness.md §3, §4).

Wraps a `casbin.Enforcer` loaded from `app/harness/policies/{model.conf,policy.csv}`.
Enforcement is deny-by-default: a `(sub, dom, obj, act)` tuple with no matching
allow row is denied.

Since plan_mk2 Phase 2 the user->role->domain **`g` assignments are materialized
live from the MemberStore** (`data/members.sqlite`, `app/auth/users.get_store()`)
for ACTIVE members only — policy.csv keeps only the role-level `p` rows. An
unknown or disabled member has no `g` row, so every action is denied by default.
A version guard makes `g` refresh automatically: the enforcer caches the store's
`members_version` write counter and rebuilds the Casbin instance (under a lock)
lazily on the next enforce call after any member create/update/deactivate/reset.

Tool scopes are `dept:action` (e.g. `crm:read`) and map to Casbin
`(obj=dept, act=action)`. The subject is the authenticated user (`sub`) resolved
to a role via its `g` role-assignment.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import casbin

from app.agent.tools import ToolSpec
from app.auth.users import get_store
from app.memory.scopes import SCOPE_CENTRAL

POLICY_DIR = Path(__file__).resolve().parent / "policies"
MODEL_PATH = POLICY_DIR / "model.conf"
POLICY_PATH = POLICY_DIR / "policy.csv"


def head_role_for(dept: str) -> str:
    """Canonical head role for a department (Mk12): `marketing` -> `marketing_head`."""
    return f"{dept}_head"


def dept_of_scope(scope: str) -> str | None:
    """Owning department of a `dept/<name>` scope, else None (Mk12)."""
    kind, _, value = scope.partition("/")
    return value if kind == "dept" and value else None


class PolicyEnforcer:
    def __init__(
        self,
        model_path: str | Path = MODEL_PATH,
        policy_path: str | Path = POLICY_PATH,
    ):
        self._model_path = str(model_path)
        self._policy_path = str(policy_path)
        self._lock = threading.Lock()
        self._members_version = -1
        self._build()

    # ---- g materialization + version guard ----

    def _store_version(self) -> int:
        return get_store().version

    def _member_assignments(self) -> list[tuple[str, str, str]]:
        """g rows for ACTIVE members only (unknown/disabled => no g => deny)."""
        return [
            (m.username, m.role, m.dept)
            for m in get_store().list()
            if m.active
        ]

    def _build(self) -> None:
        enforcer = casbin.Enforcer(self._model_path, self._policy_path)
        enforcer.load_policy()
        _enforcer = enforcer
        for sub, role, dept in self._member_assignments():
            _enforcer.add_role_for_user_in_domain(sub, role, dept)
        self._enforcer = _enforcer
        self._members_version = self._store_version()

    def _sync_g(self) -> None:
        """Rebuild g under the lock if the member store changed (stale-g fix)."""
        if self._store_version() != self._members_version:
            with self._lock:
                if self._store_version() != self._members_version:
                    self._build()

    def enforce(self, sub: str, dom: str, obj: str, act: str) -> bool:
        """True iff the request (sub, dom, obj, act) matches an allow row."""
        self._sync_g()
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
        """Can (role, dept) read/write a memory scope? (plan_mk1_core_harness.md §5.1, §5.10).

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

    def is_dept_head(self, claims: dict[str, Any], scope: str) -> bool:
        """True iff claims hold the `{dept}_head` role for this dept scope (Mk12).

        `dept/marketing` is head-owned by role `marketing_head` with `dept == marketing`.
        Central/user scopes are never head-owned.
        """
        kind, _, value = scope.partition("/")
        if kind != "dept" or not value:
            return False
        return (
            str(claims.get("role", "")) == f"{value}_head"
            and str(claims.get("dept", "")) == value
        )

    def memory_recall_allowed(self, claims: dict[str, Any]) -> bool:
        """Gate for the `recall` tool (FTS5 episodic search)."""
        return self.enforce(
            str(claims.get("sub", "")),
            str(claims.get("dept", "")),
            "memory_recall",
            "use",
        )

    def skill_allowed(self, skill_name: str, claims: dict[str, Any]) -> bool:
        """Can (role, dept) use a skill? (plan_mk1_core_harness.md §9 — Casbin row per skill).

        A Marketing skill has no row for HR, so it is invisible to HR (filter_tools)
        and denied even if invoked at runtime (intercept_skill).
        """
        return self.enforce(
            str(claims.get("sub", "")),
            str(claims.get("dept", "")),
            f"skill_{skill_name}",
            "use",
        )
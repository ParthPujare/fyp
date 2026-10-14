"""Attack A1 — exhaustive policy matrix (roles × departments × resources).

The policy matrix doubles as the test suite (plan_mk1_core_harness.md §12 "Policy matrix"). This
module cross-validates the Casbin enforcer against the *source of truth*: the
allow rows in `app/harness/policies/policy.csv` and the ACTIVE-member
user→role→department assignments materialized from the MemberStore (plan_mk2
Phase 2 — `g` no longer lives in policy.csv). Everything not granted by an
explicit row is denied (deny-by-default) — including unknown objects, actions,
and users.
"""

from __future__ import annotations

from pathlib import Path

from app.agent.tools import TOOL_REGISTRY
from app.auth.users import get_store
from app.harness.enforcer import POLICY_DIR, PolicyEnforcer
from app.memory.scopes import SCOPE_CENTRAL, scope_dept, scope_user
from app.skills.registry import SkillRegistry
from tests.attack.helpers import ADMIN, ALICE, BOB, CAROL, DAVE

USERS = [ALICE, BOB, CAROL, DAVE, ADMIN]


def _parse_policy(path: Path) -> list[list[str]]:
    """Parse policy.csv into the allow rows (`p`) — `g` is store-materialized."""
    rows: list[list[str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        cols = [c.strip() for c in line.split(",")]
        if cols[0] == "p":
            rows.append(cols)
    return rows


def _store_assignments() -> dict[str, tuple[str, str]]:
    """{username: (role, dept)} for ACTIVE members only (deny-by-default gate)."""
    return {
        m.username: (m.role, m.dept)
        for m in get_store().list()
        if m.active
    }


POLICY = _parse_policy(POLICY_DIR / "policy.csv")
ASSIGNMENTS = _store_assignments()


def _obj_act(tool) -> tuple[str, str]:
    obj, _, act = tool.required_scope.partition(":")
    return (obj or tool.dept, act)


def _expected(user: str, obj: str, act: str) -> bool:
    """Derive the expected allow/deny from policy.csv + store assignments (oracle)."""
    assignment = ASSIGNMENTS.get(user)
    if assignment is None:
        return False
    role, dom = assignment
    return any(
        r[1] == role and r[2] == dom and (r[3] == obj or r[3] == "*") and (r[4] == act or r[4] == "*")
        for r in POLICY
    )


def expected_tools_for(claims) -> set[str]:
    user = claims["sub"]
    return {n for n, t in TOOL_REGISTRY.items() if _expected(user, *_obj_act(t))}


def _mapping_for_scope(scope: str, user: str) -> tuple[str, str]:
    kind, _, value = scope.partition("/")
    if scope == SCOPE_CENTRAL:
        return "memory_central", "read"
    if kind == "dept":
        obj = "memory_dept" if value == ASSIGNMENTS[user][1] else None
        return obj, value
    if kind == "user":
        obj = "memory_user" if value == user else None
        return obj, value
    return None, value  # type: ignore[return-value]


# --------------------------------------------------------------------------- tools


def test_full_tool_matrix_matches_policy_rows(enforcer):
    """Every role × every tool → the enforcer's answer equals the policy.csv oracle."""
    mismatches: list[str] = []
    for claims in USERS:
        user = claims["sub"]
        for name, tool in TOOL_REGISTRY.items():
            obj, _, act = tool.required_scope.partition(":")
            obj = obj or tool.dept
            expected = _expected(user, obj, act)
            actual = enforcer.tool_allowed(tool, claims)
            if actual is not expected:
                mismatches.append(f"{user} {name}: expected {expected}, got {actual}")
    assert not mismatches, "\n".join(mismatches)


def test_per_role_allowed_tool_sets_match_oracle(enforcer):
    """Only the tool set the policy grants each role is ever allowed (cross-dept
    denial-by-default included)."""
    for claims in USERS:
        expected = expected_tools_for(claims)
        actual = {
            name for name, tool in TOOL_REGISTRY.items() if enforcer.tool_allowed(tool, claims)
        }
        assert actual == expected, f"{claims['role']}: {actual.symmetric_difference(expected)}"
    # sanity: the marketing role reaches exactly its CRM tools (+ recall), never HR/DevOps/Finance
    assert expected_tools_for(ALICE) == {
        n for n in TOOL_REGISTRY if n.startswith(("crm_", "recall"))
    }


def test_admin_wildcard_covers_every_tool(enforcer):
    for name, tool in TOOL_REGISTRY.items():
        assert enforcer.tool_allowed(tool, ADMIN), name


def test_unknown_object_action_and_user_denied(enforcer):
    # non-admin roles: deny-by-default on unknown objects/actions
    for claims in USERS[:4]:
        assert not enforcer.enforce(claims["sub"], claims["dept"], "ghost", "read")
        assert not enforcer.enforce(claims["sub"], claims["dept"], "crm", "promote")
    # Mk18 Phase E: the admin wildcard is gone — unknown objects/actions are
    # denied even for admins (least privilege; new tools need explicit rows).
    assert not enforcer.enforce(ADMIN["sub"], ADMIN["dept"], "ghost", "read")
    assert not enforcer.enforce(ADMIN["sub"], ADMIN["dept"], "crm", "promote")
    # …but a user with no role assignment is denied even in the admin domain
    assert not enforcer.enforce("intruder@evil.com", "marketing", "crm", "read")
    assert not enforcer.enforce("ghost@acme.com", "admin", "crm", "read")


# --------------------------------------------------------------------------- memory


def test_full_memory_matrix_matches_policy_rows(enforcer):
    """Read/write over every scope a role could name — central / own+other dept /
    own+other user — matches the policy oracle, for every role."""
    mismatches: list[str] = []
    for claims in USERS:
        user = claims["sub"]
        candidates = [
            SCOPE_CENTRAL,
            scope_dept(claims["dept"]),  # own dept
            scope_dept("hr" if claims["dept"] != "hr" else "finance"),  # someone else's dept
            scope_user(user),  # self
            scope_user("vip.user@acme.com"),  # another user
        ]
        for scope in candidates:
            obj, _value = _mapping_for_scope(scope, user)
            for act in ("read", "write"):
                if obj is None:
                    expected = False
                else:
                    expected = _expected(user, obj, act)
                actual = enforcer.memory_allowed(scope, act, claims)
                if actual is not expected:
                    mismatches.append(
                        f"{user} {scope} {act}: expected {expected}, got {actual}"
                    )
    assert not mismatches, "\n".join(mismatches)


def test_memory_cross_tier_denials(enforcer):
    """A marketing user can never read or write HR's or another user's tier."""
    candidates = [
        (ALICE, scope_dept("hr")),
        (ALICE, scope_dept("finance")),
        (ALICE, scope_user("bob@acme.com")),
        (BOB, scope_dept("marketing")),
        (CAROL, scope_user("alice@acme.com")),
        (DAVE, scope_user("carol@acme.com")),
    ]
    for claims, scope in candidates:
        for act in ("read", "write"):
            assert not enforcer.memory_allowed(scope, act, claims), (claims["role"], scope, act)


# --------------------------------------------------------------------------- skills


def test_full_skill_matrix_matches_policy_rows(enforcer):
    """Every role × every registry skill matches the per-skill policy rows."""
    skills = [s.name for s in SkillRegistry().load_all()]
    mismatches: list[str] = []
    for claims in USERS:
        for name in skills:
            expected = _expected(claims["sub"], f"skill_{name}", "use")
            actual = enforcer.skill_allowed(name, claims)
            if actual is not expected:
                mismatches.append(f"{claims['role']} {name}: expected {expected}, got {actual}")
    assert not mismatches, "\n".join(mismatches)


def test_skills_misuse_matrix(enforcer):
    """A marketing skill is inaccessible to every other (non-admin) department."""
    for claims in USERS[:4]:
        if claims["role"] == "marketing_agent":
            assert enforcer.skill_allowed("launch_campaign", claims)
        else:
            assert not enforcer.skill_allowed("launch_campaign", claims), claims["role"]
    for claims in USERS[:4]:
        if claims["role"] == "hr_agent":
            assert enforcer.skill_allowed("new_leave_request", claims)
        else:
            assert not enforcer.skill_allowed("new_leave_request", claims), claims["role"]
    # admin wildcard grants every skill
    for name in ("launch_campaign", "new_leave_request", "triage_incident"):
        assert enforcer.skill_allowed(name, ADMIN)


def _matrix_snapshot(enforcer) -> str:
    """Human-readable matrix used by the demo / evidence dump."""
    lines = []
    for claims in USERS:
        allowed = sorted(
            name for name, tool in TOOL_REGISTRY.items() if enforcer.tool_allowed(tool, claims)
        )
        lines.append(f"{claims['role']:18} -> {', '.join(allowed) or '(none)'}")
    return "\n".join(lines)


def test_matrix_is_stable_across_evaluations(enforcer):
    """Same input → same matrix (audit determinism at the enforcement layer)."""
    assert _matrix_snapshot(enforcer) == _matrix_snapshot(PolicyEnforcer())
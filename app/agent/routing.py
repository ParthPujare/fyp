"""Approval routing (Mk15) — who must approve a gated item, decided in one place.

Pure functions (no database, no I/O) so the whole routing table is unit-testable. The agent
gates (`reflect` for memory writes, the tool gate in `execute_tools`) look up the inputs —
department mode, active head count, allowlist hit — and ask `route_for` for the answer:

    none        commit / execute straight away
    head-only   the department head decides
    head+admin  the head approves first (step 1), then an admin (step 2)
    admin-only  an admin decides (central memory, tools no department owns, no active head)

Allowlist ("always allow this exact action", todo #11) keys are built here too:
`(member, action, args_hash)` where `action` is `memory:<scope>:<op>` or `tool:<name>` and
`args_hash` is the SHA-256 of the canonical JSON of the gated payload. Exact match only — any
changed argument is a different hash and goes back to a human.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.memory.approvals import (
    LEVEL_ADMIN,
    LEVEL_HEAD,
    ROUTE_ADMIN_ONLY,
    ROUTE_HEAD_ADMIN,
    ROUTE_HEAD_ONLY,
)

ROUTE_NONE = "none"

KIND_MEMORY = "memory"
KIND_TOOL = "tool"

RISK_CRITICAL = "critical"
RISK_WRITE = "write"

# Tool-argument keys that change on every call without changing what the call does. They
# are dropped before hashing so an "always allow" rule still matches the same action.
VOLATILE_ARG_KEYS = frozenset(
    {"timestamp", "ts", "request_id", "idempotency_key", "nonce", "trace_id"}
)


def action_key_for(kind: str, *, scope: str = "", op: str = "", tool: str = "") -> str:
    """`memory:<scope>:<op>` (e.g. `memory:dept/marketing:add`) or `tool:<name>`."""
    if kind == KIND_MEMORY:
        return f"memory:{scope}:{op}"
    return f"tool:{tool}"


def allowlist_payload(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    """The part of a gated payload an allowlist rule is matched on.

    Memory: `{op, text, match}` as-is. Tools: `{tool, args}` minus `VOLATILE_ARG_KEYS`.
    """
    if kind != KIND_TOOL:
        return dict(payload)
    args = payload.get("args") or {}
    stable = {k: v for k, v in args.items() if k not in VOLATILE_ARG_KEYS} if isinstance(args, dict) else args
    return {"tool": payload.get("tool"), "args": stable}


def canonical_args(payload: dict[str, Any]) -> str:
    """Canonical JSON: sorted keys, compact separators — byte-identical for equal payloads."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def args_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_args(payload).encode("utf-8")).hexdigest()


def is_allowlist_eligible(kind: str, *, scope: str = "", risk: str = "") -> bool:
    """Department memory writes and non-critical tools may be "always allowed".

    Central writes and `risk=critical` tools never are: they need a human every time.
    """
    if kind == KIND_MEMORY:
        return scope.startswith("dept/") and len(scope) > len("dept/")
    return risk != RISK_CRITICAL


def _is_head_of(role: str, actor_dept: str, dept: str) -> bool:
    return bool(dept) and role == f"{dept}_head" and actor_dept == dept


def _dept_route(dept_mode: str, head_count: int) -> str:
    if head_count <= 0:
        return ROUTE_ADMIN_ONLY  # nobody to take step 1: escalate (Mk12 behaviour)
    return ROUTE_HEAD_ADMIN if dept_mode == ROUTE_HEAD_ADMIN else ROUTE_HEAD_ONLY


def route_for(
    *,
    kind: str,
    scope: str = "",
    risk: str = "read",
    actor_role: str = "",
    actor_dept: str = "",
    dept_mode: str = ROUTE_HEAD_ONLY,
    head_count: int = 0,
    allowlist_hit: bool = False,
    gate_write_tools: bool = False,
) -> str:
    """The routing table. RBAC has already allowed the action before this is asked, so a
    cross-department attempt never gets here.

    Memory (`scope`):
      user/*   -> none
      central  -> admin-only (never allowlist-eligible)
      dept/X   -> none for X's own head; none on an allowlist hit; admin-only when X has no
                  active head; otherwise X's mode (head-only | head+admin)

    Tools (`risk`, attributed to the actor's department):
      critical -> always head+admin, even in a head-only department; admin-only when the
                  actor is an admin or the head themselves, or the department has no head
      write    -> ungated unless `gate_write_tools`; then like a dept memory write
      read     -> none
    """
    if kind == KIND_MEMORY:
        if scope == "central":
            return ROUTE_ADMIN_ONLY
        tier, _, dept = scope.partition("/")
        if tier != "dept" or not dept:
            return ROUTE_NONE
        if _is_head_of(actor_role, actor_dept, dept):
            # Mk18 Phase E: a head cannot self-approve their own dept writes —
            # route to an admin (the central second-admin rule still governs
            # who may decide there; dept items need any admin here).
            return ROUTE_ADMIN_ONLY
        if allowlist_hit:
            return ROUTE_NONE
        return _dept_route(dept_mode, head_count)

    is_admin = actor_role == "admin" or actor_dept in ("", "admin")
    is_head = _is_head_of(actor_role, actor_dept, actor_dept)
    if risk == RISK_CRITICAL:
        if is_admin or is_head or head_count <= 0:
            return ROUTE_ADMIN_ONLY
        return ROUTE_HEAD_ADMIN
    if risk == RISK_WRITE and gate_write_tools:
        if is_admin or is_head or allowlist_hit:
            return ROUTE_NONE
        return _dept_route(dept_mode, head_count)
    return ROUTE_NONE


def initial_level(route: str) -> str:
    """The step a freshly created approval waits on."""
    return LEVEL_HEAD if route in (ROUTE_HEAD_ONLY, ROUTE_HEAD_ADMIN) else LEVEL_ADMIN


def risk_of_tool(name: str) -> str:
    """Risk level of an internal or external tool; unknown tools count as critical."""
    from app.agent.tools import TOOL_REGISTRY

    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        try:
            from app.integrations.adapter import spec_for

            tool = spec_for(name)
        except Exception:  # noqa: BLE001 - integrations optional
            tool = None
    return tool.risk_level if tool is not None else RISK_CRITICAL


def entry_allowlist_eligible(entry: dict[str, Any]) -> bool:
    """Can a decided approval be turned into an "always allow" rule?"""
    resource = entry.get("resource") or {}
    if not entry.get("action") or not entry.get("args_hash"):
        return False  # predates Mk15, or was not department-attributed
    if resource.get("type") == KIND_MEMORY:
        return is_allowlist_eligible(KIND_MEMORY, scope=str(resource.get("id", "")))
    if not entry.get("dept"):
        return False
    return is_allowlist_eligible(KIND_TOOL, risk=risk_of_tool(str(resource.get("id", ""))))

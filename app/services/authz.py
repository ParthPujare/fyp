"""Mk19 Phase 3 — Casbin inside the department services (plan Mk19, finding 3).

The gateway's `intercept_tool` already decides every tool call, but a caller who
reaches a dept port directly (published port, host network, compromised gateway)
previously got an identity-only check. This module is the service-side decision
point using the SAME policy rows and the SAME live sources as the gateway:

* routes map to the same ``(obj, act)`` pairs as the tools they back — built from
  ``TOOL_REGISTRY`` (single source of truth), so a tool-scope change and a
  service-scope change cannot drift apart (proved by the parity test);
* fallback for routes no tool backs: ``GET→read``, ``POST→create``,
  ``DELETE→delete`` — unbacked writes land on acts ``policy.csv`` has NOT
  granted to member roles (the locked "deny for everyone" decision);
* ``PolicyEnforcer`` reads the configured policy store (policy.csv, or the shared
  ``policy_rules`` table when ``DATABASE_URL`` is set) and materializes ``g`` from
  the MemberStore — the enforcer is rebuilt when the *store object* changes so
  swapped test stores never leak grants;
* decisions audit to the shared chain with ``run_id="svc:<service>"``, so
  direct-port traffic is visible next to gateway-proxied traffic.
"""

from __future__ import annotations

import re
import threading

from app.config import get_settings
from app.harness.enforcer import PolicyEnforcer

# Fallback act for routes that no ToolSpec backs (locked decision: unbacked
# writes stay deny-by-default because policy has no matching member row).
_METHOD_FALLBACK: dict[str, str] = {"GET": "read", "POST": "create", "DELETE": "delete"}

_maps_lock = threading.Lock()
_route_maps: dict[str, dict[tuple[str, re.Pattern[str]], str]] = {}

_enforcer: PolicyEnforcer | None = None
_enforcer_store: object | None = None

_audit = None
_audit_path: str | None = None


def _template_regex(template: str) -> re.Pattern[str]:
    """/customers/{id} → ^/customers/[^/]+$ (path params never span a slash)."""
    pattern = re.escape(template)
    pattern = re.sub(r"\\\{[^/]+?\\\}", "[^/]+", pattern)
    return re.compile(f"^{pattern}$")


def _route_map(scope_obj: str) -> dict[tuple[str, re.Pattern[str]], str]:
    """(METHOD, path-regex) → act for this service's ToolSpec entries (cached)."""
    with _maps_lock:
        cached = _route_maps.get(scope_obj)
        if cached is not None:
            return cached
        from app.agent.tools import TOOL_REGISTRY

        entries: dict[tuple[str, re.Pattern[str]], str] = {}
        for tool in TOOL_REGISTRY.values():
            if tool.dept != scope_obj:
                continue
            _, _, act = tool.required_scope.partition(":")
            entries[(tool.http_method.upper(), _template_regex(tool.path_template))] = act or "read"
        _route_maps[scope_obj] = entries
        return entries


def act_for(scope_obj: str, method: str, path: str) -> str:
    """The Casbin action for one service route: tool-backed first, method default second."""
    method = method.upper()
    for (m, pattern), act in _route_map(scope_obj).items():
        if m == method and pattern.match(path):
            return act
    return _METHOD_FALLBACK.get(method, "read")


def get_service_enforcer() -> PolicyEnforcer:
    """Per-process enforcer singleton, rebuilt when the MemberStore object changes.

    The identity guard matters for the test suite: fixtures swap stores via
    `set_store`, and a version-counter-only check could miss the swap (a fresh
    store starts at version 1, same as the cached one).
    """
    global _enforcer, _enforcer_store
    from app.auth.users import get_store

    store = get_store()
    if _enforcer is None or store is not _enforcer_store:
        _enforcer = PolicyEnforcer()
        _enforcer_store = store
    return _enforcer


def decide(member, scope_obj: str, method: str, path: str) -> tuple[bool, str]:
    """(allowed, act) for one request against one service.

    `member` is the FRESH MemberStore record (not the JWT): role/dept edits and
    deactivations apply immediately, exactly like the gateway's live `g` rows.
    """
    act = act_for(scope_obj, method, path)
    allowed = get_service_enforcer().enforce(
        str(member.username), str(member.dept), scope_obj, act
    )
    return allowed, act


def get_service_audit():
    """Shared audit chain for service-side decisions (rebuilt if the path changes).

    Uses `open_audit_log` so a shared-DB deployment logs to the Postgres chain
    and a local deployment logs to the JSONL file — same as gateway/dashboard.
    """
    global _audit, _audit_path
    from app.harness.audit import open_audit_log

    path = str(get_settings().services.audit_path)
    if _audit is None or _audit_path != path:
        _audit = open_audit_log(path)
        _audit_path = path
    return _audit


def log_route_decision(
    *, scope_obj: str, method: str, path: str, member, decision: str, act: str
) -> None:
    """Append one ALLOW/DENY line for a direct-or-proxied table route."""
    resource = {"type": "route", "id": f"{scope_obj} {method} {path}"}
    actor = {
        "sub": str(member.username),
        "role": str(member.role),
        "dept": str(member.dept),
    }
    get_service_audit().log(
        decision=decision,
        resource=resource,
        actor=actor,
        run_id=f"svc:{scope_obj}",
        reason="rbac",
    )

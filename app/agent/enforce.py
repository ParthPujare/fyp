"""Scope gate stub — minimal enforcement before Casbin lands in weeks 5-6.

A tool call is allowed only if the authenticated JWT claims carry the tool's
required scope (e.g. `crm:read`). The admin user holds `["*"]` and passes
everything. This is a deliberate, documented placeholder for
`enforcer.enforce(sub, dom, obj, act)` in the Casbin milestone; the call shape
(claims + tool spec → bool) is kept identical so the swap is internal.
"""

from __future__ import annotations

from typing import Any

from app.agent.tools import ToolSpec

WILDCARD = "*"


def scope_allowed(tool: ToolSpec, claims: dict[str, Any]) -> bool:
    scopes = claims.get("scopes") or []
    if WILDCARD in scopes:
        return True
    return tool.required_scope in scopes

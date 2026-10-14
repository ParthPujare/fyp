"""intercept — runtime enforcement wrapper (harness pipeline step 4, plan_mk1_core_harness.md §4).

The deterministic gate applied to every tool call before execution: an allow
decision is audited and the call proceeds (`None` is returned so the caller
executes); a deny returns a permission error that never reaches the dept service.
This is graph logic, not prompting — the LLM cannot bypass it.
"""

from __future__ import annotations

from typing import Any

from app.agent.tools import ToolResult, ToolSpec
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.sanitize import scan_args_for_exfil

# Tools whose args are the operator's own search text (stays in-boundary) —
# exempt from the outbound exfil scan to avoid false positives.
_EXFIL_EXEMPT_TOOLS = frozenset({"recall"})


def intercept_tool(
    tool: ToolSpec,
    args: dict[str, Any],
    *,
    enforcer: PolicyEnforcer,
    claims: dict[str, Any],
    audit: AuditLog,
    run_id: str,
) -> ToolResult | None:
    """Return a denied ToolResult if blocked, else None (proceed to execution)."""
    actor = {
        "sub": claims.get("sub", ""),
        "role": claims.get("role", ""),
        "dept": claims.get("dept", ""),
    }
    resource = {"type": "tool", "id": tool.name}
    if enforcer.tool_allowed(tool, claims):
        # Mk18 Phase B: outbound DLP — secret-shaped values in args never
        # leave, even on an allowed call. Checked after RBAC so denies stay
        # RBAC-attributed; exfil gets its own audit reason.
        if tool.name not in _EXFIL_EXEMPT_TOOLS:
            label = scan_args_for_exfil(args or {})
            if label is not None:
                audit.log(decision="DENY", resource=resource, actor=actor, run_id=run_id, reason="exfil")
                return ToolResult(
                    ok=False,
                    denied=True,
                    reason="exfil",
                    content=f"Blocked: tool arguments contain a secret-shaped value ({label}).",
                )
        audit.log(decision="ALLOW", resource=resource, actor=actor, run_id=run_id)
        return None
    audit.log(decision="DENY", resource=resource, actor=actor, run_id=run_id, reason="rbac")
    return ToolResult(
        ok=False,
        denied=True,
        reason="rbac",
        content=f"Permission denied: you need scope {tool.required_scope} to call {tool.name}.",
    )

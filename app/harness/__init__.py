"""Harness — the zero-trust enforcement boundary (plan.md §4).

Policy (Casbin model + policy.csv), per-role tool filtering, runtime
interception, and the append-only audit log. Memory (weeks 7-8), PII
sanitization (weeks 9-10), and skills (week 11) will join this package.
"""

from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.filter import filter_tools
from app.harness.intercept import intercept_tool

__all__ = ["AuditLog", "PolicyEnforcer", "filter_tools", "intercept_tool"]

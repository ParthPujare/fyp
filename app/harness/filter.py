"""filter_tools — per-role tool schema filtering (harness pipeline step 1, plan.md §4).

Only tools the caller's role is allowed to use ever enter the LLM context. A
Marketing user's model context contains CRM tool schemas only — HR/Finance/DevOps
tools are invisible, so the model cannot even propose them.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.agent.tools import ToolSpec
from app.harness.enforcer import PolicyEnforcer


def filter_tools(
    tools: Iterable[ToolSpec],
    enforcer: PolicyEnforcer,
    claims: dict[str, Any],
) -> list[ToolSpec]:
    return [t for t in tools if enforcer.tool_allowed(t, claims)]

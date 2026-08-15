"""RecallService — on-demand FTS5 episodic search (plan_mk1_core_harness.md §5.3, §7.1).

The `recall` tool is gated by Casbin like any other tool; inside, it searches only
the scopes the caller is permitted to READ (central + own dept + own user). There
is no code path that searches a scope the caller cannot read.
"""

from __future__ import annotations

from typing import Any

from app.agent.tools import ToolResult
from app.config import MemorySettings
from app.harness.enforcer import PolicyEnforcer
from app.harness.sanitize import sanitize_text
from app.memory.episodic import EpisodicStore
from app.memory.scopes import SCOPE_CENTRAL, scope_dept, scope_user


class RecallService:
    def __init__(
        self,
        episodic: EpisodicStore,
        enforcer: PolicyEnforcer,
        settings: MemorySettings,
    ):
        self._episodic = episodic
        self._enforcer = enforcer
        self._settings = settings

    def readable_scopes(self, claims: dict[str, Any]) -> list[str]:
        scopes: list[str] = []
        candidates = [
            SCOPE_CENTRAL,
            scope_dept(str(claims.get("dept", ""))),
            scope_user(str(claims.get("sub", ""))),
        ]
        for scope in candidates:
            if self._enforcer.memory_allowed(scope, "read", claims):
                scopes.append(scope)
        return scopes

    def recall(self, query: str, claims: dict[str, Any]) -> ToolResult:
        scopes = self.readable_scopes(claims)
        hits = self._episodic.search(query, scopes, limit=self._settings.recall_top_k)
        if not hits:
            return ToolResult(
                ok=True,
                content=f"No recalled conversations matching {query!r} in your permitted scopes.",
            )
        lines = [
            f"[{h['scope']}] {sanitize_text(h['transcript'], claims, self._enforcer)}" for h in hits
        ]
        return ToolResult(ok=True, content="\n".join(lines))

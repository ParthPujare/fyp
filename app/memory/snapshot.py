"""Frozen memory snapshot — compiled system-prompt block (plan_mk1_core_harness.md §5.2, §5.3).

`load_memory` distills the caller's permitted scopes into one compact block that is
frozen for the session (persisted in graph state) and injected as a system message.
"""

from __future__ import annotations

from app.memory.scopes import SCOPE_CENTRAL

_HEADERS = {
    SCOPE_CENTRAL: "# Company memory (central)",
    "dept": "# Department memory",
    "user": "# Your private memory",
}


def _header(scope: str) -> str:
    if scope == SCOPE_CENTRAL:
        return _HEADERS[SCOPE_CENTRAL]
    if scope.startswith("dept/"):
        return f"{_HEADERS['dept']} ({scope})"
    if scope.startswith("user/"):
        return f"{_HEADERS['user']} ({scope})"
    return f"# {scope}"


def build_snapshot(facts_by_scope: dict[str, list[object]], max_chars: int) -> str:
    """facts_by_scope: {scope: [Item...]} with each item's value['text'] as the fact.

    Returned block is budget-capped to `max_chars` (whole lines only).
    """
    blocks: list[str] = []
    for scope, items in facts_by_scope.items():
        if not items:
            continue
        lines = [_header(scope)]
        for item in items:
            text = str(getattr(item, "value", {}).get("text", "") if hasattr(item, "value") else item).strip()
            if text:
                lines.append(f"- {text}")
        if len(lines) > 1:
            blocks.append("\n".join(lines))
    snapshot = "\n\n".join(blocks)
    if len(snapshot) <= max_chars:
        return snapshot
    truncated: list[str] = []
    used = 0
    for block in blocks:
        for line in block.split("\n"):
            if used + len(line) + 1 > max_chars:
                return "\n".join(truncated)
            truncated.append(line)
            used += len(line) + 1
    return "\n".join(truncated)

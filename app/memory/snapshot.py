"""Frozen memory snapshot — compiled system-prompt block (plan_mk1_core_harness.md §5.2, §5.3).

`load_memory` distills the caller's permitted scopes into one compact block that is
frozen for the session (persisted in graph state) and injected as a system message.
"""

from __future__ import annotations

from app.harness.untrusted import sanitize_untrusted
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


def _item_text(item: object) -> str:
    return str(
        getattr(item, "value", {}).get("text", "") if hasattr(item, "value") else item
    ).strip()


def _item_provenance(item: object) -> dict:
    value = getattr(item, "value", None)
    if isinstance(value, dict):
        prov = value.get("provenance")
        if isinstance(prov, dict):
            return {k: prov.get(k, "") for k in ("author", "run_id", "extraction")}
    return {}


def snapshot_provenance(facts_by_scope: dict[str, list[object]]) -> dict[str, list[dict]]:
    """Mk18 Phase G: per-scope author/extraction sidecar for the trace (the
    model-facing snapshot carries no provenance — storage + trace do)."""
    out: dict[str, list[dict]] = {}
    for scope, items in facts_by_scope.items():
        out[scope] = [_item_provenance(it) for it in items]
    return out


def _priority(text: str) -> int:
    """Mk18 Phase G: policy-like facts survive truncation first.

    0 = team/company policy markers, 1 = durable-preference markers, 2 = rest.
    Stable sort keeps recency order within each band.
    """
    try:
        from app.memory.extract import CENTRAL_MARKER, DEPT_MARKER, MEMORY_MARKER
    except Exception:  # noqa: BLE001
        return 2
    if CENTRAL_MARKER.search(text) or DEPT_MARKER.search(text):
        return 0
    if MEMORY_MARKER.search(text):
        return 1
    return 2


def build_snapshot(facts_by_scope: dict[str, list[object]], max_chars: int) -> str:
    """facts_by_scope: {scope: [Item...]} with each item's value['text'] as the fact.

    Returned block is budget-capped to `max_chars` (whole lines only).
    """
    blocks: list[str] = []
    for scope, items in facts_by_scope.items():
        if not items:
            continue
        lines = [_header(scope)]
        ordered = sorted(items, key=lambda it: _priority(_item_text(it)))
        for item in ordered:
            text = _item_text(item)
            if text:
                lines.append(f"- {sanitize_untrusted(f'memory:{scope}', text)}")
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

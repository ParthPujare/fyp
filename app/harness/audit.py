"""Audit log — append-only JSONL of every policy decision (plan.md §4, §12).

One line per decision: `ALLOW | DENY | APPROVED | REJECTED` (HITL decisions
arrive with weeks 9-10). Written at decision time, so the log records what
policy decided — not whether execution then succeeded. Shares event conventions
with the trace store (both keyed by `run_id`), so trace answers "what did the
agent do" and audit answers "what did policy decide".
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DECISIONS = ("ALLOW", "DENY", "APPROVED", "REJECTED")


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log(
        self,
        *,
        decision: str,
        resource: dict[str, Any],
        actor: dict[str, Any],
        run_id: str,
        reason: str = "rbac",
    ) -> dict[str, Any]:
        if decision not in DECISIONS:
            raise ValueError(f"invalid audit decision: {decision!r}")
        entry: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(),
            "event_id": str(uuid.uuid4()),
            "run_id": run_id,
            "decision": decision,
            "resource": resource,
            "actor": actor,
            "guardrail_hit": None,
            "reason": reason,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def __len__(self) -> int:
        return len(self.entries())

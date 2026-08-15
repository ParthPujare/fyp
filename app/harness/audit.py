"""Audit log — append-only JSONL of every policy decision (plan.md §4, §12).

One line per decision: `ALLOW | DENY | APPROVED | REJECTED` (HITL decisions
arrive with weeks 9-10). Written at decision time, so the log records what
policy decided — not whether execution then succeeded. Shares event conventions
with the trace store (both keyed by `run_id`), so trace answers "what did the
agent do" and audit answers "what did policy decide".

Integrity (weeks 13-14): every line carries a SHA-256 hash chained to the
previous line (`prev_hash`, genesis = 64 zero bytes), and the running head hash
is mirrored to a sidecar `.head` file — so editing, reordering, deleting, or
truncating past entries is detectable via `verify_integrity()`. Lines written
before this feature (no `entry`/`hash` envelope) are read as-is and reported as
legacy/un-covered by the chain.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DECISIONS = ("ALLOW", "DENY", "APPROVED", "REJECTED")

GENESIS = "0" * 64

_ENVELOPE = ("entry", "hash", "prev_hash")


def _hash_entry(prev_hash: str, entry: dict[str, Any]) -> str:
    """SHA-256 over the previous line's hash + this entry's canonical JSON."""
    blob = json.dumps(entry, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(prev_hash.encode("utf-8") + blob).hexdigest()


class AuditLog:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head = self.path.with_name(self.path.name + ".head")

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
        prev_hash = self._last_hash()
        entry_hash = _hash_entry(prev_hash, entry)
        record = {"prev_hash": prev_hash, "hash": entry_hash, "entry": entry}
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
        self._head.write_text(entry_hash, encoding="utf-8")
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self.path.open(encoding="utf-8") as fh:
            result: list[dict[str, Any]] = []
            for line in fh:
                if not line.strip():
                    continue
                obj = json.loads(line)
                result.append(obj.get("entry", obj))
            return result

    def verify_integrity(self) -> tuple[bool, list[str]]:
        """Recompute the chain. Returns (ok, issues). Detects edited/reordered/
        deleted/legacy-after-hashed/missing-head vs truncation."""
        if not self.path.exists():
            return False, ["audit file missing"]
        ok, issues = True, []
        expected = GENESIS
        head: str | None = None
        saw_hashed = False
        with self.path.open(encoding="utf-8") as fh:
            for lineno, line in enumerate(fh, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    ok, issues = False, issues + [f"line {lineno}: not valid JSON"]
                    continue
                if "entry" not in obj:  # legacy pre-chain line
                    if saw_hashed:
                        ok = False
                        issues.append(f"line {lineno}: legacy line after hashed line (tamper?)")
                    continue
                saw_hashed = True
                entry, stored = obj["entry"], obj.get("hash")
                if obj.get("prev_hash") != expected:
                    ok = False
                    issues.append(f"line {lineno}: prev_hash mismatch (edited/reordered/deleted)")
                computed = _hash_entry(expected, entry) if obj.get("prev_hash") == expected else None
                if computed is not None and stored != computed:
                    ok = False
                    issues.append(f"line {lineno}: hash mismatch (entry edited)")
                expected = stored
                head = stored
        if saw_hashed:
            if not self._head.exists():
                ok = False
                issues.append("head marker missing")
            elif self._head.read_text(encoding="utf-8").strip() != head:
                ok = False
                issues.append("head marker mismatch (trailing lines truncated)")
        return ok, issues

    def _last_hash(self) -> str:
        """Prev hash for the next append: the head marker when present (O(1)),
        otherwise scan the file's last hashed line once to bootstrap."""
        if self._head.exists():
            content = self._head.read_text(encoding="utf-8").strip()
            if content:
                return content
        last_hash = GENESIS
        if self.path.exists():
            with self.path.open(encoding="utf-8") as fh:
                for line in fh:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if "hash" in obj:
                        last_hash = obj["hash"]
        return last_hash

    def __len__(self) -> int:
        return len(self.entries())
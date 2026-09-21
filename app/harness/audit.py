"""Audit log — append-only JSONL of every policy decision (plan_mk1_core_harness.md §4, §12).

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
import hmac
import json
import os
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None  # type: ignore[assignment]
try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None  # type: ignore[assignment]

DECISIONS = ("ALLOW", "DENY", "APPROVED", "REJECTED")

GENESIS = "0" * 64

_ENVELOPE = ("entry", "hash", "prev_hash")


def _hash_entry(prev_hash: str, entry: dict[str, Any], key: bytes | None = None) -> str:
    """Chain hash over the previous line's hash + this entry's canonical JSON.

    Plain SHA-256 by default. With a `key` it is HMAC-SHA-256, so someone who can
    only write the log files (not the key) cannot recompute a valid chain or head.
    """
    blob = prev_hash.encode("utf-8") + json.dumps(entry, sort_keys=True, default=str).encode("utf-8")
    if key:
        return hmac.new(key, blob, hashlib.sha256).hexdigest()
    return hashlib.sha256(blob).hexdigest()


_THREAD_LOCKS: dict[str, threading.Lock] = {}
_THREAD_LOCKS_GUARD = threading.Lock()
_LOCK_TIMEOUT_S = 30.0


def _thread_lock(path: Path) -> threading.Lock:
    """One in-process lock per log file (many AuditLog instances may share a file)."""
    key = str(path.resolve())
    with _THREAD_LOCKS_GUARD:
        return _THREAD_LOCKS.setdefault(key, threading.Lock())


class AuditLog:
    def __init__(self, path: str | Path, hmac_key: str | bytes | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._head = self.path.with_name(self.path.name + ".head")
        self._lockfile = self.path.with_name(self.path.name + ".lock")
        if hmac_key is None:
            from app.config import get_settings

            hmac_key = get_settings().services.audit_hmac_key
        self._key: bytes | None = (
            hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key
        ) or None

    @contextmanager
    def _exclusive(self) -> Iterator[None]:
        """Serialise appends/reads across threads AND processes.

        The gateway and the dashboard both write this file, and FastAPI runs sync
        endpoints in a thread pool, so an unlocked read-head/append/write-head
        sequence forks the hash chain (false tamper alarms, lost lines).
        """
        with _thread_lock(self.path):
            fh = self._lockfile.open("a+b")
            try:
                deadline = time.monotonic() + _LOCK_TIMEOUT_S
                while True:
                    try:
                        if msvcrt is not None:
                            fh.seek(0)
                            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        elif fcntl is not None:
                            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() > deadline:
                            raise TimeoutError(f"audit lock busy: {self._lockfile}") from None
                        time.sleep(0.005)
                try:
                    yield
                finally:
                    if msvcrt is not None:
                        fh.seek(0)
                        msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                    elif fcntl is not None:
                        fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            finally:
                fh.close()

    def _write_head(self, entry_hash: str) -> None:
        """Replace the head marker atomically (temp file + os.replace)."""
        tmp = self._head.with_name(self._head.name + ".tmp")
        tmp.write_text(entry_hash, encoding="utf-8")
        for attempt in range(20):
            try:
                os.replace(tmp, self._head)
                return
            except PermissionError:  # Windows: a reader briefly holds the target
                if attempt == 19:
                    raise
                time.sleep(0.005)

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
        with self._exclusive():
            prev_hash = self._last_hash()
            entry_hash = _hash_entry(prev_hash, entry, self._key)
            record = {"prev_hash": prev_hash, "hash": entry_hash, "entry": entry}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
            self._write_head(entry_hash)
        return entry

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._exclusive(), self.path.open(encoding="utf-8") as fh:
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
        with self._exclusive():
            return self._verify_locked()

    def _verify_locked(self) -> tuple[bool, list[str]]:
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
                computed = _hash_entry(expected, entry, self._key) if obj.get("prev_hash") == expected else None
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
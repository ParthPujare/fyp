"""The audit chain on the shared Postgres — one tamper-evident log for every laptop (Mk13 Phase 5).

Same entries, same hash (`_hash_entry`: SHA-256, or HMAC-SHA-256 with `SERVICES__AUDIT_HMAC_KEY`),
same genesis, same verification messages as the JSONL file — only the storage moved. Every
laptop that shares the database MUST share the HMAC key too (or leave it blank everywhere):
otherwise each laptop's verification rejects the others' entries.

**Appending: optimistic first, locked when contended.** The hash is computed in Python, so a
locked append is BEGIN -> lock + read head -> insert -> COMMIT: four round trips (~250-600 ms on
Neon) on every gated tool call. So the usual append is ONE statement:

    lock the head row (FOR UPDATE: waits for a concurrent writer, then sees ITS new head)
    insert row (seq = head_seq + 1, prev_hash = head)  ONLY IF the head is still the one we hashed against
    move the head to the new row                       in the same statement
    return the current head                            always

A writer whose cached head is stale learns the real head from the reply, re-hashes and retries.
That alone gives no progress guarantee: measured on Neon, 3 laptops x 2 threads appending
non-stop made one writer lose 30 races in a row. So after `_FAST_TRIES` misses a writer takes the
locked path — a transaction that holds the head row's lock *across* the hash computation, so its
insert cannot lose — at the four-round-trip price, paid only under contention.

The primary key `(log, seq)` is the safety net under both paths: two writers that hashed against
the same head claim the same `seq`, and the second insert fails rather than forking the chain
(this also covers the very first row, when there is no head row to lock yet). The file lock only
ever protected processes on one machine. Each process caches the head it last saw per (pool, log),
shared by every `PgAuditLog` instance, and serialises its own threads so they do not race each other.

**A lost reply** (connection dropped after sending) is retried once; `event_id` is unique, so if
the first attempt had in fact landed, the retry fails on that constraint and is recognised as
success — never a duplicate entry.

**Verification** is one read. The first `verify_integrity()` in a process walks the whole chain;
`verify_integrity(full=False)` (what the per-run evals use) re-checks only rows added since the
last successful verification, anchored on the last verified row's hash, plus the head. So an edit
to a row *older* than that anchor is found by the next full verification (a new process, or
`python -m app.db verify-audit`), not by the next eval. Append-only by design: there is no prune.
"""

from __future__ import annotations

import json
import random
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import psycopg
import psycopg.errors
from psycopg_pool import ConnectionPool

from app.db import get_pool
from app.db.compat import PgSession, translate
from app.db.schema import ensure_migrated
from app.harness.audit import (
    DECISIONS,
    GENESIS,
    AuditLog,
    _hash_entry,
    _hash_matches,
    resolve_key,
    resolve_key_chain,
)

_EVENT_ID_CONSTRAINT = "audit_log_event_id_key"
_FAST_TRIES = 3  # optimistic single-statement attempts before taking the locked path
_MAX_ATTEMPTS = 30  # the locked path cannot lose a race; 30 misses = the head row is missing/corrupt


@dataclass
class _Chain:
    """Per-process knowledge about one log, shared by every PgAuditLog on the same pool."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    head: tuple[int, str] | None = None  # (seq, hash) as last seen; may be stale — appends re-check it
    verified: tuple[int, str] | None = None  # last row a verification accepted: the incremental anchor


_CHAINS: weakref.WeakKeyDictionary[ConnectionPool, dict[str, _Chain]] = weakref.WeakKeyDictionary()
_CHAINS_GUARD = threading.Lock()


def _chain(pool: ConnectionPool, log_name: str) -> _Chain:
    with _CHAINS_GUARD:
        return _CHAINS.setdefault(pool, {}).setdefault(log_name, _Chain())


_APPEND = (
    "WITH cur AS (SELECT seq, head_hash FROM audit_heads WHERE log = ? FOR UPDATE),"
    " ins AS ("
    "  INSERT INTO audit_log (log, seq, event_id, ts, run_id, decision, entry, prev_hash, hash)"
    "  SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?"
    "  WHERE COALESCE((SELECT seq FROM cur), 0) = ? AND COALESCE((SELECT head_hash FROM cur), ?) = ?"
    "  RETURNING seq, hash),"
    " up AS ("
    "  INSERT INTO audit_heads (log, seq, head_hash) SELECT ?, seq, hash FROM ins"
    "  ON CONFLICT (log) DO UPDATE SET seq = excluded.seq, head_hash = excluded.head_hash)"
    " SELECT (SELECT seq FROM ins) AS written, (SELECT seq FROM cur) AS cur_seq,"
    " (SELECT head_hash FROM cur) AS cur_head"
)

_VERIFY = (
    "SELECT h.seq AS head_seq, h.head_hash, a.seq, a.event_id, a.ts, a.run_id, a.decision,"
    " a.entry, a.prev_hash, a.hash"
    " FROM (SELECT 1) AS one"
    " LEFT JOIN audit_heads h ON h.log = ?"
    " LEFT JOIN audit_log a ON a.log = ? AND a.seq >= ?"
    " ORDER BY a.seq"
)


class PgAuditLog(AuditLog):
    def __init__(
        self,
        pool: ConnectionPool | None = None,
        *,
        log_name: str = "audit",
        hmac_key: str | bytes | None = None,
    ) -> None:
        self._pool = pool or get_pool()
        ensure_migrated(self._pool)
        self.log_name = log_name
        self.path = None  # type: ignore[assignment]  # no file: the log lives in the database
        self._key = resolve_key(hmac_key)
        self._db = PgSession(self._pool, hold=False)
        self._chain = _chain(self._pool, log_name)

    # ---- writes ---------------------------------------------------------------------------
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
        body = json.dumps(entry, sort_keys=True, default=str)
        chain = self._chain
        with chain.lock:
            retried_lost_reply = False
            for attempt in range(_MAX_ATTEMPTS):
                try:
                    if attempt < _FAST_TRIES and self._append_fast(entry, body):
                        return entry
                    if attempt >= _FAST_TRIES and self._append_locked(entry, body):
                        return entry
                except psycopg.errors.UniqueViolation as exc:
                    chain.head = None
                    if exc.diag.constraint_name == _EVENT_ID_CONSTRAINT:
                        return entry  # an earlier attempt whose reply was lost did land
                    # another writer took this seq (possible only while no head row exists to lock)
                    time.sleep(random.uniform(0, 0.01 * (attempt + 1)))
                except psycopg.OperationalError:
                    if retried_lost_reply:
                        raise
                    retried_lost_reply = True  # outcome unknown: retry; event_id makes it idempotent
                    chain.head = None
        raise TimeoutError(
            f"audit log {self.log_name!r}: could not append after {_MAX_ATTEMPTS} attempts "
            "(audit_heads is missing or disagrees with audit_log — run `python -m app.db verify-audit`)"
        )

    def _params(self, entry: dict[str, Any], body: str, seq_prev: int, prev: str) -> tuple[tuple[Any, ...], str]:
        entry_hash = _hash_entry(prev, entry, self._key)
        return (
            self.log_name,
            self.log_name, seq_prev + 1, entry["event_id"], entry["ts"], entry["run_id"], entry["decision"],
            body, prev, entry_hash,
            seq_prev, GENESIS, prev,
            self.log_name,
        ), entry_hash

    def _append_fast(self, entry: dict[str, Any], body: str) -> bool:
        """One statement against the cached head. False (and the real head cached) if it was stale.

        Unknown head (first write in this process): guess an empty log. On a non-empty log the
        guess fails harmlessly and the reply carries the real head.
        """
        chain = self._chain
        seq_prev, prev = chain.head or (0, GENESIS)
        params, entry_hash = self._params(entry, body, seq_prev, prev)
        row = self._db.execute(_APPEND, params).fetchone()
        if row["written"] is not None:
            chain.head = (seq_prev + 1, entry_hash)
            return True
        chain.head = (int(row["cur_seq"] or 0), row["cur_head"] or GENESIS)
        return False

    def _append_locked(self, entry: dict[str, Any], body: str) -> bool:
        """Hold the head row's lock across read -> hash -> insert, so no other writer can get in
        between. With no head row to lock (a brand-new log, or a deleted head) there is nothing to
        wait on: fall back to one optimistic attempt, where the primary key arbitrates."""
        # Two pipelined flights, [BEGIN + lock/read head] then [insert + COMMIT]: 2 round trips
        # instead of 4, and the lock is held for ~1 round trip, which is what bounds the whole
        # team's append rate while contended.
        with self._pool.connection() as conn:
            try:
                with conn.pipeline():
                    conn.execute("BEGIN")
                    cur = conn.execute(
                        "SELECT seq, head_hash FROM audit_heads WHERE log = %s FOR UPDATE", (self.log_name,)
                    )
                head = cur.fetchone()
                if head is None:
                    conn.execute("ROLLBACK")
                else:
                    params, entry_hash = self._params(entry, body, int(head["seq"]), head["head_hash"])
                    with conn.pipeline():
                        cur = conn.execute(translate(_APPEND), params)
                        conn.execute("COMMIT")
                    row = cur.fetchone()
            except BaseException:
                if not conn.closed and conn.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    conn.execute("ROLLBACK")
                raise
        if head is None:
            self._chain.head = None
            return self._append_fast(entry, body)
        if row["written"] is None:  # cannot happen while we hold the lock; never report success wrongly
            self._chain.head = None
            return False
        self._chain.head = (int(head["seq"]) + 1, entry_hash)
        return True

    # ---- reads ----------------------------------------------------------------------------
    def entries(self, run_id: str | None = None) -> list[dict[str, Any]]:
        query, params = "SELECT entry FROM audit_log WHERE log = ?", [self.log_name]
        if run_id is not None:
            query += " AND run_id = ?"
            params.append(run_id)
        return [json.loads(r["entry"]) for r in self._db.execute(query + " ORDER BY seq", params).fetchall()]

    def __len__(self) -> int:
        return int(self._db.execute("SELECT COUNT(*) AS n FROM audit_log WHERE log = ?", (self.log_name,)).fetchone()["n"])

    def verify_integrity(self, *, full: bool = True) -> tuple[bool, list[str]]:
        """Recompute the chain in one read. Returns (ok, issues), with the file log's wording.

        `full=False` checks only what was appended since the last successful verification in
        this process (anchored on that row's hash), falling back to a full walk the first time.
        """
        anchor = None if full else self._chain.verified
        rows = self._db.execute(_VERIFY, (self.log_name, self.log_name, anchor[0] if anchor else 0)).fetchall()
        head = rows[0]["head_hash"] if rows else None
        head_seq = rows[0]["head_seq"] if rows else None
        chain_rows = [r for r in rows if r["seq"] is not None]
        # Mk18 Phase H rotation: each row verifies under any era key.
        keys = resolve_key_chain(self._key)
        ok, issues = True, []
        expected_seq, expected = 1, GENESIS

        if anchor is not None:
            first = chain_rows[0] if chain_rows else None
            if (
                first is None
                or first["seq"] != anchor[0]
                or first["hash"] != anchor[1]
                or not _hash_matches(first["prev_hash"], json.loads(first["entry"]), anchor[1], keys)
            ):
                ok = False
                issues.append(f"row {anchor[0]}: changed or deleted since the last verification")
            chain_rows = chain_rows[1:]
            expected_seq, expected = anchor[0] + 1, anchor[1]

        last: tuple[int, str] | None = anchor
        for r in chain_rows:
            n = r["seq"]
            if n != expected_seq:
                ok = False
                issues.append(f"row {n}: sequence gap before it (row(s) deleted)")
            try:
                entry = json.loads(r["entry"])
            except json.JSONDecodeError:
                ok, issues = False, issues + [f"row {n}: not valid JSON"]
                expected, expected_seq, last = r["hash"], n + 1, (n, r["hash"])
                continue
            if r["prev_hash"] != expected:
                ok = False
                issues.append(f"row {n}: prev_hash mismatch (edited/reordered/deleted)")
            elif not _hash_matches(expected, entry, r["hash"], keys):
                ok = False
                issues.append(f"row {n}: hash mismatch under every known key (entry edited)")
            # the indexed columns are copies of hashed fields: a copy that disagrees was edited
            copies = {"event_id": r["event_id"], "ts": r["ts"], "run_id": r["run_id"], "decision": r["decision"]}
            if any(entry.get(k) != v for k, v in copies.items()):
                ok = False
                issues.append(f"row {n}: columns disagree with the hashed entry (edited)")
            expected, expected_seq, last = r["hash"], n + 1, (n, r["hash"])

        if last is not None:
            if head is None:
                ok = False
                issues.append("head marker missing")
            elif (head_seq, head) != last:
                ok = False
                issues.append("head marker mismatch (trailing lines truncated)")
        elif head is not None:
            ok = False
            issues.append("head marker mismatch (trailing lines truncated)")  # every row is gone

        with self._chain.lock:
            self._chain.verified = last if ok else None  # a failure forces the next check to be full
        return ok, issues

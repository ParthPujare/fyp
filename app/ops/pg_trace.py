"""TraceStore on the shared Postgres, with batched writes (Mk13 Phase 3).

A chat run emits ~25-50 trace events. The SQLite store writes each one immediately, which at
~150 ms per round trip to Neon would be *tens of seconds per run*. So this store keeps a run's
events in memory, computes the SHA-256 chain locally (the exact same `hash_event`), and writes
them in **one statement** when the run reaches a natural stopping point:

- `flush(run_id)` — called by the runner when a run ends or suspends (and after the evals, which
  append their own events), or earlier if a run buffers `FLUSH_AT` events (bounds memory);
- reads flush nothing: `get_run_tree` / `verify_integrity` answer from *stored rows + the
  in-memory tail*, so the deterministic evals that run right after a run finish cost zero
  database round trips for a fresh run.

One flush is a single data-modifying CTE — event rows and the run's new head hash land
atomically in one round trip. `(run_id, seq)` is unique in the table, so if two machines ever
resume the same run from a stale head, the second flush fails loudly instead of forking the chain.

Trade-off, stated plainly: events of a run that is still executing live only in the memory of
the process running it (a crash before the flush loses them; a dashboard on another machine
cannot see an in-flight run yet). The chain a flush writes is always complete and valid.
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import ConnectionPool

from app.db import get_pool
from app.db.compat import PgSession
from app.db.schema import ensure_migrated
from app.ops.schema import DEFAULT_SOURCE, TraceEventType
from app.ops.store import GENESIS, TraceStore

log = logging.getLogger(__name__)

_COLUMNS = "event_id, run_id, seq, ts, type, source, actor, payload, prev_hash"
_ROW = "(?, ?, ?, ?, ?, ?, ?, ?, ?)"
_CHUNK = 400  # rows per statement: 400 x 9 params, far below Postgres's 65 535-parameter limit


@dataclass
class _Run:
    next_seq: int
    head: str
    started_ts: str | None = None
    pending: list[tuple[Any, ...]] = field(default_factory=list)
    hashes: list[str] = field(default_factory=list)  # chain hash after each pending event


def _as_row(t: tuple[Any, ...]) -> dict[str, Any]:
    """A buffered event tuple shaped like a stored row (JSON columns still as text)."""
    keys = ("event_id", "run_id", "seq", "ts", "type", "source", "actor", "payload", "prev_hash")
    return dict(zip(keys, t, strict=True))


class PgTraceStore(TraceStore):
    FLUSH_AT = 200  # buffered events per run before an early flush
    KEEP_RUNS = 256  # in-memory run states retained (flushed ones are evicted oldest-first)

    def __init__(self, pool: ConnectionPool | None = None, hmac_key: bytes | str | None = None) -> None:
        self._pool = pool or get_pool()
        ensure_migrated(self._pool)
        self.db_path = None  # type: ignore[assignment]
        self._lock = threading.RLock()
        # Mk18 Phase H: explicit key wins, else TRACE__HMAC_KEY (None = plain SHA).
        if hmac_key is None:
            try:
                from app.config import get_settings as _gs

                hmac_key = _gs().services.trace_hmac_key
            except Exception:  # noqa: BLE001
                hmac_key = None
        self._key = (hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key) or None
        self._runs: OrderedDict[str, _Run] = OrderedDict()
        self._db = PgSession(self._pool, hold=False)

    # ---- writes ---------------------------------------------------------------------------
    def start_run(self, actor: dict[str, Any], input_summary: dict[str, Any], run_id: str | None = None) -> str:
        run_id = run_id or str(uuid.uuid4())
        with self._lock:
            self._runs[run_id] = _Run(next_seq=0, head=GENESIS)  # brand new: nothing to load
        self.add_event(run_id, TraceEventType.RUN_START, actor, {"input": input_summary})
        return run_id

    def _run(self, run_id: str) -> _Run:
        """State for a run: in memory, else loaded from the database (a resume, possibly from
        another machine that flushed it when the run suspended)."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                self._runs.move_to_end(run_id)
                return run
        row = self._db.execute(
            "SELECT (SELECT head_hash FROM trace_runs WHERE run_id = ?) AS head,"
            " (SELECT COUNT(*) FROM trace_events WHERE run_id = ?) AS n",
            (run_id, run_id),
        ).fetchone()
        run = _Run(next_seq=int(row["n"]), head=row["head"] or GENESIS)
        with self._lock:
            return self._runs.setdefault(run_id, run)

    def add_event(
        self,
        run_id: str,
        event_type: TraceEventType | str,
        actor: dict[str, Any],
        payload: dict[str, Any],
        *,
        source: str | None = None,
    ) -> str:
        ts = datetime.now(UTC).isoformat()
        type_name = event_type.value if isinstance(event_type, TraceEventType) else str(event_type)
        if source is None:
            source = DEFAULT_SOURCE.get(event_type, DEFAULT_SOURCE.get(TraceEventType(type_name), "system"))
        run = self._run(run_id)
        with self._lock:
            seq, prev_hash = run.next_seq, run.head
            content = {
                "run_id": run_id, "seq": seq, "ts": ts, "type": type_name,
                "source": source, "actor": actor, "payload": payload,
            }
            event_hash = self._chain_hash(prev_hash, content)
            event_id = str(uuid.uuid4())
            run.pending.append((
                event_id, run_id, seq, ts, type_name, source,
                json.dumps(actor, sort_keys=True, default=str),
                json.dumps(payload, sort_keys=True, default=str),
                prev_hash,
            ))
            run.hashes.append(event_hash)
            run.next_seq, run.head = seq + 1, event_hash
            run.started_ts = run.started_ts or ts
            full = len(run.pending) >= self.FLUSH_AT
        if full:
            self.flush(run_id)
        return event_id

    def flush(self, run_id: str | None = None) -> None:
        """Write buffered events (one run, or every run) — one statement per <= _CHUNK events."""
        with self._lock:
            ids = [run_id] if run_id is not None else [r for r, s in self._runs.items() if s.pending]
            for rid in ids:
                run = self._runs.get(rid)
                if run is None or not run.pending:
                    continue
                while run.pending:
                    chunk = run.pending[:_CHUNK]
                    # chain head after this chunk == the hash of its last event
                    self._write_chunk(rid, chunk, run.hashes[len(chunk) - 1], run.started_ts or chunk[0][3])
                    del run.pending[: len(chunk)]
                    del run.hashes[: len(chunk)]
            self._evict()

    def _write_chunk(self, run_id: str, chunk: list[tuple[Any, ...]], head: str, started_ts: str) -> None:
        values = ", ".join([_ROW] * len(chunk))
        params: list[Any] = [v for row in chunk for v in row]
        # Mk19 Phase 5: the run's era flag (first insert wins; conflict keeps it).
        params += [run_id, head, started_ts, int(self._key is not None)]
        self._db.execute(
            f"WITH ins AS (INSERT INTO trace_events ({_COLUMNS}) VALUES {values}) "
            "INSERT INTO trace_runs (run_id, head_hash, started_ts, keyed) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (run_id) DO UPDATE SET head_hash = excluded.head_hash",
            params,
        )

    def _evict(self) -> None:
        while len(self._runs) > self.KEEP_RUNS:
            for rid, run in self._runs.items():  # oldest first
                if not run.pending:
                    del self._runs[rid]
                    break
            else:
                return  # everything left has unflushed events: keep it all

    # ---- reads: stored rows + the in-memory tail, no flush needed --------------------------
    def _rows(self, run_id: str) -> list[dict[str, Any]]:
        """All rows of one run, in order (database part + not-yet-flushed tail)."""
        with self._lock:
            run = self._runs.get(run_id)
            pending = [_as_row(t) for t in run.pending] if run else []
        first_pending = pending[0]["seq"] if pending else None
        if first_pending == 0:
            return pending  # a fresh run that has never been flushed: nothing stored yet
        query = (
            "SELECT event_id, run_id, seq, ts, type, source, actor, payload, prev_hash "
            "FROM trace_events WHERE run_id = ?"
        )
        params: list[Any] = [run_id]
        if first_pending is not None:
            query += " AND seq < ?"
            params.append(first_pending)
        stored = [dict(zip(r.keys(), r, strict=True)) for r in self._db.execute(query + " ORDER BY seq", params).fetchall()]
        return stored + pending

    def get_run_tree(self, run_id: str, source: str | None = None) -> list[dict[str, Any]]:
        rows = self._rows(run_id)
        if source:
            rows = [r for r in rows if r["source"] == source]
        return [self._row_to_dict(r) for r in rows]

    def list_runs(self) -> list[dict[str, Any]]:
        self.flush()  # the summary is computed by the database over everything
        rows = self._db.execute(
            "SELECT e.run_id, MIN(e.ts) AS started_ts, COUNT(*) AS event_count,"
            " (SELECT f.actor FROM trace_events f WHERE f.run_id = e.run_id AND f.seq = 0) AS actor,"
            " (SELECT g.payload FROM trace_events g WHERE g.run_id = e.run_id AND g.type = 'run_end'"
            "  ORDER BY g.seq LIMIT 1) AS end_payload "
            "FROM trace_events e GROUP BY e.run_id ORDER BY e.run_id"
        ).fetchall()
        runs: list[dict[str, Any]] = []
        for r in rows:
            outcome = "completed"
            if r["end_payload"]:
                inner = (json.loads(r["end_payload"]).get("output") or {})
                outcome = "resumed" if inner.get("resumed") else "completed"
            runs.append({
                "run_id": r["run_id"], "started_ts": r["started_ts"],
                "actor": json.loads(r["actor"]) if r["actor"] else {},
                "event_count": r["event_count"], "outcome": outcome,
            })
        return runs

    def verify_integrity(self, run_id: str | None = None) -> tuple[bool, list[str]]:
        if run_id is None:
            self.flush()
            rows = [dict(zip(r.keys(), r, strict=True)) for r in self._db.execute(
                "SELECT event_id, run_id, seq, ts, type, source, actor, payload, prev_hash "
                "FROM trace_events ORDER BY run_id, seq").fetchall()]
            run_rows = self._db.execute("SELECT run_id, head_hash, keyed FROM trace_runs").fetchall()
            heads = {r["run_id"]: r["head_hash"] for r in run_rows}
            keyed_by_run = {r["run_id"]: bool(r["keyed"]) for r in run_rows}
        else:
            rows = self._rows(run_id)
            with self._lock:
                run = self._runs.get(run_id)
            head = run.head if run else None
            keyed = bool(self._key)  # an in-memory run is written under the current key
            if head is None:
                stored = self._db.execute(
                    "SELECT head_hash, keyed FROM trace_runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                head = stored["head_hash"] if stored else None
                keyed = bool(stored["keyed"]) if stored else keyed
            heads = {run_id: head} if head else {}
            keyed_by_run = {run_id: keyed}
        by_run: dict[str, list[Any]] = defaultdict(list)
        for r in rows:
            by_run[r["run_id"]].append(r)
        return self._check_chains(by_run, heads, keyed_by_run)

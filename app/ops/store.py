"""TraceStore — append-only SQLite event store with a per-run SHA-256 integrity chain.

Event chain: each event stores the hash of the previous event in the run
(genesis = 64 zero bytes). Every append also updates the run's head hash in a
separate `trace_runs` table, so truncating or editing the final event is detected.

Mk18 Phase H: optional HMAC (`hmac_key`) upgrades the chain to HMAC-SHA-256;
`TRACE__HMAC_KEY_PREV` (comma-separated retired keys) keeps rotated lines
verifiable. Blank key = plain SHA-256 (default, byte-identical behavior).
"""

import hashlib
import hmac
import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.ops.schema import DEFAULT_SOURCE, TraceEventType

GENESIS = "0" * 64


def resolve_trace_keys(explicit: bytes | str | None = None) -> list[bytes | None]:
    """[current, *retired] trace-chain keys (mirrors audit rotation)."""
    try:
        from app.config import get_settings

        current = explicit if explicit is not None else get_settings().services.trace_hmac_key
        prev_raw = get_settings().services.trace_hmac_key_prev or ""
    except Exception:  # noqa: BLE001
        current, prev_raw = explicit, ""
    keys: list[bytes | None] = []
    for raw in [current, *str(prev_raw).split(",")]:
        key = (raw.encode("utf-8") if isinstance(raw, str) else raw) or None
        if key not in keys:
            keys.append(key)
    return keys or [None]


def _hash_blob(prev_hash: str, content: dict[str, Any], key: bytes | None) -> str:
    blob = json.dumps(content, sort_keys=True, default=str).encode("utf-8")
    if key:
        return hmac.new(key, prev_hash.encode("utf-8") + blob, hashlib.sha256).hexdigest()
    return hashlib.sha256(prev_hash.encode("utf-8") + blob).hexdigest()


class TraceStore:
    def __init__(self, db_path: str | Path, hmac_key: bytes | str | None = None):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        # Mk18 Phase H: None = plain SHA-256 (default); explicit key wins,
        # else the configured TRACE__HMAC_KEY. Resolved once per store.
        if hmac_key is None:
            try:
                from app.config import get_settings as _gs

                hmac_key = _gs().services.trace_hmac_key
            except Exception:  # noqa: BLE001
                hmac_key = None
        self._key = (hmac_key.encode("utf-8") if isinstance(hmac_key, str) else hmac_key) or None
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_events (
                    event_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    ts TEXT NOT NULL,
                    type TEXT NOT NULL,
                    source TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL
                )
                """
            )
            # Migration: pre-week-12 trace.sqlite files predate the `source`
            # column. SQLite has no ADD COLUMN IF NOT EXISTS, so swallow the
            # "duplicate column" error and carry on.
            try:
                conn.execute("ALTER TABLE trace_events ADD COLUMN source TEXT NOT NULL DEFAULT 'system'")
            except sqlite3.OperationalError:
                pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_runs (
                    run_id TEXT PRIMARY KEY,
                    head_hash TEXT NOT NULL,
                    started_ts TEXT NOT NULL,
                    keyed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            # Mk19 Phase 5: pre-existing files get the era column (0 = plain era).
            try:
                conn.execute("ALTER TABLE trace_runs ADD COLUMN keyed INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass  # column already present
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_run ON trace_events (run_id)")

    @staticmethod
    def hash_event(prev_hash: str, content: dict[str, Any], key: bytes | None = None) -> str:
        """Chain hash (plain SHA-256 by default; HMAC with `key`). Kept static
        so callers (PG twin, prune) can hash explicitly."""
        return _hash_blob(prev_hash, content, key)

    def _chain_hash(self, prev_hash: str, content: dict[str, Any]) -> str:
        return _hash_blob(prev_hash, content, self._key)

    def start_run(self, actor: dict[str, Any], input_summary: dict[str, Any], run_id: str | None = None) -> str:
        run_id = run_id or str(uuid.uuid4())
        self.add_event(run_id, TraceEventType.RUN_START, actor, {"input": input_summary})
        return run_id

    def end_run(self, run_id: str, output_summary: dict[str, Any]) -> str:
        return self.add_event(run_id, TraceEventType.RUN_END, {}, {"output": output_summary})

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
        type_name = (
            event_type.value if isinstance(event_type, TraceEventType) else str(event_type)
        )
        if source is None:
            source = DEFAULT_SOURCE.get(
                event_type, DEFAULT_SOURCE.get(TraceEventType(type_name), "system")
            )
        with self._connect() as conn:
            head = conn.execute(
                "SELECT head_hash FROM trace_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            prev_hash = GENESIS if head is None else head["head_hash"]
            seq = conn.execute(
                "SELECT COUNT(*) FROM trace_events WHERE run_id = ?", (run_id,)
            ).fetchone()[0]

            content = {
                "run_id": run_id,
                "seq": seq,
                "ts": ts,
                "type": type_name,
                "source": source,
                "actor": actor,
                "payload": payload,
            }
            event_hash = self._chain_hash(prev_hash, content)
            event_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO trace_events (event_id, run_id, seq, ts, type, source, actor, payload, prev_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    seq,
                    ts,
                    type_name,
                    source,
                    json.dumps(actor, sort_keys=True, default=str),
                    json.dumps(payload, sort_keys=True, default=str),
                    prev_hash,
                ),
            )
            conn.execute(
                """
                INSERT INTO trace_runs (run_id, head_hash, started_ts, keyed)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET head_hash = excluded.head_hash
                """,
                (run_id, event_hash, ts, int(self._key is not None)),
            )
        return event_id

    def get_run_tree(self, run_id: str, source: str | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT event_id, run_id, seq, ts, type, source, actor, payload, prev_hash
            FROM trace_events WHERE run_id = ?
        """
        params: tuple[Any, ...] = (run_id,)
        if source:
            query += " AND source = ?"
            params = (run_id, source)
        query += " ORDER BY seq"
        with self._connect() as conn:
            rows = conn.execute(query, params).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "seq": row["seq"],
            "ts": row["ts"],
            "type": row["type"],
            "source": row["source"],
            "actor": json.loads(row["actor"]),
            "payload": json.loads(row["payload"]),
            "prev_hash": row["prev_hash"],
        }

    def list_runs(self) -> list[dict[str, Any]]:
        """One summary row per run, for the dashboard ops tab.

        Actor comes from the run's first event (run_start); outcome + event
        count come from the last event (run_end, or whatever head the run
        stopped at if it never completed).
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT run_id, seq, ts, type, source, actor, payload
                FROM trace_events ORDER BY run_id, seq
                """
            ).fetchall()

        by_run: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            by_run[r["run_id"]].append(r)

        runs: list[dict[str, Any]] = []
        for rid in sorted(by_run):
            tree = by_run[rid]
            first = tree[0]
            actor = json.loads(first["actor"])
            outcome = "completed"
            for event in tree:
                if event["type"] == "run_end":
                    status = json.loads(event["payload"])
                    inner = status.get("output") or {}
                    outcome = "resumed" if inner.get("resumed") else "completed"
                    break
            runs.append(
                {
                    "run_id": rid,
                    "started_ts": first["ts"],
                    "actor": actor,
                    "event_count": len(tree),
                    "outcome": outcome,
                }
            )
        return runs

    def verify_integrity(self, run_id: str | None = None) -> tuple[bool, list[str]]:
        if run_id is None:
            where, params = "", ()
        else:
            where, params = "WHERE run_id = ?", (run_id,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, run_id, seq, ts, type, source, actor, payload, prev_hash
                FROM trace_events {where} ORDER BY run_id, seq
                """,
                params,
            ).fetchall()
            heads = {r["run_id"]: r["head_hash"] for r in conn.execute("SELECT run_id, head_hash FROM trace_runs").fetchall()}
            # Mk19 Phase 5: the run's era flag (plain runs keep verifying after keying).
            keyed_by_run = {
                r["run_id"]: bool(r["keyed"])
                for r in conn.execute("SELECT run_id, keyed FROM trace_runs").fetchall()
            }

        by_run: dict[str, list[Any]] = defaultdict(list)
        for r in rows:
            by_run[r["run_id"]].append(r)
        return self._check_chains(by_run, heads, keyed_by_run)

    def flush(self, run_id: str | None = None) -> None:
        """Persist any buffered events. SQLite writes immediately, so this is a no-op here;
        the Postgres store batches a run's events and writes them in one round trip."""
        return

    def redact_run(self, run_id: str) -> int:
        """Mk18 Phase G: scrub PII from a run's payloads, rehashing forward.

        Structure survives; emails/SSNs/sensitive fields become `***`;
        `verify_integrity` stays green because every hash from the first
        rewritten event is recomputed (same ts/seq/actor/type/source).
        Returns the number of events rewritten.
        """
        from app.harness.sanitize import redact_for_retention

        with self._connect() as conn:
            rows = conn.execute(
                "SELECT event_id, seq, ts, type, source, actor, payload"
                " FROM trace_events WHERE run_id = ? ORDER BY seq",
                (run_id,),
            ).fetchall()
            if not rows:
                return 0
            # Mk19 Phase 5: rehash in the run's OWN era — a plain run redacted
            # after the store was keyed must stay plain-hashed (its flag says 0).
            run_row = conn.execute("SELECT keyed FROM trace_runs WHERE run_id = ?", (run_id,)).fetchone()
            era_key = self._key if (run_row is None or run_row["keyed"]) else None

            def _era_hash(prev_hash: str, content: dict[str, Any]) -> str:
                return _hash_blob(prev_hash, content, era_key)

            prev = GENESIS
            for r in rows:
                actor = json.loads(r["actor"])
                new_payload = redact_for_retention(json.loads(r["payload"]))
                content = {
                    "run_id": run_id,
                    "seq": r["seq"],
                    "ts": r["ts"],
                    "type": r["type"],
                    "source": r["source"],
                    "actor": actor,
                    "payload": new_payload,
                }
                conn.execute(
                    "UPDATE trace_events SET payload = ?, prev_hash = ? WHERE event_id = ?",
                    (json.dumps(new_payload, sort_keys=True, default=str), prev, r["event_id"]),
                )
                prev = _era_hash(prev, content)
            conn.execute("UPDATE trace_runs SET head_hash = ? WHERE run_id = ?", (prev, run_id))
            return len(rows)

    def _check_chains(
        self,
        by_run: dict[str, list[Any]],
        heads: dict[str, str],
        keyed_by_run: dict[str, bool] | None = None,
    ) -> tuple[bool, list[str]]:
        """Recompute every run's hash chain from its event rows (`row["seq"]`, `row["actor"]` as
        JSON text, ...) and compare with the stored head. Shared by all trace back-ends.

        Mk18 Phase H rotation: each run verifies under any era key (a run is
        minutes long; rotate during a quiet period so no run spans two eras).
        Mk19 Phase 5 mixed-mode: a run whose stored era flag says plain
        verifies under None even after the store is keyed (keying never
        invalidates old runs); a keyed run uses the rotation chain.
        """
        keys = resolve_trace_keys(self._key)
        keyed_by_run = keyed_by_run or {}
        ok = True
        issues: list[str] = []
        for rid, events in by_run.items():
            contents = [
                {
                    "run_id": rid,
                    "seq": r["seq"],
                    "ts": r["ts"],
                    "type": r["type"],
                    "source": r["source"],
                    "actor": json.loads(r["actor"]),
                    "payload": json.loads(r["payload"]),
                }
                for r in events
            ]
            run_keys = keys if keyed_by_run.get(rid, bool(self._key)) else [None]
            verified = False
            run_issues: list[str] = []
            for key in run_keys:
                prev_expected = GENESIS
                last_hash: str | None = None
                trial: list[str] = []
                for expected_seq, (r, content) in enumerate(zip(events, contents)):
                    if r["seq"] != expected_seq:
                        trial.append(f"{rid}: seq gap at {r['seq']} (expected {expected_seq})")
                    if r["prev_hash"] != prev_expected:
                        trial.append(f"{rid} seq {r['seq']}: prev_hash mismatch")
                    last_hash = TraceStore.hash_event(r["prev_hash"], content, key=key)
                    prev_expected = last_hash
                if last_hash is not None and heads.get(rid) != last_hash:
                    trial.append(f"{rid}: head hash mismatch (chain truncated or last event edited)")
                if not trial:
                    verified = True
                    break
                run_issues = trial
            if not verified:
                ok = False
                issues.extend(run_issues or [f"{rid}: chain unverifiable under every known key"])
        return ok, issues

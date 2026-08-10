"""TraceStore — append-only SQLite event store with a per-run SHA-256 integrity chain.

Event chain: each event stores the hash of the previous event in the run
(genesis = 64 zero bytes). Every append also updates the run's head hash in a
separate `trace_runs` table, so truncating or editing the final event is detected.
"""

import hashlib
import json
import sqlite3
import uuid
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.ops.schema import TraceEventType

GENESIS = "0" * 64


class TraceStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
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
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    prev_hash TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trace_runs (
                    run_id TEXT PRIMARY KEY,
                    head_hash TEXT NOT NULL,
                    started_ts TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_trace_run ON trace_events (run_id)")

    @staticmethod
    def hash_event(prev_hash: str, content: dict[str, Any]) -> str:
        blob = json.dumps(content, sort_keys=True, default=str).encode("utf-8")
        return hashlib.sha256(prev_hash.encode("utf-8") + blob).hexdigest()

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
    ) -> str:
        ts = datetime.now(UTC).isoformat()
        type_name = (
            event_type.value if isinstance(event_type, TraceEventType) else str(event_type)
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
                "actor": actor,
                "payload": payload,
            }
            event_hash = self.hash_event(prev_hash, content)
            event_id = str(uuid.uuid4())
            conn.execute(
                """
                INSERT INTO trace_events (event_id, run_id, seq, ts, type, actor, payload, prev_hash)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    run_id,
                    seq,
                    ts,
                    type_name,
                    json.dumps(actor, sort_keys=True, default=str),
                    json.dumps(payload, sort_keys=True, default=str),
                    prev_hash,
                ),
            )
            conn.execute(
                """
                INSERT INTO trace_runs (run_id, head_hash, started_ts) VALUES (?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET head_hash = excluded.head_hash
                """,
                (run_id, event_hash, ts),
            )
        return event_id

    def get_run_tree(self, run_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT event_id, run_id, seq, ts, type, actor, payload, prev_hash
                FROM trace_events WHERE run_id = ? ORDER BY seq
                """,
                (run_id,),
            ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "event_id": row["event_id"],
            "run_id": row["run_id"],
            "seq": row["seq"],
            "ts": row["ts"],
            "type": row["type"],
            "actor": json.loads(row["actor"]),
            "payload": json.loads(row["payload"]),
            "prev_hash": row["prev_hash"],
        }

    def verify_integrity(self, run_id: str | None = None) -> tuple[bool, list[str]]:
        if run_id is None:
            where, params = "", ()
        else:
            where, params = "WHERE run_id = ?", (run_id,)
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT event_id, run_id, seq, ts, type, actor, payload, prev_hash
                FROM trace_events {where} ORDER BY run_id, seq
                """,
                params,
            ).fetchall()
            heads = {r["run_id"]: r["head_hash"] for r in conn.execute("SELECT run_id, head_hash FROM trace_runs").fetchall()}

        ok = True
        issues: list[str] = []
        by_run: dict[str, list[sqlite3.Row]] = defaultdict(list)
        for r in rows:
            by_run[r["run_id"]].append(r)

        for rid, events in by_run.items():
            prev_expected = GENESIS
            last_hash: str | None = None
            for expected_seq, r in enumerate(events):
                if r["seq"] != expected_seq:
                    ok = False
                    issues.append(f"{rid}: seq gap at {r['seq']} (expected {expected_seq})")
                if r["prev_hash"] != prev_expected:
                    ok = False
                    issues.append(f"{rid} seq {r['seq']}: prev_hash mismatch")
                content = {
                    "run_id": rid,
                    "seq": r["seq"],
                    "ts": r["ts"],
                    "type": r["type"],
                    "actor": json.loads(r["actor"]),
                    "payload": json.loads(r["payload"]),
                }
                last_hash = self.hash_event(r["prev_hash"], content)
                prev_expected = last_hash
            if last_hash is not None and heads.get(rid) != last_hash:
                ok = False
                issues.append(f"{rid}: head hash mismatch (chain truncated or last event edited)")
        return ok, issues

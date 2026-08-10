"""ApprovalStore — pending HITL decisions (plan.md §5.9, §8).

Central-tier memory writes suspend via LangGraph `interrupt()`; this SQLite store
holds the pending item so an admin can approve/reject. The dashboard (wk 9-10)
will render these; for now the store + resume API are exercised by tests.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"


class ApprovalStore:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_ts TEXT NOT NULL,
                    decided_ts TEXT,
                    decided_by TEXT
                )
                """
            )

    def create(
        self,
        run_id: str,
        thread_id: str,
        resource: dict[str, Any],
        actor: dict[str, Any],
        payload: dict[str, Any],
    ) -> str:
        """Create a pending approval, deduplicating on (run_id, resource, payload).

        LangGraph re-runs the interrupted node on resume (`Command(resume=...)`),
        so the memory/tool gate executes twice. Returning the existing id for the
        same logical request prevents a stale duplicate pending row from being
        created on the second pass — the resumed call then re-interrupts against
        the already-decided approval and proceeds.
        """
        existing = self._find(run_id, resource, payload)
        if existing is not None:
            return existing
        approval_id = str(uuid.uuid4())
        with self._conn:
            self._conn.execute(
                "INSERT INTO approvals (id, run_id, thread_id, resource, actor, payload, status, created_ts) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    run_id,
                    thread_id,
                    json.dumps(resource, sort_keys=True),
                    json.dumps(actor, sort_keys=True),
                    json.dumps(payload, sort_keys=True),
                    STATUS_PENDING,
                    datetime.now(UTC).isoformat(),
                ),
            )
        return approval_id

    def _find(
        self, run_id: str, resource: dict[str, Any], payload: dict[str, Any]
    ) -> str | None:
        row = self._conn.execute(
            "SELECT id FROM approvals WHERE run_id = ? AND resource = ? AND payload = ? LIMIT 1",
            (
                run_id,
                json.dumps(resource, sort_keys=True),
                json.dumps(payload, sort_keys=True),
            ),
        ).fetchone()
        return row["id"] if row else None

    def get(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def pending_items(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status = ? ORDER BY created_ts",
            (STATUS_PENDING,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def decide(self, approval_id: str, approve: bool, admin: str) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"approval {approval_id!r} not found")
        if row["status"] != STATUS_PENDING:
            raise ValueError(f"approval {approval_id!r} already {row['status']}")
        status = STATUS_APPROVED if approve else STATUS_REJECTED
        with self._conn:
            self._conn.execute(
                "UPDATE approvals SET status = ?, decided_ts = ?, decided_by = ? WHERE id = ?",
                (status, datetime.now(UTC).isoformat(), admin, approval_id),
            )
        return self.get(approval_id)  # type: ignore[return-value]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "thread_id": row["thread_id"],
            "resource": json.loads(row["resource"]),
            "actor": json.loads(row["actor"]),
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "created_ts": row["created_ts"],
            "decided_ts": row["decided_ts"],
            "decided_by": row["decided_by"],
        }

    def close(self) -> None:
        self._conn.close()

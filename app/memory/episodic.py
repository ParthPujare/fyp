"""EpisodicStore — SQLite FTS5 transcript recall (plan_mk1_core_harness.md §5.2, §7.3).

Every completed session's transcript is appended to a tier-scoped FTS5 table.
`search` runs a full-text query restricted to the scopes the caller is permitted
to read — there is no cross-scope search path (the caller passes only its allowed
scopes; the harness computes those via Casbin).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class EpisodicStore:
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
                "CREATE VIRTUAL TABLE IF NOT EXISTS episodic USING fts5("
                "scope, run_id, ts, actor, transcript)"
            )

    @staticmethod
    def _fts_query(text: str) -> str:
        """Quote arbitrary user text as an FTS5 phrase query (escapes `"`)."""
        return '"' + text.replace('"', '""') + '"'

    def append_transcript(
        self, scope: str, run_id: str, actor: dict[str, Any], transcript: str
    ) -> str:
        """Append a session transcript, deduplicated by (scope, run_id).

        A HITL-suspended run is re-executed from the interrupted node on resume,
        so the `reflect` node runs twice — without dedup the transcript would be
        appended twice for the same run.
        """
        row = self._conn.execute(
            "SELECT 1 FROM episodic WHERE scope = ? AND run_id = ? LIMIT 1",
            (scope, run_id),
        ).fetchone()
        if row is not None:
            existing = self._conn.execute(
                "SELECT ts FROM episodic WHERE scope = ? AND run_id = ? LIMIT 1",
                (scope, run_id),
            ).fetchone()
            return existing["ts"]
        ts = datetime.now(UTC).isoformat()
        with self._conn:
            self._conn.execute(
                "INSERT INTO episodic (scope, run_id, ts, actor, transcript) VALUES (?, ?, ?, ?, ?)",
                (scope, run_id, ts, json.dumps(actor, sort_keys=True), transcript),
            )
        return ts

    def search(
        self,
        query: str,
        scopes: list[str],
        *,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        if not scopes:
            return []
        placeholders = ", ".join("?" for _ in scopes)
        rows = self._conn.execute(
            f"""
            SELECT scope, run_id, ts, actor, transcript, bm25(episodic) AS score
            FROM episodic
            WHERE episodic MATCH ? AND scope IN ({placeholders})
            ORDER BY rank
            LIMIT ?
            """,
            (self._fts_query(query), *scopes, limit),
        ).fetchall()
        return [
            {
                "scope": r["scope"],
                "run_id": r["run_id"],
                "ts": r["ts"],
                "actor": json.loads(r["actor"]),
                "transcript": r["transcript"],
                "score": r["score"],
            }
            for r in rows
        ]

    def transcripts_in(self, scope: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT scope, run_id, ts, actor, transcript FROM episodic "
            "WHERE scope = ? ORDER BY ts DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return [
            {
                "scope": r["scope"],
                "run_id": r["run_id"],
                "ts": r["ts"],
                "actor": json.loads(r["actor"]),
                "transcript": r["transcript"],
            }
            for r in rows
        ]

    def close(self) -> None:
        self._conn.close()

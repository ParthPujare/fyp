"""EpisodicStore on the shared Postgres — full-text recall without cross-scope leakage (Mk13 Phase 4).

The security property (proved in research_objective/01_ztae_formal_spec.md, Theorem 2): for every
caller `c` and query `q`, recall returns no transcript whose scope is outside `R(c)`, the scopes
the policy engine lets `c` read. Here that property is built from independent layers, because a
single WHERE clause is one forgotten predicate away from a leak and — measured on the target
database — Postgres Row Level Security is NOT available as a backstop: the app's role on Neon
(`fyp_owner`) has `BYPASSRLS`, so a policy would look like protection while enforcing nothing.

1. **Exact-match scope predicate in SQL** — `scope = ANY(%s)`, parameterised, never LIKE, never a
   string built from user input, never case-folded (`dept/hr` != `dept/HR` != `dept/hr2`).
2. **Fail closed on every returned row** — each hit's scope must be in the requested set;
   otherwise the whole search raises `RecallIsolationError` and returns nothing (a bug must be
   loud, not a silent partial leak).
3. **Independent policy re-check** — `RecallService` re-asks the enforcer whether the caller may
   read each hit's scope and drops any that fail (see recall.py), so even a store that returned
   the wrong rows could not get them to the model.
4. **The query cannot carry operators** — the text is normalised to alphanumeric words and turned
   into a tsquery with `phraseto_tsquery` (plain text in, phrase out), never `to_tsquery`.
   Punctuation such as `| & ! : * ( ) <->` is stripped before it can reach the index.
5. **No side channels** — ranks/snippets are computed but only `transcript` and metadata of rows
   that passed 1-3 are returned; scores are internal ordering only.

Behaviour matches the FTS5 store: a search is a *phrase* over case-insensitive alphanumeric
words (no stemming: `simple` configuration).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import ConnectionPool

from app.db import get_pool
from app.db.compat import PgSession
from app.db.schema import ensure_migrated
from app.memory.episodic import EpisodicStore, RecallIsolationError, check_in_scope, normalise_query

__all__ = ["PgEpisodicStore", "RecallIsolationError", "check_in_scope", "normalise_query"]

MAX_TRANSCRIPT_CHARS = 200_000  # a tsvector must stay under 1 MB; far above any real transcript


def _strip_nul(text: str) -> str:
    return text.replace("\x00", " ")  # Postgres text cannot hold NUL; one would fail the whole write


class PgEpisodicStore(EpisodicStore):
    def __init__(self, pool: ConnectionPool | None = None) -> None:
        self._pool = pool or get_pool()
        ensure_migrated(self._pool)
        self.db_path = None  # type: ignore[assignment]
        self._db = PgSession(self._pool, hold=False)

    def append_transcript(self, scope: str, run_id: str, actor: dict[str, Any], transcript: str) -> str:
        """Append a session transcript, deduplicated by (scope, run_id) — one round trip.

        A HITL-suspended run is re-executed from the interrupted node on resume, so `reflect`
        runs twice; the unique constraint makes the second append a no-op that returns the
        original timestamp.
        """
        ts = datetime.now(UTC).isoformat()
        body = _strip_nul(transcript)[:MAX_TRANSCRIPT_CHARS]
        row = self._db.execute(
            "WITH ins AS ("
            " INSERT INTO episodic (scope, run_id, ts, actor, transcript) VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT (scope, run_id) DO NOTHING RETURNING ts)"
            " SELECT ts FROM ins UNION ALL SELECT ts FROM episodic WHERE scope = ? AND run_id = ? LIMIT 1",
            (scope, run_id, ts, _strip_nul(json.dumps(actor, sort_keys=True)), body, scope, run_id),
        ).fetchone()
        return row["ts"]

    def search(self, query: str, scopes: list[str], *, limit: int = 5) -> list[dict[str, Any]]:
        scopes = list(dict.fromkeys(scopes))  # de-duplicate, keep order
        words = normalise_query(query)
        if not scopes or not words or limit <= 0:
            return []
        rows = self._db.execute(
            "SELECT e.scope, e.run_id, e.ts, e.actor, e.transcript, ts_rank_cd(e.tsv, q.query) AS score"
            " FROM episodic e, phraseto_tsquery('simple', ?::text) AS q(query)"
            " WHERE e.scope = ANY(?::text[]) AND e.tsv @@ q.query"
            " ORDER BY score DESC, e.ts DESC LIMIT ?",
            (words, scopes, limit),
        ).fetchall()
        hits = [
            {
                "scope": r["scope"],
                "run_id": r["run_id"],
                "ts": r["ts"],
                "actor": json.loads(r["actor"]),
                "transcript": r["transcript"],
                "score": float(r["score"]),  # higher = better here (SQLite's bm25 is lower = better)
            }
            for r in rows
        ]
        return check_in_scope(hits, scopes)

    def transcripts_in(self, scope: str, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT scope, run_id, ts, actor, transcript FROM episodic WHERE scope = ? ORDER BY ts DESC LIMIT ?",
            (scope, limit),
        ).fetchall()
        return check_in_scope(
            [
                {
                    "scope": r["scope"], "run_id": r["run_id"], "ts": r["ts"],
                    "actor": json.loads(r["actor"]), "transcript": r["transcript"],
                }
                for r in rows
            ],
            [scope],
        )

    def close(self) -> None:  # connections belong to the shared pool
        return

"""ApprovalStore and SqliteBaseStore on the shared Postgres (Mk13 Phase 2).

Same code as the SQLite classes, run over `PgSession(hold=False)`: each statement (or `with`
block) borrows a pooled connection and returns it immediately, so these long-lived store
objects never pin a connection that Neon would cut while idle. No read caching here on
purpose — approvals must show up across laptops instantly, and memory facts are read once per
new thread and written rarely.
"""

from __future__ import annotations

from psycopg_pool import ConnectionPool

from app.db import get_pool
from app.db.compat import PgSession
from app.db.schema import ensure_migrated
from app.memory.approvals import ApprovalStore
from app.memory.store import SqliteBaseStore


class PgApprovalStore(ApprovalStore):
    def __init__(self, pool: ConnectionPool | None = None) -> None:
        self._pool = pool or get_pool()
        ensure_migrated(self._pool)
        self.db_path = None  # type: ignore[assignment]
        self._conn = PgSession(self._pool, hold=False)  # type: ignore[assignment]


class PgBaseStore(SqliteBaseStore):
    _TIE = "seq"  # Postgres has no rowid; migration 0002 adds an insertion-order column

    def batch(self, ops):  # type: ignore[no-untyped-def]
        ops = list(ops)
        if len(ops) == 1:
            # A single statement is atomic by itself, so skip the transaction: at ~150 ms per
            # round trip BEGIN + statement + COMMIT costs three trips, the bare statement one.
            # (Every MemoryEditor put/delete and every get/search is a single-op batch.)
            return [self._run_op(ops[0])]
        return super().batch(ops)

    def __init__(self, pool: ConnectionPool | None = None) -> None:
        self._pool = pool or get_pool()
        ensure_migrated(self._pool)
        self.db_path = None  # type: ignore[assignment]
        self._conn = PgSession(self._pool, hold=False)  # type: ignore[assignment]

"""The shared authorization-state counter (`meta.members_version`) for non-member stores (Mk13 Phase 6).

Policy rows and skills change *who may do what*, exactly like member/department changes, so
their writes bump the same counter in the same statement/transaction. Every process already
polls that counter (`PgMemberStore.version`, at most once per second) and the enforcer already
rebuilds when it moves — so a rule or skill added on one laptop reaches every laptop within
~1 s, at zero extra round trips per request.

`SharedVersion.get()` reuses the member store's throttled poll when that store is on the same
pool (the production case). Otherwise (tests that mix back-ends) it polls the counter itself
with the same throttle.
"""

from __future__ import annotations

import threading
import time

from psycopg_pool import ConnectionPool

BUMP_SQL = (
    "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'members_version'"
)


class SharedVersion:
    def __init__(self, pool: ConnectionPool, ttl: float = 1.0) -> None:
        self._pool = pool
        self._ttl = ttl
        self._lock = threading.Lock()
        self._value = 0
        self._checked: float | None = None

    def _member_store(self):
        from app.auth.pg_store import PgMemberStore
        from app.auth.users import get_store

        store = get_store()
        if isinstance(store, PgMemberStore) and store._pool is self._pool:
            return store
        return None

    def get(self) -> int:
        store = self._member_store()
        if store is not None:
            return store.version
        with self._lock:
            if self._checked is not None and time.monotonic() - self._checked < self._ttl:
                return self._value
        with self._pool.connection() as conn:
            row = conn.execute("SELECT value FROM meta WHERE key = 'members_version'").fetchone()
        with self._lock:
            self._value = int(row["value"]) if row else 0
            self._checked = time.monotonic()
            return self._value

    def changed_here(self) -> None:
        """This process just bumped the counter: read it fresh next time (own writes are
        visible at once, not after the poll interval)."""
        with self._lock:
            self._checked = None
        store = self._member_store()
        if store is not None:
            store._invalidate()

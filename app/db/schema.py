"""Make sure the shared database's schema is current before a store uses it (Mk13 Phase 2)."""

from __future__ import annotations

import threading
import weakref

import psycopg.errors
from psycopg_pool import ConnectionPool

from app.db.migrate import apply_pending, discover

# Weak, not `id(pool)`: ids are reused once a pool is garbage-collected, which made a brand-new
# pool look already-migrated. A WeakSet forgets a pool exactly when the pool dies.
_done: weakref.WeakSet[ConnectionPool] = weakref.WeakSet()
_lock = threading.Lock()


def _up_to_date(pool: ConnectionPool) -> bool:
    """One read: does the database already have every migration, with matching checksums?"""
    try:
        with pool.connection() as conn:
            rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    except psycopg.errors.UndefinedTable:
        return False
    recorded = {r["version"]: r["checksum"] for r in rows}
    return all(recorded.get(m.version) == m.checksum for m in discover())


def ensure_migrated(pool: ConnectionPool) -> None:
    """Apply pending migrations once per process per pool.

    Containers already migrate at start-up (`python -m app.db migrate`), so normally this finds
    nothing to do; it exists so a plain `python`/test/native run against a fresh Neon database
    also works, and so a store can never query tables that do not exist yet. Cheap after the
    first call (a set lookup) — important when every statement costs a network round trip.
    """
    if pool in _done:
        return
    with _lock:
        if pool in _done:
            return
        if not _up_to_date(pool):  # the usual case at ~150 ms/round trip: 1 read, no locking
            apply_pending(pool)
        _done.add(pool)

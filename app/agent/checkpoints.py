"""Where a chat's LangGraph state lives: local SQLite, or the shared Postgres (Mk13 Phase 3).

With `DATABASE_URL` set every process uses LangGraph's `PostgresSaver` on the shared pool, so a
run started on one laptop can be approved and resumed on another, and a member's chat list and
history follow them between machines.

Latency (~150 ms per Neon round trip) is why runs use `durability="exit"` on Postgres: LangGraph
otherwise writes a checkpoint after *every* graph step (several statements each), which would
cost seconds per chat turn. With "exit" the state is persisted once when the graph finishes or
pauses at an approval. Trade-off: a process that dies mid-run loses that run's progress (the user
re-sends the message); a run *paused for approval* is always persisted, so HITL is unaffected.
"""

from __future__ import annotations

import random
import sqlite3
import threading
import time
import weakref
from typing import Any

import psycopg
from psycopg_pool import ConnectionPool

from app.config import Settings, get_settings
from app.db import connection, get_pool, is_enabled

_savers: weakref.WeakKeyDictionary[ConnectionPool, Any] = weakref.WeakKeyDictionary()
_lock = threading.Lock()


def postgres_saver(pool: ConnectionPool | None = None):
    """The (cached) `PostgresSaver` for a pool, with its tables created on first use."""
    from langgraph.checkpoint.postgres import PostgresSaver

    pool = pool or get_pool()
    with _lock:
        saver = _savers.get(pool)
        if saver is None:
            saver = PostgresSaver(pool)
            _setup(saver, pool)
            _savers[pool] = saver
        return saver


# Errors that mean "another machine is running the same migration right now", not "broken":
# LangGraph's migrations include CREATE INDEX CONCURRENTLY, so simultaneous `setup()` calls can
# deadlock each other (TransactionRollback) or collide creating the same table/index.
_RACE_ERRORS = (
    psycopg.errors.DeadlockDetected,
    psycopg.errors.SerializationFailure,
    psycopg.errors.TransactionRollback,
    psycopg.errors.LockNotAvailable,
    psycopg.errors.DuplicateTable,
    psycopg.errors.DuplicateObject,
    psycopg.errors.UniqueViolation,
)


def _schema_current(saver: Any, pool: ConnectionPool) -> bool:
    """One read: has LangGraph's schema already been migrated to the version this code needs?"""
    try:
        with pool.connection() as conn:
            row = conn.execute("SELECT MAX(v) AS v FROM checkpoint_migrations").fetchone()
    except psycopg.errors.UndefinedTable:
        return False
    return row["v"] is not None and row["v"] >= len(saver.MIGRATIONS) - 1


def _setup(saver: Any, pool: ConnectionPool, attempts: int = 8) -> None:
    """Create/upgrade LangGraph's tables — skipped (1 round trip) when already current, which is
    every start-up but the first. Several containers booting on a fresh database *will* race, so
    lose gracefully: back off with jitter and re-check, because whoever won has usually finished."""
    for attempt in range(attempts):
        if _schema_current(saver, pool):
            return
        try:
            saver.setup()
            return
        except _RACE_ERRORS:
            if attempt == attempts - 1:
                raise
            time.sleep(random.uniform(0.2, 0.6) * (attempt + 1))


def make_checkpointer(settings: Settings | None = None):
    settings = settings or get_settings()
    if is_enabled():
        return postgres_saver()
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(settings.services.checkpoint_path, check_same_thread=False)
    return SqliteSaver(conn)


def invoke_options(checkpointer: Any) -> dict[str, Any]:
    """Extra `graph.invoke(...)` keywords for this checkpointer (see the module docstring)."""
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
    except ImportError:  # pragma: no cover - dependency is declared, defensive only
        return {}
    return {"durability": "exit"} if isinstance(checkpointer, PostgresSaver) else {}


def list_user_threads(username: str, settings: Settings | None = None) -> list[str]:
    """Thread ids that belong to `username` (`user:<name>` and `user:<name>:<chat>`).

    Matching is an exact prefix, not SQL LIKE: `_` and `%` are LIKE wildcards, so a name such as
    `a_b@x.com` used to also match `aXb@x.com`'s threads and list them for the wrong person.
    """
    default = f"user:{username}"
    prefix = default + ":"
    if is_enabled():
        with connection() as conn:
            rows = conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id = %s OR left(thread_id, %s) = %s",
                (default, len(prefix), prefix),
            ).fetchall()
        found = [r["thread_id"] for r in rows]
    else:
        settings = settings or get_settings()
        conn = sqlite3.connect(settings.services.checkpoint_path, check_same_thread=False)
        try:
            rows = conn.execute(
                "SELECT DISTINCT thread_id FROM checkpoints WHERE thread_id LIKE ? OR thread_id LIKE ?",
                (default, prefix + "%"),
            ).fetchall()
            found = [r[0] for r in rows]
        except sqlite3.Error:
            found = []
        finally:
            conn.close()
    return sorted(t for t in found if t == default or t.startswith(prefix))


def is_own_thread(thread_id: str, username: str) -> bool:
    """Mk19 Phase 1: the single ownership predicate for every thread-scoped endpoint.

    True only for `user:<username>` and `user:<username>:<chat>` — the exact-prefix
    form (colon-terminated) prevents a username that is a prefix of another
    (`alice@x.com` vs `alice@x.com.evil`) from matching the other's threads.
    Shared by the gateway `/chat` + history and the dashboard history/delete/
    by-thread checks (previously four copy-pasted predicates). Owner-only:
    admins get no override (locked decision, plan Mk19).
    """
    return thread_id == f"user:{username}" or thread_id.startswith(f"user:{username}:")


def prune_chats(pool: ConnectionPool, thread_ids: list[str] | None = None) -> dict[str, int]:
    """Keep only each chat's LATEST checkpoint; drop the superseded ones and what only they used.

    Why: LangGraph stores the full message list again for every turn, so a chat's footprint grows
    roughly quadratically and the Neon free tier is 512 MB. The latest checkpoint is all that
    history display, continuing the chat and resuming a paused run need, and its *pending writes*
    (what a run paused for approval resumes from) are kept with it.

    LangGraph's own `PostgresSaver.prune` is not implemented in the installed version, so this
    works on its tables directly (`checkpoints`, `checkpoint_writes`, `checkpoint_blobs`). It is
    only ever run explicitly (`python -m app.db prune --chats`), inside one transaction, and the
    test-suite runs it against the installed LangGraph so a schema change fails loudly in CI.
    "Latest" = highest `checkpoint_id` (time-ordered ids), the same rule `get_tuple` uses.
    Returns rows removed per table.
    """
    with pool.connection() as conn, conn.transaction():
        conn.execute(
            "CREATE TEMP TABLE _latest_checkpoints ON COMMIT DROP AS "
            "SELECT DISTINCT ON (thread_id, checkpoint_ns) thread_id, checkpoint_ns, checkpoint_id "
            "FROM checkpoints WHERE (%(all)s OR thread_id = ANY(%(ids)s)) "
            "ORDER BY thread_id, checkpoint_ns, checkpoint_id DESC",
            {"all": thread_ids is None, "ids": thread_ids or []},
        )
        scope = "(%(all)s OR {t}.thread_id = ANY(%(ids)s))"
        args = {"all": thread_ids is None, "ids": thread_ids or []}
        writes = conn.execute(
            "DELETE FROM checkpoint_writes w WHERE " + scope.format(t="w") + " AND NOT EXISTS ("
            "SELECT 1 FROM _latest_checkpoints l WHERE l.thread_id = w.thread_id "
            "AND l.checkpoint_ns = w.checkpoint_ns AND l.checkpoint_id = w.checkpoint_id)",
            args,
        ).rowcount
        checkpoints = conn.execute(
            "DELETE FROM checkpoints c WHERE " + scope.format(t="c") + " AND NOT EXISTS ("
            "SELECT 1 FROM _latest_checkpoints l WHERE l.thread_id = c.thread_id "
            "AND l.checkpoint_ns = c.checkpoint_ns AND l.checkpoint_id = c.checkpoint_id)",
            args,
        ).rowcount
        # a blob is (channel, version); it is still needed iff a remaining checkpoint lists that version
        blobs = conn.execute(
            "DELETE FROM checkpoint_blobs b WHERE " + scope.format(t="b") + " AND NOT EXISTS ("
            "SELECT 1 FROM checkpoints c WHERE c.thread_id = b.thread_id AND c.checkpoint_ns = b.checkpoint_ns "
            "AND c.checkpoint -> 'channel_versions' ->> b.channel = b.version)",
            args,
        ).rowcount
    return {"checkpoints": checkpoints, "writes": writes, "blobs": blobs}

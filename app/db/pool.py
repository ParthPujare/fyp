"""Shared Postgres connection pool (Mk13 Phase 1) — lazy, one pool per process.

Built for Neon's free tier specifically:
- **Idle connections die.** Neon suspends compute after 5 idle minutes and drops its
  connections, so a connection that has sat unused is validated on checkout and transparently
  replaced if dead. A connection used in the last `CHECK_AFTER_IDLE_S` seconds is trusted
  as-is: at ~150 ms per round trip to Neon an unconditional ping would double every query.
- **Cold start.** A suspended database takes a few seconds to wake, so opening waits up to
  `OPEN_TIMEOUT_S` and each connect allows `CONNECT_TIMEOUT_S`.
- **Pooled endpoint = PgBouncer in transaction mode.** Server-side prepared statements do not
  survive that (`prepare_threshold=None` turns them off), and session-level state (advisory
  locks, `SET`) must not be relied on — use transaction-scoped features instead.
- `autocommit=True` + `dict_row` is also exactly what `langgraph-checkpoint-postgres` requires
  for Phase 3, so one connection style serves both.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.config import get_settings

CONNECT_TIMEOUT_S = 30
OPEN_TIMEOUT_S = 45
MIN_SIZE = 1
MAX_SIZE = 4  # per process; the container runs a handful of processes, Neon's pooler fans in
MAX_IDLE_S = 240  # recycle before Neon's 5-minute idle suspend cuts the socket
CHECK_AFTER_IDLE_S = 30  # only ping connections idle longer than this (see _check)


PROBE_ATTEMPTS = 3
# Errors retrying can never fix. Anything else (timeouts, refused, "starting up") is treated as
# transient — e.g. Neon still waking from idle — and retried.
_FATAL = (
    "password authentication failed",
    "does not exist",
    "could not translate host name",
    "name or service not known",
    "getaddrinfo failed",
    "failed to resolve host",
    "no address associated with hostname",
    "no pg_hba.conf entry",
)


class DatabaseNotConfigured(RuntimeError):
    """`DATABASE_URL` is unset, so the app is (correctly) running on local SQLite."""


_pool: ConnectionPool | None = None
_lock = threading.Lock()


def database_url() -> str | None:
    return get_settings().database_url


def is_enabled() -> bool:
    """True when a shared Postgres is configured."""
    return database_url() is not None


def _stamp(conn: Connection) -> None:
    """Record when a connection was created / last handed out (see `_check`)."""
    conn._fyp_used = time.monotonic()  # type: ignore[attr-defined]


def _check(conn: Connection) -> None:
    """Pool `check`: ping only connections that have been idle long enough to have been cut.

    "Idle" is measured between consecutive checkouts. It is stamped here, at checkout, rather
    than in a `reset` callback on return: psycopg_pool runs `reset` in a background worker, so a
    just-returned connection would be briefly unavailable and the pool would open extra
    connections (each a slow TLS handshake to Neon).
    """
    now = time.monotonic()
    last = getattr(conn, "_fyp_used", 0.0)
    conn._fyp_used = now  # type: ignore[attr-defined]
    if now - last < CHECK_AFTER_IDLE_S:
        return
    ConnectionPool.check_connection(conn)


def _probe(conninfo: str, connect_kwargs: dict[str, Any]) -> None:
    """Connect once (retrying transient failures) so a misconfiguration fails in seconds.

    `ConnectionPool.open(wait=True)` alone retries *everything* until its timeout, so a wrong
    password or a typo'd host would look like a 45-second hang. Probing first lets those fail
    immediately with the real reason while a cold Neon still gets a few patient attempts.
    """
    last: psycopg.OperationalError | None = None
    for attempt in range(PROBE_ATTEMPTS):
        try:
            with psycopg.connect(conninfo, **connect_kwargs):
                return
        except psycopg.OperationalError as exc:
            if any(marker in str(exc).lower() for marker in _FATAL):
                raise
            last = exc
            if attempt + 1 < PROBE_ATTEMPTS:
                time.sleep(min(2 * (attempt + 1), 5))
    assert last is not None
    raise last


def make_pool(conninfo: str, **overrides: Any) -> ConnectionPool:
    """Build (and open) a pool with the project's connection settings.

    Public so tests and one-off tools can point at a different database without touching the
    process-wide singleton.
    """
    kwargs = {
        "autocommit": True,
        "row_factory": dict_row,
        "prepare_threshold": None,
        "connect_timeout": CONNECT_TIMEOUT_S,
    }
    extra = overrides.pop("kwargs", {})
    _probe(conninfo, {"connect_timeout": CONNECT_TIMEOUT_S, **extra})
    pool = ConnectionPool(
        conninfo=conninfo,
        min_size=overrides.pop("min_size", MIN_SIZE),
        max_size=overrides.pop("max_size", MAX_SIZE),
        max_idle=overrides.pop("max_idle", MAX_IDLE_S),
        kwargs={**kwargs, **extra},
        check=_check,
        configure=_stamp,
        open=False,
        name=overrides.pop("name", "fyp"),
        **overrides,
    )
    try:
        pool.open(wait=True, timeout=OPEN_TIMEOUT_S)
    except BaseException:
        pool.close()  # don't leave background reconnect threads behind on a bad DSN
        raise
    return pool


def get_pool() -> ConnectionPool:
    """The process-wide pool, created on first use from `DATABASE_URL`."""
    global _pool
    url = database_url()
    if url is None:
        raise DatabaseNotConfigured(
            "DATABASE_URL is not set — the app is using local SQLite files. "
            "Set DATABASE_URL in .env to use the shared Neon database."
        )
    with _lock:
        if _pool is None:
            _pool = make_pool(url)
        return _pool


@contextmanager
def connection() -> Iterator[Connection]:
    """Borrow a pooled connection (autocommit, dict rows) for the `with` block."""
    with get_pool().connection() as conn:
        yield conn


def close_pool() -> None:
    """Close the process-wide pool (tests, clean shutdown). Safe to call when unopened."""
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None

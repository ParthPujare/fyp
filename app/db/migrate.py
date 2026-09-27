"""Numbered SQL migrations for the shared Postgres (Mk13 Phase 1).

Files live in `app/db/migrations/` as `NNNN_snake_case.sql`, contiguous from 0001, and are
applied in order, each in its own transaction, then recorded in `schema_migrations`.

Safe for a team, not just one laptop:
- **Concurrent starts.** Every teammate's container may boot and migrate at the same moment.
  Each migration runs under `pg_advisory_xact_lock` (transaction-scoped, so it also works
  through Neon's PgBouncer pooled endpoint, where session-level locks do not), and re-checks
  `schema_migrations` after taking the lock — the loser of the race sees it already applied
  and skips it.
- **Edited history is an error.** The SHA-256 of each applied file is stored; a file whose
  contents changed afterwards (different checksum) stops the run. Fix forward with a new file.
  Line endings are normalised first, so a Windows CRLF checkout hashes the same as Linux LF.
- **Atomic.** A failing migration rolls back completely and is not recorded.

Authoring rules: no `BEGIN`/`COMMIT` inside a file (the runner owns the transaction), no
`CREATE INDEX CONCURRENTLY` (cannot run in a transaction), and keep changes *additive* — a
teammate on an older checkout may still be running against the newer schema.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from psycopg_pool import ConnectionPool

from app.db.pool import get_pool

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
_FILENAME = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")
LOCK_KEY = 0x46595031  # arbitrary constant ("FYP1") shared by every migrating process

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    name       text NOT NULL,
    checksum   text NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """A migration file is malformed, was edited after being applied, or failed to run."""


@dataclass(frozen=True)
class Migration:
    version: str
    name: str
    sql: str

    @property
    def checksum(self) -> str:
        return checksum_of(self.sql)


@dataclass(frozen=True)
class StatusRow:
    version: str
    name: str
    state: str  # "applied" | "pending" | "edited" | "unknown"


def checksum_of(sql: str) -> str:
    """SHA-256 of the file text with line endings normalised (CRLF/LF checkouts agree)."""
    normalised = sql.replace("\r\n", "\n").replace("\r", "\n")
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def discover(directory: Path | str = MIGRATIONS_DIR) -> list[Migration]:
    """Load and validate every migration file (naming, no gaps, no duplicates)."""
    directory = Path(directory)
    found: list[Migration] = []
    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME.match(path.name)
        if match is None:
            raise MigrationError(f"bad migration filename {path.name!r}: expected NNNN_snake_case.sql")
        found.append(Migration(match.group(1), match.group(2), path.read_text(encoding="utf-8")))
    versions = [m.version for m in found]
    if len(set(versions)) != len(versions):
        raise MigrationError(f"duplicate migration version in {directory}")
    expected = [f"{i:04d}" for i in range(1, len(found) + 1)]
    if versions != expected:
        raise MigrationError(
            f"migrations must be contiguous from 0001: found {versions}, expected {expected}"
        )
    return found


def _lock(conn) -> None:
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_KEY,))


def _ensure_table(conn) -> None:
    with conn.transaction():
        _lock(conn)
        conn.execute(_CREATE_TABLE)


def _recorded(conn) -> dict[str, str]:
    rows = conn.execute("SELECT version, checksum FROM schema_migrations").fetchall()
    return {r["version"]: r["checksum"] for r in rows}


def status(
    pool: ConnectionPool | None = None, migrations: list[Migration] | None = None
) -> list[StatusRow]:
    """Where each migration stands against the database (read-only apart from creating the table)."""
    migrations = discover() if migrations is None else migrations
    pool = pool or get_pool()
    with pool.connection() as conn:
        _ensure_table(conn)
        recorded = _recorded(conn)
    rows: list[StatusRow] = []
    for m in migrations:
        if m.version not in recorded:
            rows.append(StatusRow(m.version, m.name, "pending"))
        elif recorded[m.version] != m.checksum:
            rows.append(StatusRow(m.version, m.name, "edited"))
        else:
            rows.append(StatusRow(m.version, m.name, "applied"))
    known = {m.version for m in migrations}
    for version in sorted(set(recorded) - known):
        rows.append(StatusRow(version, "(not in this checkout)", "unknown"))
    return rows


def apply_pending(
    pool: ConnectionPool | None = None,
    migrations: list[Migration] | None = None,
    log: Callable[[str], None] = lambda _msg: None,
) -> list[str]:
    """Apply every migration not yet recorded; returns the versions applied by *this* call."""
    migrations = discover() if migrations is None else migrations
    pool = pool or get_pool()
    applied_now: list[str] = []
    with pool.connection() as conn:
        _ensure_table(conn)
        for m in migrations:
            with conn.transaction():
                _lock(conn)
                row = conn.execute(
                    "SELECT checksum FROM schema_migrations WHERE version = %s", (m.version,)
                ).fetchone()
                if row is not None:
                    if row["checksum"] != m.checksum:
                        raise MigrationError(
                            f"migration {m.version}_{m.name}.sql was edited after it was applied "
                            "(checksum differs). Never change an applied migration — add a new one."
                        )
                    continue
                try:
                    conn.execute(m.sql)
                except Exception as exc:
                    raise MigrationError(f"migration {m.version}_{m.name}.sql failed: {exc}") from exc
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (m.version, m.name, m.checksum),
                )
                applied_now.append(m.version)
                log(f"applied {m.version}_{m.name}.sql")
        ahead = set(_recorded(conn)) - {m.version for m in migrations}
    if ahead:
        log(
            f"WARNING: database has migrations this checkout does not ({', '.join(sorted(ahead))}) — "
            "another teammate is ahead of you; `git pull` before relying on the newer schema."
        )
    return applied_now

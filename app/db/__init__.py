"""Shared-database layer (Mk13). SQLite stays the default; this is the Postgres (Neon) side.

`is_enabled()` is the single switch: when `DATABASE_URL` is unset nothing here is used and
every store keeps its local file. Command line: `python -m app.db {check,migrate,status}`.
"""

from app.db.pool import (
    DatabaseNotConfigured,
    close_pool,
    connection,
    database_url,
    get_pool,
    is_enabled,
    make_pool,
)

__all__ = [
    "DatabaseNotConfigured",
    "close_pool",
    "connection",
    "database_url",
    "get_pool",
    "is_enabled",
    "make_pool",
]

"""Pick the store back-end: shared Postgres when `DATABASE_URL` is set, else local SQLite files.

Every process that needs a store (gateway, dashboard, the agent runner) goes through here, so
the switch is one setting and cannot be half-applied.
"""

from __future__ import annotations

from pathlib import Path

from app.db import is_enabled
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.store import SqliteBaseStore


def open_base_store(path: str | Path) -> SqliteBaseStore:
    if is_enabled():
        from app.memory.pg_stores import PgBaseStore

        return PgBaseStore()
    return SqliteBaseStore(path)


def open_approval_store(path: str | Path) -> ApprovalStore:
    if is_enabled():
        from app.memory.pg_stores import PgApprovalStore

        return PgApprovalStore()
    return ApprovalStore(path)


def open_episodic_store(path: str | Path) -> EpisodicStore:
    if is_enabled():
        from app.memory.pg_episodic import PgEpisodicStore

        return PgEpisodicStore()
    return EpisodicStore(path)

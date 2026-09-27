"""Pick the trace back-end: shared Postgres (batched writes) when `DATABASE_URL` is set, else SQLite."""

from __future__ import annotations

from pathlib import Path

from app.db import is_enabled
from app.ops.store import TraceStore


def open_trace_store(path: str | Path) -> TraceStore:
    if is_enabled():
        from app.ops.pg_trace import PgTraceStore

        return PgTraceStore()
    return TraceStore(path)

"""SqliteBaseStore — a persistent SQLite backend for LangGraph's BaseStore.

The installed LangGraph only ships `InMemoryStore` (non-persistent); there is no
official SQLite BaseStore. This subclass implements the abstract `batch`/`abatch`
primitive ops (GetOp / PutOp / SearchOp / ListNamespacesOp) over a SQLite table so
the harness can use the framework-native `BaseStore` API with real persistence.

Namespace model (plan_mk1_core_harness.md §5.2): ``("central",)``, ``("dept", <name>)``,
``("user", <uid>)``. Search ordering is most-recent-first (updated_at DESC) so the
memory browser and recall see fresh facts first.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    Item,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)

_NS_SEP = "/"


def _ns_str(namespace: tuple[str, ...]) -> str:
    return _NS_SEP.join(namespace)


def _ns_tuple(ns_str: str) -> tuple[str, ...]:
    return tuple(ns_str.split(_NS_SEP)) if ns_str else ()


class SqliteBaseStore(BaseStore):
    supports_ttl = False
    # Insertion-order tiebreak for equal timestamps: SQLite's implicit `rowid`; the Postgres
    # subclass uses its explicit `seq` column (same meaning: kept across upserts).
    _TIE = "rowid"

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS memory_items (
                    ns TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (ns, key)
                )
                """
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_memory_ns ON memory_items (ns)")

    # ---- BaseStore primitive ops ----

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        ops = list(ops)
        results: list[Result] = [None] * len(ops)
        with self._conn:
            for i, op in enumerate(ops):
                results[i] = self._run_op(op)
        return results

    def _run_op(self, op: Op) -> Result:
        if isinstance(op, GetOp):
            return self._get(op)
        if isinstance(op, PutOp):
            self._put(op)
            return None
        if isinstance(op, SearchOp):
            return self._search(op)
        if isinstance(op, ListNamespacesOp):
            return self._list_namespaces(op)
        raise TypeError(f"unsupported op: {type(op).__name__}")  # pragma: no cover - only four exist

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return self.batch(ops)

    # ---- op handlers ----

    def _get(self, op: GetOp) -> Item | None:
        row = self._conn.execute(
            "SELECT ns, key, value, created_at, updated_at FROM memory_items WHERE ns = ? AND key = ?",
            (_ns_str(op.namespace), op.key),
        ).fetchone()
        return self._row_to_item(row) if row else None

    def _put(self, op: PutOp) -> None:
        ns = _ns_str(op.namespace)
        now = datetime.now(UTC).isoformat()
        if op.value is None:
            self._conn.execute("DELETE FROM memory_items WHERE ns = ? AND key = ?", (ns, op.key))
            return
        self._conn.execute(
            """
            INSERT INTO memory_items (ns, key, value, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(ns, key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (ns, op.key, json.dumps(op.value, sort_keys=True, default=str), now, now),
        )

    def _search(self, op: SearchOp) -> list[SearchItem]:
        prefix = op.namespace_prefix
        rows = self._conn.execute(
            "SELECT ns, key, value, created_at, updated_at FROM memory_items "
            "WHERE ns = ? OR ns LIKE ? "
            f"ORDER BY updated_at DESC, {self._TIE} DESC",
            (_ns_str(prefix), _ns_str(prefix) + _NS_SEP + "%"),
        ).fetchall()
        items: list[SearchItem] = []
        for row in rows:
            ns = _ns_tuple(row["ns"])
            if ns[: len(prefix)] != prefix:
                continue
            item = self._row_to_item(row)
            if item is not None and self._matches_filter(item.value, op.filter):
                items.append(SearchItem(
                    namespace=item.namespace,
                    key=item.key,
                    value=item.value,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                    score=None,
                ))
        return items[op.offset : op.offset + op.limit]

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        rows = self._conn.execute("SELECT DISTINCT ns FROM memory_items").fetchall()
        namespaces: list[tuple[str, ...]] = []
        for row in rows:
            ns = _ns_tuple(row["ns"])
            if self._matches_conditions(ns, op.match_conditions):
                if op.max_depth is not None:
                    ns = ns[: op.max_depth]
                namespaces.append(ns)
        namespaces = list(dict.fromkeys(namespaces))
        namespaces.sort()
        return namespaces[op.offset : op.offset + op.limit]

    @staticmethod
    def _row_to_item(row: sqlite3.Row) -> Item | None:
        return Item(
            value=json.loads(row["value"]),
            key=row["key"],
            namespace=_ns_tuple(row["ns"]),
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
        )

    @staticmethod
    def _matches_filter(value: dict[str, Any], filter: dict[str, Any] | None) -> bool:
        if not filter:
            return True
        for key, expected in filter.items():
            if isinstance(expected, dict) and any(
                k.startswith("$") for k in expected
            ):
                if not SqliteBaseStore._compare_op(value.get(key), expected):
                    return False
            elif value.get(key) != expected:
                return False
        return True

    @staticmethod
    def _compare_op(actual: Any, spec: dict[str, Any]) -> bool:
        for operator, expected in spec.items():
            if operator == "$eq" and actual != expected:
                return False
            if operator == "$ne" and actual == expected:
                return False
            if operator == "$gt" and not (actual is not None and actual > expected):
                return False
            if operator == "$gte" and not (actual is not None and actual >= expected):
                return False
            if operator == "$lt" and not (actual is not None and actual < expected):
                return False
            if operator == "$lte" and not (actual is not None and actual <= expected):
                return False
        return True

    @staticmethod
    def _matches_conditions(
        ns: tuple[str, ...],
        conditions: tuple[Any, ...] | None,
    ) -> bool:
        if not conditions:
            return True
        for cond in conditions:
            path = cond.path
            if cond.match_type == "prefix":
                if len(ns) >= len(path) and all(
                    p == "*" or p == a for p, a in zip(path, ns, strict=False)
                ):
                    return True
            else:  # suffix
                if len(ns) >= len(path) and all(
                    p == "*" or p == a for p, a in zip(path, ns[-len(path) :], strict=False)
                ):
                    return True
        return False

    # ---- convenience (memory browser / editor) ----

    def items_in(self, namespace: tuple[str, ...], *, oldest_first: bool = False) -> list[Item]:
        order = "ASC" if oldest_first else "DESC"
        # rowid breaks timestamp ties in insertion order; a random key tiebreak made
        # "oldest-out" eviction non-deterministic when two writes share a clock tick.
        rows = self._conn.execute(
            "SELECT ns, key, value, created_at, updated_at FROM memory_items "
            "WHERE ns = ? ORDER BY updated_at " + order + f", {self._TIE} " + order,
            (_ns_str(namespace),),
        ).fetchall()
        return [i for r in rows if (i := self._row_to_item(r)) is not None]

    def close(self) -> None:
        self._conn.close()

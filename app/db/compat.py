"""A `sqlite3.Connection` look-alike over the Postgres pool (Mk13 Phase 2).

The stores (`MemberStore`, `ApprovalStore`, `SqliteBaseStore`) are written against sqlite3's
API. Instead of maintaining a second copy of ~1500 lines of store code for Postgres, they run
unchanged on a `PgSession`, which reproduces exactly the slice of sqlite3 they use:

    conn.execute(sql, params)  -> cursor with fetchone()/fetchall()/rowcount
    row["col"] and row[0]      -> `Row`
    conn.commit() / conn.rollback() / conn.close()
    with conn: ...             -> commit on success, roll back on error

Behavioural notes that matter over a ~150 ms link (Neon, from an Indian laptop):
- **Transactions are opened lazily, at the first write.** psycopg's default wraps every
  statement as BEGIN -> statement -> COMMIT: three round trips (~440 ms) for a plain SELECT.
  Here a SELECT is one round trip. This is behaviour-preserving under Postgres's default READ
  COMMITTED isolation, where each statement already gets its own snapshot and a plain SELECT
  takes no locks - an enclosing transaction adds nothing for reads. From the first write on
  (INSERT/UPDATE/DELETE...), everything up to commit()/rollback()/`with` exit shares one
  transaction on one pooled connection, so multi-statement writes stay atomic, exactly like
  sqlite3's implicit transactions, which the store code relies on.
- Results are fetched eagerly, so a connection is never held open by an unread cursor.
- `hold=True` (per-operation sessions, e.g. MemberStore): the connection is kept until
  `close()`; closing with an open transaction discards it, like sqlite3.
- `hold=False` (long-lived session objects, e.g. ApprovalStore): every statement outside a
  `with` block is a single autocommit statement (one round trip, atomic by itself) and the
  connection goes straight back to the pool, so the store never pins a connection that Neon
  would drop while idle. A `with` block borrows one connection for its whole transaction.
- A SELECT whose connection turned out to be dead is retried once on a fresh connection
  (harmless: a read). Writes are NOT retried: after a lost reply there is no way to know
  whether the server already applied them. The pool's idle ping (see pool.py) is what protects
  writes from connections Neon cut while idle.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator, Sequence
from functools import lru_cache
from typing import Any, Self

import psycopg
from psycopg.rows import tuple_row
from psycopg_pool import ConnectionPool

from app.db.sql import qmark_to_format


@lru_cache(maxsize=1024)
def translate(sql: str) -> str:
    """`?` placeholders -> `%s` (memoised: the stores reuse a few dozen statements)."""
    return qmark_to_format(sql)


def _is_read(sql: str) -> bool:
    """A plain SELECT (needs no transaction). Anything else is treated as a write, conservatively."""
    return sql.lstrip()[:6].upper() == "SELECT"


class Row:
    """sqlite3.Row look-alike: `row["name"]`, `row[0]`, `keys()`, iteration over values."""

    __slots__ = ("_index", "_values")

    def __init__(self, index: dict[str, int], values: Sequence[Any]) -> None:
        self._index = index
        self._values = tuple(values)

    def __getitem__(self, key: int | str | slice) -> Any:
        if isinstance(key, str):
            return self._values[self._index[key]]
        return self._values[key]

    def keys(self) -> list[str]:
        return list(self._index)

    def __iter__(self) -> Iterator[Any]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Row({dict(zip(self._index, self._values, strict=False))!r})"


class Cursor:
    """Eagerly-fetched result set with the sqlite3 cursor methods the stores call."""

    def __init__(self, names: list[str], rows: list[tuple[Any, ...]], rowcount: int) -> None:
        index = {name: i for i, name in enumerate(names)}
        self._rows = [Row(index, r) for r in rows]
        self._pos = 0
        self.rowcount = rowcount
        self.lastrowid = None  # sqlite-only; no store code path on Postgres needs it

    def fetchone(self) -> Row | None:
        if self._pos >= len(self._rows):
            return None
        row = self._rows[self._pos]
        self._pos += 1
        return row

    def fetchall(self) -> list[Row]:
        rest = self._rows[self._pos :]
        self._pos = len(self._rows)
        return rest

    def __iter__(self) -> Iterator[Row]:
        return iter(self.fetchall())


class _State:
    """One thread's borrowed connection + transaction bookkeeping."""

    __slots__ = ("depth", "in_tx", "raw", "wrote")

    def __init__(self) -> None:
        self.raw: psycopg.Connection | None = None
        self.depth = 0  # nesting of `with session:` blocks
        self.in_tx = False  # a real transaction is open (opened by the first write)
        self.wrote = False  # a write happened since the last commit


class PgSession:
    """Thread-safe: every thread gets its own connection/transaction state, so one long-lived
    instance (e.g. a store shared by all request threads) never lends one thread's connection
    to another."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        hold: bool,
        on_commit: Callable[[], None] | None = None,
    ) -> None:
        self._pool = pool
        self._hold = hold
        self._on_commit = on_commit
        self._local = threading.local()

    @property
    def _st(self) -> _State:
        st = getattr(self._local, "st", None)
        if st is None:
            st = self._local.st = _State()
        return st

    # ---- connection lifecycle -------------------------------------------------------------
    def _acquire(self) -> psycopg.Connection:
        st = self._st
        if st.raw is None:
            st.raw = self._pool.getconn()  # pool connections are autocommit; see execute()
        return st.raw

    def _release(self) -> None:
        st = self._st
        raw, st.raw = st.raw, None
        in_tx, st.in_tx, st.wrote = st.in_tx, False, False
        if raw is None:
            return
        try:
            if not raw.closed:
                if in_tx or raw.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
                    raw.rollback()  # closing with uncommitted work discards it (sqlite3 semantics)
                raw.autocommit = True  # the pool's other users expect autocommit connections
        except psycopg.Error:
            pass  # a broken connection is discarded by putconn below
        self._pool.putconn(raw)

    def _fire_commit(self) -> None:
        if self._on_commit is not None:
            self._on_commit()

    # ---- sqlite3 API ----------------------------------------------------------------------
    def execute(self, sql: str, params: Sequence[Any] = ()) -> Cursor:
        query = translate(sql)
        args = tuple(params)
        read = _is_read(sql)
        st = self._st
        # Outside any `with` block an auto-release session runs each statement on its own
        # (autocommit): one round trip, atomic by itself.
        standalone = not self._hold and st.depth == 0
        retried = False
        while True:
            raw = self._acquire()
            if not read and not standalone and not st.in_tx:
                raw.autocommit = False  # first write of a unit of work: psycopg will BEGIN
                st.in_tx = True
            try:
                with raw.cursor(row_factory=tuple_row) as cur:
                    cur.execute(query, args)
                    described = cur.description
                    names = [d.name for d in described] if described else []
                    rows = cur.fetchall() if described else []
                    result = Cursor(names, rows, cur.rowcount)
            except psycopg.OperationalError:
                dead = raw.closed or raw.broken
                if dead and read and not st.in_tx and not retried:
                    retried = True
                    self._release()  # discard the dead connection, take a fresh one
                    continue
                self._abort_if_unowned()
                raise
            except BaseException:
                self._abort_if_unowned()
                raise
            if not read:
                st.wrote = True
            if standalone:
                wrote = st.wrote
                self._release()
                if wrote:
                    self._fire_commit()
            return result

    def _abort_if_unowned(self) -> None:
        """A failed statement outside a `with` block on an auto-release session: give the
        connection back rolled-back (Postgres aborts the whole transaction on any error)."""
        if not self._hold and self._st.depth == 0:
            self._release()

    def commit(self) -> None:
        st = self._st
        raw = st.raw
        if raw is not None and st.in_tx and not raw.closed:
            raw.commit()
            raw.autocommit = True
        st.in_tx = False
        wrote, st.wrote = st.wrote, False
        if wrote:
            self._fire_commit()

    def rollback(self) -> None:
        st = self._st
        raw = st.raw
        if raw is not None and st.in_tx and not raw.closed:
            raw.rollback()
            raw.autocommit = True
        st.in_tx = False
        st.wrote = False

    def close(self) -> None:
        self._st.depth = 0
        self._release()

    def __enter__(self) -> Self:
        self._st.depth += 1
        self._acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        st = self._st
        st.depth -= 1
        if st.depth > 0:
            return
        if exc_type is None:
            self.commit()
        else:
            self.rollback()
        if not self._hold:
            self._release()

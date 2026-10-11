"""Casbin policy rows on the shared Postgres (Mk13 Phase 6).

Table `policy_rules` (migration 0006) holds what `policy.csv` held: one row per Casbin rule,
keyed by the rule itself, so a rule exists at most once however many laptops add it.

- **First boot seeds from the checkout's `policy.csv`** (`source = 'file'`), once per database:
  a `meta.policy_seeded` marker is claimed in the same transaction, so two laptops starting
  together seed exactly once. After that the database is the source of truth; later edits to
  `policy.csv` in git reach the shared database only through `python -m app.db sync-config`
  (explicit on purpose: a laptop on an old checkout must not silently re-grant a rule the team
  removed, nor revoke one it added).
- **Runtime additions** (dashboard: registering a department, enabling a toolkit) are
  `source = 'dashboard'`; `sync-config --prune` never removes those.
- **Every change bumps `members_version`** in the same statement, which is what makes every
  laptop's enforcer rebuild (see `app/db/version.py`). The enforcer only reads the rules when it
  rebuilds, so steady-state enforcement costs no extra round trips.
"""

from __future__ import annotations

import logging
import threading
import weakref
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

from psycopg_pool import ConnectionPool

from app.db import get_pool
from app.db.schema import ensure_migrated
from app.db.version import BUMP_SQL, SharedVersion
from app.harness.policy_store import POLICY_PATH, Rules, RulesAdapter, parse_policy_file

log = logging.getLogger(__name__)

ARITY = {"p": 4, "g": 3}  # model.conf: p = sub, dom, obj, act; g = _, _, _
_seeded: weakref.WeakSet[ConnectionPool] = weakref.WeakSet()
_seed_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def normalise(row: Iterable[str]) -> tuple[str, str, str, str, str]:
    """("p", sub, dom, obj, act) / ("g", a, b, c) -> a 5-tuple padded with ''. Refuses anything
    Casbin would not load, so a bad row cannot sit in the table looking like a grant."""
    parts = tuple(str(p).strip() for p in row)
    if not parts or parts[0] not in ARITY:
        raise ValueError(f"policy row must start with one of {sorted(ARITY)}: {parts!r}")
    values = parts[1:]
    if len(values) != ARITY[parts[0]] or not all(values):
        raise ValueError(f"{parts[0]} row needs {ARITY[parts[0]]} non-empty values: {parts!r}")
    padded = values + ("",) * (4 - len(values))
    return (parts[0], *padded)  # type: ignore[return-value]


def file_rows(path: str | Path) -> list[tuple[str, str, str, str, str]]:
    rules = parse_policy_file(path)
    out: list[tuple[str, str, str, str, str]] = []
    for ptype, rows in rules.items():
        for r in rows:
            row = normalise((ptype, *r))
            if row not in out:
                out.append(row)
    return out


def _columns(rows: list[tuple[str, ...]]) -> list[list[str]]:
    return [list(col) for col in zip(*rows, strict=True)] if rows else [[], [], [], [], []]


_INSERT = (
    "INSERT INTO policy_rules (ptype, v0, v1, v2, v3, source, created_at, created_by)"
    " SELECT t.ptype, t.v0, t.v1, t.v2, t.v3, %s, %s, %s"
    " FROM unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[]) WITH ORDINALITY"
    "   AS t(ptype, v0, v1, v2, v3, n) ORDER BY t.n"
    " ON CONFLICT DO NOTHING RETURNING 1"
)


class PgPolicyStore:
    backend = "postgres"

    def __init__(
        self,
        pool: ConnectionPool | None = None,
        *,
        seed_from: str | Path | None = None,
        version_ttl: float = 1.0,
    ) -> None:
        self._pool = pool or get_pool()
        ensure_migrated(self._pool)
        self._version = SharedVersion(self._pool, version_ttl)
        if seed_from is None:
            try:
                from app.config import get_settings

                seed_from = get_settings().services.policy_path
            except Exception:
                seed_from = POLICY_PATH
        if seed_from is not None:
            seed_policy(self._pool, seed_from)

    # ---- reads -------------------------------------------------------------------------
    def rows(self) -> list[tuple[str, str, str, str, str]]:
        with self._pool.connection() as conn:
            got = conn.execute("SELECT ptype, v0, v1, v2, v3 FROM policy_rules ORDER BY seq").fetchall()
        return [(r["ptype"], r["v0"], r["v1"], r["v2"], r["v3"]) for r in got]

    def rules(self) -> Rules:
        rules: Rules = {}
        for ptype, *values in self.rows():
            rules.setdefault(ptype, []).append(values[: ARITY[ptype]])
        return rules

    def casbin_source(self) -> RulesAdapter:
        return RulesAdapter(self.rules())

    def has_row(self, row: tuple[str, ...]) -> bool:
        key = normalise(row)
        with self._pool.connection() as conn:
            hit = conn.execute(
                "SELECT 1 FROM policy_rules WHERE ptype = %s AND v0 = %s AND v1 = %s AND v2 = %s AND v3 = %s",
                key,
            ).fetchone()
        return hit is not None

    def version(self) -> int:
        return self._version.get()

    # ---- writes ------------------------------------------------------------------------
    def add_rows(self, rows: Iterable[tuple[str, ...]], actor: str = "", *, source: str = "dashboard") -> int:
        """Insert the missing rows and bump the counter — one statement, one round trip."""
        wanted: list[tuple[str, ...]] = []
        for row in rows:
            key = normalise(row)
            if key not in wanted:
                wanted.append(key)
        if not wanted:
            return 0
        sql = (
            f"WITH ins AS ({_INSERT}),"
            f" bump AS ({BUMP_SQL} AND EXISTS (SELECT 1 FROM ins) RETURNING 1)"
            " SELECT (SELECT COUNT(*) FROM ins) AS added"
        )
        with self._pool.connection() as conn:
            added = conn.execute(sql, (source, _now(), actor, *_columns(wanted))).fetchone()["added"]
        if added:
            self._version.changed_here()
        return int(added)

    def remove_rows(self, rows: Iterable[tuple[str, ...]], *, sources: tuple[str, ...] = ("file",)) -> int:
        keys = list(dict.fromkeys(normalise(r) for r in rows))
        if not keys:
            return 0
        sql = (
            "WITH del AS (DELETE FROM policy_rules p USING"
            " unnest(%s::text[], %s::text[], %s::text[], %s::text[], %s::text[]) AS t(ptype, v0, v1, v2, v3)"
            " WHERE p.ptype = t.ptype AND p.v0 = t.v0 AND p.v1 = t.v1 AND p.v2 = t.v2 AND p.v3 = t.v3"
            " AND p.source = ANY(%s) RETURNING 1),"
            f" bump AS ({BUMP_SQL} AND EXISTS (SELECT 1 FROM del) RETURNING 1)"
            " SELECT (SELECT COUNT(*) FROM del) AS removed"
        )
        with self._pool.connection() as conn:
            removed = conn.execute(sql, (*_columns(keys), list(sources))).fetchone()["removed"]
        if removed:
            self._version.changed_here()
        return int(removed)

    # ---- file <-> database drift (sync-config) ------------------------------------------
    def drift(self, path: str | Path | None = None) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
        if path is None:
            try:
                from app.config import get_settings

                path = get_settings().services.policy_path
            except Exception:
                path = POLICY_PATH
        return policy_drift(self._pool, path)


def policy_drift(
    pool: ConnectionPool, path: str | Path | None = None
) -> tuple[list[tuple[str, ...]], list[tuple[str, ...]]]:
    """(rows in the file but not the database, file-seeded rows no longer in the file)."""
    if path is None:
        try:
            from app.config import get_settings

            path = get_settings().services.policy_path
        except Exception:
            path = POLICY_PATH
    in_file = file_rows(path)
    with pool.connection() as conn:
        got = conn.execute("SELECT ptype, v0, v1, v2, v3, source FROM policy_rules ORDER BY seq").fetchall()
    in_db = {(r["ptype"], r["v0"], r["v1"], r["v2"], r["v3"]): r["source"] for r in got}
    missing = [r for r in in_file if r not in in_db]
    file_set = set(in_file)
    stale = [r for r, src in in_db.items() if src == "file" and r not in file_set]
    return missing, stale


def _warn_drift(pool: ConnectionPool, path: str | Path) -> None:
    """Start-up hint (never an error): this checkout's policy.csv and the shared rules differ."""
    try:
        missing, stale = policy_drift(pool, path)
    except Exception:
        log.debug("policy drift check failed", exc_info=True)
        return
    if missing or stale:
        log.warning(
            "policy: %d rule(s) in %s are not in the shared database and %d seeded rule(s) are no "
            "longer in it — review with `python -m app.db sync-config --dry-run`",
            len(missing), Path(path).name, len(stale),
        )


def seed_policy(pool: ConnectionPool, path: str | Path | None = None) -> int:
    """Copy `policy.csv` into an empty shared database, once per database (race-safe).

    Returns the number of rows inserted (0 if this or another laptop already seeded it).
    Checked once per process per pool, so it costs one read at start-up and nothing after.
    """
    if path is None:
        try:
            from app.config import get_settings

            path = get_settings().services.policy_path
        except Exception:
            path = POLICY_PATH
    if pool in _seeded:
        return 0
    with _seed_lock:
        if pool in _seeded:
            return 0
        with pool.connection() as conn:
            done = conn.execute("SELECT 1 FROM meta WHERE key = 'policy_seeded'").fetchone()
        inserted = 0
        if done is None:
            rows = file_rows(path)
            with pool.connection() as conn, conn.transaction():
                # Claiming the marker first serialises simultaneous first boots: the loser waits
                # for the winner's commit, then sees the conflict and inserts nothing.
                claimed = conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('policy_seeded', %s) ON CONFLICT DO NOTHING RETURNING 1",
                    (_now(),),
                ).fetchone()
                if claimed is not None:
                    inserted = conn.execute(
                        f"WITH ins AS ({_INSERT}) SELECT COUNT(*) AS n FROM ins",
                        ("file", _now(), "seed", *_columns(rows)),
                    ).fetchone()["n"]
                    conn.execute(BUMP_SQL)
            if inserted:
                log.info("policy: seeded %d rule(s) from %s into the shared database", inserted, path)
        else:
            _warn_drift(pool, path)
        _seeded.add(pool)
        return int(inserted)

"""SkillRegistry on the shared Postgres (Mk13 Phase 6).

Table `skills` (migration 0006) holds each skill's full `skill.md` document, keyed by name, so
the dashboard editor's save on one laptop is the skill every laptop runs. The document is
parsed with the same `parse_skill_document` as the files, so validation and the LLM-facing
schema are identical on both back-ends.

- **First boot imports `skills/`** from the checkout (`source = 'file'`), once per database
  (`meta.skills_seeded` claimed in the same transaction). Later additions to `skills/` in git
  are imported by `python -m app.db sync-config`; it never overwrites a skill that exists in the
  database unless asked (`--overwrite-skills`), because the dashboard copy may be newer.
- **Hot reload is kept.** The agent loads the skill list on every run; here that list is cached
  per process and re-read only when `members_version` moves — which every skill write bumps in
  the same statement. So an edit anywhere is live on every laptop within ~1 s, and a run costs
  no round trip for skills in the steady state.
"""

from __future__ import annotations

import logging
import threading
import weakref
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from psycopg_pool import ConnectionPool

from app.db import get_pool
from app.db.schema import ensure_migrated
from app.db.version import BUMP_SQL, SharedVersion
from app.skills.model import Skill, parse_skill_document, render_skill
from app.skills.registry import SkillRegistry

log = logging.getLogger(__name__)

_seeded: weakref.WeakSet[ConnectionPool] = weakref.WeakSet()
_seed_lock = threading.Lock()


def _now() -> str:
    return datetime.now(UTC).isoformat()


def db_path(name: str) -> str:
    """What `Skill.path` says for a database skill (shown in the dashboard's skill list)."""
    return f"db:skills/{name}"


def disk_skills(root: str | Path) -> tuple[list[Skill], list[str]]:
    return SkillRegistry(root).load_with_errors()


_UPSERT = (
    "INSERT INTO skills (name, scope, document, source, updated_at, updated_by)"
    " VALUES (%s, %s, %s, %s, %s, %s)"
)


class PgSkillRegistry(SkillRegistry):
    def __init__(
        self,
        pool: ConnectionPool | None = None,
        *,
        root: str | Path | None = None,
        seed: bool = True,
        version_ttl: float = 1.0,
    ) -> None:
        from app.config import get_settings

        self._pool = pool or get_pool()
        ensure_migrated(self._pool)
        self._version = SharedVersion(self._pool, version_ttl)
        self._cache: tuple[int, list[Skill], list[str]] | None = None
        self._cache_lock = threading.Lock()
        # The checkout's skills/ folder: first-boot seed + sync-config only; runs read the table.
        self.root = Path(root) if root is not None else get_settings().skills.skills_dir
        if seed:
            seed_skills(self._pool, self.root)

    # ---- reading -----------------------------------------------------------------------
    def _read(self) -> tuple[list[Skill], list[str]]:
        with self._pool.connection() as conn:
            rows = conn.execute("SELECT name, document FROM skills ORDER BY name").fetchall()
        skills: list[Skill] = []
        errors: list[str] = []
        for r in rows:
            try:
                skill = parse_skill_document(r["document"], fallback_name=r["name"])
            except Exception as exc:  # noqa: BLE001 - surface a malformed stored skill, like a bad file
                errors.append(str(exc))
                continue
            skills.append(replace(skill, path=db_path(r["name"])))
        return skills, errors

    def load_with_errors(self) -> tuple[list[Skill], list[str]]:
        version = self._version.get()
        with self._cache_lock:
            if self._cache is not None and self._cache[0] == version:
                return list(self._cache[1]), list(self._cache[2])
        skills, errors = self._read()
        with self._cache_lock:
            self._cache = (version, skills, errors)
        return list(skills), list(errors)

    def _changed(self) -> None:
        with self._cache_lock:
            self._cache = None
        self._version.changed_here()

    # ---- writing (dashboard editor) ------------------------------------------------------
    def save(self, skill: Skill, actor: str = "", *, source: str = "dashboard") -> str:  # type: ignore[override]
        """Create or replace a skill; live on every laptop on their next run."""
        sql = (
            f"WITH up AS ({_UPSERT} ON CONFLICT (name) DO UPDATE SET scope = EXCLUDED.scope,"
            " document = EXCLUDED.document, source = EXCLUDED.source,"
            " updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by RETURNING 1),"
            f" bump AS ({BUMP_SQL} RETURNING 1)"
            " SELECT (SELECT COUNT(*) FROM up) AS n"
        )
        with self._pool.connection() as conn:
            conn.execute(sql, (skill.name, skill.scope, render_skill(skill), source, _now(), actor))
        self._changed()
        return db_path(skill.name)

    def delete(self, name: str) -> bool:
        sql = (
            "WITH del AS (DELETE FROM skills WHERE name = %s RETURNING 1),"
            f" bump AS ({BUMP_SQL} AND EXISTS (SELECT 1 FROM del) RETURNING 1)"
            " SELECT (SELECT COUNT(*) FROM del) AS n"
        )
        with self._pool.connection() as conn:
            n = conn.execute(sql, (name,)).fetchone()["n"]
        if n:
            self._changed()
        return bool(n)

    # ---- disk <-> database drift (sync-config) ------------------------------------------
    def drift(self, root: str | Path | None = None) -> tuple[list[Skill], list[Skill], list[str]]:
        return skills_drift(self._pool, root or self.root)

    def import_skills(self, skills: list[Skill], *, overwrite: bool = False) -> int:
        """Add skills (sync-config). Existing names are left alone unless `overwrite`."""
        if not skills:
            return 0
        conflict = (
            "ON CONFLICT (name) DO UPDATE SET scope = EXCLUDED.scope, document = EXCLUDED.document,"
            " source = EXCLUDED.source, updated_at = EXCLUDED.updated_at, updated_by = EXCLUDED.updated_by"
            if overwrite
            else "ON CONFLICT (name) DO NOTHING"
        )
        n = 0
        with self._pool.connection() as conn, conn.transaction():
            for s in skills:
                cur = conn.execute(
                    f"{_UPSERT} {conflict} RETURNING 1",
                    (s.name, s.scope, render_skill(s), "file", _now(), "sync-config"),
                )
                n += cur.fetchone() is not None
            if n:
                conn.execute(BUMP_SQL)
        if n:
            self._changed()
        return n


def skills_drift(pool: ConnectionPool, root: str | Path) -> tuple[list[Skill], list[Skill], list[str]]:
    """(skills on disk missing from the database, skills on disk whose content differs from
    the database copy, disk parse errors)."""
    on_disk, errors = disk_skills(root)
    with pool.connection() as conn:
        rows = conn.execute("SELECT name, document FROM skills").fetchall()
    stored = {r["name"]: r["document"] for r in rows}
    missing = [s for s in on_disk if s.name not in stored]
    differs = [s for s in on_disk if s.name in stored and stored[s.name] != render_skill(s)]
    return missing, differs, errors


def seed_skills(pool: ConnectionPool, root: str | Path) -> int:
    """Import `skills/` into an empty shared database, once per database (race-safe)."""
    if pool in _seeded:
        return 0
    with _seed_lock:
        if pool in _seeded:
            return 0
        with pool.connection() as conn:
            done = conn.execute("SELECT 1 FROM meta WHERE key = 'skills_seeded'").fetchone()
        inserted = 0
        if done is None:
            skills, errors = disk_skills(root)
            for err in errors:
                log.warning("skills: not imported (invalid skill.md): %s", err)
            with pool.connection() as conn, conn.transaction():
                claimed = conn.execute(
                    "INSERT INTO meta (key, value) VALUES ('skills_seeded', %s) ON CONFLICT DO NOTHING RETURNING 1",
                    (_now(),),
                ).fetchone()
                if claimed is not None:
                    for s in skills:
                        cur = conn.execute(
                            f"{_UPSERT} ON CONFLICT (name) DO NOTHING RETURNING 1",
                            (s.name, s.scope, render_skill(s), "file", _now(), "seed"),
                        )
                        inserted += cur.fetchone() is not None
                    conn.execute(BUMP_SQL)
            if inserted:
                log.info("skills: seeded %d skill(s) from %s into the shared database", inserted, root)
        else:
            try:
                missing, differs, _errors = skills_drift(pool, root)
            except Exception:  # noqa: BLE001 - a hint must never stop a service from starting
                missing, differs = [], []
            if missing or differs:
                log.warning(
                    "skills: %d skill(s) in %s are not in the shared database and %d differ from it — "
                    "review with `python -m app.db sync-config --dry-run`",
                    len(missing), root, len(differs),
                )
        _seeded.add(pool)
        return inserted


_registries: weakref.WeakKeyDictionary[ConnectionPool, PgSkillRegistry] = weakref.WeakKeyDictionary()
_registries_lock = threading.Lock()


def shared_registry(pool: ConnectionPool | None = None) -> PgSkillRegistry:
    """One registry (one cache) per pool per process: the runner asks on every run."""
    pool = pool or get_pool()
    with _registries_lock:
        reg = _registries.get(pool)
        if reg is None:
            reg = PgSkillRegistry(pool)
            _registries[pool] = reg
        return reg

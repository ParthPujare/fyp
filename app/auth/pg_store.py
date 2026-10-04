"""MemberStore on the shared Postgres (Mk13 Phase 2).

Runs the *same* store code as the SQLite `MemberStore` — only the connection (`PgSession`) and
schema set-up differ — plus a read cache, because the shared database is ~150 ms away (Neon,
from an Indian laptop) and the app asks the same questions constantly:

- `is_active(sub)` on every HTTP request, `get()` on every audit entry / permission lookup,
- the enforcer's `version` on **every** `enforce()` call (dozens per chat turn),
- `is_app_connected` once per discovered external tool.

Uncached, one chat turn would spend seconds just re-reading a 7-row table. Design:

- **`version` is polled at most once per `VERSION_TTL_S`.** It is the shared change counter
  (`meta.members_version`, bumped in the same transaction as every member/department change),
  so when *another laptop* changes a member or department, the next poll sees a new number and
  this process drops its cache. Worst-case staleness for that: `VERSION_TTL_S` (1 s). Because
  the counter guards them, members/departments can safely be cached for a long time
  (`MEMBER_TTL_S`, only a safety net).
- **Data the counter does not cover** (LLM-key metadata, connections, toolkit catalog) is cached
  for `READ_TTL_S` (30 s): another person's change shows up within that window. Either kind is
  cleared at once on any commit made by *this* process, so you always read your own writes.
- **Hot data is pre-loaded at start-up**, so the first request after a container starts is not
  a cold one (a cold `is_active` costs two round trips, a cold chat turn ~14).
- **Login (`verify`) never uses the cache**: a password reset or deactivation elsewhere must
  take effect on the next sign-in attempt, not up to 3 s later.
- A generation counter stops a slow read that started before a write from re-populating the
  cache with pre-write data.
"""

from __future__ import annotations

import copy
import threading
import time
from collections.abc import Callable
from typing import Any

import psycopg.errors
from psycopg_pool import ConnectionPool

from app.auth.store import (
    DepartmentRecord,
    LLMKeyRecord,
    MemberRecord,
    MemberStore,
    verify_password,
)
from app.db import get_pool
from app.db.compat import PgSession
from app.db.schema import ensure_migrated


class PgMemberStore(MemberStore):
    INTEGRITY_ERRORS = (psycopg.errors.IntegrityError,)
    VERSION_TTL_S = 1.0
    MEMBER_TTL_S = 300.0  # members/departments: the version counter invalidates them on change
    READ_TTL_S = 30.0  # keys / connections / catalog: no change counter, so bounded staleness

    def __init__(
        self,
        pool: ConnectionPool | None = None,
        audit: Any = None,
        *,
        version_ttl: float | None = None,
        read_ttl: float | None = None,
        member_ttl: float | None = None,
    ) -> None:
        self._pool = pool or get_pool()
        self._version_ttl = self.VERSION_TTL_S if version_ttl is None else version_ttl
        self._read_ttl = self.READ_TTL_S if read_ttl is None else read_ttl
        # An explicit read_ttl (tests use 0 = "cache off") also governs members unless overridden.
        self._member_ttl = (
            member_ttl if member_ttl is not None else (self.MEMBER_TTL_S if read_ttl is None else read_ttl)
        )
        self._cache: dict[Any, tuple[float, Any]] = {}
        self._cache_lock = threading.Lock()
        self._generation = 0
        self._seen_version: int | None = None
        self._version_checked = 0.0
        super().__init__(path=None, audit=audit)  # -> _init_schema() below

    # ---- back-end hooks ------------------------------------------------------------------
    def _connect(self) -> PgSession:  # type: ignore[override]
        return PgSession(self._pool, hold=True, on_commit=self._invalidate)

    def _init_schema(self) -> None:
        ensure_migrated(self._pool)  # tables come from migrations, not CREATE TABLE IF NOT EXISTS
        self._seed_if_empty()  # idempotent (ON CONFLICT DO NOTHING) — safe if two laptops race
        self._warm()

    def _warm(self) -> None:
        """Pre-load what nearly every request reads so the first request is not a cold one."""
        self.list()
        self.list_departments()
        self.list_toolkits()
        self.list_cached_tools()

    # ---- cache plumbing ------------------------------------------------------------------
    def _invalidate(self) -> None:
        with self._cache_lock:
            self._cache.clear()
            self._generation += 1
            self._version_checked = 0.0  # re-read the counter next time: it just changed

    def _observe_version(self) -> int:
        now = time.monotonic()
        if self._seen_version is not None and now - self._version_checked < self._version_ttl:
            return self._seen_version
        version = MemberStore.version.fget(self)  # type: ignore[attr-defined]
        if self._seen_version is not None and version != self._seen_version:
            self._invalidate()  # someone else changed members/departments
        with self._cache_lock:
            self._seen_version = version
            self._version_checked = time.monotonic()
        return version

    def _memo(
        self, key: Any, loader: Callable[[], Any], ttl: float | None = None, *, copy_out: bool = True
    ) -> Any:
        ttl = self._read_ttl if ttl is None else ttl
        self._observe_version()
        with self._cache_lock:
            hit = self._cache.get(key)
            generation = self._generation
            if hit is not None and time.monotonic() - hit[0] < ttl:
                return copy.deepcopy(hit[1]) if copy_out else hit[1]
        value = loader()
        with self._cache_lock:
            if generation == self._generation:  # nothing was written while we were reading
                self._cache[key] = (time.monotonic(), value)
        return copy.deepcopy(value) if copy_out else value

    @property
    def version(self) -> int:  # type: ignore[override]
        return self._observe_version()

    # ---- cached reads ---------------------------------------------------------------------
    def _members_view(self) -> list[MemberRecord]:
        """The cached member list itself (NOT a copy) — read-only, for internal lookups."""
        return self._memo(("list",), lambda: MemberStore.list(self), self._member_ttl, copy_out=False)

    def get(self, username: str) -> MemberRecord | None:
        # Served from the cached member list: a per-user read would cost a round trip for every
        # new user, while the whole (tiny) table is already loaded and version-guarded.
        for member in self._members_view():
            if member.username == username:
                return copy.deepcopy(member)
        return None

    def list(self) -> list[MemberRecord]:
        return self._memo(("list",), lambda: MemberStore.list(self), self._member_ttl)

    def list_departments(self) -> list[DepartmentRecord]:
        return self._memo(("depts",), lambda: MemberStore.list_departments(self), self._member_ttl)

    def get_department(self, name: str) -> DepartmentRecord | None:
        return self._memo(("dept", name), lambda: MemberStore.get_department(self, name), self._member_ttl)

    def list_llm_keys(self, username: str) -> list[LLMKeyRecord]:
        return self._memo(("llmkeys", username), lambda: MemberStore.list_llm_keys(self, username))

    def get_active_llm_key_meta(self, username: str) -> tuple[str, str] | None:
        return self._memo(("llmmeta", username), lambda: MemberStore.get_active_llm_key_meta(self, username))

    def _fetch_llm_key(self, query: str, params: tuple[Any, ...]) -> tuple[str, str, str, str] | None:
        # Caches the ENCRYPTED row only; decryption still happens per call in `_llm_key_row`.
        return self._memo(("llmrow", query, params), lambda: MemberStore._fetch_llm_key(self, query, params))

    def get_platform_llm_meta(self) -> tuple[str, str, str] | None:
        return self._memo(("platformmeta",), lambda: MemberStore.get_platform_llm_meta(self))

    def _fetch_platform_llm(self) -> tuple[str, str, str, str] | None:
        # Caches the ENCRYPTED row only; decryption still happens per call in `get_platform_llm`.
        return self._memo(("platformrow",), lambda: MemberStore._fetch_platform_llm(self))

    def get_composio_key(self, username: str) -> str | None:
        return self._memo(("composio", username), lambda: MemberStore.get_composio_key(self, username))

    def list_connected_accounts(self, username: str) -> list[dict[str, Any]]:
        return self._memo(("accounts", username), lambda: MemberStore.list_connected_accounts(self, username))

    def is_app_connected(self, username: str, app: str) -> bool:
        return self._memo(("connected", username, app), lambda: MemberStore.is_app_connected(self, username, app))

    def list_toolkits(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        return self._memo(("toolkits", enabled_only), lambda: MemberStore.list_toolkits(self, enabled_only))

    def list_cached_tools(self, toolkit: str | None = None) -> list[dict[str, Any]]:
        return self._memo(("tools", toolkit), lambda: MemberStore.list_cached_tools(self, toolkit))

    # ---- never cached ---------------------------------------------------------------------
    def verify(self, username: str, password: str) -> bool:
        """Sign-in check straight from the database (a reset/deactivation must apply at once)."""
        member = MemberStore.get(self, username)
        if member is None or not verify_password(password, member.password_hash):
            return False
        return member.active

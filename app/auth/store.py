"""MemberStore — the single source of truth for identity (plan_mk2 §1–§4).

`data/members.sqlite` replaces the static `USERS` dict in `app/auth/users.py`:
every member (username, hashed password, role, dept, scopes, active,
created/updated timestamps) lives here. The static `User`/`USERS` shape is kept
as a store-backed read shim so tests + demo scripts work unchanged; writes only
happen through this store so every change can be guard-railed and audit-logged.

Passwords are stored as stdlib `hashlib.scrypt` digests only — never plaintext.
The 5 seeded demo accounts keep their documented demo passwords (hashed here);
`USERS[x].password` in the compat shim returns those seed plaintexts solely so
HTTP login tests keep working (plan_mk2 §2.3).

Lockout guard (plan_mk2 §2): the last active admin and the acting admin can
never be deactivated. Enforced at the store layer so any caller that mutates
through the store is covered.

Phase 1 = identity only. The Casbin `g` rows are still materialized from
`policy.csv` this phase; store-driven `g` lands in Phase 2 (plan_mk2 §4 row 2).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import get_settings

# Mk17: identity is email. `members.username` physically stores the email
# (kept column name to avoid FK rewrites); `display_name` is UI-only.
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def is_valid_email(value: str) -> bool:
    return bool(_EMAIL_RE.match((value or "").strip())) and len(value.strip()) <= 254


def default_display_name(email: str) -> str:
    return (email or "").split("@")[0].strip() or email.strip()


def validate_display_name(value: str | None) -> str:
    if value is None:
        return ""
    v = str(value).strip()
    if len(v) > 100:
        raise MemberError("display name must be at most 100 characters")
    if "\n" in v or "\r" in v:
        raise MemberError("display name must not contain newlines")
    return v


# Mk18 Phase C: brute-force throttle (shared SQLite + PG via MemberStore.verify).
_LOCK_AFTER_ATTEMPTS = 10
_LOCK_MINUTES = 15


def _locked_until_dt(value: str | None) -> datetime | None:
    try:
        dt = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt


def is_login_locked(locked_until: str | None, now: datetime | None = None) -> bool:
    dt = _locked_until_dt(locked_until)
    if dt is None:
        return False
    return dt > (now or datetime.now(UTC))


def verify_totp(secret_b32: str, code: str | int, *, window: int = 1) -> bool:
    """Stdlib TOTP check (30s steps, ±`window`). Never raises — False on any error."""
    try:
        key = base64.b32decode(str(secret_b32 or "").strip().upper())
        step = int(time.time()) // 30
    except Exception:  # noqa: BLE001
        return False
    want = re.sub(r"\D", "", str(code or ""))
    if not want:
        return False
    import hmac as _hmac
    import struct as _struct

    for offset in range(-window, window + 1):
        msg = _struct.pack(">Q", step + offset)
        digest = _hmac.new(key, msg, hashlib.sha1).digest()
        o = digest[-1] & 0x0F
        num = (_struct.unpack(">I", digest[o : o + 4])[0] & 0x7FFFFFFF) % 1_000_000
        if f"{num:06d}" == want:
            return True
    return False


class MemberError(Exception):
    """Base for member-store failures."""


class MemberExistsError(MemberError):
    """create() called with an existing username."""


class LockoutGuardError(MemberError):
    """Deactivation refused by the last-admin / self lockout guard."""


class DepartmentExistsError(MemberError):
    """register_department() called with an existing department name."""


class DepartmentError(MemberError):
    """Department not found / invalid."""


# Mk15: who must approve a department's gated items (set per department by an admin).
APPROVAL_MODE_HEAD_ONLY = "head-only"
APPROVAL_MODE_HEAD_ADMIN = "head+admin"
APPROVAL_MODES = (APPROVAL_MODE_HEAD_ONLY, APPROVAL_MODE_HEAD_ADMIN)


# Seed demo accounts — migrated into the store on first open (plan_mk2 §5).
# Passwords are the documented demo credentials; only their hashes are stored.
SEED_MEMBERS: list[dict[str, Any]] = [
    {
        "username": "alice@acme.com",
        "password": "alice123",
        "role": "marketing_agent",
        "dept": "marketing",
        "scopes": ["crm:read", "crm:create"],
    },
    {
        "username": "bob@acme.com",
        "password": "bob123",
        "role": "hr_agent",
        "dept": "hr",
        "scopes": ["hr:read", "hr:create"],
    },
    {
        "username": "carol@acme.com",
        "password": "carol123",
        "role": "finance_agent",
        "dept": "finance",
        "scopes": ["finance:read", "finance:transfer"],
    },
    {
        "username": "dave@acme.com",
        "password": "dave123",
        "role": "devops_agent",
        "dept": "devops",
        "scopes": ["devops:read", "devops:create"],
    },
    {
        "username": "admin@acme.com",
        "password": "admin123",
        "role": "admin",
        "dept": "admin",
        "scopes": ["*"],
    },
]

# Seed plaintexts, kept for the `USERS` compat shim only (see module docstring).
SEED_PASSWORDS = {m["username"]: m["password"] for m in SEED_MEMBERS}

# Seed departments — migrated into the store on first open (plan_mk2 Phase 4).
# Service label + URL follow `_service_for`/`ServiceSettings` today; `resources`
# are left empty for seeds because `/api/departments` still derives the resource
# list live from policy.csv `p` rows (unchanged source, plan_mk2 §3.2) and merges
# it with any stored resources (set by admin at registration time).
SEED_DEPARTMENTS: list[dict[str, Any]] = [
    {
        "name": "marketing",
        "service": "crm",
        "service_url": "http://127.0.0.1:8002",
        "resources": [],
        "role": "marketing_agent",
    },
    {
        "name": "hr",
        "service": "hr",
        "service_url": "http://127.0.0.1:8003",
        "resources": [],
        "role": "hr_agent",
    },
    {
        "name": "finance",
        "service": "finance",
        "service_url": "http://127.0.0.1:8005",
        "resources": [],
        "role": "finance_agent",
    },
    {
        "name": "devops",
        "service": "devops",
        "service_url": "http://127.0.0.1:8004",
        "resources": [],
        "role": "devops_agent",
    },
    {
        "name": "admin",
        "service": "harness gateway",
        "service_url": "http://127.0.0.1:8001",
        "resources": [],
        "role": "admin",
    },
]

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P, _SCRYPT_DKLEN = 2**14, 8, 1, 32
_MAXMEM = 128 * 1024 * 1024


def hash_password(password: str) -> str:
    """scrypt digest in self-describing form `scrypt$n$r$p$<b64salt>$<b64hash>`."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_DKLEN,
        maxmem=_MAXMEM,
    )
    return (
        f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}$"
        f"{base64.b64encode(salt).decode('ascii')}$"
        f"{base64.b64encode(digest).decode('ascii')}"
    )


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        return False
    try:
        n, r, p = int(parts[1]), int(parts[2]), int(parts[3])
        salt = base64.b64decode(parts[4])
        expected = base64.b64decode(parts[5])
    except ValueError:
        return False
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=n,
        r=r,
        p=p,
        dklen=len(expected),
        maxmem=_MAXMEM,
    )
    return hmac.compare_digest(digest, expected)


@dataclass(frozen=True)
class MemberRecord:
    username: str
    password_hash: str
    role: str
    dept: str
    scopes: list[str] = field(default_factory=list)
    active: bool = True
    created_at: str = ""
    updated_at: str = ""
    display_name: str = ""
    # Mk18 Phase C: TOTP secret (blank = second factor off). NEVER in to_public.
    totp_secret: str = ""

    @property
    def email(self) -> str:
        """Mk17: identity is email; `username` physically stores it."""
        return self.username

    def claims(self) -> dict[str, Any]:
        """JWT-style claims (sub/role/dept/scopes) for this member."""
        return {
            "sub": self.username,
            "role": self.role,
            "dept": self.dept,
            "scopes": list(self.scopes),
        }

    def to_public(self) -> dict[str, Any]:
        """Dashboard-safe view (no password hash)."""
        return {
            "username": self.username,
            "email": self.username,
            "display_name": self.display_name or default_display_name(self.username),
            "role": self.role,
            "dept": self.dept,
            "scopes": list(self.scopes),
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class DepartmentRecord:
    name: str
    service: str
    service_url: str
    resources: list[str] = field(default_factory=list)
    role: str = ""
    created_at: str = ""
    updated_at: str = ""
    approval_mode: str = APPROVAL_MODE_HEAD_ONLY  # Mk15: head-only | head+admin

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "service": self.service,
            "service_url": self.service_url,
            "resources": list(self.resources),
            "role": self.role,
            "approval_mode": self.approval_mode,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class LLMKeyRecord:
    id: str
    username: str
    provider: str
    model: str
    active: bool
    created_at: str
    updated_at: str
    base_url: str = ""  # required for custom_openai; blank = the provider default

    def to_public(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "provider": self.provider,
            "model": self.model,
            "base_url": self.base_url,
            "active": self.active,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat()


class MemberStore:
    """SQLite-backed identity store (single source of truth, plan_mk2 §3.2).

    `PgMemberStore` (app/auth/pg_store.py) runs this same code on the shared Postgres by
    overriding `_connect`/`_init_schema` — keep SQL here portable (`?` placeholders, no
    SQLite-only functions, `ON CONFLICT` upserts, ANSI `CAST`).
    """

    # Exception type(s) a unique/PK violation raises on this back-end (overridden for Postgres).
    INTEGRITY_ERRORS: tuple[type[Exception], ...] = (sqlite3.IntegrityError,)

    def __init__(self, path: str | Path | None = None, audit: Any = None):
        self.path = Path(path) if path is not None else get_settings().services.members_path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._audit_log = audit
        self._init_schema()

    # ---- low-level ----

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        conn = self._connect()
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS members (
                    username      TEXT PRIMARY KEY,
                    password_hash TEXT NOT NULL,
                    role          TEXT NOT NULL,
                    dept          TEXT NOT NULL,
                    scopes        TEXT NOT NULL,
                    active        INTEGER NOT NULL DEFAULT 1,
                    created_at    TEXT NOT NULL,
                    updated_at    TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES ('members_version', '0') "
                "ON CONFLICT(key) DO NOTHING"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS departments (
                    name        TEXT PRIMARY KEY,
                    service     TEXT NOT NULL,
                    service_url TEXT NOT NULL,
                    resources   TEXT NOT NULL,
                    role        TEXT NOT NULL,
                    created_at  TEXT NOT NULL,
                    updated_at  TEXT NOT NULL
                )
                """
            )
            # Mk17 migration (SQLite twin of 0010): UI-only display name.
            # Physical `username` column stays the email identity (FK-safe).
            try:
                conn.execute(
                    "ALTER TABLE members ADD COLUMN display_name TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass
            # Mk18 migration (SQLite twin of 0011): login throttle + TOTP.
            for _ddl in (
                "ALTER TABLE members ADD COLUMN login_attempts INTEGER NOT NULL DEFAULT 0",
                "ALTER TABLE members ADD COLUMN locked_until TEXT NOT NULL DEFAULT ''",
                "ALTER TABLE members ADD COLUMN totp_secret TEXT NOT NULL DEFAULT ''",
            ):
                try:
                    conn.execute(_ddl)
                except sqlite3.OperationalError:
                    pass
            # Mk15 migration (SQLite twin of 0008): per-department approval mode.
            try:
                conn.execute(
                    "ALTER TABLE departments ADD COLUMN approval_mode TEXT NOT NULL"
                    " DEFAULT 'head-only'"
                )
            except sqlite3.OperationalError:
                pass
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approval_allowlist (
                    member     TEXT NOT NULL,
                    action     TEXT NOT NULL,
                    args_hash  TEXT NOT NULL,
                    args       TEXT NOT NULL,
                    dept       TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (member, action, args_hash)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS member_llm_keys (
                    id                TEXT PRIMARY KEY,
                    username          TEXT NOT NULL,
                    provider          TEXT NOT NULL,
                    model             TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    base_url          TEXT NOT NULL DEFAULT '',
                    active            INTEGER NOT NULL DEFAULT 0,
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL,
                    UNIQUE(username, provider, model)
                )
                """
            )
            # Migration: DBs created before custom OpenAI-compatible endpoints
            # predate `base_url`. SQLite has no ADD COLUMN IF NOT EXISTS.
            try:
                conn.execute(
                    "ALTER TABLE member_llm_keys ADD COLUMN base_url TEXT NOT NULL DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass
            # Mk16 follow-up (SQLite twin of 0009): the single shared platform
            # row (id = 'openrouter') — model plaintext, key Fernet-encrypted.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS platform_llm (
                    id                TEXT PRIMARY KEY,
                    model             TEXT NOT NULL,
                    api_key_encrypted TEXT NOT NULL,
                    updated_by        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS member_composio_keys (
                    username          TEXT PRIMARY KEY,
                    api_key_encrypted TEXT NOT NULL,
                    created_at        TEXT NOT NULL,
                    updated_at        TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS connected_accounts (
                    username   TEXT NOT NULL,
                    app        TEXT NOT NULL,
                    account_id TEXT NOT NULL DEFAULT '',
                    status     TEXT NOT NULL DEFAULT 'pending',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (username, app)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS toolkit_catalog (
                    toolkit        TEXT PRIMARY KEY,
                    label          TEXT NOT NULL DEFAULT '',
                    enabled        INTEGER NOT NULL DEFAULT 1,
                    auth_config_id TEXT NOT NULL DEFAULT '',
                    updated_at     TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS toolkit_tools (
                    toolkit      TEXT NOT NULL,
                    slug         TEXT NOT NULL,
                    description  TEXT NOT NULL DEFAULT '',
                    input_schema TEXT NOT NULL DEFAULT '{}',
                    risk         TEXT NOT NULL DEFAULT 'read',
                    updated_at   TEXT NOT NULL,
                    PRIMARY KEY (toolkit, slug)
                )
                """
            )
            # Mk18 twin of 0013: second-eyes proposals for high-impact writes.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS second_eyes (
                    id          TEXT PRIMARY KEY,
                    kind        TEXT NOT NULL,
                    payload     TEXT NOT NULL,
                    proposed_by TEXT NOT NULL,
                    proposed_at TEXT NOT NULL,
                    status      TEXT NOT NULL DEFAULT 'pending',
                    decided_by  TEXT NOT NULL DEFAULT '',
                    decided_at  TEXT NOT NULL DEFAULT ''
                )
                """
            )
            # Mk18 twin of 0012: per-user/day LLM spend accounting.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS llm_usage (
                    username   TEXT NOT NULL,
                    day        TEXT NOT NULL,
                    calls      INTEGER NOT NULL DEFAULT 0,
                    cost_cents INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (username, day)
                )
                """
            )
            conn.commit()
        finally:
            conn.close()
        self._seed_if_empty()

    def _seed_if_empty(self) -> None:
        """Migrate the 5 current users + departments on an empty table (plan_mk2 Phase 1/4).

        Mk19 Phase 4 (fail-closed seeded credentials):
        * member seeding runs only when `ENV=dev` — a fresh non-dev store starts
          with ZERO members (the operator provisions real accounts);
        * departments still seed everywhere (they carry no credentials);
        * on EVERY init in a non-dev env, any member whose stored hash still
          verifies against the repo-public `SEED_PASSWORDS` plaintext is
          deactivated (`actor="system:seed-lockdown"`, idempotent) — a store
          that ever ran dev seeding cannot be promoted to prod with those
          passwords live.
        """
        env = get_settings().env.strip().lower()
        conn = self._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
            if count == 0 and env == "dev":
                now = _now()
                for m in SEED_MEMBERS:
                    conn.execute(
                        "INSERT INTO members (username, password_hash, role, dept, scopes,"
                        " active, created_at, updated_at, display_name) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)"
                        " ON CONFLICT(username) DO NOTHING",
                        (
                            m["username"],
                            hash_password(m["password"]),
                            m["role"],
                            m["dept"],
                            json.dumps(m["scopes"]),
                            now,
                            now,
                            default_display_name(m["username"]),
                        ),
                    )
            dept_count = conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
            if dept_count == 0:
                now = _now()
                for d in SEED_DEPARTMENTS:
                    conn.execute(
                        "INSERT INTO departments (name, service, service_url, resources, role,"
                        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(name) DO NOTHING",
                        (
                            d["name"],
                            d["service"],
                            d["service_url"],
                            json.dumps(d["resources"]),
                            d["role"],
                            now,
                            now,
                        ),
                    )
            if env != "dev":
                # Mk19 Phase 4: never let repo-public seed passwords stay live
                # outside dev (idempotent; no-op on already-clean stores).
                self._lockdown_seed_passwords(conn)
            conn.commit()
        finally:
            conn.close()

    def _lockdown_seed_passwords(self, conn: sqlite3.Connection) -> None:
        """Mk19 Phase 4: deactivate members still holding a seed password.

        Runs on every init outside `ENV=dev`. A member counts as "still holding"
        the seed password when `verify_password(SEED_PASSWORDS[username],
        stored_hash)` passes — i.e. nobody has rotated it yet. Already-inactive
        members are skipped so repeated inits stay quiet (idempotent).
        """
        rows = conn.execute("SELECT username, password_hash, active FROM members").fetchall()
        now = _now()
        deactivated: list[str] = []
        for row in rows:
            username = str(row["username"])
            if not row["active"]:
                continue
            plain = SEED_PASSWORDS.get(username)
            if plain and verify_password(plain, str(row["password_hash"])):
                conn.execute(
                    "UPDATE members SET active = 0, updated_at = ? WHERE username = ?",
                    (now, username),
                )
                deactivated.append(username)
        for username in deactivated:
            self._audit("system:seed-lockdown", "deactivate", username)

    def _row_to_record(self, row: sqlite3.Row | None) -> MemberRecord | None:
        if row is None:
            return None
        try:
            display = row["display_name"]
        except Exception:  # noqa: BLE001
            display = ""
        try:
            totp = row["totp_secret"]
        except Exception:  # noqa: BLE001
            totp = ""
        return MemberRecord(
            username=row["username"],
            password_hash=row["password_hash"],
            role=row["role"],
            dept=row["dept"],
            scopes=json.loads(row["scopes"]),
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            display_name=str(display or ""),
            totp_secret=str(totp or ""),
        )

    def _audit(self, actor: str, action: str, username: str) -> None:
        self._audit_entry(actor, action, "member", username)

    def _audit_department(self, actor: str, action: str, dept: str) -> None:
        self._audit_entry(actor, action, "department", dept)

    def _audit_entry(self, actor: str, action: str, rtype: str, rid: str) -> None:
        log = self._audit_log
        if log is None:
            from app.harness.audit import open_audit_log

            log = open_audit_log(get_settings().services.audit_path)
        actor_claims = {
            "sub": actor,
            "role": "",
            "dept": "",
        }
        member = self.get(actor)
        if member is not None:
            actor_claims["role"] = member.role
            actor_claims["dept"] = member.dept
        log.log(
            decision="ALLOW",
            resource={"type": rtype, "id": rid},
            actor=actor_claims,
            run_id="members",
            reason=f"admin:members:{action}",
        )

    def _active_admin_count(self, exclude: str | None = None) -> int:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT username FROM members WHERE role = 'admin' AND active = 1"
            ).fetchall()
        finally:
            conn.close()
        admins = [r["username"] for r in rows]
        if exclude is not None:
            admins = [a for a in admins if a != exclude]
        return len(admins)

    def count_active_admins(self) -> int:
        """Mk17 public: active admin count for the central second-admin rule."""
        return self._active_admin_count()

    # ---- LLM spend accounting (Mk18 Phase H) ----

    @staticmethod
    def _today() -> str:
        return datetime.now(UTC).date().isoformat()

    def record_llm_call(self, username: str, cost_cents: int = 1) -> None:
        """Increment today's (calls, cost) for a user. No version bump — high
        frequency bookkeeping must not rebuild the enforcer."""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO llm_usage (username, day, calls, cost_cents, updated_at)"
                    " VALUES (?, ?, 1, ?, ?)"
                    " ON CONFLICT(username, day) DO UPDATE SET"
                    " calls = llm_usage.calls + 1,"
                    " cost_cents = llm_usage.cost_cents + excluded.cost_cents,"
                    " updated_at = excluded.updated_at",
                    (username, self._today(), int(cost_cents or 0), _now()),
                )
        finally:
            conn.close()

    def spend_status(self, username: str, call_cap: int, cost_cents_cap: int) -> dict[str, Any]:
        """Today's spend vs caps. Tolerates pre-0012 databases (zeros)."""
        conn = self._connect()
        try:
            try:
                row = conn.execute(
                    "SELECT calls, cost_cents FROM llm_usage WHERE username = ? AND day = ?",
                    (username, self._today()),
                ).fetchone()
            except Exception:  # noqa: BLE001
                return {"calls_today": 0, "cost_today_cents": 0, "capped": False}
        finally:
            conn.close()
        calls = int((row["calls"] if row else 0) or 0)
        cost = int((row["cost_cents"] if row else 0) or 0)
        return {
            "calls_today": calls,
            "cost_today_cents": cost,
            "capped": calls >= call_cap or cost >= cost_cents_cap,
        }

    def _bump_version(self, conn: sqlite3.Connection) -> None:
        """Increment `members_version` inside the caller's transaction so the
        PolicyEnforcer's lazy g rebuild detects the change (plan_mk2 §8)."""
        conn.execute(
            "UPDATE meta SET value = CAST(CAST(value AS INTEGER) + 1 AS TEXT) WHERE key = 'members_version'"
        )

    # ---- reads (public) ----

    @property
    def version(self) -> int:
        """Write counter bumped on every member mutation (g-rebuild trigger)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT value FROM meta WHERE key = 'members_version'"
            ).fetchone()
        finally:
            conn.close()
        return int(row["value"]) if row is not None else 0

    def is_active(self, username: str) -> bool:
        member = self.get(username)
        return member is not None and member.active

    def get(self, username: str) -> MemberRecord | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM members WHERE username = ?", (username,)).fetchone()
        finally:
            conn.close()
        return self._row_to_record(row)

    def get_direct(self, username: str) -> MemberRecord | None:
        """Mk17: uncached read for the login path (PG overrides memoised get)."""
        return self.get(username)

    def get_user(self, username: str) -> MemberRecord | None:
        """Alias kept for demos/tests migrating off the static `USERS` dict."""
        return self.get(username)

    def claims_for(self, username: str) -> dict[str, Any] | None:
        member = self.get(username)
        return member.claims() if member is not None else None

    def list(self) -> list[MemberRecord]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM members ORDER BY username").fetchall()
        finally:
            conn.close()
        return [r for r in (self._row_to_record(x) for x in rows) if r is not None]

    # ---- departments (plan_mk2 Phase 4: data, not code) ----

    def _dept_row_to_record(self, row: sqlite3.Row | None) -> DepartmentRecord | None:
        if row is None:
            return None
        try:
            mode = row["approval_mode"] or APPROVAL_MODE_HEAD_ONLY
        except (KeyError, IndexError):  # schema older than Mk15
            mode = APPROVAL_MODE_HEAD_ONLY
        return DepartmentRecord(
            name=row["name"],
            service=row["service"],
            service_url=row["service_url"],
            resources=json.loads(row["resources"]),
            role=row["role"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            approval_mode=mode,
        )

    def list_departments(self) -> list[DepartmentRecord]:
        conn = self._connect()
        try:
            rows = conn.execute("SELECT * FROM departments ORDER BY name").fetchall()
        finally:
            conn.close()
        return [r for r in (self._dept_row_to_record(x) for x in rows) if r is not None]

    def get_department(self, name: str) -> DepartmentRecord | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM departments WHERE name = ?", (name,)
            ).fetchone()
        finally:
            conn.close()
        return self._dept_row_to_record(row)

    def register_department(
        self,
        name: str,
        service: str,
        service_url: str,
        resources: list[str] | None = None,
        role: str = "",
        actor: str = "admin",
        approval_mode: str = APPROVAL_MODE_HEAD_ONLY,
    ) -> DepartmentRecord:
        """Register a new department record. Bumps `members_version` (same as any
        policy-affecting mutation) so the enforcer's next build reloads the
        policy file — picking up any p rows written for the new role."""
        self._validate_mode(approval_mode)
        if self.get_department(name) is not None:
            raise DepartmentExistsError(f"department {name!r} already exists")
        now = _now()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO departments (name, service, service_url, resources, role,"
                " created_at, updated_at, approval_mode) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    name,
                    service,
                    service_url,
                    json.dumps(resources or []),
                    role or f"{name}_agent",
                    now,
                    now,
                    approval_mode,
                ),
            )
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit_department(actor, f"register:{name}", name)
        dept = self.get_department(name)
        assert dept is not None
        return dept

    @staticmethod
    def _validate_mode(mode: str) -> None:
        if mode not in APPROVAL_MODES:
            raise DepartmentError(
                f"approval mode must be one of {', '.join(APPROVAL_MODES)} (got {mode!r})"
            )

    def set_department_mode(self, name: str, mode: str, actor: str = "admin") -> DepartmentRecord:
        """Switch a department between `head-only` and `head+admin` (Mk15).

        Only items created afterwards follow the new mode: an approval keeps the route it
        was created with. Bumps `members_version` so every laptop's cache refreshes."""
        self._validate_mode(mode)
        if self.get_department(name) is None:
            raise DepartmentError(f"department {name!r} not found")
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE departments SET approval_mode = ?, updated_at = ? WHERE name = ?",
                (mode, _now(), name),
            )
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit_department(actor, f"approval_mode:{mode}", name)
        dept = self.get_department(name)
        assert dept is not None
        return dept

    def department_mode(self, name: str) -> str:
        """Approval mode of a department; unregistered departments are `head-only`."""
        dept = self.get_department(name)
        return dept.approval_mode if dept is not None else APPROVAL_MODE_HEAD_ONLY

    def active_head_count(self, dept: str) -> int:
        """Active `{dept}_head` members owning this department (Mk12 escalation check)."""
        head_role = f"{dept}_head"
        return sum(1 for m in self.list() if m.active and m.role == head_role and m.dept == dept)

    # ---- approve-once allowlist (Mk15, todo #11) ----
    # Key = (member, action, args_hash): one member, one action, one exact set of arguments.
    # Never cached (also not in PgMemberStore): a revoke must apply to the very next request.

    @staticmethod
    def _allow_row(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "member": row["member"],
            "action": row["action"],
            "args_hash": row["args_hash"],
            "args": json.loads(row["args"]),
            "dept": row["dept"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def allowlist_add(
        self,
        member: str,
        action: str,
        args_hash: str,
        args: dict[str, Any],
        dept: str,
        actor: str,
    ) -> dict[str, Any]:
        """Always allow this exact action for this member. Idempotent."""
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO approval_allowlist (member, action, args_hash, args, dept,"
                    " created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT(member, action, args_hash) DO NOTHING",
                    (member, action, args_hash, json.dumps(args, sort_keys=True), dept, actor, _now()),
                )
        finally:
            conn.close()
        self._audit_entry(actor, "allowlist:create", "allowlist", f"{member}|{action}|{args_hash}")
        rule = self.allowlist_get(member, action, args_hash)
        assert rule is not None
        return rule

    def allowlist_get(self, member: str, action: str, args_hash: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM approval_allowlist WHERE member = ? AND action = ? AND args_hash = ?",
                (member, action, args_hash),
            ).fetchone()
        finally:
            conn.close()
        return self._allow_row(row) if row is not None else None

    def allowlist_match(self, member: str, action: str, args_hash: str) -> bool:
        """Exact match only: same member, same action, same canonical arguments."""
        return self.allowlist_get(member, action, args_hash) is not None

    def allowlist_list(self, dept: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if dept is None:
                rows = conn.execute(
                    "SELECT * FROM approval_allowlist ORDER BY dept, member, action, created_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approval_allowlist WHERE dept = ?"
                    " ORDER BY member, action, created_at",
                    (dept,),
                ).fetchall()
        finally:
            conn.close()
        return [self._allow_row(r) for r in rows]

    def allowlist_revoke(self, member: str, action: str, args_hash: str, actor: str) -> bool:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "DELETE FROM approval_allowlist WHERE member = ? AND action = ? AND args_hash = ?",
                    (member, action, args_hash),
                )
        finally:
            conn.close()
        if cur.rowcount == 0:
            return False
        self._audit_entry(actor, "allowlist:revoke", "allowlist", f"{member}|{action}|{args_hash}")
        return True

    # ---- second eyes (Mk18 Phase E) ----

    #: Proposal kinds gated when ≥2 active admins exist (single admin = direct + audit).
    SECOND_EYES_KINDS = (
        "policy_rows",
        "skill_save",
        "skill_delete",
        "llm_key_write",
        "llm_key_delete",
        "platform_key",
        "composio_key",
    )

    def second_eyes_propose(self, kind: str, payload: dict[str, Any], proposer: str) -> dict[str, Any]:
        """Stage a high-impact write for a second admin. Secrets in `payload`
        must already be Fernet-encrypted by the caller (never plaintext)."""
        if kind not in self.SECOND_EYES_KINDS:
            raise MemberError(f"unknown second-eyes kind {kind!r}")
        pid, now = str(uuid.uuid4()), _now()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO second_eyes (id, kind, payload, proposed_by, proposed_at, status)"
                    " VALUES (?, ?, ?, ?, ?, 'pending')",
                    (pid, kind, json.dumps(payload, sort_keys=True, default=str), proposer, now),
                )
        finally:
            conn.close()
        self._audit_entry(proposer, "second-eyes:proposed", "second-eyes", f"{kind}|{pid}")
        proposal = self.second_eyes_get(pid)
        assert proposal is not None
        return proposal

    def second_eyes_get(self, pid: str) -> dict[str, Any] | None:
        conn = self._connect()
        try:
            row = conn.execute("SELECT * FROM second_eyes WHERE id = ?", (pid,)).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "payload": json.loads(row["payload"]),
            "proposed_by": row["proposed_by"],
            "proposed_at": row["proposed_at"],
            "status": row["status"],
            "decided_by": row["decided_by"],
            "decided_at": row["decided_at"],
        }

    def second_eyes_pending(self) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM second_eyes WHERE status = 'pending' ORDER BY proposed_at"
            ).fetchall()
        finally:
            conn.close()
        return [p for p in (self.second_eyes_get(r["id"]) for r in rows) if p is not None]

    def second_eyes_decide(self, pid: str, approver: str, approve: bool) -> dict[str, Any]:
        """Confirm (apply by caller) or reject a proposal. The proposer can
        never decide their own item — a different admin must."""
        proposal = self.second_eyes_get(pid)
        if proposal is None:
            raise MemberError(f"proposal {pid!r} not found")
        if proposal["status"] != "pending":
            raise MemberError(f"proposal {pid!r} already {proposal['status']}")
        if proposal["proposed_by"] == approver:
            raise MemberError("a second admin must decide (proposer cannot self-confirm)")
        status = "approved" if approve else "rejected"
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE second_eyes SET status = ?, decided_by = ?, decided_at = ?"
                    " WHERE id = ? AND status = 'pending'",
                    (status, approver, _now(), pid),
                )
        finally:
            conn.close()
        if cur.rowcount == 0:
            raise MemberError(f"proposal {pid!r} already decided")
        self._audit_entry(approver, f"second-eyes:{status}", "second-eyes", pid)
        updated = self.second_eyes_get(pid)
        assert updated is not None
        return updated

    def _throttle_state(self, username: str) -> tuple[int, str]:
        """(attempts, locked_until) — tolerates pre-0011 databases without the columns."""
        conn = self._connect()
        try:
            try:
                row = conn.execute(
                    "SELECT login_attempts, locked_until FROM members WHERE username = ?",
                    (username,),
                ).fetchone()
            except Exception:  # noqa: BLE001
                return 0, ""
        finally:
            conn.close()
        if row is None:
            return 0, ""
        try:
            return int(row["login_attempts"] or 0), str(row["locked_until"] or "")
        except Exception:  # noqa: BLE001
            return 0, ""

    def _record_failed_login(self, username: str) -> None:
        # NOTE: no _bump_version — login noise must not rebuild the enforcer.
        conn = self._connect()
        try:
            try:
                attempts, _ = self._throttle_state(username)
                attempts += 1
                if attempts >= _LOCK_AFTER_ATTEMPTS:
                    locked = (datetime.now(UTC) + timedelta(minutes=_LOCK_MINUTES)).isoformat()
                    conn.execute(
                        "UPDATE members SET login_attempts = ?, locked_until = ? WHERE username = ?",
                        (attempts, locked, username),
                    )
                else:
                    conn.execute(
                        "UPDATE members SET login_attempts = ? WHERE username = ?",
                        (attempts, username),
                    )
                conn.commit()
            except Exception:  # noqa: BLE001, S110
                pass
        finally:
            conn.close()

    def _clear_failed_login(self, username: str) -> None:
        conn = self._connect()
        try:
            try:
                conn.execute(
                    "UPDATE members SET login_attempts = 0, locked_until = '' WHERE username = ?",
                    (username,),
                )
                conn.commit()
            except Exception:  # noqa: BLE001, S110
                pass
        finally:
            conn.close()

    def verify(self, username: str, password: str) -> bool:
        # Direct read (never the memo): resets/deactivations apply at once.
        member = self.get_direct(username)
        if member is None:
            return False
        _, locked_until = self._throttle_state(username)
        if is_login_locked(locked_until):
            return False
        if not verify_password(password, member.password_hash):
            self._record_failed_login(username)
            return False
        self._clear_failed_login(username)
        return member.active

    def set_totp_secret(self, username: str, secret_b32: str, actor: str = "admin") -> MemberRecord:
        """Mk18 Phase C: opt-in TOTP second factor (blank clears). Audit-logged."""
        if self.get(username) is None:
            raise MemberError(f"member {username!r} not found")
        secret = (secret_b32 or "").strip().upper()
        if secret:
            try:
                base64.b32decode(secret)
            except Exception as exc:
                raise MemberError("totp secret must be base32") from exc
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE members SET totp_secret = ?, updated_at = ? WHERE username = ?",
                (secret, _now(), username),
            )
            conn.commit()
        finally:
            conn.close()
        self._audit(actor, "totp_set" if secret else "totp_cleared", username)
        member = self.get(username)
        assert member is not None
        return member

    # ---- llm keys (BYOK Plan Mk3) ----

    def list_llm_keys(self, username: str) -> list[LLMKeyRecord]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT id, username, provider, model, base_url, active, created_at, updated_at "
                "FROM member_llm_keys WHERE username = ? ORDER BY provider, model",
                (username,),
            ).fetchall()
        finally:
            conn.close()
        return [
            LLMKeyRecord(
                id=r["id"],
                username=r["username"],
                provider=r["provider"],
                model=r["model"],
                base_url=r["base_url"] or "",
                active=bool(r["active"]),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )
            for r in rows
        ]

    def set_llm_key(
        self,
        username: str,
        provider: str,
        model: str,
        api_key_plaintext: str,
        active: bool = True,
        actor: str | None = None,
        base_url: str = "",
    ) -> LLMKeyRecord:
        if self.get(username) is None:
            raise MemberError(f"member {username!r} not found")
        from app.auth.crypto import encrypt_secret

        encrypted = encrypt_secret(api_key_plaintext)
        now = _now()
        conn = self._connect()
        try:
            with conn:
                if active:
                    conn.execute(
                        "UPDATE member_llm_keys SET active = 0, updated_at = ? WHERE username = ?",
                        (now, username),
                    )
                existing = conn.execute(
                    "SELECT id FROM member_llm_keys WHERE username = ? AND provider = ? AND model = ?",
                    (username, provider, model),
                ).fetchone()
                if existing:
                    key_id = existing["id"]
                    conn.execute(
                        "UPDATE member_llm_keys SET api_key_encrypted = ?, base_url = ?, active = ?,"
                        " updated_at = ? WHERE id = ?",
                        (encrypted, base_url, 1 if active else 0, now, key_id),
                    )
                else:
                    key_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO member_llm_keys (id, username, provider, model, api_key_encrypted,"
                        " base_url, active, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (key_id, username, provider, model, encrypted, base_url, 1 if active else 0, now, now),
                    )
        finally:
            conn.close()
        self._audit_key(actor or username, f"set_key:{provider}:{model}", username)
        keys = self.list_llm_keys(username)
        for k in keys:
            if k.id == key_id:
                return k
        raise MemberError("Failed to store LLM key")

    def update_llm_key(
        self,
        username: str,
        key_id: str,
        model: str | None = None,
        base_url: str | None = None,
        active: bool | None = None,
        actor: str | None = None,
    ) -> LLMKeyRecord:
        if self.get(username) is None:
            raise MemberError(f"member {username!r} not found")
        now = _now()
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT id, provider, model, base_url, active FROM member_llm_keys WHERE id = ? AND username = ?",
                    (key_id, username),
                ).fetchone()
                if not row:
                    raise MemberError("LLM key not found")

                new_model = model.strip() if model is not None else row["model"]
                new_base_url = base_url.strip() if base_url is not None else (row["base_url"] or "")
                new_active = active if active is not None else bool(row["active"])

                if not new_model:
                    raise MemberError("model cannot be empty")

                if new_model != row["model"]:
                    conflict = conn.execute(
                        "SELECT id FROM member_llm_keys WHERE username = ? AND provider = ? AND model = ? AND id != ?",
                        (username, row["provider"], new_model, key_id),
                    ).fetchone()
                    if conflict:
                        raise MemberError(
                            f"LLM key configuration for provider {row['provider']!r} and model {new_model!r} already exists"
                        )

                if new_active and not row["active"]:
                    conn.execute(
                        "UPDATE member_llm_keys SET active = 0, updated_at = ? WHERE username = ?",
                        (now, username),
                    )

                conn.execute(
                    "UPDATE member_llm_keys SET model = ?, base_url = ?, active = ?, updated_at = ? WHERE id = ?",
                    (new_model, new_base_url, 1 if new_active else 0, now, key_id),
                )
        except self.INTEGRITY_ERRORS as exc:
            raise MemberError(f"Failed to update LLM key: {exc}") from exc
        finally:
            conn.close()

        self._audit_key(actor or username, f"update_key:{key_id}:{new_model}", username)
        keys = self.list_llm_keys(username)
        for k in keys:
            if k.id == key_id:
                return k
        raise MemberError("Failed to retrieve updated LLM key")

    def activate_llm_key(self, username: str, key_id: str, actor: str | None = None) -> None:
        now = _now()
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT id FROM member_llm_keys WHERE id = ? AND username = ?",
                    (key_id, username),
                ).fetchone()
                if not row:
                    raise MemberError("LLM key not found")
                conn.execute(
                    "UPDATE member_llm_keys SET active = 0, updated_at = ? WHERE username = ?",
                    (now, username),
                )
                conn.execute(
                    "UPDATE member_llm_keys SET active = 1, updated_at = ? WHERE id = ?",
                    (now, key_id),
                )
        finally:
            conn.close()
        self._audit_key(actor or username, f"activate_key:{key_id}", username)

    def delete_llm_key(self, username: str, key_id: str, actor: str | None = None) -> None:
        conn = self._connect()
        try:
            with conn:
                row = conn.execute(
                    "SELECT id FROM member_llm_keys WHERE id = ? AND username = ?",
                    (key_id, username),
                ).fetchone()
                if not row:
                    raise MemberError("LLM key not found")
                conn.execute(
                    "DELETE FROM member_llm_keys WHERE id = ? AND username = ?",
                    (key_id, username),
                )
        finally:
            conn.close()
        self._audit_key(actor or username, f"delete_key:{key_id}", username)

    def get_active_llm_key(self, username: str) -> tuple[str, str, str, str] | None:
        """(provider, model, decrypted_api_key, base_url) for the user's active key."""
        return self._llm_key_row(
            "SELECT provider, model, api_key_encrypted, base_url FROM member_llm_keys"
            " WHERE username = ? AND active = 1",
            (username,),
        )

    def get_active_llm_key_meta(self, username: str) -> tuple[str, str] | None:
        """(provider, model) for the user's active key — no decryption.

        Status surfaces (e.g. `/api/users`) must never decrypt: one stale token
        (rotated `LLM__FERNET_KEY`) would otherwise fail the whole listing.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT provider, model FROM member_llm_keys WHERE username = ? AND active = 1",
                (username,),
            ).fetchone()
        finally:
            conn.close()
        return (row["provider"], row["model"]) if row else None

    def get_llm_key_by_id(self, username: str, key_id: str) -> tuple[str, str, str, str] | None:
        """(provider, model, decrypted_api_key, base_url) for one of the user's keys."""
        return self._llm_key_row(
            "SELECT provider, model, api_key_encrypted, base_url FROM member_llm_keys"
            " WHERE id = ? AND username = ?",
            (key_id, username),
        )

    def _fetch_llm_key(self, query: str, params: tuple[Any, ...]) -> tuple[str, str, str, str] | None:
        """(provider, model, ENCRYPTED api key, base_url) — never decrypts, so it is safe to cache."""
        conn = self._connect()
        try:
            row = conn.execute(query, params).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return row["provider"], row["model"], row["api_key_encrypted"], row["base_url"] or ""

    def _llm_key_row(self, query: str, params: tuple[Any, ...]) -> tuple[str, str, str, str] | None:
        raw = self._fetch_llm_key(query, params)
        if raw is None:
            return None
        from app.auth.crypto import decrypt_secret

        provider, model, encrypted, base_url = raw
        return provider, model, decrypt_secret(encrypted), base_url

    def _audit_key(self, actor: str, action: str, username: str) -> None:
        self._audit_entry(actor, action, "llm_key", username)

    # ---- platform llm (Mk16 follow-up: DB-backed shared key) ----

    PLATFORM_LLM_ID = "openrouter"

    def get_platform_llm_meta(self) -> tuple[str, str, str] | None:
        """(model, updated_by, updated_at) for the shared row — no decryption.

        Status surfaces must never decrypt: one stale token (rotated
        `LLM__FERNET_KEY`) would otherwise fail the whole listing.
        """
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT model, updated_by, updated_at FROM platform_llm WHERE id = ?",
                (self.PLATFORM_LLM_ID,),
            ).fetchone()
        finally:
            conn.close()
        return (row["model"], row["updated_by"], row["updated_at"]) if row else None

    def _fetch_platform_llm(self) -> tuple[str, str, str, str] | None:
        """(model, ENCRYPTED key, updated_by, updated_at) — never decrypts, safe to cache."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT model, api_key_encrypted, updated_by, updated_at FROM platform_llm"
                " WHERE id = ?",
                (self.PLATFORM_LLM_ID,),
            ).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        return row["model"], row["api_key_encrypted"], row["updated_by"], row["updated_at"]

    def get_platform_llm(self) -> tuple[str, str, str, str] | None:
        """(model, decrypted key, updated_by, updated_at) for the shared row.

        Raises on stale Fernet (`decrypt_secret` HTTPException) — callers treat
        that as decrypt-failed and fall back to the env key (see `app/llm/system.py`).
        """
        raw = self._fetch_platform_llm()
        if raw is None:
            return None
        from app.auth.crypto import decrypt_secret

        model, encrypted, by, at = raw
        return model, decrypt_secret(encrypted), by, at

    def set_platform_llm(
        self, *, model: str, api_key_plaintext: str | None, actor: str = "admin"
    ) -> tuple[str, str, str]:
        """Upsert the shared row; returns (model, updated_by, updated_at).

        `api_key_plaintext=None` keeps the existing key (model-only change).
        Raises `ValueError` if the model is not a `:free` id (billing guard),
        `MemberError` if there is no stored key and none is provided.
        Bumps `members_version` so every laptop's cache refreshes.
        """
        model = (model or "").strip()
        if not model or ":free" not in model:
            raise ValueError(
                f"platform model {model!r} must be a free-tier id (contain ':free')"
            )
        from app.auth.crypto import encrypt_secret

        conn = self._connect()
        try:
            if api_key_plaintext and api_key_plaintext.strip():
                encrypted = encrypt_secret(api_key_plaintext.strip())
            else:
                row = conn.execute(
                    "SELECT api_key_encrypted FROM platform_llm WHERE id = ?",
                    (self.PLATFORM_LLM_ID,),
                ).fetchone()
                if row is None:
                    raise MemberError("no platform key stored and none provided")
                encrypted = row["api_key_encrypted"]
            now = _now()
            conn.execute(
                "INSERT INTO platform_llm (id, model, api_key_encrypted, updated_by, updated_at)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET model = excluded.model,"
                " api_key_encrypted = excluded.api_key_encrypted,"
                " updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (self.PLATFORM_LLM_ID, model, encrypted, actor, now),
            )
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit_entry(actor, "platform_llm_set", "platform_llm", self.PLATFORM_LLM_ID)
        return model, actor, now

    def clear_platform_llm(self, actor: str = "admin") -> bool:
        """Delete the shared row (reverts to the env key). Returns True if a row existed."""
        conn = self._connect()
        try:
            cur = conn.execute("DELETE FROM platform_llm WHERE id = ?", (self.PLATFORM_LLM_ID,))
            removed = cur.rowcount > 0
            if removed:
                self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        if removed:
            self._audit_entry(actor, "platform_llm_clear", "platform_llm", self.PLATFORM_LLM_ID)
        return removed

    # ---- composio keys + connected accounts (Mk9 external tools) ----

    def set_composio_key(self, username: str, api_key_plaintext: str, actor: str | None = None) -> None:
        if self.get(username) is None:
            raise MemberError(f"member {username!r} not found")
        from app.auth.crypto import encrypt_secret

        encrypted = encrypt_secret(api_key_plaintext)
        now = _now()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO member_composio_keys (username, api_key_encrypted, created_at, updated_at)"
                    " VALUES (?, ?, ?, ?)"
                    " ON CONFLICT(username) DO UPDATE SET api_key_encrypted=excluded.api_key_encrypted,"
                    " updated_at=excluded.updated_at",
                    (username, encrypted, now, now),
                )
        finally:
            conn.close()
        self._audit_entry(actor or username, "set_composio_key", "composio_key", username)

    def get_composio_key(self, username: str) -> str | None:
        """Raw encrypted token (never decrypted here; decrypt at use time)."""
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT api_key_encrypted FROM member_composio_keys WHERE username = ?",
                (username,),
            ).fetchone()
        finally:
            conn.close()
        return row["api_key_encrypted"] if row else None

    def delete_composio_key(self, username: str, actor: str | None = None) -> None:
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "DELETE FROM member_composio_keys WHERE username = ?",
                    (username,),
                )
        finally:
            conn.close()
        self._audit_entry(actor or username, "delete_composio_key", "composio_key", username)

    def set_connected_account(
        self, username: str, app: str, account_id: str = "", status: str = "pending"
    ) -> dict[str, Any]:
        now = _now()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO connected_accounts (username, app, account_id, status, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(username, app) DO UPDATE SET account_id=excluded.account_id,"
                    " status=excluded.status, updated_at=excluded.updated_at",
                    (username, app, account_id, status, now),
                )
        finally:
            conn.close()
        return {
            "username": username,
            "app": app,
            "account_id": account_id,
            "status": status,
            "updated_at": now,
        }

    def list_connected_accounts(self, username: str) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT app, account_id, status, updated_at FROM connected_accounts"
                " WHERE username = ? ORDER BY app",
                (username,),
            ).fetchall()
        finally:
            conn.close()
        return [
            {
                "app": r["app"],
                "account_id": r["account_id"],
                "status": r["status"],
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def is_app_connected(self, username: str, app: str) -> bool:
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT status FROM connected_accounts WHERE username = ? AND app = ?",
                (username, app),
            ).fetchone()
        finally:
            conn.close()
        return row is not None and row["status"] == "active"

    # ---- toolkit catalog (Mk9 simplified: admin-curated, auto auth-config) ----

    _CATALOG_SEEDS = ("gmail", "github")

    def _ensure_catalog_seeds(self) -> None:
        if getattr(self, "_catalog_seeded", False):
            return  # once per process: on the shared DB each call would be a network write
        conn = self._connect()
        try:
            with conn:
                for tk in self._CATALOG_SEEDS:
                    conn.execute(
                        "INSERT INTO toolkit_catalog (toolkit, label, enabled, auth_config_id, updated_at)"
                        " VALUES (?, ?, 1, '', ?) ON CONFLICT(toolkit) DO NOTHING",
                        (tk, tk, _now()),
                    )
        finally:
            conn.close()
        self._catalog_seeded = True

    def list_toolkits(self, enabled_only: bool = False) -> list[dict[str, Any]]:
        self._ensure_catalog_seeds()
        conn = self._connect()
        try:
            q = "SELECT toolkit, label, enabled, auth_config_id, updated_at FROM toolkit_catalog"
            if enabled_only:
                q += " WHERE enabled = 1"
            q += " ORDER BY toolkit"
            rows = conn.execute(q).fetchall()
        finally:
            conn.close()
        return [
            {
                "toolkit": r["toolkit"],
                "label": r["label"] or r["toolkit"],
                "enabled": bool(r["enabled"]),
                "auth_config_id": r["auth_config_id"] or "",
                "updated_at": r["updated_at"],
            }
            for r in rows
        ]

    def upsert_toolkit(
        self, toolkit: str, label: str = "", enabled: bool = True,
        auth_config_id: str = "", actor: str = "admin",
    ) -> dict[str, Any]:
        toolkit = (toolkit or "").strip().lower()
        if not toolkit:
            raise MemberError("toolkit is required")
        now = _now()
        conn = self._connect()
        try:
            with conn:
                conn.execute(
                    "INSERT INTO toolkit_catalog (toolkit, label, enabled, auth_config_id, updated_at)"
                    " VALUES (?, ?, ?, ?, ?)"
                    " ON CONFLICT(toolkit) DO UPDATE SET label=excluded.label,"
                    " enabled=excluded.enabled, auth_config_id=excluded.auth_config_id,"
                    " updated_at=excluded.updated_at",
                    (toolkit, label or toolkit, 1 if enabled else 0, auth_config_id, now),
                )
        finally:
            conn.close()
        self._audit_entry(actor, f"toolkit:{toolkit}", "toolkit", toolkit)
        return {
            "toolkit": toolkit, "label": label or toolkit, "enabled": enabled,
            "auth_config_id": auth_config_id, "updated_at": now,
        }

    def set_toolkit_enabled(self, toolkit: str, enabled: bool, actor: str = "admin") -> None:
        conn = self._connect()
        try:
            with conn:
                cur = conn.execute(
                    "UPDATE toolkit_catalog SET enabled = ?, updated_at = ? WHERE toolkit = ?",
                    (1 if enabled else 0, _now(), toolkit),
                )
                if cur.rowcount == 0:
                    raise MemberError(f"toolkit {toolkit!r} not in catalog")
        finally:
            conn.close()
        self._audit_entry(actor, f"toolkit:{'enable' if enabled else 'disable'}:{toolkit}", "toolkit", toolkit)

    def cache_toolkit_tools(self, toolkit: str, tools: list[dict[str, Any]]) -> int:
        from app.integrations.adapter import risk_for_slug

        now = _now()
        conn = self._connect()
        try:
            with conn:
                for t in tools:
                    conn.execute(
                        "INSERT INTO toolkit_tools (toolkit, slug, description, input_schema, risk, updated_at)"
                        " VALUES (?, ?, ?, ?, ?, ?)"
                        " ON CONFLICT(toolkit, slug) DO UPDATE SET description=excluded.description,"
                        " input_schema=excluded.input_schema, risk=excluded.risk,"
                        " updated_at=excluded.updated_at",
                        (
                            toolkit, t.get("slug", ""), str(t.get("description", "")),
                            json.dumps(t.get("input_schema") or {"type": "object"}),
                            risk_for_slug(str(t.get("slug", ""))),
                            now,
                        ),
                    )
        finally:
            conn.close()
        return len(tools)

    def list_cached_tools(self, toolkit: str | None = None) -> list[dict[str, Any]]:
        conn = self._connect()
        try:
            if toolkit:
                rows = conn.execute(
                    "SELECT toolkit, slug, description, input_schema, risk FROM toolkit_tools"
                    " WHERE toolkit = ? ORDER BY slug",
                    (toolkit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT toolkit, slug, description, input_schema, risk FROM toolkit_tools"
                    " ORDER BY toolkit, slug"
                ).fetchall()
        finally:
            conn.close()
        out = []
        for r in rows:
            try:
                schema = json.loads(r["input_schema"])
            except ValueError:
                schema = {"type": "object"}
            out.append(
                {
                    "toolkit": r["toolkit"], "slug": r["slug"],
                    "description": r["description"], "input_schema": schema,
                    "risk": r["risk"],
                }
            )
        return out

    # ---- writes (admin-only, every change audit-logged) ----

    @staticmethod
    def _validate_head_role(role: str, dept: str) -> None:
        """Mk12: a `*_head` role must own its own dept (`marketing_head` ⟺ `marketing`)."""
        if role.endswith("_head") and role != f"{dept}_head":
            raise MemberError(f"head role {role!r} must match its department {dept!r}")

    @staticmethod
    def _validate_email(value: str) -> str:
        """Mk17: identity is strictly email (Composio + org requirement)."""
        v = (value or "").strip()
        if not is_valid_email(v):
            raise MemberError(f"email {value!r} is not a valid email address")
        return v

    @staticmethod
    def _validate_role_dept(role: str, dept: str) -> None:
        """Mk17 strict: role must belong to dept; admin must be dept admin.

        - `admin` ⟺ `admin` only.
        - `{dept}_head` / `{dept}_agent` ⟺ that dept only.
        Existing mismatched rows are grandfathered (flagged, not failed);
        all new creates/updates are rejected.
        """
        role = (role or "").strip()
        dept = (dept or "").strip()
        if not role or not dept:
            raise MemberError("role and dept are required")
        if role == "admin":
            if dept != "admin":
                raise MemberError(f"admin role must belong to department 'admin', got {dept!r}")
            return
        if role.endswith(("_head", "_agent")):
            expected = role[: -len("_head")] if role.endswith("_head") else role[: -len("_agent")]
            if expected != dept:
                if role.endswith("_head"):
                    raise MemberError(
                        f"head role {role!r} must match its department {dept!r}"
                    )
                raise MemberError(f"role {role!r} must match its department {dept!r}")
            return
        raise MemberError(f"unknown role {role!r}")

    def list_role_dept_mismatches(self) -> list[dict[str, str]]:
        """Mk17 grandfather flag: existing rows violating the strict map.

        Returns [{username, role, dept}] for admin review; does not mutate.
        """
        out: list[dict[str, str]] = []
        for m in self.list():
            try:
                self._validate_role_dept(m.role, m.dept)
            except MemberError:
                out.append({"username": m.username, "role": m.role, "dept": m.dept})
        return out

    def create(
        self,
        username: str,
        password: str,
        role: str,
        dept: str,
        scopes: list[str] | None = None,
        actor: str = "admin",
        display_name: str | None = None,
        email: str | None = None,
    ) -> MemberRecord:
        # Mk17: `username` param is the email identity; `email=` alias accepted.
        identity = self._validate_email(email if email is not None else username)
        self._validate_role_dept(role, dept)
        disp = validate_display_name(display_name) or default_display_name(identity)
        if self.get(identity) is not None:
            raise MemberExistsError(f"member {identity!r} already exists")
        now = _now()
        conn = self._connect()
        try:
            try:
                conn.execute(
                    "INSERT INTO members (username, password_hash, role, dept, scopes, active,"
                    " created_at, updated_at, display_name) VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)",
                    (
                        identity,
                        hash_password(password),
                        role,
                        dept,
                        json.dumps(scopes or []),
                        now,
                        now,
                        disp,
                    ),
                )
            except self.INTEGRITY_ERRORS as exc:  # lost a race with another creator
                raise MemberExistsError(f"member {identity!r} already exists") from exc
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit(actor, "create", identity)
        member = self.get(identity)
        assert member is not None
        return member

    def update(
        self,
        username: str,
        *,
        role: str,
        dept: str,
        scopes: list[str] | None = None,
        actor: str,
        display_name: str | None = None,
    ) -> MemberRecord:
        if self.get(username) is None:
            raise MemberError(f"member {username!r} not found")
        self._validate_email(username)
        self._validate_role_dept(role, dept)
        conn = self._connect()
        try:
            if display_name is None:
                conn.execute(
                    "UPDATE members SET role = ?, dept = ?, scopes = ?, updated_at = ? WHERE username = ?",
                    (role, dept, json.dumps(scopes or []), _now(), username),
                )
            else:
                disp = validate_display_name(display_name)
                conn.execute(
                    "UPDATE members SET role = ?, dept = ?, scopes = ?, display_name = ?, updated_at = ? WHERE username = ?",
                    (role, dept, json.dumps(scopes or []), disp, _now(), username),
                )
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit(actor, "update", username)
        member = self.get(username)
        assert member is not None
        return member

    def set_active(self, username: str, active: bool, actor: str = "admin") -> MemberRecord:
        member = self.get(username)
        if member is None:
            raise MemberError(f"member {username!r} not found")
        if not active and member.role == "admin":
            if actor == username:
                raise LockoutGuardError("an admin cannot deactivate themselves")
            if self._active_admin_count(exclude=username) == 0:
                raise LockoutGuardError("cannot deactivate the last active admin")
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE members SET active = ?, updated_at = ? WHERE username = ?",
                (1 if active else 0, _now(), username),
            )
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit(actor, "activated" if active else "deactivated", username)
        member = self.get(username)
        assert member is not None
        return member

    def reset_password(self, username: str, new_password: str, actor: str = "admin") -> MemberRecord:
        if self.get(username) is None:
            raise MemberError(f"member {username!r} not found")
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE members SET password_hash = ?, updated_at = ? WHERE username = ?",
                (hash_password(new_password), _now(), username),
            )
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit(actor, "password_reset", username)
        member = self.get(username)
        assert member is not None
        return member

    def __len__(self) -> int:
        return len(self.list())


if __name__ == "__main__":  # manual migration / demo: uv run python -m app.auth.store
    store = MemberStore()
    print(f"members db: {store.path}")
    for m in store.list():
        print(f"  {m.username:<22} role={m.role:<16} dept={m.dept:<10} active={m.active}")
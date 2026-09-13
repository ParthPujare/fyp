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
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings


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

    def to_public(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "service": self.service,
            "service_url": self.service_url,
            "resources": list(self.resources),
            "role": self.role,
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
    """SQLite-backed identity store (single source of truth, plan_mk2 §3.2)."""

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
            conn.commit()
        finally:
            conn.close()
        self._seed_if_empty()

    def _seed_if_empty(self) -> None:
        """Migrate the 5 current users + departments on an empty table (plan_mk2 Phase 1/4)."""
        conn = self._connect()
        try:
            count = conn.execute("SELECT COUNT(*) FROM members").fetchone()[0]
            if count == 0:
                now = _now()
                for m in SEED_MEMBERS:
                    conn.execute(
                        "INSERT INTO members (username, password_hash, role, dept, scopes,"
                        " active, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                        (
                            m["username"],
                            hash_password(m["password"]),
                            m["role"],
                            m["dept"],
                            json.dumps(m["scopes"]),
                            now,
                            now,
                        ),
                    )
            dept_count = conn.execute("SELECT COUNT(*) FROM departments").fetchone()[0]
            if dept_count == 0:
                now = _now()
                for d in SEED_DEPARTMENTS:
                    conn.execute(
                        "INSERT INTO departments (name, service, service_url, resources, role,"
                        " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
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
            conn.commit()
        finally:
            conn.close()

    def _row_to_record(self, row: sqlite3.Row | None) -> MemberRecord | None:
        if row is None:
            return None
        return MemberRecord(
            username=row["username"],
            password_hash=row["password_hash"],
            role=row["role"],
            dept=row["dept"],
            scopes=json.loads(row["scopes"]),
            active=bool(row["active"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _audit(self, actor: str, action: str, username: str) -> None:
        self._audit_entry(actor, action, "member", username)

    def _audit_department(self, actor: str, action: str, dept: str) -> None:
        self._audit_entry(actor, action, "department", dept)

    def _audit_entry(self, actor: str, action: str, rtype: str, rid: str) -> None:
        log = self._audit_log
        if log is None:
            from app.harness.audit import AuditLog

            log = AuditLog(get_settings().services.audit_path)
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

    def _bump_version(self, conn: sqlite3.Connection) -> None:
        """Increment `members_version` inside the caller's transaction so the
        PolicyEnforcer's lazy g rebuild detects the change (plan_mk2 §8)."""
        conn.execute(
            "UPDATE meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'members_version'"
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
        return DepartmentRecord(
            name=row["name"],
            service=row["service"],
            service_url=row["service_url"],
            resources=json.loads(row["resources"]),
            role=row["role"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
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
    ) -> DepartmentRecord:
        """Register a new department record. Bumps `members_version` (same as any
        policy-affecting mutation) so the enforcer's next build reloads the
        policy file — picking up any p rows written for the new role."""
        if self.get_department(name) is not None:
            raise DepartmentExistsError(f"department {name!r} already exists")
        now = _now()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO departments (name, service, service_url, resources, role,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (name, service, service_url, json.dumps(resources or []), role or f"{name}_agent", now, now),
            )
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit_department(actor, f"register:{name}", name)
        dept = self.get_department(name)
        assert dept is not None
        return dept

    def verify(self, username: str, password: str) -> bool:
        member = self.get(username)
        if member is None or not verify_password(password, member.password_hash):
            return False
        return member.active

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
        except sqlite3.IntegrityError as exc:
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

    def _llm_key_row(self, query: str, params: tuple[Any, ...]) -> tuple[str, str, str, str] | None:
        conn = self._connect()
        try:
            row = conn.execute(query, params).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        from app.auth.crypto import decrypt_secret

        decrypted = decrypt_secret(row["api_key_encrypted"])
        return row["provider"], row["model"], decrypted, row["base_url"] or ""

    def _audit_key(self, actor: str, action: str, username: str) -> None:
        self._audit_entry(actor, action, "llm_key", username)

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

    def create(
        self,
        username: str,
        password: str,
        role: str,
        dept: str,
        scopes: list[str] | None = None,
        actor: str = "admin",
    ) -> MemberRecord:
        self._validate_head_role(role, dept)
        if self.get(username) is not None:
            raise MemberExistsError(f"member {username!r} already exists")
        now = _now()
        conn = self._connect()
        try:
            conn.execute(
                "INSERT INTO members (username, password_hash, role, dept, scopes, active,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                (
                    username,
                    hash_password(password),
                    role,
                    dept,
                    json.dumps(scopes or []),
                    now,
                    now,
                ),
            )
            self._bump_version(conn)
            conn.commit()
        finally:
            conn.close()
        self._audit(actor, "create", username)
        member = self.get(username)
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
    ) -> MemberRecord:
        if self.get(username) is None:
            raise MemberError(f"member {username!r} not found")
        self._validate_head_role(role, dept)
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE members SET role = ?, dept = ?, scopes = ?, updated_at = ? WHERE username = ?",
                (role, dept, json.dumps(scopes or []), _now(), username),
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
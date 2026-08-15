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
            conn.commit()
        finally:
            conn.close()
        self._seed_if_empty()

    def _seed_if_empty(self) -> None:
        """Migrate the 5 current users on an empty table (plan_mk2 Phase 1)."""
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
        log = self._audit_log
        if log is None:
            from app.harness.audit import AuditLog

            log = AuditLog(get_settings().services.audit_path)
        actor_claims = {
            "sub": actor,
            "role": "",
            "dept": "",
        }
        member = self.get(username)
        if member is not None:
            actor_claims["role"] = member.role
            actor_claims["dept"] = member.dept
        log.log(
            decision="ALLOW",
            resource={"type": "member", "id": username},
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

    # ---- reads (public) ----

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

    def verify(self, username: str, password: str) -> bool:
        member = self.get(username)
        if member is None or not verify_password(password, member.password_hash):
            return False
        return member.active

    # ---- writes (admin-only, every change audit-logged) ----

    def create(
        self,
        username: str,
        password: str,
        role: str,
        dept: str,
        scopes: list[str] | None = None,
        actor: str = "admin",
    ) -> MemberRecord:
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
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE members SET role = ?, dept = ?, scopes = ?, updated_at = ? WHERE username = ?",
                (role, dept, json.dumps(scopes or []), _now(), username),
            )
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
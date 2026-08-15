"""Compat shim for the seeded mock-user store (plan_mk2 Phase 1).

Before Mk2 this module held a static `USERS` dict; it is now a read-through
Mapping over `app.auth.store.MemberStore`, which is the single source of truth
for identity (users + credentials live in `data/members.sqlite`). The `User`
dataclass + `USERS` Mapping shape are preserved so tests and demo scripts keep
working unchanged (plan_mk2 §3.3): any code that reads `USERS[username].role`,
`.dept`, `.scopes`, iterates `USERS.values()`, or `USERS.get(username)` still
works and now reads the live store.

`User.password` is a convenience for HTTP login tests/demos against the 5
seeded demo accounts only (seed plaintexts, hashed in the store, plan_mk2 §2.3);
it is `None` for any member created afterwards.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.auth.store import SEED_PASSWORDS, MemberRecord, MemberStore


@dataclass(frozen=True)
class User:
    username: str
    password: str | None  # seed demo plaintext only (see module docstring); None otherwise
    role: str
    dept: str
    scopes: list[str] = field(default_factory=list)
    active: bool = True


_STORE: MemberStore | None = None


def get_store() -> MemberStore:
    """The shared MemberStore (swappable by tests via `set_store`)."""
    global _STORE
    if _STORE is None:
        _STORE = MemberStore()
    return _STORE


def set_store(store: MemberStore | None) -> None:
    global _STORE
    _STORE = store


def _to_user(record: MemberRecord) -> User:
    return User(
        username=record.username,
        password=SEED_PASSWORDS.get(record.username),
        role=record.role,
        dept=record.dept,
        scopes=list(record.scopes),
        active=record.active,
    )


class _UserRegistry(Mapping[str, User]):
    """Read-only Mapping facade over the MemberStore (preserves the old shape)."""

    def __getitem__(self, username: str) -> User:
        record = get_store().get(username)
        if record is None:
            raise KeyError(username)
        return _to_user(record)

    def __iter__(self) -> Iterator[str]:
        return (m.username for m in get_store().list())

    def __len__(self) -> int:
        return len(get_store().list())

    def __contains__(self, username: object) -> bool:
        return isinstance(username, str) and get_store().get(username) is not None


USERS: Mapping[str, User] = _UserRegistry()


def get_user(username: str) -> User | None:
    record = get_store().get(username)
    return _to_user(record) if record is not None else None


def claims_for(username: str) -> dict[str, Any] | None:
    """JWT-style claims (sub/role/dept/scopes) for a username — store-backed."""
    return get_store().claims_for(username)
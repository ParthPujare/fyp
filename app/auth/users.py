"""Seeded mock user store. Identity = simple JWT claims (see plan.md Decision 13)."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class User:
    username: str
    password: str  # mock only; real SSO out of scope
    role: str
    dept: str
    scopes: list[str] = field(default_factory=list)


USERS: dict[str, User] = {
    "alice@acme.com": User(
        "alice@acme.com", "alice123", "marketing_agent", "marketing", ["crm:read", "crm:create"]
    ),
    "bob@acme.com": User(
        "bob@acme.com", "bob123", "hr_agent", "hr", ["hr:read", "hr:create"]
    ),
    "carol@acme.com": User(
        "carol@acme.com", "carol123", "finance_agent", "finance", ["finance:read", "finance:transfer"]
    ),
    "dave@acme.com": User(
        "dave@acme.com", "dave123", "devops_agent", "devops", ["devops:read", "devops:create"]
    ),
    "admin@acme.com": User("admin@acme.com", "admin123", "admin", "admin", ["*"]),
}

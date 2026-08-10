"""Memory scope helpers (plan.md §5.1) + reversible user-uid encoding.

Scope notation (used in Casbin obj + audit resource id + episodic `scope` col):
    "central" | "dept/<name>" | "user/<uid>"

BaseStore namespaces reject labels containing "." (LangGraph validation), so the
user uid (an email) is reversibly base64url-encoded in the namespace label while
the human-readable scope string keeps the raw uid.
"""

from __future__ import annotations

import base64
from typing import Any

SCOPE_CENTRAL = "central"


def scope_dept(name: str) -> str:
    return f"dept/{name}"


def scope_user(sub: str) -> str:
    return f"user/{sub}"


def uid_label(sub: str) -> str:
    return base64.urlsafe_b64encode(sub.encode("utf-8")).decode("ascii").rstrip("=")


def sub_from_label(label: str) -> str:
    padded = label + "=" * (-len(label) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii")).decode("utf-8")


def scope_to_namespace(scope: str) -> tuple[str, ...]:
    if scope == SCOPE_CENTRAL:
        return ("central",)
    kind, _, value = scope.partition("/")
    if kind == "dept":
        return ("dept", value)
    if kind == "user":
        return ("user", uid_label(value))
    raise ValueError(f"invalid memory scope: {scope!r}")


def namespace_to_scope(namespace: tuple[str, ...]) -> str:
    if not namespace:
        raise ValueError("empty memory namespace")
    if namespace[0] == "central":
        return SCOPE_CENTRAL
    if namespace[0] == "dept":
        return scope_dept(namespace[1])
    if namespace[0] == "user":
        return scope_user(sub_from_label(namespace[1]))
    raise ValueError(f"unknown memory namespace: {namespace!r}")


def actor_claims(actor: dict[str, Any]) -> dict[str, Any]:
    return {"sub": actor.get("sub", ""), "role": actor.get("role", ""), "dept": actor.get("dept", "")}

"""Browser session cookie names + helpers (Mk18 Phase D).

Dual httpOnly slots preserve the identity switcher without localStorage
tokens (XSS-readable today): `<base>_admin`, `<base>_member`, plus a readable
`<base>_active` slot cookie ("admin" | "member" | ""). Claims stay in
localStorage — they are UI data, not secrets.
"""

from __future__ import annotations


def slot_cookie(base: str, label: str) -> str:
    return f"{base}_{label}"


def active_cookie(base: str) -> str:
    return f"{base}_active"


def label_for_role(role: str) -> str:
    return "admin" if role == "admin" else "member"

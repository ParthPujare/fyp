"""CSRF protection for cookie-authenticated browser sessions (Mk18 Phase D).

Bearer-header API clients (tests, curl, service-to-service) are exempt: they
prove possession per request and are not ambient-authority browsers. Cookie
sessions must present `X-CSRF-Token` on mutating methods.

The token is derived, not stored: HMAC-SHA256(jwt_secret, "csrf:" + sub).
No database, no rotation story beyond the JWT secret itself.
"""

from __future__ import annotations

import hashlib
import hmac
from typing import Any


def csrf_token_for(sub: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), f"csrf:{sub}".encode(), hashlib.sha256
    ).hexdigest()


def csrf_valid(token: str | None, sub: str, secret: str) -> bool:
    if not token or not sub or not secret:
        return False
    try:
        return hmac.compare_digest(csrf_token_for(sub, secret), token)
    except Exception:  # noqa: BLE001
        return False


MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def csrf_exempt_path(path: str) -> bool:
    """Entry points and non-mutating surfaces that never require CSRF.

    Note: /api/logout is NOT exempt — cookie logout without a valid token
    fails closed (200 only with CSRF). Worst case the cookie survives to
    expiry; a forged logout is a nuisance, a forged anything-else is worse.
    """
    if path in ("/health", "/api/login", "/api/csrf", "/auth/login", "/auth/logout"):
        return True
    return path == "/" or path.startswith("/static")


def claims_sub(claims: dict[str, Any]) -> str:
    return str((claims or {}).get("sub", ""))

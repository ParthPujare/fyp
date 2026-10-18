"""JWT issue + verify (PyJWT, HS256). Claims carry sub, role, dept, scopes.

Mk19 Phase 6: tokens also carry `cred_ver` — the member's credential
generation at mint time. `verify_token` re-reads the store and rejects any
token minted under an older generation (password reset, role/dept/scope
update, TOTP set) as `token superseded`. Strict: no grace window, no legacy
acceptance; a member that does not exist is generation 0 on both sides, so
demo/ghost tokens keep their old behavior.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.auth.users import User


class AuthError(Exception):
    """Raised when a token is invalid, tampered, expired, or superseded."""


def _credentials_version(username: str) -> int:
    """Current generation for one member (0 = no such member). Lazy store read:
    every verify site shares this guard, and a swapped store (tests) is picked
    up because get_store() resolves fresh."""
    from app.auth.users import get_store

    return int(get_store().credentials_version(username))


def create_token(
    user: User,
    secret: str,
    algorithm: str = "HS256",
    expiry_minutes: int = 60,
    *,
    cred_ver: int | None = None,
) -> str:
    now = datetime.now(UTC)
    if cred_ver is None:
        cred_ver = _credentials_version(user.username)
    payload = {
        "sub": user.username,
        "role": user.role,
        "dept": user.dept,
        "scopes": user.scopes,
        "cred_ver": int(cred_ver),
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expiry_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def verify_token(token: str, secret: str, algorithm: str = "HS256") -> dict[str, Any]:
    try:
        claims = jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid token") from exc
    # Mk19 Phase 6: reject tokens minted under an older credential generation.
    if int(claims.get("cred_ver", 0)) != _credentials_version(str(claims.get("sub", ""))):
        raise AuthError("token superseded")
    return claims

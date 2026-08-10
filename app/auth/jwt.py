"""JWT issue + verify (PyJWT, HS256). Claims carry sub, role, dept, scopes."""

from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.auth.users import User


class AuthError(Exception):
    """Raised when a token is invalid, tampered, or expired."""


def create_token(
    user: User,
    secret: str,
    algorithm: str = "HS256",
    expiry_minutes: int = 60,
) -> str:
    now = datetime.now(UTC)
    payload = {
        "sub": user.username,
        "role": user.role,
        "dept": user.dept,
        "scopes": user.scopes,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=expiry_minutes)).timestamp()),
    }
    return jwt.encode(payload, secret, algorithm=algorithm)


def verify_token(token: str, secret: str, algorithm: str = "HS256") -> dict[str, Any]:
    try:
        return jwt.decode(token, secret, algorithms=[algorithm])
    except jwt.ExpiredSignatureError as exc:
        raise AuthError("token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise AuthError("invalid token") from exc

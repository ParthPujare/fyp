"""Auth service — FastAPI on :8000. Mock JWT login backed by the MemberStore.

Since Mk2 Phase 1 the identity source is `app.auth.store.MemberStore`
(`data/members.sqlite`), not the static `USERS` dict: `/auth/login` verifies the
scrypt hash, `/auth/me` re-reads the member so an admin edit (role/dept/scopes)
is reflected on the *next* claims read even with an existing token
(plan_mk2 §3.1). Inactive members cannot log in; Phase 2 additionally denies
their already-issued tokens at enforcement time.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel

from app.auth import users
from app.auth.jwt import AuthError, create_token, verify_token
from app.config import get_settings

settings = get_settings()

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Shared DB (Mk13): connect, migrate and pre-load the member cache at boot, so no user
    pays that ~3 s (a dozen round trips to Neon) on their first request. No-op on SQLite."""
    from app.db import is_enabled

    if is_enabled():
        users.get_store()
    yield


app = FastAPI(title="auth service", version="0.1.0", lifespan=_lifespan)


class LoginRequest(BaseModel):
    username: str
    password: str
    totp: str | None = None  # Mk18 Phase C: required only when the member set one


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "auth"}


@app.post("/auth/login")
def login(req: LoginRequest, response: Response) -> dict:
    store = users.get_store()
    # Mk17: verify first (direct DB read, never cached), then direct member
    # read for claims — a member created <1s ago must not 401 via the 1s memo.
    if not store.verify(req.username, req.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    get_direct = getattr(store, "get_direct", store.get)
    member = get_direct(req.username)
    if member is None or not member.active:
        raise HTTPException(status_code=401, detail="invalid credentials")
    # Mk18 Phase C: second factor enforced only for members that opted in
    # (totp_secret set via set_totp_secret); everyone else is unchanged.
    if getattr(member, "totp_secret", ""):
        from app.auth.store import verify_totp

        if not verify_totp(member.totp_secret, req.totp or ""):
            raise HTTPException(status_code=401, detail="invalid second factor")
    claims = member.claims()
    token = create_token(
        member,
        settings.jwt.secret,
        settings.jwt.algorithm,
        settings.jwt.expiry_minutes,
    )
    # Mk18 Phase D: dual httpOnly session slots (XSS cannot read tokens).
    # The body token stays for one release so API clients/tests keep working.
    from app.harness.cookies import active_cookie, label_for_role, slot_cookie

    label = label_for_role(str(claims.get("role", "")))
    cookie_kwargs = {
        "max_age": settings.jwt.expiry_minutes * 60,
        "httponly": True,
        "secure": settings.jwt.cookie_secure,
        "samesite": settings.jwt.cookie_samesite,
        "path": "/",
    }
    response.set_cookie(key=slot_cookie(settings.jwt.cookie_name, label), value=token, **cookie_kwargs)  # type: ignore[arg-type]
    response.set_cookie(key=active_cookie(settings.jwt.cookie_name), value=label, httponly=False, secure=settings.jwt.cookie_secure, samesite=settings.jwt.cookie_samesite, path="/", max_age=settings.jwt.expiry_minutes * 60)  # type: ignore[arg-type]
    return {"access_token": token, "token_type": "bearer", "claims": claims}


def _token_from(request: Request | None, authorization: str | None) -> str | None:
    """Mk18 Phase D dual-read: explicit Bearer header, else active slot cookie."""
    if authorization and authorization.lower().startswith("bearer "):
        return authorization.split(" ", 1)[1].strip() or None
    if request is not None:
        from app.harness.cookies import active_cookie, slot_cookie

        active = request.cookies.get(active_cookie(settings.jwt.cookie_name), "")
        if active in ("admin", "member"):
            cookie = request.cookies.get(slot_cookie(settings.jwt.cookie_name, active), "")
            if cookie:
                return cookie
    return None


@app.post("/auth/logout")
def logout(response: Response) -> dict:
    """Mk18 Phase D: clear the session cookies (tokens are stateless)."""
    from app.harness.cookies import active_cookie, slot_cookie

    for name in (
        slot_cookie(settings.jwt.cookie_name, "admin"),
        slot_cookie(settings.jwt.cookie_name, "member"),
        active_cookie(settings.jwt.cookie_name),
    ):
        response.delete_cookie(key=name, path="/")
    return {"logged_out": True}


@app.get("/auth/me")
def me(request: Request, authorization: str | None = Header(default=None)) -> dict:
    token = _token_from(request, authorization)
    if not token:
        raise HTTPException(status_code=401, detail="missing bearer token")
    try:
        verified = verify_token(token, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    member = users.get_store().get(verified["sub"])
    if member is None:
        raise HTTPException(status_code=404, detail="member deactivated or removed")
    return {
        "sub": member.username,
        "role": member.role,
        "dept": member.dept,
        "scopes": member.scopes,
    }
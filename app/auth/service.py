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

from fastapi import FastAPI, Header, HTTPException
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


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "auth"}


@app.post("/auth/login")
def login(req: LoginRequest) -> dict:
    store = users.get_store()
    # Mk17: verify first (direct DB read, never cached), then direct member
    # read for claims — a member created <1s ago must not 401 via the 1s memo.
    if not store.verify(req.username, req.password):
        raise HTTPException(status_code=401, detail="invalid credentials")
    get_direct = getattr(store, "get_direct", store.get)
    member = get_direct(req.username)
    if member is None or not member.active:
        raise HTTPException(status_code=401, detail="invalid credentials")
    claims = member.claims()
    token = create_token(
        member,
        settings.jwt.secret,
        settings.jwt.algorithm,
        settings.jwt.expiry_minutes,
    )
    return {"access_token": token, "token_type": "bearer", "claims": claims}


@app.get("/auth/me")
def me(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
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
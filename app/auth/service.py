"""Auth service — FastAPI on :8000. Mock JWT login."""

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from app.auth.jwt import AuthError, create_token, verify_token
from app.auth.users import USERS
from app.config import get_settings

settings = get_settings()

app = FastAPI(title="auth service", version="0.1.0")


class LoginRequest(BaseModel):
    username: str
    password: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "auth"}


@app.post("/auth/login")
def login(req: LoginRequest) -> dict:
    user = USERS.get(req.username)
    if user is None or user.password != req.password:
        raise HTTPException(status_code=401, detail="invalid credentials")
    token = create_token(
        user,
        settings.jwt.secret,
        settings.jwt.algorithm,
        settings.jwt.expiry_minutes,
    )
    claims = verify_token(token, settings.jwt.secret, settings.jwt.algorithm)
    return {"access_token": token, "token_type": "bearer", "claims": claims}


@app.get("/auth/me")
def me(authorization: str | None = Header(default=None)) -> dict:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        claims = verify_token(token, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {
        "sub": claims["sub"],
        "role": claims["role"],
        "dept": claims["dept"],
        "scopes": claims["scopes"],
    }

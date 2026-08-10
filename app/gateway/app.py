"""Harness gateway shell — FastAPI on :8001.

Milestone 1 ships the role-aware `/hello` endpoint. Weeks 3-4 grow this into the
full LangGraph gateway with filter/intercept/guardrails.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from app.auth.jwt import AuthError, verify_token
from app.config import get_settings

settings = get_settings()

app = FastAPI(title="harness gateway", version="0.1.0")


class HelloRequest(BaseModel):
    token: str


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "gateway"}


@app.post("/hello")
def hello(req: HelloRequest) -> dict:
    try:
        claims = verify_token(req.token, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return {
        "message": (
            f"Hello {claims['sub']}! You are a {claims['role']} "
            f"in the {claims['dept']} department."
        ),
        "claims": {
            "sub": claims["sub"],
            "role": claims["role"],
            "dept": claims["dept"],
            "scopes": claims["scopes"],
        },
    }

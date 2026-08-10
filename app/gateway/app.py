"""Harness gateway — FastAPI on :8001.

`/hello` (Milestone 1) and `/chat` (Milestone 2): a JWT-verified, bounded,
traced single-turn agent run. The graph/injectables are behind a FastAPI
dependency (`get_runner`) so tests can override them with in-process components.
"""

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.agent.runner import AgentResult, run_agent
from app.auth.jwt import AuthError, verify_token
from app.config import get_settings

settings = get_settings()

app = FastAPI(title="harness gateway", version="0.1.0")


@dataclass
class Runner:
    def __call__(self, claims: dict, message: str) -> AgentResult:
        return run_agent(claims, message)


def get_runner() -> Runner:
    return Runner()


class HelloRequest(BaseModel):
    token: str


class ChatRequest(BaseModel):
    token: str
    message: str


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


@app.post("/chat")
def chat(req: ChatRequest, runner: Annotated[Runner, Depends(get_runner)]) -> dict:
    try:
        claims = verify_token(req.token, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    result = runner(claims, req.message)
    return {
        "reply": result.reply,
        "run_id": result.run_id,
        "guardrail_hits": result.guardrail_hits,
        "tool_calls": result.tool_calls,
    }

"""Harness gateway — FastAPI on :8001.

`/hello` (Milestone 1) and `/chat` (Milestones 2-4): a JWT-verified, bounded,
traced agent run. Since Milestone 4, `/chat` is thread-backed (SQLite checkpointer)
with an optional `thread_id` (default: per-user) so conversation memory + the
frozen snapshot persist across turns. A minimal `/approvals/{id}/decide` endpoint
lets an admin approve/reject a suspended central-tier memory write (the full
dashboard arrives with weeks 9-10).

The graph injectables are behind FastAPI dependencies (`get_runner`, `get_resumer`)
so tests can override them with in-process components.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.agent.runner import AgentResult, resume_agent, run_agent
from app.auth.jwt import AuthError, verify_token
from app.config import get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore

settings = get_settings()

app = FastAPI(title="harness gateway", version="0.1.0")


def _checkpointer():
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(settings.services.checkpoint_path, check_same_thread=False)
    return SqliteSaver(conn)


_components: dict[str, Any] | None = None


def _get_components() -> dict[str, Any]:
    """Persistent shared components (file-backed so state survives requests)."""
    global _components
    if _components is None:
        _components = {
            "trace": TraceStore(settings.services.data_dir / "trace.sqlite"),
            "enforcer": PolicyEnforcer(),
            "audit": AuditLog(settings.services.audit_path),
            "store": SqliteBaseStore(settings.services.memory_path),
            "episodic": EpisodicStore(settings.services.memory_path),
            "approvals": ApprovalStore(settings.services.memory_path),
            "checkpointer": _checkpointer(),
        }
        _components["recall"] = RecallService(
            _components["episodic"], _components["enforcer"], settings.memory
        )
    return _components


@dataclass
class Runner:
    def __call__(self, claims: dict, message: str, thread_id: str | None = None) -> AgentResult:
        if thread_id is None:
            thread_id = f"user:{claims['sub']}"
        c = _get_components()
        return run_agent(claims, message, thread_id=thread_id, **c)


def get_runner() -> Runner:
    return Runner()


def get_resumer():
    def _resume(approval_id: str):
        return resume_agent(approval_id, **_get_components())

    return _resume


def get_approval_store() -> ApprovalStore:
    return _get_components()["approvals"]


class HelloRequest(BaseModel):
    token: str


class ChatRequest(BaseModel):
    token: str
    message: str
    thread_id: str | None = None


class ApprovalDecisionRequest(BaseModel):
    approve: bool
    admin: str


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
    result = runner(claims, req.message, req.thread_id)
    return {
        "reply": result.reply,
        "run_id": result.run_id,
        "guardrail_hits": result.guardrail_hits,
        "tool_calls": result.tool_calls,
        "thread_id": result.thread_id,
        "interrupted": result.interrupted,
        "approval_id": result.approval_id,
    }


@app.post("/approvals/{approval_id}/decide")
def decide_approval(
    approval_id: str,
    req: ApprovalDecisionRequest,
    resumer: Annotated[Any, Depends(get_resumer)],
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
) -> dict:
    entry = approvals.get(approval_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if entry["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"approval {approval_id!r} already {entry['status']}")
    approvals.decide(approval_id, req.approve, req.admin)
    result = resumer(approval_id)
    decided = approvals.get(approval_id)
    return {
        "approval_id": approval_id,
        "status": decided["status"],
        "thread_id": result.thread_id,
        "reply": result.reply,
    }

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

import inspect
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel

from app.agent.checkpoints import list_user_threads, make_checkpointer
from app.agent.runner import AgentResult, resume_agent, run_agent, run_state_is_local
from app.auth.jwt import AuthError, verify_token
from app.auth.users import get_store, is_active_member
from app.config import get_settings
from app.harness.audit import open_audit_log
from app.harness.enforcer import PolicyEnforcer
from app.memory.approvals import ApprovalStore
from app.memory.factory import open_approval_store, open_base_store, open_episodic_store
from app.memory.recall import RecallService
from app.ops.factory import open_trace_store
from app.skills.registry import SkillRegistry

settings = get_settings()

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Shared DB (Mk13): build the stores/enforcer at boot (connect + migration check + cache
    pre-load) instead of on the first chat. No-op on SQLite, where construction is instant."""
    from app.db import is_enabled

    if is_enabled():
        _get_components()
        get_store()
        SkillRegistry.open().load_all()  # Phase 6: seed-if-empty + cache the shared skills
    yield


app = FastAPI(title="harness gateway", version="0.1.0", lifespan=_lifespan)


_components: dict[str, Any] | None = None


def _get_components() -> dict[str, Any]:
    """Persistent shared components (file-backed so state survives requests)."""
    global _components
    if _components is None:
        _components = {
            "trace": open_trace_store(settings.services.data_dir / "trace.sqlite"),
            "enforcer": PolicyEnforcer(),
            "audit": open_audit_log(settings.services.audit_path),
            "store": open_base_store(settings.services.memory_path),
            "episodic": open_episodic_store(settings.services.memory_path),
            "approvals": open_approval_store(settings.services.memory_path),
            "checkpointer": make_checkpointer(settings),
        }
        _components["recall"] = RecallService(
            _components["episodic"], _components["enforcer"], settings.memory
        )
    return _components


@dataclass
class Runner:
    def __call__(self, claims: dict, message: str, thread_id: str | None = None, key_id: str | None = None) -> AgentResult:
        if thread_id is None:
            thread_id = f"user:{claims['sub']}"
        c = _get_components()
        return run_agent(claims, message, thread_id=thread_id, key_id=key_id, **c)


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
    key_id: str | None = None


class ThreadListRequest(BaseModel):
    token: str


class ApprovalDecisionRequest(BaseModel):
    approve: bool
    token: str  # admin JWT — the decider is derived from verified claims, never trusted input


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "gateway"}


def _verified_active(token: str) -> dict:
    """Verify the JWT (401) then require the member to exist AND be active (403).

    plan_mk2 Phase 2: a disabled or unknown user is denied at every endpoint even
    if they hold an unexpired token — tokens no longer outlive membership.
    """
    try:
        claims = verify_token(token, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not is_active_member(str(claims.get("sub", ""))):
        raise HTTPException(status_code=403, detail="account disabled or not a member")
    return claims


@app.post("/hello")
def hello(req: HelloRequest) -> dict:
    claims = _verified_active(req.token)
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


def _call_runner(runner: Any, claims: dict, message: str, thread_id: str | None, key_id: str | None):
    """Invoke a runner, passing `key_id` only if it accepts one.

    Tests override `get_runner` with 3-argument callables. Deciding by signature
    rather than catching TypeError matters: a TypeError raised *inside* the agent
    would otherwise be swallowed and the whole turn silently re-run without the
    caller's selected model.
    """
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):  # builtins / C callables have no signature
        params = {}
    accepts_key_id = "key_id" in params or any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    )
    if accepts_key_id:
        return runner(claims, message, thread_id, key_id=key_id)
    return runner(claims, message, thread_id)


@app.post("/chat")
def chat(req: ChatRequest, runner: Annotated[Runner, Depends(get_runner)]) -> dict:
    claims = _verified_active(req.token)
    try:
        result = _call_runner(runner, claims, req.message, req.thread_id, req.key_id)
    except (RuntimeError, ValueError) as exc:
        # Providers raise RuntimeError carrying a classified, sanitized message
        # (bad key / quota / model not found / upstream 4xx) and ValueError for an
        # unusable provider config (unknown type, custom endpoint with no base_url).
        # Surfacing those as 502 keeps the cause visible in the UI instead of a
        # bare 500 whose only trace is the gateway console.
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    connect_required: list[str] = []
    for tc in result.tool_calls or []:
        if tc.get("outcome") == "denied" and "is not connected" in str(tc.get("content", "")):
            from app.integrations.service import connect_toolkit_for

            connect_required.append(connect_toolkit_for(str(tc.get("name", ""))))
    # Deduplicate, keep order; frontend renders inline Connect buttons + auto-retry hint.
    seen: set[str] = set()
    connect_required = [t for t in connect_required if t and not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
    return {
        "reply": result.reply,
        "run_id": result.run_id,
        "guardrail_hits": result.guardrail_hits,
        "tool_calls": result.tool_calls,
        "thread_id": result.thread_id,
        "interrupted": result.interrupted,
        "approval_id": result.approval_id,
        "approval_tier": getattr(result, "approval_tier", None),
        "connect_required": connect_required,
    }


def _thread_messages(checkpointer: Any, thread_id: str, limit: int = 50) -> list[dict[str, str]]:
    """Latest checkpoint's user/assistant messages (Mk8 #4). Best-effort: [] on any failure."""
    try:
        tuples = list(checkpointer.list({"configurable": {"thread_id": thread_id}}, limit=1))
    except Exception:  # noqa: BLE001 - history is best-effort, never fail the request
        return []
    if not tuples:
        return []
    try:
        values = tuples[0].checkpoint.get("channel_values", {}) if hasattr(tuples[0], "checkpoint") else {}
    except Exception:  # noqa: BLE001 - malformed checkpoint yields empty history
        return []
    raw = values.get("messages", []) if isinstance(values, dict) else []
    out: list[dict[str, str]] = []
    for m in raw:
        role = m.get("role") if isinstance(m, dict) else getattr(m, "type", None)
        content = m.get("content") if isinstance(m, dict) else getattr(m, "content", "")
        if role == "human":
            role = "user"
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        out.append({"role": role, "content": content})
    return out[-limit:]


def get_checkpointer_obj() -> Any:
    return _get_components()["checkpointer"]


class ThreadHistoryRequest(BaseModel):
    token: str


@app.post("/chat/threads/{thread_id:path}/history")
def thread_history(
    thread_id: str, req: ThreadHistoryRequest, checkpointer: Annotated[Any, Depends(get_checkpointer_obj)]
) -> dict:
    claims = _verified_active(req.token)
    username = str(claims.get("sub", ""))
    if thread_id != f"user:{username}" and not thread_id.startswith(f"user:{username}:"):
        raise HTTPException(status_code=403, detail="not your thread")
    return {"thread_id": thread_id, "messages": _thread_messages(checkpointer, thread_id)}


@app.post("/chat/threads")
def list_threads(req: ThreadListRequest) -> list[str]:
    claims = _verified_active(req.token)
    username = claims["sub"]
    threads = list_user_threads(username, settings)
    if not threads:  # brand-new user: offer the default chat (deleted chats must not reappear)
        threads.insert(0, f"user:{username}")
    return threads


@app.post("/approvals/{approval_id}/decide")
def decide_approval(
    approval_id: str,
    req: ApprovalDecisionRequest,
    resumer: Annotated[Any, Depends(get_resumer)],
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
) -> dict:
    """Admin-only HITL decision. The caller must present an active admin JWT; the
    decider identity comes from the verified claims (never from the body)."""
    claims = _verified_active(req.token)
    if claims.get("role") != "admin":
        raise HTTPException(status_code=403, detail="only admins may decide approvals")

    entry = approvals.get(approval_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if entry["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"approval {approval_id!r} already {entry['status']}")
    from app.db import is_enabled

    if is_enabled() and not run_state_is_local(_get_components()["checkpointer"], entry["thread_id"]):
        raise HTTPException(
            status_code=409,
            detail="this run's saved state was not found (was the chat deleted?), so it cannot be resumed",
        )
    try:
        approvals.decide(approval_id, req.approve, claims["sub"])
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:  # lost a race with another decider
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    result = resumer(approval_id)
    decided = approvals.get(approval_id)
    return {
        "approval_id": approval_id,
        "status": decided["status"],
        "thread_id": result.thread_id,
        "reply": result.reply,
        "interrupted": result.interrupted,
        "next_approval_id": result.approval_id if result.interrupted else None,
    }

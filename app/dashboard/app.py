"""Dashboard v1 — FastAPI on :8006. Admin + ops web panel (plan.md §10, §16).

Milestone 5 scope (decisions from session_006):
- HITL approvals queue: list pending ApprovalStore items, approve/reject (admin
  JWT required; decision resumes the suspended run via `resume_agent`).
- Users + departments: read-only lists (full CRUD deferred to a later milestone).

The dashboard is a standalone process that shares the same SQLite files as the
gateway (checkpoint/memory/trace paths from settings) and reuses `resume_agent`
directly, so an approval decided here resumes the exact suspended thread. Every
`/api/*` endpoint except `/api/login` requires a verified admin JWT.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.runner import resume_agent
from app.auth.jwt import AuthError, verify_token
from app.auth.users import USERS
from app.config import get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.sanitize import MASK
from app.memory.approvals import ApprovalStore
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore

settings = get_settings()

app = FastAPI(title="harness dashboard", version="0.1.0")

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


def _checkpointer():
    from langgraph.checkpoint.sqlite import SqliteSaver

    conn = sqlite3.connect(settings.services.checkpoint_path, check_same_thread=False)
    return SqliteSaver(conn)


_components: dict[str, Any] | None = None


def _get_components() -> dict[str, Any]:
    """Persistent shared components — same files the gateway uses."""
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


def _verify_admin(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        claims = verify_token(token, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if claims.get("role") != "admin":
        raise HTTPException(status_code=403, detail="only admins may access the dashboard API")
    return claims


def get_approval_store() -> ApprovalStore:
    return _get_components()["approvals"]


def get_resumer():
    def _resume(approval_id: str):
        return resume_agent(approval_id, **_get_components())

    return _resume


def _mask_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Mask sensitive tool args (e.g. finance transfer memo) in the UI list."""
    if "args" in payload and isinstance(payload["args"], dict):
        args = dict(payload["args"])
        for key in ("memo", "from_account", "to_account"):
            if key in args:
                args[key] = MASK
        payload = {**payload, "args": args}
    return payload


class LoginRequest(BaseModel):
    username: str
    password: str


class DecideRequest(BaseModel):
    approve: bool


@app.get("/", include_in_schema=False)
def index() -> HTMLResponse:
    return RedirectResponse("/static/index.html")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "dashboard"}


@app.post("/api/login")
def login(req: LoginRequest) -> dict:
    """Proxy the mock auth service login so the browser never holds credentials."""
    try:
        resp = httpx.post(
            f"{settings.services.auth_url}/auth/login",
            json={"username": req.username, "password": req.password},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"auth service unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="invalid credentials")
    return resp.json()


@app.get("/api/approvals/pending")
def pending_approvals(
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _verify_admin(authorization)
    return [
        {
            "id": item["id"],
            "run_id": item["run_id"],
            "thread_id": item["thread_id"],
            "resource": item["resource"],
            "actor": item["actor"],
            "payload": _mask_payload(item["payload"]),
            "status": item["status"],
            "created_ts": item["created_ts"],
        }
        for item in approvals.pending_items()
    ]


@app.post("/api/approvals/{approval_id}/decide")
def decide_approval(
    approval_id: str,
    req: DecideRequest,
    resumer: Annotated[Any, Depends(get_resumer)],
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    entry = approvals.get(approval_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if entry["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"approval {approval_id!r} already {entry['status']}")
    approvals.decide(approval_id, req.approve, claims["sub"])
    result = resumer(approval_id)
    decided = approvals.get(approval_id)
    return {
        "approval_id": approval_id,
        "status": decided["status"],
        "thread_id": result.thread_id,
        "reply": result.reply,
    }


@app.get("/api/users")
def users(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _verify_admin(authorization)
    return [
        {"username": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}
        for u in sorted(USERS.values(), key=lambda u: u.username)
    ]


@app.get("/api/departments")
def departments(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _verify_admin(authorization)
    depts: dict[str, list[str]] = {}
    for u in USERS.values():
        depts.setdefault(u.dept, [])
    with open(_policy_csv()) as fh:
        for line in fh:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 5 and parts[0] == "p" and parts[3] != "*":
                depts.setdefault(parts[2], [])
                if parts[3] not in depts[parts[2]]:
                    depts[parts[2]].append(parts[3])
    return [
        {
            "name": name,
            "resources": sorted(resources),
            "service": _service_for(name),
            "members": [u.username for u in USERS.values() if u.dept == name],
        }
        for name, resources in sorted(depts.items())
    ]


def _policy_csv() -> Path:
    return Path(__file__).resolve().parents[1] / "harness" / "policies" / "policy.csv"


def _service_for(dept: str) -> str:
    mapping = {
        "marketing": "crm",
        "hr": "hr",
        "devops": "devops",
        "finance": "finance",
        "admin": "harness gateway",
    }
    return mapping.get(dept, "")

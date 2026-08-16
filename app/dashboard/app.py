"""Dashboard v1 — FastAPI on :8006. Admin + ops web panel (plan_mk1_core_harness.md §10, §16).

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
from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.runner import resume_agent
from app.auth.jwt import AuthError, verify_token
from app.auth.users import USERS, is_active_member
from app.config import get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.sanitize import MASK
from app.memory.approvals import ApprovalStore
from app.memory.edit import MemoryEditor
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.scopes import (
    SCOPE_CENTRAL,
    namespace_to_scope,
    scope_dept,
    scope_to_namespace,
    scope_user,
)
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from app.skills.model import SkillValidationError, parse_skill_document, render_skill
from app.skills.registry import SkillRegistry

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
        _store = SqliteBaseStore(settings.services.memory_path)
        _components = {
            "trace": TraceStore(settings.services.data_dir / "trace.sqlite"),
            "enforcer": PolicyEnforcer(),
            "audit": AuditLog(settings.services.audit_path),
            "store": _store,
            "episodic": EpisodicStore(settings.services.memory_path),
            "approvals": ApprovalStore(settings.services.memory_path),
            "checkpointer": _checkpointer(),
            "skills": SkillRegistry(settings.skills.skills_dir),
            "memory_editor": MemoryEditor(_store, settings.memory),
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
    if not is_active_member(str(claims.get("sub", ""))):
        raise HTTPException(status_code=403, detail="account disabled or not a member")
    return claims


def get_approval_store() -> ApprovalStore:
    return _get_components()["approvals"]


def get_resumer():
    def _resume(approval_id: str):
        return resume_agent(approval_id, **_get_components())

    return _resume


def get_gateway_sender():
    """Forward a verified member chat to the gateway (SEAM: tests override this)."""

    def _send(token: str, message: str, thread_id: str | None) -> dict:
        try:
            resp = httpx.post(
                f"{settings.services.gateway_url}/chat",
                json={"token": token, "message": message, "thread_id": thread_id},
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=resp.text)
        return resp.json()

    return _send


def _verified_member(authorization: str | None) -> tuple[dict[str, Any], str]:
    """Verify a member JWT (401), require existence + active in the store (403).

    plan_mk2 Phase 3: `/api/chat` is the dashboard's single non-admin API. We
    verify the token locally (and check the Phase-2 active-member gate) before
    forwarding the *same* token to the gateway, so a disabled/unknown member is
    rejected here before any run starts.
    """
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        claims = verify_token(token, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not is_active_member(str(claims.get("sub", ""))):
        raise HTTPException(status_code=403, detail="account disabled or not a member")
    return claims, token


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


class ChatRequest(BaseModel):
    message: str
    thread_id: str | None = None


class DecideRequest(BaseModel):
    approve: bool


class SkillWriteRequest(BaseModel):
    content: str  # raw skill.md document (frontmatter + body)


class MemoryFactRequest(BaseModel):
    text: str


class MemoryItemRequest(BaseModel):
    text: str


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


@app.post("/api/chat")
def chat_member(
    req: ChatRequest,
    sender: Annotated[Any, Depends(get_gateway_sender)],
    authorization: str | None = Header(default=None),
) -> dict:
    """Any-user chat (plan_mk2 Phase 3). The caller's own member JWT is verified
    and forwarded to the gateway `/chat`; the gateway envelope
    (reply/guardrail_hits/tool_calls/interrupted/approval_id/…) is returned
    unchanged so the UI renders it directly. Admin endpoints stay admin-only."""
    _claims, token = _verified_member(authorization)
    return sender(token, req.message, req.thread_id)


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
        {
            "username": u.username,
            "role": u.role,
            "dept": u.dept,
            "scopes": u.scopes,
            "active": u.active,
        }
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


def _skill_registry() -> SkillRegistry:
    return _get_components()["skills"]


def _skill_summary(skill: Any) -> dict[str, Any]:
    return {
        "name": skill.name,
        "scope": skill.scope,
        "owner_role": skill.owner_role,
        "version": skill.version,
        "trigger": skill.trigger,
        "steps": skill.steps,
        "tool_calls": skill.tool_calls,
        "path": skill.path,
        "policy_row": _has_policy_row(skill.name),
    }


def _has_policy_row(skill_name: str) -> bool:
    """True iff policy.csv has a non-admin `p, role, dom, skill_<name>, use` row."""
    needle = f"skill_{skill_name}"
    with open(_policy_csv()) as fh:
        for line in fh:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 5 and parts[0] == "p" and parts[1] != "admin" and parts[3] == needle:
                return True
    return False


@app.get("/api/skills")
def list_skills(
    authorization: str | None = Header(default=None),
) -> dict:
    """List all skills on disk (registry is the folder, hot-reloaded)."""
    _verify_admin(authorization)
    skills, errors = _get_components()["skills"].load_with_errors()
    return {"skills": [_skill_summary(s) for s in skills], "errors": errors}


@app.get("/api/skills/{name}")
def get_skill(
    name: str,
    authorization: str | None = Header(default=None),
) -> dict:
    _verify_admin(authorization)
    skill = _get_components()["skills"].get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    return {"content": render_skill(skill)}


@app.post("/api/skills")
def create_skill(
    req: SkillWriteRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    claims = _verify_admin(authorization)
    try:
        skill = parse_skill_document(req.content)
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    registry = _get_components()["skills"]
    if registry.get(skill.name) is not None:
        raise HTTPException(status_code=409, detail=f"skill {skill.name!r} already exists")
    path = registry.save(skill)
    _get_components()["audit"].log(
        decision="ALLOW",
        resource={"type": "skill", "id": skill.name},
        actor={"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]},
        run_id="dashboard",
        reason="admin",
    )
    return {"name": skill.name, "path": str(path), "saved": True}


@app.put("/api/skills/{name}")
def update_skill(
    name: str,
    req: SkillWriteRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    """Save an edited skill.md — takes effect (hot reload) on the next run."""
    claims = _verify_admin(authorization)
    registry = _get_components()["skills"]
    if registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    try:
        skill = parse_skill_document(req.content, fallback_name=name)
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if skill.name != name:
        raise HTTPException(status_code=409, detail="skill name in frontmatter must match the url")
    path = registry.save(skill)
    _get_components()["audit"].log(
        decision="ALLOW",
        resource={"type": "skill", "id": skill.name},
        actor={"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]},
        run_id="dashboard",
        reason="admin",
    )
    return {"name": skill.name, "path": str(path), "saved": True}


@app.delete("/api/skills/{name}")
def delete_skill(
    name: str,
    authorization: str | None = Header(default=None),
) -> dict:
    claims = _verify_admin(authorization)
    registry = _get_components()["skills"]
    if not registry.delete(name):
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    _get_components()["audit"].log(
        decision="ALLOW",
        resource={"type": "skill", "id": name},
        actor={"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]},
        run_id="dashboard",
        reason="admin",
    )
    return {"name": name, "deleted": True}


# ---- Memory browser/editor (ops face) ----


def _scope_namespaces() -> list[dict[str, Any]]:
    """All memory namespaces split into scopes, with their items."""
    store = _get_components()["store"]
    namespaces = store.list_namespaces()
    scopes: dict[str, dict[str, Any]] = {}
    for ns in namespaces:
        scope = namespace_to_scope(ns)
        items = [
            {
                "key": item.key,
                "text": str(item.value.get("text", "")),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in store.items_in(ns)
            if item.value.get("text")
        ]
        scopes.setdefault(scope, {"scope": scope, "items": []})["items"] = items
    for scope in (SCOPE_CENTRAL,):
        scopes.setdefault(scope, {"scope": scope, "items": []})
    for dept in ["marketing", "hr", "devops", "finance"]:
        scopes.setdefault(scope_dept(dept), {"scope": scope_dept(dept), "items": []})
    for user in USERS.values():
        scopes.setdefault(scope_user(user.username), {"scope": scope_user(user.username), "items": []})
    return [
        {"scope": s["scope"], "items": s["items"]}
        for s in sorted(scopes.values(), key=lambda s: s["scope"])
    ]


def _audit_memory(claims: dict[str, Any], scope: str, action: str) -> None:
    _get_components()["audit"].log(
        decision="ALLOW",
        resource={"type": "memory", "id": scope},
        actor={"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]},
        run_id="dashboard",
        reason=f"admin:{action}",
    )


@app.get("/api/memory")
def memory_browser(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _verify_admin(authorization)
    return _scope_namespaces()


@app.post("/api/memory/{scope:path}")
def add_memory_fact(
    scope: str,
    req: MemoryFactRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    claims = _verify_admin(authorization)
    _validate_scope(scope)
    outcome = _get_components()["memory_editor"].add(scope, req.text)
    _audit_memory(claims, scope, outcome.op)
    return {"scope": scope, "op": outcome.op, "text": req.text, "evicted": outcome.evicted}


@app.put("/api/memory/{scope:path}/{key}")
def update_memory_fact(
    scope: str,
    key: str,
    req: MemoryItemRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    claims = _verify_admin(authorization)
    _validate_scope(scope)
    store = _get_components()["store"]
    item = store.get(scope_to_namespace(scope), key)
    if item is None:
        raise HTTPException(status_code=404, detail=f"memory {key!r} not found in {scope}")
    store.put(scope_to_namespace(scope), key, {"text": req.text, "op": "replace"})
    _audit_memory(claims, scope, "replace")
    return {"scope": scope, "key": key, "text": req.text, "op": "replace"}


@app.delete("/api/memory/{scope:path}/{key}")
def delete_memory_fact(
    scope: str,
    key: str,
    authorization: str | None = Header(default=None),
) -> dict:
    claims = _verify_admin(authorization)
    _validate_scope(scope)
    store = _get_components()["store"]
    if store.get(scope_to_namespace(scope), key) is None:
        raise HTTPException(status_code=404, detail=f"memory {key!r} not found in {scope}")
    store.delete(scope_to_namespace(scope), key)
    _audit_memory(claims, scope, "remove")
    return {"scope": scope, "key": key, "deleted": True}


def _validate_scope(scope: str) -> None:
    if scope == SCOPE_CENTRAL:
        return
    kind, _, value = scope.partition("/")
    if kind == "dept" and value:
        return
    if kind == "user" and value:
        return
    raise HTTPException(status_code=422, detail=f"invalid memory scope {scope!r}")


# ---- Ops face: trajectory viewer + evals (Week 12) ----


def _trace() -> TraceStore:
    return _get_components()["trace"]


@app.get("/api/ops/runs")
def ops_runs(
    trace: Annotated[TraceStore, Depends(_trace)],
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    """Runs summary for the ops tab (admin-only)."""
    _verify_admin(authorization)
    return trace.list_runs()


@app.get("/api/ops/runs/{run_id}")
def ops_run(
    run_id: str,
    trace: Annotated[TraceStore, Depends(_trace)],
    source: Annotated[list[str] | None, Query()] = None,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """One run's full trajectory, optionally filtered by event source (e.g.
    ``?source=llm&source=tool``). Trajectory text is PII-sanitized at write
    time; this endpoint is admin-JWT only (plan_mk1_core_harness.md §7)."""
    _verify_admin(authorization)
    tree = trace.get_run_tree(run_id)
    if not tree:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    sources = {s for s in (source or []) if s}
    if sources:
        tree = [e for e in tree if e.get("source") in sources]
    return {"run_id": run_id, "filter": sorted(sources) or None, "events": tree}


@app.get("/api/ops/runs/{run_id}/evals")
def ops_run_evals(
    run_id: str,
    trace: Annotated[TraceStore, Depends(_trace)],
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    """Eval suites (deterministic / judge) recorded for a run."""
    _verify_admin(authorization)
    tree = trace.get_run_tree(run_id)
    if not tree:
        raise HTTPException(status_code=404, detail=f"run {run_id!r} not found")
    return [dict(e) for e in tree if e["type"] == "eval"]

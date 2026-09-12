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

import re
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
from app.auth.store import (
    DepartmentExistsError,
    LockoutGuardError,
    MemberError,
    MemberExistsError,
)
from app.auth.users import USERS, get_store, is_active_member
from app.config import get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.harness.sanitize import MASK
from app.llm.base import strip_routing_prefix
from app.llm.registry import PROVIDERS, is_supported, normalize_provider_type
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
        c = _get_components()
        return resume_agent(
            approval_id,
            trace=c["trace"],
            enforcer=c["enforcer"],
            audit=c["audit"],
            store=c["store"],
            episodic=c["episodic"],
            approvals=c["approvals"],
            recall=c["recall"],
            checkpointer=c["checkpointer"],
        )

    return _resume


def get_gateway_sender():
    """Forward a verified member chat to the gateway (SEAM: tests override this)."""

    def _send(token: str, message: str, thread_id: str | None, key_id: str | None = None) -> dict:
        try:
            resp = httpx.post(
                f"{settings.services.gateway_url}/chat",
                json={"token": token, "message": message, "thread_id": thread_id, "key_id": key_id},
                timeout=60.0,
            )
        except httpx.HTTPError as exc:
            raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
        if resp.status_code != 200:
            raise HTTPException(status_code=resp.status_code, detail=_gateway_detail(resp))
        return resp.json()

    return _send


def _gateway_detail(resp: httpx.Response) -> str:
    """The gateway's own error message, unwrapped from its JSON envelope.

    Without this the browser gets `{"detail": "{\\"detail\\": \\"…\\"}"}` — or, for an
    unhandled 500, the opaque string "Internal Server Error" with the real cause
    left behind in the gateway's console.
    """
    try:
        body = resp.json()
    except ValueError:
        return resp.text
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        return body["detail"]
    return resp.text


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
    key_id: str | None = None


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
    return sender(token, req.message, req.thread_id, key_id=req.key_id)


def get_dashboard_checkpointer() -> Any:
    return _get_components()["checkpointer"]


def _thread_messages_local(checkpointer: Any, thread_id: str, limit: int = 50) -> list[dict[str, str]]:
    """Same extraction as the gateway (Mk8 #4); empty on any failure."""
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


@app.get("/api/chat/threads/{thread_id:path}/history")
def own_thread_history(
    thread_id: str,
    checkpointer: Annotated[Any, Depends(get_dashboard_checkpointer)],
    authorization: str | None = Header(default=None),
) -> dict:
    claims, _token = _verified_member(authorization)
    username = str(claims.get("sub", ""))
    if thread_id != f"user:{username}" and not thread_id.startswith(f"user:{username}:"):
        raise HTTPException(status_code=403, detail="not your thread")
    return {"thread_id": thread_id, "messages": _thread_messages_local(checkpointer, thread_id)}


@app.get("/api/chat/threads")
def list_own_threads(authorization: str | None = Header(default=None)) -> list[str]:
    _claims, token = _verified_member(authorization)
    try:
        resp = httpx.post(
            f"{settings.services.gateway_url}/chat/threads",
            json={"token": token},
            timeout=10.0,
        )
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"gateway unreachable: {exc}") from exc
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)
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
    store = get_store()
    out = []
    for m in store.list():
        pub = m.to_public()
        active_key = store.get_active_llm_key(m.username)
        pub["active_llm_key"] = {"provider": active_key[0], "model": active_key[1]} if active_key else None
        out.append(pub)
    return out


class MemberCreateRequest(BaseModel):
    username: str
    password: str
    role: str
    dept: str
    scopes: list[str] = []


class MemberUpdateRequest(BaseModel):
    role: str
    dept: str
    scopes: list[str] = []


class ResetPasswordRequest(BaseModel):
    new_password: str


@app.post("/api/members")
def create_member(
    req: MemberCreateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Admin add-member (plan_mk2 Phase 4). Username immutable afterwards
    (store PK); role/dept/scopes editable; password set to the returned member's."""
    claims = _verify_admin(authorization)
    username = (req.username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="username is required")
    if len(req.password or "") < 6:
        raise HTTPException(status_code=422, detail="password must be at least 6 characters")
    try:
        return get_store().create(
            username,
            req.password,
            role=req.role,
            dept=req.dept,
            scopes=list(req.scopes or []),
            actor=claims["sub"],
        ).to_public()
    except MemberExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@app.put("/api/members/{username}")
def update_member(
    username: str,
    req: MemberUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    try:
        return get_store().update(
            username,
            role=req.role,
            dept=req.dept,
            scopes=list(req.scopes or []),
            actor=claims["sub"],
        ).to_public()
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/members/{username}/activate")
def activate_member(
    username: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    try:
        return get_store().set_active(username, True, actor=claims["sub"]).to_public()
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/members/{username}/deactivate")
def deactivate_member(
    username: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Deactivate — lockout guard enforced server-side (last admin / self)."""
    claims = _verify_admin(authorization)
    try:
        return get_store().set_active(username, False, actor=claims["sub"]).to_public()
    except LockoutGuardError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/members/{username}/reset-password")
def reset_password(
    username: str,
    req: ResetPasswordRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    if len(req.new_password or "") < 6:
        raise HTTPException(status_code=422, detail="password must be at least 6 characters")
    try:
        return get_store().reset_password(
            username, req.new_password, actor=claims["sub"]
        ).to_public()
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.get("/api/departments")
def departments(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _verify_admin(authorization)
    store = get_store()
    dept_roles = [d.name for d in store.list_departments()]
    # policy.csv `p` rows can still introduce/denote knowable objects per domain;
    # merge them so the resource list stays accurate even before registration.
    policy_objs: dict[str, list[str]] = {}
    with open(_policy_csv(), encoding="utf-8") as fh:
        for line in fh:
            parts = [p.strip() for p in line.split(",")]
            if len(parts) == 5 and parts[0] == "p" and parts[3] != "*":
                policy_objs.setdefault(parts[2], [])
                if parts[3] not in policy_objs[parts[2]]:
                    policy_objs[parts[2]].append(parts[3])
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for name in sorted(set(dept_roles) | set(policy_objs)):
        seen.add(name)
        rec = store.get_department(name)
        resources = list(policy_objs.get(name, []))
        if rec is not None:
            for r in rec.resources:
                if r not in resources:
                    resources.append(r)
        rows.append(
            {
                "name": name,
                "service": rec.service if rec is not None else "",
                "service_url": rec.service_url if rec is not None else "",
                "resources": sorted(resources),
                "members": [u.username for u in USERS.values() if u.dept == name],
                "role": rec.role if rec is not None else "",
            }
        )
    return rows


class DepartmentRegisterRequest(BaseModel):
    name: str
    service: str = ""
    service_url: str = ""
    resources: list[str] = []
    role: str = ""


@app.post("/api/departments")
def register_department(
    req: DepartmentRegisterRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Register a new department (plan_mk2 §2.2 / §4 row 4). The department
    record lands in the store (replacing the hardcoded `_service_for` map) and a
    default `p, <role>, <dept>, *, *` row is appended to policy.csv so the new
    role is immediately grantable. Appending to the policy file is the one place
    the dashboard writes it — kept tiny + explicit."""
    claims = _verify_admin(authorization)
    store = get_store()
    name = (req.name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]+", name or ""):
        raise HTTPException(status_code=422, detail="department name must be lowercase [a-z0-9_-]")
    role = (req.role or "").strip() or f"{name}_agent"
    try:
        store.register_department(
            name,
            service=(req.service or "").strip(),
            service_url=(req.service_url or "").strip(),
            resources=list(req.resources or []),
            role=role,
            actor=claims["sub"],
        )
    except DepartmentExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    _append_policy_row(role, name)
    rec = store.get_department(name)
    assert rec is not None
    return rec.to_public()


def _append_policy_row(role: str, dept: str) -> None:
    """Append `p, <role>, <dept>, *, *` to policy.csv so the role is grantable.
    `_build`/`load_policy` re-reads the file on the next version guard rebuild,
    so the newly-registered role takes effect without a restart.

    Idempotent: an equivalent row already in the file is left alone. Casbin
    ignores duplicate rows, but an unconditional append grows the file without
    bound every time a department is re-registered.
    """
    row = ("p", role, dept, "*", "*")
    if _policy_has_row(row):
        return
    with open(_policy_csv(), "a", encoding="utf-8") as fh:
        fh.write(f"p, {role}, {dept}, *, *\n")


def _policy_has_row(row: tuple[str, ...]) -> bool:
    """True iff policy.csv already carries this row (whitespace-insensitive)."""
    with open(_policy_csv(), encoding="utf-8") as fh:
        for line in fh:
            if line.lstrip().startswith("#"):
                continue
            if tuple(p.strip() for p in line.split(",")) == row:
                return True
    return False


def _policy_csv() -> Path:
    """Policy file — settings-driven so tests can point at a temp copy.

    Resolved through `get_settings()` on every call, not through this module's
    import-time `settings` snapshot: any test that calls `get_settings.cache_clear()`
    (the BYOK suites do, to reload `LLM__FERNET_KEY`) replaces the cached Settings
    instance, and a redirect applied to the new instance would not be seen here —
    so department registration would append to the REAL repo policy.csv.
    """
    return get_settings().services.policy_path


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
    with open(_policy_csv(), encoding="utf-8") as fh:
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


# ---- Member memory read (plan_mk2 Phase 5) ----


@app.get("/api/memory/me")
def member_memory(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    """A member's own readable memory (plan_mk2 §4 row 5).

    The second member-only API (beside `/api/chat`). It returns exactly what the
    agent's `load_memory` would inject for this caller — the scopes they may READ
    (central + own dept + own user, Casbin-filtered) with their facts — so the
    browser memory-test can prove (1) a 2nd marketing member reads the shared
    dept/marketing memory and (2) a member from another dept cannot see it.
    Admin browsing stays on `/api/memory`; this is the same data, scoped to the
    caller's own readable set.
    """
    claims, _token = _verified_member(authorization)
    components = _get_components()
    store = components["store"]
    rows: list[dict[str, Any]] = []
    for scope in components["recall"].readable_scopes(claims):
        items = [
            {
                "key": item.key,
                "text": str(item.value.get("text", "")),
                "updated_at": item.updated_at.isoformat(),
            }
            for item in store.items_in(scope_to_namespace(scope))
            if item.value.get("text")
        ]
        rows.append({"scope": scope, "items": items})
    return rows


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
    for dept in [d.name for d in get_store().list_departments()]:
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


def _normalize_model(provider: str, model: str) -> str:
    """Store the model id exactly as that provider's API expects it.

    This used to ADD a LiteLLM routing prefix (`groq/`, `gemini/`, `anthropic/`).
    The direct providers reject those, and the prefix corrupts ids that carry a
    real vendor namespace of their own: Groq serves GPT-OSS as
    `openai/gpt-oss-20b`, which became `groq/openai/gpt-oss-20b` and 404'd. Now it
    only removes a prefix that redundantly repeats the selected provider, so both
    `groq/llama-3.3-70b-versatile` and `openai/gpt-oss-20b` end up correct.
    """
    return strip_routing_prefix((model or "").strip(), normalize_provider_type(provider))


class LLMKeyRequest(BaseModel):
    provider: str
    model: str
    api_key: str
    base_url: str = ""  # required for custom_openai; blank = the provider's default


class LLMKeyUpdateRequest(BaseModel):
    model: str | None = None
    base_url: str | None = None
    active: bool | None = None


def _validated_key_fields(req: LLMKeyRequest) -> tuple[str, str, str, str]:
    """Normalize + validate a BYOK submission (shared by the member and admin routes)."""
    provider = (req.provider or "").strip()
    model = _normalize_model(provider, req.model or "")
    api_key = (req.api_key or "").strip()
    base_url = (req.base_url or "").strip()
    if not provider or not model or not api_key:
        raise HTTPException(status_code=422, detail="provider, model, and api_key are required")
    if not is_supported(provider):
        raise HTTPException(
            status_code=422,
            detail=f"unsupported provider {provider!r}; supported: {sorted(PROVIDERS)}",
        )
    if normalize_provider_type(provider) == "custom_openai" and not base_url:
        raise HTTPException(
            status_code=422,
            detail="base_url is required for a custom OpenAI-compatible endpoint",
        )
    return provider, model, api_key, base_url


@app.get("/api/account/llm-keys")
def get_own_llm_keys(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    claims, _token = _verified_member(authorization)
    store = get_store()
    return [k.to_public() for k in store.list_llm_keys(claims["sub"])]


@app.post("/api/account/llm-keys")
def set_own_llm_key(
    req: LLMKeyRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims, _token = _verified_member(authorization)
    provider, model, api_key, base_url = _validated_key_fields(req)
    record = get_store().set_llm_key(
        claims["sub"], provider, model, api_key, active=True, actor=claims["sub"], base_url=base_url
    )
    return record.to_public()


@app.post("/api/account/llm-keys/{key_id}/activate")
def activate_own_llm_key(
    key_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims, _token = _verified_member(authorization)
    store = get_store()
    try:
        store.activate_llm_key(claims["sub"], key_id, actor=claims["sub"])
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "activated", "id": key_id}


@app.patch("/api/account/llm-keys/{key_id}")
def update_own_llm_key(
    key_id: str,
    req: LLMKeyUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims, _token = _verified_member(authorization)
    store = get_store()
    keys = [k for k in store.list_llm_keys(claims["sub"]) if k.id == key_id]
    if not keys:
        raise HTTPException(status_code=404, detail="LLM key not found")
    current_key = keys[0]

    normalized_model = None
    if req.model is not None:
        model_str = req.model.strip()
        if not model_str:
            raise HTTPException(status_code=422, detail="model cannot be empty")
        normalized_model = _normalize_model(current_key.provider, model_str)

    try:
        record = store.update_llm_key(
            claims["sub"],
            key_id,
            model=normalized_model,
            base_url=req.base_url,
            active=req.active,
            actor=claims["sub"],
        )
    except MemberError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.to_public()


@app.delete("/api/account/llm-keys/{key_id}")
def delete_own_llm_key(
    key_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims, _token = _verified_member(authorization)
    store = get_store()
    try:
        store.delete_llm_key(claims["sub"], key_id, actor=claims["sub"])
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "id": key_id}


@app.get("/api/members/{username}/llm-keys")
def get_member_llm_keys(
    username: str,
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _verify_admin(authorization)
    store = get_store()
    if store.get(username) is None:
        raise HTTPException(status_code=404, detail=f"member {username!r} not found")
    return [k.to_public() for k in store.list_llm_keys(username)]


@app.post("/api/members/{username}/llm-keys")
def set_member_llm_key(
    username: str,
    req: LLMKeyRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    store = get_store()
    if store.get(username) is None:
        raise HTTPException(status_code=404, detail=f"member {username!r} not found")
    provider, model, api_key, base_url = _validated_key_fields(req)
    record = store.set_llm_key(
        username, provider, model, api_key, active=True, actor=claims["sub"], base_url=base_url
    )
    return record.to_public()


@app.post("/api/members/{username}/llm-keys/{key_id}/activate")
def activate_member_llm_key(
    username: str,
    key_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    store = get_store()
    try:
        store.activate_llm_key(username, key_id, actor=claims["sub"])
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "activated", "id": key_id}


@app.patch("/api/members/{username}/llm-keys/{key_id}")
def update_member_llm_key(
    username: str,
    key_id: str,
    req: LLMKeyUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    store = get_store()
    if store.get(username) is None:
        raise HTTPException(status_code=404, detail=f"member {username!r} not found")
    keys = [k for k in store.list_llm_keys(username) if k.id == key_id]
    if not keys:
        raise HTTPException(status_code=404, detail="LLM key not found")
    current_key = keys[0]

    normalized_model = None
    if req.model is not None:
        model_str = req.model.strip()
        if not model_str:
            raise HTTPException(status_code=422, detail="model cannot be empty")
        normalized_model = _normalize_model(current_key.provider, model_str)

    try:
        record = store.update_llm_key(
            username,
            key_id,
            model=normalized_model,
            base_url=req.base_url,
            active=req.active,
            actor=claims["sub"],
        )
    except MemberError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return record.to_public()


@app.delete("/api/members/{username}/llm-keys/{key_id}")
def delete_member_llm_key(
    username: str,
    key_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    store = get_store()
    try:
        store.delete_llm_key(username, key_id, actor=claims["sub"])
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"status": "deleted", "id": key_id}


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


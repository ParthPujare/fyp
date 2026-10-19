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

import inspect
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.checkpoints import is_own_thread, make_checkpointer
from app.agent.routing import entry_allowlist_eligible
from app.agent.runner import resume_agent, run_state_is_local
from app.auth.jwt import AuthError, verify_token
from app.auth.store import (
    APPROVAL_MODE_HEAD_ONLY,
    DepartmentError,
    DepartmentExistsError,
    LockoutGuardError,
    MemberError,
    MemberExistsError,
)
from app.auth.users import USERS, get_store, is_active_member
from app.config import get_settings
from app.harness.audit import open_audit_log
from app.harness.enforcer import PolicyEnforcer
from app.harness.policy_store import FilePolicyStore
from app.harness.sanitize import MASK
from app.harness.url_policy import UrlPolicyError, validate_outbound_url
from app.llm.base import strip_routing_prefix
from app.llm.registry import PROVIDERS, is_supported, normalize_provider_type
from app.memory.approval_flow import (
    DecisionError,
    apply_decision,
    check_decision,
    decision_response,
    is_head,
)
from app.memory.approvals import LEVEL_ADMIN, LEVEL_HEAD, ROUTE_HEAD_ADMIN, ApprovalStore
from app.memory.edit import MemoryEditor
from app.memory.factory import open_approval_store, open_base_store, open_episodic_store
from app.memory.recall import RecallService
from app.memory.scopes import (
    SCOPE_CENTRAL,
    namespace_to_scope,
    scope_dept,
    scope_to_namespace,
    scope_user,
)
from app.ops.factory import open_trace_store
from app.ops.store import TraceStore
from app.skills.model import SkillValidationError, parse_skill_document, render_skill
from app.skills.registry import SkillRegistry

settings = get_settings()

@asynccontextmanager
async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
    """Shared DB (Mk13): build the stores at boot rather than on the first request."""
    from app.db import is_enabled

    if is_enabled():
        _get_components()["skills"].load_all()  # Phase 6: seed-if-empty + cache the shared skills
        get_store()
    yield


app = FastAPI(title="harness dashboard", version="0.1.0", lifespan=_lifespan)

STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.middleware("http")
async def _cookie_session(request: Request, call_next):
    """Mk18 Phase D: cookie sessions + CSRF gate.

    - No `Authorization` header but a valid session cookie → inject it as
      Bearer so every verifier works unchanged; mark `request.state.auth_via`.
    - Cookie-authenticated mutating requests need `X-CSRF-Token` (derived per
      sub, see `app/harness/csrf.py`). Bearer API clients are exempt.
    """
    from app.harness.cookies import active_cookie, slot_cookie
    from app.harness.csrf import MUTATING_METHODS, csrf_exempt_path, csrf_valid

    headers = {k.lower(): v for k, v in request.headers.items()}
    via = "bearer" if headers.get("authorization", "").lower().startswith("bearer ") else None
    if via is None:
        # Mk18 Phase D: active dual-slot cookie (XSS-proof session).
        active = request.cookies.get(active_cookie(settings.jwt.cookie_name), "")
        if active in ("admin", "member"):
            cookie = request.cookies.get(slot_cookie(settings.jwt.cookie_name, active), "")
            if cookie:
                try:
                    claims = verify_token(cookie, settings.jwt.secret, settings.jwt.algorithm)
                    via = "cookie"
                    request.scope["headers"] = [
                        (k, v) for k, v in request.scope.get("headers", [])
                        if k.lower() != b"authorization"
                    ] + [(b"authorization", f"Bearer {cookie}".encode("latin-1"))]
                    request.state.csrf_sub = str(claims.get("sub", ""))
                    request.state.csrf_ver = int(claims.get("cred_ver", 0) or 0)
                except Exception:  # noqa: BLE001
                    via = None
    request.state.auth_via = via or "none"
    if (
        via == "cookie"
        and request.method.upper() in MUTATING_METHODS
        and not csrf_exempt_path(request.url.path)
    ):
        sub = getattr(request.state, "csrf_sub", "")
        ver = getattr(request.state, "csrf_ver", 0)
        if not csrf_valid(headers.get("x-csrf-token"), sub, settings.jwt.secret, ver):
            return JSONResponse({"detail": "missing or invalid CSRF token"}, status_code=403)
    return await call_next(request)


_components: dict[str, Any] | None = None


def _get_components() -> dict[str, Any]:
    """Persistent shared components — same files the gateway uses."""
    global _components
    if _components is None:
        _store = open_base_store(settings.services.memory_path)
        _components = {
            "trace": open_trace_store(settings.services.data_dir / "trace.sqlite"),
            "enforcer": PolicyEnforcer(),
            "audit": open_audit_log(settings.services.audit_path),
            "store": _store,
            "episodic": open_episodic_store(settings.services.memory_path),
            "approvals": open_approval_store(settings.services.memory_path),
            "checkpointer": make_checkpointer(settings),
            "skills": SkillRegistry.open(settings.skills.skills_dir),
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


def _verify_dept_head(authorization: str | None) -> dict[str, Any]:
    """Verify a department-head JWT (Mk12): valid + active + `{dept}_head` role.

    The role must own its own dept (`marketing_head` ⟺ `dept == marketing`);
    stale/mismatched tokens are rejected so a moved member keeps no head power.
    """
    from app.harness.enforcer import head_role_for

    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    token = authorization.split(" ", 1)[1]
    try:
        claims = verify_token(token, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if not is_active_member(str(claims.get("sub", ""))):
        raise HTTPException(status_code=403, detail="account disabled or not a member")
    role, dept = str(claims.get("role", "")), str(claims.get("dept", ""))
    if not role.endswith("_head") or role != head_role_for(dept):
        raise HTTPException(status_code=403, detail="only department heads may access this API")
    return claims


def _active_head_count(dept: str) -> int:
    """Active `{dept}_head` members owning this dept (Mk12 escalation check)."""
    return get_store().active_head_count(dept)


def _verify_admin_or_head(authorization: str | None) -> tuple[dict[str, Any], bool]:
    """(claims, is_admin) for the surfaces admins and department heads share (Mk15).

    A head gets their own department's slice; everyone else is refused."""
    claims, _token = _verified_member(authorization)
    if claims.get("role") == "admin":
        return claims, True
    if is_head(claims):
        return claims, False
    raise HTTPException(status_code=403, detail="only admins and department heads may access this API")


RESUME_ELSEWHERE = (
    "This run's saved state was not found (was the chat deleted?), so it cannot be resumed."
)


def _resumable_here(entry: dict[str, Any]) -> bool:
    """Only meaningful on the shared database, where approvals can come from other machines."""
    from app.db import is_enabled

    if not is_enabled():
        return True
    return run_state_is_local(_get_components()["checkpointer"], entry["thread_id"])


def _approval_impact(item: dict[str, Any]) -> str:
    """Mk18 Phase B: non-secret impact summary for approval cards.

    Cards keep `***` masking; this line gives deciders signal (amounts,
    domains, lengths) without exposing raw secrets. Full args live behind
    `GET /api/approvals/{id}/args` (privileged, audit-logged).
    """
    resource = item.get("resource") or {}
    payload = item.get("payload") or {}
    if resource.get("type") == "memory":
        text = str(payload.get("text", "") or "")
        return f"{resource.get('id', '?')}: {payload.get('op', 'add')} {len(text)} chars"
    args = payload.get("args") or {}
    name = str(payload.get("tool", resource.get("id", "?")))
    if name == "finance_transfer":
        masked = sum(1 for k in ("memo", "from_account", "to_account") if k in args)
        return f"transfer amount={args.get('amount', '?')} ({masked} masked fields)"
    if name.startswith("gmail_"):
        to = str(args.get("to", ""))
        domain = to.split("@")[-1] if "@" in to else "?"
        return (
            f"to domain {domain}, subject {len(str(args.get('subject', '')))} chars, "
            f"body {len(str(args.get('body', '')))} chars"
        )
    return f"{name}: {len(args)} args"


def _approval_view(item: dict[str, Any]) -> dict[str, Any]:
    dept = ApprovalStore.dept_of(item)
    status = item["status"]
    level = ApprovalStore.level_of(item)
    route = ApprovalStore.route_of(item)
    is_memory = (item.get("resource") or {}).get("type") == "memory"
    return {
        "id": item["id"],
        "run_id": item["run_id"],
        "thread_id": item["thread_id"],
        "resource": item["resource"],
        "actor": item["actor"],
        "payload": _mask_payload(item["payload"]),
        "status": status,
        "created_ts": item["created_ts"],
        "decided_ts": item.get("decided_ts"),
        "decided_by": item.get("decided_by"),
        "resumed_by": item.get("resumed_by"),
        "resumed_at": item.get("resumed_at"),
        # Decided but not yet resumed on the owner's machine (Mk14): the owner
        # polls this and resumes locally so their own model/keys are used.
        "needs_resume": status in ("approved", "rejected") and not item.get("resumed_by"),
        "tier": "dept" if dept is not None and is_memory else item["resource"].get("type", "memory"),
        "dept": dept,
        "resumable_here": _resumable_here(item),
        # Mk15: which step it waits on (head | admin | done), its creation-time route, and
        # the decisions so far ({level, by, at, decision, comment, always_allow}).
        "level": level,
        "route": route,
        "total_steps": 2 if route == ROUTE_HEAD_ADMIN else 1,
        "steps": item.get("steps") or [],
        "action": item.get("action"),
        "needs_admin": status == "pending" and level == LEVEL_ADMIN,
        "allowlist_eligible": entry_allowlist_eligible(item),
        "impact": _approval_impact(item),
    }


@app.get("/api/approvals/{approval_id}/args")
def approval_args(
    approval_id: str,
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Mk18 Phase B: privileged full-args view for deciders (audit-logged).

    Admins may view any item; heads only their own department's. Every view is
    audit-logged as `approval:args_view` so blind-signing pressure leaves a trail.
    """
    claims, is_admin = _verify_admin_or_head(authorization)
    entry = approvals.get(approval_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    if not is_admin:
        dept = ApprovalStore.dept_of(entry)
        if dept is None or dept != str(claims.get("dept", "")):
            raise HTTPException(status_code=403, detail="not your department's approval")
    _get_components()["audit"].log(
        decision="ALLOW",
        resource={"type": "approval-args", "id": approval_id},
        actor={"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]},
        run_id=entry.get("run_id", "dashboard"),
        reason="approval:args_view",
    )
    return {"approval_id": approval_id, "payload": entry.get("payload")}


def _decide_or_http_error(approvals: ApprovalStore, approval_id: str, approve: bool, decider: str) -> None:
    """Record a terminal decision; a lost race with another decider is a 409, not a 500."""
    try:
        approvals.decide(approval_id, approve, decider)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


def _decide(
    approval_id: str,
    req: DecideRequest,
    claims: dict[str, Any],
    *,
    as_level: str,
    defer: bool,
    resumer: Any,
    approvals: ApprovalStore,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """Shared body of the admin and dept decide routes (Mk14 defer + Mk15 steps).

    A lost race with another decider is a 409, not a 500. A head's step-1 approve on a
    `head+admin` item records the step and stops: the item moves to the admin queue and
    nothing resumes until the admin step is decided.
    """
    try:
        entry = check_decision(
            approvals,
            approval_id,
            claims,
            approve=req.approve,
            as_level=as_level,
            always_allow=req.always_allow,
        )
        hands_to_admin = (
            as_level == LEVEL_HEAD and req.approve and ApprovalStore.route_of(entry) == ROUTE_HEAD_ADMIN
        )
        if not defer and not hands_to_admin and not _resumable_here(entry):
            raise HTTPException(status_code=409, detail=RESUME_ELSEWHERE)
        decided = apply_decision(
            approvals,
            get_store(),
            approval_id,
            claims,
            approve=req.approve,
            as_level=as_level,
            always_allow=req.always_allow,
            comment=req.comment,
        )
    except DecisionError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    if defer or decided["status"] == "pending":
        return decision_response(decided)
    result = _call_dashboard_resumer(resumer, approval_id, auth_token)
    decided = approvals.get(approval_id)
    return {
        "approval_id": approval_id,
        "status": decided["status"],
        "thread_id": result.thread_id,
        "reply": result.reply,
        "interrupted": result.interrupted,
        "next_approval_id": result.approval_id if result.interrupted else None,
    }


def get_resumer():
    def _resume(approval_id: str, auth_token: str | None = None):
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
            auth_token=auth_token,
        )

    return _resume


def get_gateway_sender():
    """Forward a verified member chat to the gateway (SEAM: tests override this)."""

    def _send(token: str, message: str, thread_id: str | None, key_id: str | None = None) -> dict:
        try:
            resp = httpx.post(
                f"{settings.services.gateway_url}/chat",
                json={"token": token, "message": message, "thread_id": thread_id, "key_id": key_id},
                # Local reasoning models (Gemma via LM Studio) take minutes per
                # turn (agent + memory-extract calls); don't cut them off at 60s.
                timeout=300.0,
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


# Approval cards are seen only by the deciders (admins, the department head) and the
# requester. Mk20: they must be able to read WHAT they approve — the recipient, subject
# and a preview of the message — while account numbers and memos stay masked. The preview
# is capped and has SSNs / card numbers masked.
_APPROVER_MASKED_ARGS = ("memo", "from_account", "to_account")
_APPROVER_PREVIEW_ARGS = ("body",)
_PREVIEW_CHARS = 280
_CARD_RE = re.compile(r"\b(?:\d[ -]*?){13,19}\b")
_SSN_PREVIEW_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


def _preview(value: Any) -> str:
    text = str(value)
    text = _CARD_RE.sub(MASK, _SSN_PREVIEW_RE.sub(MASK, text))
    return text if len(text) <= _PREVIEW_CHARS else text[:_PREVIEW_CHARS] + "…"


def _mask_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Approver view of a gated call's args: secrets masked, the rest readable."""
    if "args" in payload and isinstance(payload["args"], dict):
        args = dict(payload["args"])
        for key in _APPROVER_MASKED_ARGS:
            if key in args:
                args[key] = MASK
        for key in _APPROVER_PREVIEW_ARGS:
            if key in args:
                args[key] = _preview(args[key])
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
    always_allow: bool = False  # Mk15: also allow this exact action for this member from now on
    comment: str | None = None


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
def login(req: LoginRequest, response: Response) -> dict:
    """Proxy the mock auth service login so the browser never holds credentials.

    Mk18 Phase D: forwards the auth service's session cookie to the browser
    (httpOnly) while keeping the JSON body for API clients.
    """
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
    body = resp.json()
    # Mk18 Phase D: forward the auth service's session cookies (dual slots +
    # active marker) so the browser session is httpOnly.
    for raw in resp.headers.get_list("set-cookie", []):
        response.headers.append("set-cookie", raw)
    return body


@app.post("/api/logout")
def logout(response: Response) -> dict:
    """Mk18 Phase D: clear the browser session cookies (tokens are stateless)."""
    from app.harness.cookies import active_cookie, slot_cookie

    for name in (
        slot_cookie(settings.jwt.cookie_name, "admin"),
        slot_cookie(settings.jwt.cookie_name, "member"),
        active_cookie(settings.jwt.cookie_name),
    ):
        response.delete_cookie(key=name, path="/")
    return {"logged_out": True}


class SessionSwitchRequest(BaseModel):
    label: str  # "admin" | "member"


@app.post("/api/session/switch")
def switch_session(req: SessionSwitchRequest, request: Request, response: Response) -> dict:
    """Mk20: make the identity-switcher pill decide who the browser acts as.

    After a reload the tokens live only in the httpOnly slot cookies, and the server
    picks the slot named by `<cookie>_active`. Switching pills used to change only the
    page, so requests kept going out as whoever signed in last. This flips the active
    marker to a slot the browser already holds a valid session for — nothing else.
    """
    from app.harness.cookies import active_cookie, slot_cookie

    label = (req.label or "").strip()
    if label not in ("admin", "member"):
        raise HTTPException(status_code=422, detail="label must be 'admin' or 'member'")
    slot = request.cookies.get(slot_cookie(settings.jwt.cookie_name, label), "")
    if not slot:
        raise HTTPException(status_code=409, detail=f"no {label} session in this browser")
    try:
        claims = verify_token(slot, settings.jwt.secret, settings.jwt.algorithm)
    except AuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    response.set_cookie(
        key=active_cookie(settings.jwt.cookie_name),
        value=label,
        httponly=False,
        secure=settings.jwt.cookie_secure,
        samesite=settings.jwt.cookie_samesite,  # type: ignore[arg-type]
        path="/",
        max_age=settings.jwt.expiry_minutes * 60,
    )
    return {"active": label, "sub": claims.get("sub")}


@app.get("/api/csrf")
def csrf_token(authorization: str | None = Header(default=None)) -> dict:
    """Mk18 Phase D: derived per-session CSRF token for cookie browsers.

    Works for Bearer callers too (harmless); cookie sessions are injected as
    Bearer by middleware before reaching here.
    """
    from app.harness.csrf import csrf_token_for

    claims, _token = _verified_member(authorization)
    return {
        "csrf_token": csrf_token_for(
            str(claims.get("sub", "")), settings.jwt.secret, int(claims.get("cred_ver", 0) or 0)
        )
    }


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
    claims, token = _verified_member(authorization)
    # Mk19 Phase 1 (IDOR fix): owner-only thread access, enforced here as well as at
    # the gateway so the browser-facing seam can never forward a foreign thread_id.
    if req.thread_id is not None and not is_own_thread(req.thread_id, str(claims.get("sub", ""))):
        raise HTTPException(status_code=403, detail="not your thread")
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
    if not is_own_thread(thread_id, str(claims.get("sub", ""))):
        raise HTTPException(status_code=403, detail="not your thread")
    return {"thread_id": thread_id, "messages": _thread_messages_local(checkpointer, thread_id)}


@app.delete("/api/chat/threads/{thread_id:path}")
def delete_own_thread(
    thread_id: str,
    checkpointer: Annotated[Any, Depends(get_dashboard_checkpointer)],
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> dict:
    """Delete one of the caller's own chats (its checkpointed conversation).

    Approvals still pending for that thread are rejected without resuming — a
    resume needs the checkpoint that is being removed. Team episodic memory
    (`recall`) is deliberately left alone: it is shared, not per-chat.
    """
    claims, _token = _verified_member(authorization)
    username = str(claims.get("sub", ""))
    if not is_own_thread(thread_id, username):
        raise HTTPException(status_code=403, detail="not your thread")
    rejected = 0
    for item in approvals.pending_items():
        if item["thread_id"] == thread_id:
            try:
                approvals.decide(item["id"], False, f"system:chat-deleted:{username}")
                rejected += 1
            except (KeyError, ValueError):  # decided by someone else meanwhile
                pass
    try:
        checkpointer.delete_thread(thread_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"could not delete chat: {exc}") from exc
    return {"thread_id": thread_id, "deleted": True, "approvals_rejected": rejected}


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


def _bearer_token(authorization: str | None) -> str | None:
    """Raw Bearer token from a verified header (Mk18: forwarded for dept-service
    passthrough). Returns None when absent — verifiers already rejected those."""
    if not authorization or not authorization.lower().startswith("bearer "):
        return None
    return authorization.split(" ", 1)[1].strip() or None


def _call_dashboard_resumer(resumer: Any, approval_id: str, auth_token: str | None = None):
    """Invoke a resumer, passing `auth_token` only if it accepts it; map a
    spend-cap stop to HTTP 429 (Mk18 Phase H). Test doubles stay single-arg."""
    from app.llm.metered import SpendCapExceeded as _SpendCapExceeded

    try:
        try:
            params = inspect.signature(resumer).parameters
        except (TypeError, ValueError):
            params = {}
        if "auth_token" in params or any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
        ):
            return resumer(approval_id, auth_token=auth_token)
        return resumer(approval_id)
    except _SpendCapExceeded as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc


@app.get("/api/approvals/pending")
def pending_approvals(
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    """Admin queue: central + admin-only tool gates + ESCALATIONS only (Mk12, Mk15).

    A department item reaches an admin in exactly two ways: (i) it is at its admin step —
    a `head+admin` item whose head already approved ("Step 1 ✓"), or one created while the
    department had no active head; (ii) it still waits on a head but the department has
    none left. Head-only items a head can decide never appear here (audit/Ops only).
    """
    _verify_admin(authorization)
    out: list[dict[str, Any]] = []
    for item in approvals.pending_items():
        dept = ApprovalStore.dept_of(item)
        if (
            dept is not None
            and ApprovalStore.level_of(item) == LEVEL_HEAD
            and _active_head_count(dept) > 0
        ):
            continue
        out.append(_approval_view(item))
    return out


@app.get("/api/dept/approvals/pending")
def pending_dept_approvals(
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    """A head's own-dept queue (step 1) — separate from the admin queue (Mk12, Mk15)."""
    claims = _verify_dept_head(authorization)
    return [_approval_view(i) for i in approvals.pending_for_dept(str(claims.get("dept", "")))]


@app.post("/api/approvals/{approval_id}/decide")
def decide_approval(
    approval_id: str,
    req: DecideRequest,
    resumer: Annotated[Any, Depends(get_resumer)],
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
    defer: bool = Query(default=False),
) -> dict[str, Any]:
    """Admin decision — always terminal (Mk14: `?defer=1` records it without resuming).

    Deferred mode is the org path: any admin may approve, and the originator's
    own machine later resumes via `POST .../resume` so their model/keys run.
    Coupled mode (`defer=0`, default) keeps single-PC behavior unchanged.
    An item a head rejected is already `rejected`: deciding it again is a 409 (Mk15:
    a head reject is final, an admin cannot revive it).
    """
    claims = _verify_admin(authorization)
    return _decide(
        approval_id, req, claims, as_level=LEVEL_ADMIN, defer=defer, resumer=resumer, approvals=approvals,
        auth_token=_bearer_token(authorization),
    )


@app.post("/api/dept/approvals/{approval_id}/decide")
def decide_dept_approval(
    approval_id: str,
    req: DecideRequest,
    resumer: Annotated[Any, Depends(get_resumer)],
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
    defer: bool = Query(default=False),
) -> dict[str, Any]:
    """A head decides step 1 of an item from their OWN dept queue (Mk12 + Mk14 + Mk15).

    Cross-dept decides are 403; an admin deciding an escalated dept item must use
    the admin endpoint. Any single head's decision resolves (multiple heads allowed).
    On a `head+admin` item an approve hands it to the admin queue (`next_level=admin`,
    nothing resumes yet); a reject is final.
    """
    claims = _verify_dept_head(authorization)
    return _decide(
        approval_id, req, claims, as_level=LEVEL_HEAD, defer=defer, resumer=resumer, approvals=approvals,
        auth_token=_bearer_token(authorization),
    )


def _resume_owned(
    approval_id: str,
    claims: dict[str, Any],
    resumer: Any,
    approvals: ApprovalStore,
    auth_token: str | None = None,
) -> dict[str, Any]:
    """Owner-only resume shared by the admin and dept resume routes (Mk14)."""
    entry = approvals.get(approval_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    actor = entry.get("actor") or {}
    if str(actor.get("sub", "")) != str(claims.get("sub", "")):
        raise HTTPException(status_code=403, detail="only the requester may resume this run")
    if entry["status"] not in ("approved", "rejected"):
        raise HTTPException(status_code=409, detail=f"approval {approval_id!r} has not been decided")
    if not _resumable_here(entry):
        raise HTTPException(status_code=409, detail=RESUME_ELSEWHERE)
    try:
        result = _call_dashboard_resumer(resumer, approval_id, auth_token)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc.args[0])) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    decided = approvals.get(approval_id)
    return {
        "approval_id": approval_id,
        "status": decided["status"],
        "thread_id": result.thread_id,
        "reply": result.reply,
        "interrupted": result.interrupted,
        "next_approval_id": result.approval_id if result.interrupted else None,
    }


@app.post("/api/approvals/{approval_id}/resume")
def resume_approval(
    approval_id: str,
    resumer: Annotated[Any, Depends(get_resumer)],
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Resume a decided approval on the requester's own machine (Mk14).

    Any active member may call this, but only for approvals whose `actor` is
    themselves — this is what keeps per-member models/keys local when the
    decision came from another PC. Wait-for-owner: no admin fallback.
    """
    claims, _token = _verified_member(authorization)
    return _resume_owned(approval_id, claims, resumer, approvals, auth_token=_bearer_token(authorization))


@app.post("/api/dept/approvals/{approval_id}/resume")
def resume_dept_approval(
    approval_id: str,
    resumer: Annotated[Any, Depends(get_resumer)],
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Dept-scoped twin of the owner resume (Mk14): owner + own dept required."""
    claims, _token = _verified_member(authorization)
    entry = approvals.get(approval_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"approval {approval_id!r} not found")
    dept = ApprovalStore.dept_of(entry)
    if dept is not None and dept != str(claims.get("dept", "")):
        raise HTTPException(status_code=403, detail="not your department's approval")
    return _resume_owned(approval_id, claims, resumer, approvals, auth_token=_bearer_token(authorization))


@app.get("/api/approvals/by-thread/{thread_id:path}")
def approvals_by_thread(
    thread_id: str,
    approvals: Annotated[ApprovalStore, Depends(get_approval_store)],
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    """Decided-or-pending approvals for one thread (Mk14 owner poll).

    Owners see their own threads; admins see any thread. Powers auto-resume
    without scanning the full pending queues.
    """
    claims, _token = _verified_member(authorization)
    username = str(claims.get("sub", ""))
    is_admin = str(claims.get("role", "")) == "admin"
    if not is_admin and not is_own_thread(thread_id, username):
        raise HTTPException(status_code=403, detail="not your thread")
    return [_approval_view(i) for i in approvals.for_thread(thread_id)]


@app.get("/api/users")
def users(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _verify_admin(authorization)
    store = get_store()
    out = []
    for m in store.list():
        pub = m.to_public()
        try:
            active_key = store.get_active_llm_key(m.username)
        except HTTPException:
            # One stale token (rotated LLM__FERNET_KEY) must not break the whole
            # members listing — report metadata with a stale flag so the key can
            # be re-saved from the Account tab.
            meta = store.get_active_llm_key_meta(m.username)
            pub["active_llm_key"] = (
                {"provider": meta[0], "model": meta[1], "stale": True} if meta else None
            )
        else:
            pub["active_llm_key"] = (
                {"provider": active_key[0], "model": active_key[1]} if active_key else None
            )
        out.append(pub)
    return out


class MemberCreateRequest(BaseModel):
    username: str
    password: str
    role: str
    dept: str
    scopes: list[str] = []
    display_name: str | None = None
    email: str | None = None


class MemberUpdateRequest(BaseModel):
    role: str
    dept: str
    scopes: list[str] = []
    display_name: str | None = None


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
    username = (req.email or req.username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="email is required")
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
            display_name=req.display_name,
        ).to_public()
    except MemberExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemberError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
            display_name=req.display_name,
        ).to_public()
    except LockoutGuardError:
        raise
    except MemberExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemberError as exc:
        msg = str(exc)
        if "head role" in msg or "must match" in msg or "must belong" in msg or "valid email" in msg or "unknown role" in msg:
            raise HTTPException(status_code=422, detail=msg) from exc
        raise HTTPException(status_code=404, detail=msg) from exc


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


# ---- Department head: roster + scoped member management (Mk15) ----


class DeptMemberCreateRequest(BaseModel):
    username: str
    password: str
    role: str = ""  # blank = the department's member role; head/admin roles are refused
    display_name: str | None = None
    email: str | None = None


class DeptMemberUpdateRequest(BaseModel):
    active: bool | None = None
    password_reset: str | None = None


def _dept_member_role(dept: str) -> str:
    rec = get_store().get_department(dept)
    return (rec.role if rec is not None and rec.role else "") or f"{dept}_agent"


def _dept_member_view(m: Any) -> dict[str, Any]:
    return {"username": m.username, "role": m.role, "active": m.active, "created_at": m.created_at}


@app.get("/api/dept/members")
def dept_members(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    """A head's own-department roster, read from the store."""
    claims = _verify_dept_head(authorization)
    dept = str(claims.get("dept", ""))
    return [_dept_member_view(m) for m in get_store().list() if m.dept == dept]


@app.post("/api/dept/members")
def create_dept_member(
    req: DeptMemberCreateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """A head adds a member to their OWN department with the department's member role.

    Heads cannot mint heads or admins (403) and cannot choose another department: the
    dept comes from the head's verified claims, never from the body."""
    claims = _verify_dept_head(authorization)
    dept = str(claims.get("dept", ""))
    username = (req.email or req.username or "").strip()
    if not username:
        raise HTTPException(status_code=422, detail="email is required")
    if len(req.password or "") < 6:
        raise HTTPException(status_code=422, detail="password must be at least 6 characters")
    member_role = _dept_member_role(dept)
    role = (req.role or "").strip() or member_role
    if role != member_role:
        raise HTTPException(
            status_code=403,
            detail=f"a department head can only add members with the role {member_role!r}",
        )
    store = get_store()
    # Same scopes as an existing colleague with that role (a head cannot hand out new ones).
    peer = next((m for m in store.list() if m.dept == dept and m.role == role), None)
    try:
        member = store.create(
            username,
            req.password,
            role=role,
            dept=dept,
            scopes=list(peer.scopes) if peer is not None else [],
            actor=claims["sub"],
            display_name=req.display_name,
        )
    except MemberExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except MemberError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _dept_member_view(member)


@app.patch("/api/dept/members/{username}")
def update_dept_member(
    username: str,
    req: DeptMemberUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """A head (de)activates or resets the password of a member of their OWN department.

    No role/dept changes (a head cannot promote), and never another head, an admin, or
    themselves — those stay with the admin."""
    claims = _verify_dept_head(authorization)
    dept = str(claims.get("dept", ""))
    store = get_store()
    target = store.get(username)
    if target is None:
        raise HTTPException(status_code=404, detail=f"member {username!r} not found")
    if target.dept != dept:
        raise HTTPException(status_code=403, detail="not a member of your department")
    if target.role == "admin" or target.role.endswith("_head") or target.username == claims["sub"]:
        raise HTTPException(status_code=403, detail="heads and admins are managed by an admin")
    if req.active is None and req.password_reset is None:
        raise HTTPException(status_code=422, detail="nothing to change: send active and/or password_reset")
    if req.password_reset is not None and len(req.password_reset) < 6:
        raise HTTPException(status_code=422, detail="password must be at least 6 characters")
    try:
        if req.password_reset is not None:
            target = store.reset_password(username, req.password_reset, actor=claims["sub"])
        if req.active is not None:
            target = store.set_active(username, req.active, actor=claims["sub"])
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _dept_member_view(target)


# ---- Approve-once allowlist (Mk15, todo #11) ----


@app.get("/api/allowlist")
def list_allowlist(
    authorization: str | None = Header(default=None),
    dept: str | None = Query(default=None),
    member: str | None = Query(default=None),
) -> list[dict[str, Any]]:
    """"Always allowed" rules: admins see all (optional `dept` / `member` filter), a head
    sees their own department's."""
    claims, is_admin = _verify_admin_or_head(authorization)
    rules = get_store().allowlist_list(dept if is_admin else str(claims.get("dept", "")))
    if member:
        rules = [r for r in rules if r["member"] == member]
    return rules


@app.delete("/api/allowlist/{member}/{action:path}/{args_hash}")
def revoke_allowlist(
    member: str,
    action: str,
    args_hash: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Revoke a rule: the member's next identical request needs approval again.
    Admins revoke any rule, a head only their own department's."""
    claims, is_admin = _verify_admin_or_head(authorization)
    store = get_store()
    rule = store.allowlist_get(member, action, args_hash)
    if rule is None:
        raise HTTPException(status_code=404, detail="allowlist rule not found")
    if not is_admin and rule["dept"] != str(claims.get("dept", "")):
        raise HTTPException(status_code=403, detail="not your department's rule")
    store.allowlist_revoke(member, action, args_hash, actor=claims["sub"])
    return {"member": member, "action": action, "args_hash": args_hash, "revoked": True}


# ---- Second eyes (Mk18 Phase E) ----


def _second_eyes_required(claims: dict[str, Any]) -> bool:
    """True when the caller is an admin and a second admin exists to confirm.

    Single-admin deployments (and non-admin callers, who never reach the gated
    writes) proceed directly with an audited fallback note.
    """
    return str(claims.get("role", "")) == "admin" and get_store().count_active_admins() >= 2


def _apply_second_eyes(kind: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
    """Apply a confirmed proposal (or a single-admin direct write). Raises
    MemberError/ValueError/SkillValidationError on stale targets — the confirm
    endpoint maps those to 404/422/409."""
    from app.auth.store import MemberError as _ME

    store = get_store()
    if kind == "policy_rows":
        rows = [tuple(r) for r in payload.get("rows", [])]
        if not rows:
            raise _ME("proposal has no policy rows")
        _policy_store().add_rows(rows, actor)  # type: ignore[arg-type]
        return {"rows_added": len(rows)}
    if kind == "skill_save":
        skill = parse_skill_document(payload.get("content", ""))
        path = _get_components()["skills"].save(skill, actor=actor)
        _policy_store().add_rows([("p", "admin", "admin", skill.policy_obj, "use")], actor)
        return {"name": skill.name, "path": str(path), "saved": True}
    if kind == "skill_delete":
        name = str(payload.get("name", ""))
        if not _get_components()["skills"].delete(name):
            raise _ME(f"skill {name!r} not found")
        return {"name": name, "deleted": True}
    if kind == "llm_key_write":
        from app.auth.crypto import decrypt_secret

        _validated_base_url(str(payload.get("base_url", "")))  # Mk19 Phase 2 (SSRF)
        record = store.set_llm_key(
            str(payload["username"]),
            str(payload["provider"]),
            str(payload["model"]),
            decrypt_secret(str(payload["api_key_enc"])),
            active=bool(payload.get("active", True)),
            actor=actor,
            base_url=str(payload.get("base_url", "")),
        )
        return record.to_public()
    if kind == "llm_key_delete":
        store.delete_llm_key(str(payload["username"]), str(payload["key_id"]), actor=actor)
        return {"username": payload["username"], "key_id": payload["key_id"], "deleted": True}
    if kind == "platform_key":
        if payload.get("clear"):
            removed = store.clear_platform_llm(actor=actor)
        else:
            from app.auth.crypto import decrypt_secret

            enc = str(payload.get("api_key_enc") or "")
            store.set_platform_llm(
                model=str(payload.get("model", "")),
                api_key_plaintext=decrypt_secret(enc) if enc else None,
                actor=actor,
            )
            removed = False
        from app.llm.system import clear_system_provider_cache

        clear_system_provider_cache()
        return {"configured": True, "cleared": bool(payload.get("clear", False)), "removed": removed}
    if kind == "allowlist_rule":
        rule = store.allowlist_add(
            str(payload["member"]),
            str(payload["action"]),
            str(payload["args_hash"]),
            dict(payload.get("args") or {}),
            str(payload["dept"]),
            actor=actor,
        )
        return {"allowlist_rule": rule}
    raise _ME(f"unknown second-eyes kind {kind!r}")


@app.get("/api/second-eyes/pending")
def second_eyes_pending(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    """Admin queue of staged high-impact writes awaiting a second admin."""
    _verify_admin(authorization)
    proposals = get_store().second_eyes_pending()
    # Never leak encrypted secrets to the queue view.
    redacted = []
    for p in proposals:
        payload = dict(p.get("payload") or {})
        for key in ("api_key_enc",):
            if key in payload:
                payload[key] = "***"
        redacted.append({**p, "payload": payload})
    return redacted


@app.post("/api/second-eyes/{proposal_id}/confirm")
def second_eyes_confirm(
    proposal_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """A *different* admin confirms a staged write; it is applied now."""
    claims = _verify_admin(authorization)
    store = get_store()
    proposal = store.second_eyes_get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"proposal {proposal_id!r} not found")
    if proposal["status"] != "pending":
        raise HTTPException(status_code=409, detail=f"proposal {proposal_id!r} already {proposal['status']}")
    if proposal["proposed_by"] == str(claims.get("sub", "")):
        raise HTTPException(status_code=409, detail="a second admin must confirm (proposer cannot self-confirm)")
    try:
        result = _apply_second_eyes(proposal["kind"], proposal["payload"], str(claims.get("sub", "")))
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        from app.auth.store import MemberError as _ME

        if isinstance(exc, _ME):
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        raise
    try:
        store.second_eyes_decide(proposal_id, str(claims.get("sub", "")), True)
    except Exception as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"proposal_id": proposal_id, "status": "approved", "applied": result}


@app.post("/api/second-eyes/{proposal_id}/reject")
def second_eyes_reject(
    proposal_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """A *different* admin rejects a staged write; nothing is applied."""
    claims = _verify_admin(authorization)
    try:
        decided = get_store().second_eyes_decide(proposal_id, str(claims.get("sub", "")), False)
    except Exception as exc:
        from app.auth.store import MemberError as _ME

        if isinstance(exc, _ME):
            msg = str(exc)
            if "not found" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            raise HTTPException(status_code=409, detail=msg) from exc
        raise
    return {"proposal_id": proposal_id, "status": decided["status"]}


@app.get("/api/departments")
def departments(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    _verify_admin(authorization)
    store = get_store()
    members = store.list()  # store-sourced (one read): never stale on the shared database
    dept_roles = [d.name for d in store.list_departments()]
    # policy.csv `p` rows can still introduce/denote knowable objects per domain;
    # merge them so the resource list stays accurate even before registration.
    policy_objs: dict[str, list[str]] = {}
    for _sub, dom, obj, _act in _policy_store().rules().get("p", []):
        if obj != "*":
            policy_objs.setdefault(dom, [])
            if obj not in policy_objs[dom]:
                policy_objs[dom].append(obj)
    rows: list[dict[str, Any]] = []
    for name in sorted(set(dept_roles) | set(policy_objs)):
        rec = store.get_department(name)
        resources = list(policy_objs.get(name, []))
        if rec is not None:
            for r in rec.resources:
                if r not in resources:
                    resources.append(r)
        head_role = f"{name}_head"
        rows.append(
            {
                "name": name,
                "service": rec.service if rec is not None else "",
                "service_url": rec.service_url if rec is not None else "",
                "resources": sorted(resources),
                "members": [m.username for m in members if m.dept == name],
                "role": rec.role if rec is not None else "",
                "approval_mode": rec.approval_mode if rec is not None else APPROVAL_MODE_HEAD_ONLY,
                "head_count": sum(1 for m in members if m.active and m.dept == name and m.role == head_role),
                "registered": rec is not None,
            }
        )
    return rows


class DepartmentRegisterRequest(BaseModel):
    name: str
    service: str = ""
    service_url: str = ""
    resources: list[str] = []
    role: str = ""
    approval_mode: str = APPROVAL_MODE_HEAD_ONLY  # Mk15: head-only | head+admin


class DepartmentUpdateRequest(BaseModel):
    approval_mode: str


@app.post("/api/departments")
def register_department(
    req: DepartmentRegisterRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Register a new department (plan_mk2 §2.2 / §4 row 4). The department
    record lands in the store (replacing the hardcoded `_service_for` map) and the
    granular member + head `p` rows are added to the policy so the new roles are
    immediately grantable. Adding these rows is the one place the dashboard writes
    the policy — kept tiny + explicit."""
    claims = _verify_admin(authorization)
    store = get_store()
    name = (req.name or "").strip().lower()
    if not re.fullmatch(r"[a-z0-9_-]+", name or ""):
        raise HTTPException(status_code=422, detail="department name must be lowercase [a-z0-9_-]")
    role = (req.role or "").strip() or f"{name}_agent"
    service = (req.service or "").strip()
    resources = list(req.resources or [])
    try:
        store.register_department(
            name,
            service=service,
            service_url=(req.service_url or "").strip(),
            resources=resources,
            role=role,
            actor=claims["sub"],
            approval_mode=(req.approval_mode or APPROVAL_MODE_HEAD_ONLY).strip(),
        )
    except DepartmentExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except DepartmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Mk18 Phase E: the policy rows are the grant — gate them behind second
    # eyes when a second admin exists (the record alone grants nothing).
    rows = _policy_rows_for_dept(role, name, [service, *resources])

    def _apply() -> dict[str, Any]:
        _append_policy_row(role, name, actor=claims["sub"], objects=[service, *resources])
        rec = store.get_department(name)
        assert rec is not None
        return rec.to_public()

    if _second_eyes_required(claims):
        proposal = get_store().second_eyes_propose(
            "policy_rows", {"rows": rows, "dept": name}, str(claims.get("sub", ""))
        )
        return JSONResponse(
            status_code=202,
            content={
                "dept": store.get_department(name).to_public(),
                "proposal_id": proposal["id"],
                "needs_second_admin": True,
                "kind": "policy_rows",
            },
        )
    return _apply()


@app.patch("/api/departments/{name}")
def update_department(
    name: str,
    req: DepartmentUpdateRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Switch a department's approval mode: `head-only` ↔ `head+admin` (Mk15, admin only).

    Applies to items created from now on; an approval already in flight keeps the route
    it was created with."""
    claims = _verify_admin(authorization)
    store = get_store()
    if store.get_department(name) is None:
        raise HTTPException(status_code=404, detail=f"department {name!r} not found")
    try:
        return store.set_department_mode(name, (req.approval_mode or "").strip(), actor=claims["sub"]).to_public()
    except DepartmentError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# What a department role may do besides its own tools (mirrors the seeded depts' rows).
_BASE_ROLE_GRANTS: tuple[tuple[str, str], ...] = (
    ("memory_central", "read"),
    ("memory_dept", "read"),
    ("memory_dept", "write"),
    ("memory_user", "read"),
    ("memory_user", "write"),
    ("memory_recall", "use"),
    ("pii_email", "read"),
    ("skill_triage_incident", "use"),
)


def _policy_rows_for_dept(role: str, dept: str, objects: list[str] | None = None) -> list[list[str]]:
    """Row lists `_append_policy_row` would write (Mk18: also the proposal payload)."""
    from app.harness.enforcer import head_role_for

    tool_objs: list[str] = []
    for obj in objects or []:
        obj = (obj or "").strip().lower()
        if re.fullmatch(r"[a-z0-9_]+", obj) and obj not in tool_objs:
            tool_objs.append(obj)
    grants = [(obj, act) for obj in tool_objs for act in ("read", "create")] + list(_BASE_ROLE_GRANTS)
    rows = [["p", r, dept, obj, act] for r in (role, head_role_for(dept)) for obj, act in grants]
    rows += [["p", "admin", "admin", obj, act] for obj in tool_objs for act in ("read", "create")]
    return rows


def _append_policy_row(role: str, dept: str, actor: str = "", objects: list[str] | None = None) -> None:
    """Add the granular `p` rows for a newly registered department's member role and its
    Mk12 `{dept}_head` role — the same shape the seeded departments have: `read`/`create`
    on each of the department's own tool objects (`objects`: its service label and
    registered resources), plus dept/user memory, central read, recall, email PII and the
    base skill. (Before Mk15 this wrote `p, <role>, <dept>, *, *`, which also granted
    central-memory writes and every critical tool; rows written back then stay valid.)

    The enforcer's version guard notices the change (file mtime, or `members_version`
    on the shared database) and rebuilds on its next call, so the newly-registered role
    takes effect without a restart.

    Idempotent: an equivalent row already present is left alone. Casbin
    ignores duplicate rows, but an unconditional append grows the file without
    bound every time a department is re-registered.
    """
    _policy_store().add_rows(
        [tuple(r) for r in _policy_rows_for_dept(role, dept, objects)], actor  # type: ignore[arg-type]
    )


def _policy_has_row(row: tuple[str, ...]) -> bool:
    """True iff the policy already carries this row (whitespace-insensitive)."""
    return _policy_store().has_row(row)


def _policy_store() -> Any:
    """Where the dashboard reads/adds policy rows (Mk13 Phase 6): the shared database's
    `policy_rules` when `DATABASE_URL` is set (the enforcer's own store, so its cache
    and the dashboard's writes cannot disagree), else policy.csv via `_policy_csv()`."""
    from app.db import is_enabled

    if is_enabled():
        return _get_components()["enforcer"].policy
    return FilePolicyStore(_policy_csv)


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


def _skill_summary(skill: Any, granted: set[str]) -> dict[str, Any]:
    return {
        "name": skill.name,
        "scope": skill.scope,
        "owner_role": skill.owner_role,
        "version": skill.version,
        "trigger": skill.trigger,
        "steps": skill.steps,
        "tool_calls": skill.tool_calls,
        "path": skill.path,
        "policy_row": f"skill_{skill.name}" in granted,
    }


def _granted_skill_objs() -> set[str]:
    """`skill_<name>` objects with a non-admin `p, role, dom, skill_<name>, use` row
    (read once per request: on the shared database each read is a round trip)."""
    return {
        obj
        for sub, _dom, obj, _act in _policy_store().rules().get("p", [])
        if sub != "admin" and obj.startswith("skill_")
    }


@app.get("/api/skills")
def list_skills(
    authorization: str | None = Header(default=None),
) -> dict:
    """List all skills (the skills/ folder, or the shared database; hot-reloaded)."""
    _verify_admin(authorization)
    skills, errors = _get_components()["skills"].load_with_errors()
    granted = _granted_skill_objs()
    return {"skills": [_skill_summary(s, granted) for s in skills], "errors": errors}


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
    # Mk18 Phase E: new executable procedure — second admin confirms when one exists.
    if _second_eyes_required(claims):
        proposal = get_store().second_eyes_propose(
            "skill_save", {"content": req.content}, str(claims.get("sub", ""))
        )
        return JSONResponse(
            status_code=202,
            content={"proposal_id": proposal["id"], "needs_second_admin": True, "kind": "skill_save", "name": skill.name},
        )
    try:
        path = registry.save(skill, actor=claims["sub"])
    except ValueError as exc:
        # Mk17: traversal guard is defense-in-depth; invalid scope/name is 422, never 500.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Mk18: the admin wildcard is gone — every new skill needs its own admin
    # visibility row (idempotent; dept roles are granted by admins explicitly).
    _policy_store().add_rows([("p", "admin", "admin", skill.policy_obj, "use")], claims["sub"])
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
    # Mk18 Phase E: edited procedure — second admin confirms when one exists.
    if _second_eyes_required(claims):
        proposal = get_store().second_eyes_propose(
            "skill_save", {"content": req.content}, str(claims.get("sub", ""))
        )
        return JSONResponse(
            status_code=202,
            content={"proposal_id": proposal["id"], "needs_second_admin": True, "kind": "skill_save", "name": name},
        )
    try:
        path = registry.save(skill, actor=claims["sub"])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # Mk18: keep the admin visibility row present (wildcard is gone).
    _policy_store().add_rows([("p", "admin", "admin", skill.policy_obj, "use")], claims["sub"])
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
    if registry.get(name) is None:
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    # Mk18 Phase E: deleting shared procedure — second admin confirms when one exists.
    if _second_eyes_required(claims):
        proposal = get_store().second_eyes_propose(
            "skill_delete", {"name": name}, str(claims.get("sub", ""))
        )
        return JSONResponse(
            status_code=202,
            content={"proposal_id": proposal["id"], "needs_second_admin": True, "kind": "skill_delete", "name": name},
        )
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


# ---- Department head: own-department skills (Mk15) ----


def _own_dept_skill(name: str, claims: dict[str, Any]) -> Any:
    skill = _get_components()["skills"].get(name)
    if skill is None:
        raise HTTPException(status_code=404, detail=f"skill {name!r} not found")
    if skill.scope != scope_dept(str(claims.get("dept", ""))):
        raise HTTPException(status_code=403, detail="not your department's skill")
    return skill


@app.get("/api/dept/skills")
def list_dept_skills(authorization: str | None = Header(default=None)) -> dict:
    """Skills scoped to the head's own department (`scope: dept/<dept>`)."""
    claims = _verify_dept_head(authorization)
    own = scope_dept(str(claims.get("dept", "")))
    skills, errors = _get_components()["skills"].load_with_errors()
    granted = _granted_skill_objs()
    return {"skills": [_skill_summary(s, granted) for s in skills if s.scope == own], "errors": errors}


@app.get("/api/dept/skills/{name}")
def get_dept_skill(name: str, authorization: str | None = Header(default=None)) -> dict:
    claims = _verify_dept_head(authorization)
    return {"content": render_skill(_own_dept_skill(name, claims))}


@app.put("/api/dept/skills/{name}")
def update_dept_skill(
    name: str,
    req: SkillWriteRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    """A head edits one of their OWN department's skills. The skill must stay in the
    department (same name, same `dept/<dept>` scope); creating/deleting skills and the
    policy rows that make them usable stay with the admin."""
    claims = _verify_dept_head(authorization)
    _own_dept_skill(name, claims)
    try:
        skill = parse_skill_document(req.content, fallback_name=name)
    except SkillValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if skill.name != name:
        raise HTTPException(status_code=409, detail="skill name in frontmatter must match the url")
    if skill.scope != scope_dept(str(claims.get("dept", ""))):
        raise HTTPException(status_code=403, detail="a department skill must stay in your department's scope")
    path = _get_components()["skills"].save(skill, actor=claims["sub"])
    _get_components()["audit"].log(
        decision="ALLOW",
        resource={"type": "skill", "id": skill.name},
        actor={"sub": claims["sub"], "role": claims["role"], "dept": claims["dept"]},
        run_id="dashboard",
        reason="head",
    )
    return {"name": skill.name, "path": str(path), "saved": True}


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


@app.get("/api/system-memory/status")
def system_memory_status(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Platform extractor health (Mk16 fix) — any active member may read.

    Tells the Memory tab *why* learning degraded: unconfigured vs a recent
    `rate-limited`/`error`/`decrypt-failed` failure (per-process, in-memory —
    a restart resets it). Never exposes key material.
    """
    _verified_member(authorization)
    from app.llm.system import describe_platform_source
    from app.memory import system_status

    source, model = describe_platform_source()
    return system_status.get_status(
        configured=source != "none", source=source, model=model
    )


class PlatformLLMRequest(BaseModel):
    api_key: str | None = None
    model: str | None = None


@app.get("/api/system-memory/config")
def get_platform_config(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Shared platform key metadata (Mk16 follow-up) — admin only, never the key.

    Any instance on the same database uses this exact row; per-machine env is
    only the pre-bootstrap fallback.
    """
    _verify_admin(authorization)
    from app.auth.users import get_store
    from app.llm.system import describe_platform_source

    source, model = describe_platform_source()
    meta = get_store().get_platform_llm_meta()
    return {
        "configured": source != "none",
        "source": source,
        "model": model,
        "updated_by": meta[1] if meta else None,
        "updated_at": meta[2] if meta else None,
    }


@app.put("/api/system-memory/config")
def set_platform_config(
    req: PlatformLLMRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Set/rotate the shared platform key and/or model (Mk16 follow-up) — admin only.

    `api_key=None` keeps the stored key (model-only change). The model must
    stay a `:free` id (422 otherwise) so a typo can never become a billing
    event. The key is Fernet-encrypted before it touches the database and is
    never returned. Bumps `members_version` so every laptop picks it up.
    """
    claims = _verify_admin(authorization)
    from fastapi import HTTPException

    from app.auth.store import MemberError
    from app.auth.users import get_store

    api_key = (req.api_key or "").strip() or None
    model = (req.model or "").strip() or None
    if api_key is None and model is None:
        raise HTTPException(status_code=422, detail="provide api_key and/or model")
    store = get_store()
    if model is None:
        meta = store.get_platform_llm_meta()
        if meta is None:
            raise HTTPException(status_code=422, detail="no platform row stored yet: provide model with the first key")
        model = meta[0]
    # Mk18 Phase E: the shared extractor key serves everyone — second admin
    # confirms when one exists. Secrets are Fernet-encrypted in the proposal.
    if _second_eyes_required(claims):
        from app.auth.crypto import encrypt_secret

        proposal = store.second_eyes_propose(
            "platform_key",
            {"model": model, "api_key_enc": encrypt_secret(api_key) if api_key else None},
            str(claims.get("sub", "admin")),
        )
        return JSONResponse(
            status_code=202,
            content={"proposal_id": proposal["id"], "needs_second_admin": True, "kind": "platform_key"},
        )
    try:
        saved_model, by, at = store.set_platform_llm(
            model=model, api_key_plaintext=api_key, actor=str(claims.get("sub", "admin"))
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except MemberError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from app.llm.system import clear_system_provider_cache

    clear_system_provider_cache()
    return {"configured": True, "source": "db", "model": saved_model,
            "updated_by": by, "updated_at": at}


@app.delete("/api/system-memory/config")
def clear_platform_config(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Delete the shared row (reverts to the env key) — admin only."""
    claims = _verify_admin(authorization)
    from app.auth.users import get_store
    from app.llm.system import clear_system_provider_cache, describe_platform_source

    # Mk18 Phase E: removing everyone's extractor key needs second eyes too.
    if _second_eyes_required(claims):
        proposal = get_store().second_eyes_propose(
            "platform_key", {"clear": True}, str(claims.get("sub", "admin"))
        )
        return JSONResponse(
            status_code=202,
            content={"proposal_id": proposal["id"], "needs_second_admin": True, "kind": "platform_key"},
        )
    removed = get_store().clear_platform_llm(actor=str(claims.get("sub", "admin")))
    clear_system_provider_cache()
    source, model = describe_platform_source()
    return {"configured": source != "none", "source": source, "model": model,
            "removed": removed}


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
        reason=f"{'admin' if claims.get('role') == 'admin' else 'head'}:{action}",
    )


def _verify_memory_editor(authorization: str | None, scope: str) -> dict[str, Any]:
    """Admins edit any scope; a department head edits only `dept/<own dept>` (Mk15)."""
    claims, _token = _verified_member(authorization)
    if claims.get("role") == "admin":
        return claims
    if is_head(claims) and scope == scope_dept(str(claims.get("dept", ""))):
        return claims
    raise HTTPException(status_code=403, detail="only admins may access the dashboard API")


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
    claims = _verify_memory_editor(authorization, scope)
    _validate_scope(scope)
    try:
        outcome = _get_components()["memory_editor"].add(scope, req.text)
    except Exception as exc:
        # Mk17: MemoryValidationError empty → 422, too_long → 413.
        from app.memory.edit import MemoryValidationError as _MVE

        if isinstance(exc, _MVE):
            raise HTTPException(
                status_code=413 if exc.code == "too_long" else 422, detail=str(exc)
            ) from exc
        raise
    _audit_memory(claims, scope, outcome.op)
    return {"scope": scope, "op": outcome.op, "text": req.text, "evicted": outcome.evicted}


@app.put("/api/memory/{scope:path}/{key}")
def update_memory_fact(
    scope: str,
    key: str,
    req: MemoryItemRequest,
    authorization: str | None = Header(default=None),
) -> dict:
    claims = _verify_memory_editor(authorization, scope)
    _validate_scope(scope)
    store = _get_components()["store"]
    item = store.get(scope_to_namespace(scope), key)
    if item is None:
        raise HTTPException(status_code=404, detail=f"memory {key!r} not found in {scope}")
    # Mk17: validate empty/size then direct put + budget (was raw put, no checks).
    try:

        editor = _get_components()["memory_editor"]
        editor._check_text(req.text)
    except Exception as exc:
        from app.memory.edit import MemoryValidationError as _MVE2

        if isinstance(exc, _MVE2):
            raise HTTPException(
                status_code=413 if exc.code == "too_long" else 422, detail=str(exc)
            ) from exc
        raise
    store.put(scope_to_namespace(scope), key, {"text": req.text, "op": "replace"})
    try:
        editor._enforce_budget(scope_to_namespace(scope), scope)
    except Exception:  # noqa: BLE001, S110
        pass
    _audit_memory(claims, scope, "replace")
    return {"scope": scope, "key": key, "text": req.text, "op": "replace"}


@app.delete("/api/memory/{scope:path}/{key}")
def delete_memory_fact(
    scope: str,
    key: str,
    authorization: str | None = Header(default=None),
) -> dict:
    claims = _verify_memory_editor(authorization, scope)
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
    Zen additionally accepts the `opencode/` spelling (`opencode/<id>` in Zen docs).
    """
    p_type = normalize_provider_type(provider)
    if p_type == "zen":
        return strip_routing_prefix((model or "").strip(), "zen", "opencode")
    if p_type in ("lmstudio", "ollama", "llamacpp"):
        return strip_routing_prefix(
            (model or "").strip(), p_type, "lm_studio", "lm-studio", "llama.cpp", "llama_cpp"
        )
    return strip_routing_prefix((model or "").strip(), p_type)


class LLMKeyRequest(BaseModel):
    provider: str
    model: str
    api_key: str
    base_url: str = ""  # required for custom_openai; blank = the provider's default


class LLMKeyUpdateRequest(BaseModel):
    model: str | None = None
    base_url: str | None = None
    active: bool | None = None


def _validated_base_url(base_url: str, *, allow_public: bool = True) -> str:
    """Mk19 Phase 2 (SSRF): every non-empty member-/admin-supplied `base_url` must
    pass the outbound URL policy before it is probed or persisted. Fixed-message
    422 — the offending URL is never echoed back.

    `allow_public=True` (stored BYOK endpoints): public LLM gateways are legal,
    metadata/link-local/CGNAT, own-service ports and bad schemes are not.
    The live fetch in `/api/account/local-models` passes `allow_public=False`
    (strict loopback/private allowlist) — see app/harness/url_policy.py.
    """
    if not base_url:
        return base_url
    try:
        return validate_outbound_url(base_url, settings, allow_public=allow_public)
    except UrlPolicyError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _validated_key_fields(req: LLMKeyRequest) -> tuple[str, str, str, str]:
    """Normalize + validate a BYOK submission (shared by the member and admin routes)."""
    provider = (req.provider or "").strip()
    model = _normalize_model(provider, req.model or "")
    api_key = (req.api_key or "").strip()
    base_url = (req.base_url or "").strip()
    _validated_base_url(base_url)  # Mk19 Phase 2: reject before anything is stored
    p_type = normalize_provider_type(provider)
    if p_type in ("lmstudio", "ollama", "llamacpp"):
        from app.llm.lmstudio_provider import DUMMY_API_KEY, LOCAL_BASE_URLS

        if not model:
            raise HTTPException(status_code=422, detail="model is required")
        if not api_key:
            api_key = DUMMY_API_KEY
        if not base_url:
            base_url = LOCAL_BASE_URLS[p_type]
        if not is_supported(provider):
            raise HTTPException(
                status_code=422,
                detail=f"unsupported provider {provider!r}; supported: {sorted(PROVIDERS)}",
            )
        return provider, model, api_key, base_url
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
    if normalize_provider_type(provider) == "zen" and not base_url:
        from app.llm.zen_provider import BASE_URL as _ZEN_BASE_URL

        base_url = _ZEN_BASE_URL
    return provider, model, api_key, base_url


@app.get("/api/account/llm-keys")
def get_own_llm_keys(authorization: str | None = Header(default=None)) -> list[dict[str, Any]]:
    claims, _token = _verified_member(authorization)
    store = get_store()
    return [k.to_public() for k in store.list_llm_keys(claims["sub"])]


@app.get("/api/account/local-models")
def list_local_models(
    provider: str = Query(default="lmstudio"),
    base_url: str = Query(default=""),
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Loaded-model ids from a local OpenAI-compatible server (plan Mk11).

    Proxies `GET {base}/v1/models` so the Account tab can offer the models the
    server actually has loaded (LM Studio loads several at once). Member JWT
    only; short timeout — a stopped server is a 502, never a hang.
    """
    _verified_member(authorization)
    p_type = normalize_provider_type(provider or "lmstudio")
    base = (base_url or "").strip()
    if p_type == "custom_openai":
        if not base:
            raise HTTPException(status_code=422, detail="base_url is required")
    elif p_type in ("lmstudio", "ollama", "llamacpp"):
        from app.llm.lmstudio_provider import LOCAL_BASE_URLS

        base = base or LOCAL_BASE_URLS[p_type]
    else:
        raise HTTPException(
            status_code=422,
            detail=f"model detection supports local providers only, got {provider!r}",
        )
    url = base.rstrip("/") + "/models"
    # Mk19 Phase 2: STRICT allowlist before any server-side fetch (public internet
    # denied here — the probe exists to discover local servers only).
    _validated_base_url(url, allow_public=False)
    try:
        resp = httpx.get(url, timeout=5.0)
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"local server unreachable: {exc}") from exc
    if resp.status_code != 200:
        # Mk19 Phase 2: never echo upstream response bodies (SSRF data exfil path).
        raise HTTPException(
            status_code=502, detail=f"local server returned {resp.status_code}"
        )
    try:
        data = resp.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="local server returned invalid JSON") from exc
    items = data.get("data", []) if isinstance(data, dict) else []
    ids = [str(d.get("id")) for d in items if isinstance(d, dict) and d.get("id")]
    return {"provider": p_type, "base_url": base, "models": ids}


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

    if req.base_url is not None:  # Mk19 Phase 2: PATCH is its own base_url entry point
        _validated_base_url(req.base_url)

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
    # Mk18 Phase E: an admin writing ANOTHER member's key needs second eyes.
    if _second_eyes_required(claims):
        from app.auth.crypto import encrypt_secret

        proposal = store.second_eyes_propose(
            "llm_key_write",
            {"username": username, "provider": provider, "model": model,
             "api_key_enc": encrypt_secret(api_key), "base_url": base_url, "active": True},
            str(claims.get("sub", "")),
        )
        return JSONResponse(
            status_code=202,
            content={"proposal_id": proposal["id"], "needs_second_admin": True, "kind": "llm_key_write"},
        )
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

    if req.base_url is not None:  # Mk19 Phase 2: PATCH is its own base_url entry point
        _validated_base_url(req.base_url)

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
    # Mk18 Phase E: deleting another member's key needs second eyes.
    if _second_eyes_required(claims):
        if store.get(username) is None:
            raise HTTPException(status_code=404, detail=f"member {username!r} not found")
        if not [k for k in store.list_llm_keys(username) if k.id == key_id]:
            raise HTTPException(status_code=404, detail="LLM key not found")
        proposal = store.second_eyes_propose(
            "llm_key_delete", {"username": username, "key_id": key_id}, str(claims.get("sub", ""))
        )
        return JSONResponse(
            status_code=202,
            content={"proposal_id": proposal["id"], "needs_second_admin": True, "kind": "llm_key_delete"},
        )
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


# ---- Integrations (Mk9: per-user BYO Composio + connected apps) ----


class ComposioKeyRequest(BaseModel):
    api_key: str


class IntegrationConnectRequest(BaseModel):
    app: str  # any admin-enabled toolkit: gmail, github, slack, notion, ...


class ToolkitAddRequest(BaseModel):
    toolkit: str


class ToolkitEnableRequest(BaseModel):
    enabled: bool


def _catalog_toolkits(enabled_only: bool = True) -> list[dict[str, Any]]:
    try:
        return get_store().list_toolkits(enabled_only=enabled_only)
    except Exception:  # noqa: BLE001 - fall back to seeds offline
        return [{"toolkit": t, "label": t, "enabled": True, "auth_config_id": ""} for t in ("gmail", "github")]


def _validate_integration_app(app: str) -> str:
    name = (app or "").strip().lower()
    if not name:
        raise HTTPException(status_code=422, detail="app is required")
    known = {t["toolkit"] for t in _catalog_toolkits(enabled_only=False)}
    if name not in known:
        raise HTTPException(
            status_code=422,
            detail=f"unknown app {app!r} — ask an admin to add it under Integrations → Catalog",
        )
    return name


def _require_enabled_app(app: str) -> str:
    name = _validate_integration_app(app)
    enabled = {t["toolkit"] for t in _catalog_toolkits(enabled_only=True)}
    if name not in enabled:
        raise HTTPException(status_code=422, detail=f"{app!r} is disabled by your admin")
    return name


@app.post("/api/integrations/key")
def set_own_composio_key(
    req: ComposioKeyRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Key wizard step 1: validate (friendly, no leak) then save per-user BYO key."""
    claims, _token = _verified_member(authorization)
    api_key = (req.api_key or "").strip()
    if not api_key:
        raise HTTPException(status_code=422, detail="paste your Composio API key first")
    from app.integrations.client import build_client

    client = build_client(api_key, settings.composio.base_url, settings.composio.timeout_seconds)
    if hasattr(client, "validate_key"):
        ok, note = client.validate_key()  # type: ignore[union-attr]
        if not ok:
            raise HTTPException(status_code=502, detail=note)
    get_store().set_composio_key(claims["sub"], api_key, actor=claims["sub"])
    return {"status": "saved", "note": note if hasattr(client, "validate_key") else "saved"}


@app.delete("/api/integrations/key")
def delete_own_composio_key(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims, _token = _verified_member(authorization)
    get_store().delete_composio_key(claims["sub"], actor=claims["sub"])
    return {"status": "deleted"}


@app.get("/api/integrations/status")
def integration_status(
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Metadata-only: enabled-catalog apps + per-user status (never raw key)."""
    claims, _token = _verified_member(authorization)
    store = get_store()
    connected = {c["app"]: c for c in store.list_connected_accounts(claims["sub"])}
    apps = [
        {
            "app": t["toolkit"],
            "label": t.get("label", t["toolkit"]),
            "status": connected.get(t["toolkit"], {}).get("status", "disconnected"),
            "updated_at": connected.get(t["toolkit"], {}).get("updated_at", ""),
        }
        for t in _catalog_toolkits(enabled_only=True)
    ]
    return {"apps": apps, "has_key": store.get_composio_key(claims["sub"]) is not None}


def _auth_config_for(app_name: str) -> str | None:
    """Catalog DB first (auto-provisioned); legacy .env as fallback. No pasting by members."""
    try:
        for t in get_store().list_toolkits(enabled_only=False):
            if t["toolkit"] == app_name and t.get("auth_config_id"):
                return str(t["auth_config_id"])
    except Exception:  # noqa: BLE001, S110 - fall through to settings
        pass
    return {
        "gmail": settings.composio.gmail_auth_config,
        "github": settings.composio.github_auth_config,
    }.get(app_name)


@app.post("/api/integrations/connect")
def integration_connect(
    req: IntegrationConnectRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims, _token = _verified_member(authorization)
    app_name = _require_enabled_app(req.app)
    from app.integrations.client import build_client
    from app.integrations.vault import get_composio_key

    api_key = get_composio_key(claims["sub"], settings.composio.api_key)
    if not api_key:
        raise HTTPException(
            status_code=422,
            detail="save your Composio API key in Account → Integrations first",
        )
    auth_config = _auth_config_for(app_name)
    if not auth_config:
        # Mk18 Phase F: members can no longer auto-provision shared auth
        # configs (that minted project-global credentials from a member call).
        # An admin curates the toolkit via Integrations → Catalog first.
        raise HTTPException(
            status_code=422,
            detail=f"{app_name!r} is not ready yet — ask an admin to enable it under Integrations → Catalog",
        )
    client = build_client(api_key, settings.composio.base_url, settings.composio.timeout_seconds)
    try:
        url = client.get_connect_url(app_name, claims["sub"], auth_config)  # type: ignore[union-attr]
    except RuntimeError as exc:
        # Mk18 Phase F: no member-driven catalog self-heal (shared rows must
        # not be rewritable from a member call) — surface the admin path.
        raise HTTPException(
            status_code=502,
            detail=(
                f"the saved login setup for {app_name!r} does not exist in this Composio project. "
                "It was likely copied from a different project, deleted, or is not the Auth Config ID. "
                "Ask an admin to re-add it under Integrations → Catalog (auto-creates a fresh one)."
            ),
        ) from exc
    # Remember the working ID in the catalog so the next Connect skips the fallback.
    if auth_config:
        try:
            get_store().upsert_toolkit(app_name, enabled=True, auth_config_id=auth_config, actor=claims["sub"])
        except Exception:  # noqa: BLE001, S110 - cache sync is best-effort
            pass
    get_store().set_connected_account(claims["sub"], app_name, account_id="", status="pending")
    return {"app": app_name, "redirectUrl": url, "status": "pending"}


@app.post("/api/integrations/refresh")
def integration_refresh(
    req: IntegrationConnectRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Manual sync (user choice): check Composio for ACTIVE connection, flip pending→active."""
    claims, _token = _verified_member(authorization)
    app_name = _validate_integration_app(req.app)
    from app.integrations.client import build_client
    from app.integrations.vault import get_composio_key

    api_key = get_composio_key(claims["sub"], settings.composio.api_key)
    if not api_key:
        raise HTTPException(status_code=422, detail="save your Composio API key first")
    client = build_client(api_key, settings.composio.base_url, settings.composio.timeout_seconds)
    if hasattr(client, "list_for_user"):
        try:
            remote = client.list_for_user(claims["sub"])  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            remote = None
        if remote is not None:
            for item in remote:
                toolkit = str(item.get("toolkit", "")).lower()
                status = str(item.get("status", "")).upper()
                if app_name in toolkit and status == "ACTIVE":
                    get_store().set_connected_account(
                        claims["sub"], app_name,
                        account_id=str(item.get("id", "")), status="active",
                    )
                    return {"app": app_name, "status": "active"}
            # Mk18 Phase F: Composio affirmatively reports no ACTIVE connection
            # (and no error) → the local row is stale: revoke it (audited).
            # Errors leave local state untouched (fail closed the other way).
            local = [c for c in get_store().list_connected_accounts(claims["sub"]) if c["app"] == app_name]
            if local and local[0]["status"] in ("active", "pending"):
                get_store().set_connected_account(claims["sub"], app_name, account_id="", status="revoked")
                get_store()._audit_entry(
                    claims["sub"], "integrations:revoked-remote", "connected-account",
                    f"{claims['sub']}|{app_name}",
                )
                return {"app": app_name, "status": "revoked"}
    entry = [c for c in get_store().list_connected_accounts(claims["sub"]) if c["app"] == app_name]
    return {"app": app_name, "status": entry[0]["status"] if entry else "disconnected"}


@app.post("/api/integrations/disconnect")
def integration_disconnect(
    req: IntegrationConnectRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Mk18 Phase F: remote revoke first — only a remote success (or an
    explicitly unsupported SDK) flips the local row. An explicit remote
    failure is 502 with local state unchanged (fail closed)."""
    claims, _token = _verified_member(authorization)
    app_name = _validate_integration_app(req.app)
    from app.integrations.client import build_client
    from app.integrations.vault import get_composio_key

    store = get_store()
    local = [c for c in store.list_connected_accounts(claims["sub"]) if c["app"] == app_name]
    account_id = str(local[0].get("account_id", "") if local else "")
    api_key = get_composio_key(claims["sub"], settings.composio.api_key)
    if api_key:
        client = build_client(api_key, settings.composio.base_url, settings.composio.timeout_seconds)
        if hasattr(client, "delete_connection"):
            ok, note = client.delete_connection(claims["sub"], account_id)  # type: ignore[union-attr]
            if not ok:
                raise HTTPException(status_code=502, detail=f"remote revoke failed: {note}")
    store.set_connected_account(claims["sub"], app_name, account_id="", status="revoked")
    return {"app": app_name, "status": "revoked"}


# ---- Integrations catalog (admin-curated toolkits, Mk9 simplified) ----


def _ensure_toolkit_policy_rows(toolkit: str, actor: str = "") -> None:
    """Grant every department's member role AND its head role read+write on a newly enabled
    toolkit (writes still HITL-gated by risk). Departments come from the store, so ones
    registered through the dashboard are covered too (Mk15: heads mirror their members).

    Mk18: the admin wildcard is gone — admin gets explicit rows here too."""
    from app.harness.enforcer import head_role_for

    rows = [
        ("p", "admin", "admin", toolkit, "read"),
        ("p", "admin", "admin", toolkit, "write"),
    ]
    for dept in get_store().list_departments():
        if dept.name == "admin":
            continue
        for role in (dept.role or f"{dept.name}_agent", head_role_for(dept.name)):
            rows.append(("p", role, dept.name, toolkit, "read"))
            rows.append(("p", role, dept.name, toolkit, "write"))
    _policy_store().add_rows(rows, actor)


@app.get("/api/integrations/catalog")
def integration_catalog(
    authorization: str | None = Header(default=None),
) -> list[dict[str, Any]]:
    claims, _token = _verified_member(authorization)
    store = get_store()
    items = store.list_toolkits(enabled_only=False)
    if claims.get("role") != "admin":
        return [t for t in items if t["enabled"]]
    return items


@app.post("/api/integrations/catalog")
def integration_catalog_add(
    req: ToolkitAddRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Admin adds a toolkit: validate → auto-provision auth config → discover tools → enable + policy."""
    claims = _verify_admin(authorization)
    toolkit = (req.toolkit or "").strip().lower()
    if not toolkit:
        raise HTTPException(status_code=422, detail="toolkit is required (e.g. slack, notion)")
    if not re.fullmatch(r"[a-z0-9_-]{1,64}", toolkit):  # Mk20: same rule as the store
        raise HTTPException(
            status_code=422, detail="toolkit name must be lowercase letters, digits, '_' or '-' (max 64)"
        )
    store = get_store()
    admin_key_enc = store.get_composio_key(claims["sub"])
    admin_key = None
    if admin_key_enc:
        from app.auth.crypto import decrypt_secret

        try:
            admin_key = decrypt_secret(admin_key_enc)
        except Exception:  # noqa: BLE001 - corrupt key is unusable (Mk18: no global fallback)
            admin_key = None
    if not admin_key:
        raise HTTPException(
            status_code=422,
            detail="save your Composio key first (Account → Integrations), then add the toolkit",
        )
    from app.integrations.client import build_client

    client = build_client(admin_key, settings.composio.base_url, settings.composio.timeout_seconds)
    ok, note = client.toolkit_exists(toolkit) if hasattr(client, "toolkit_exists") else (True, "")  # type: ignore[union-attr]
    if not ok:
        raise HTTPException(status_code=422, detail=note)
    auth_id, auth_note = client.ensure_auth_config(toolkit) if hasattr(client, "ensure_auth_config") else (None, "")  # type: ignore[union-attr]
    tools, _tnote = client.discover_tools(toolkit) if hasattr(client, "discover_tools") else ([], "")  # type: ignore[union-attr]
    item = store.upsert_toolkit(toolkit, enabled=True, auth_config_id=auth_id or "", actor=claims["sub"])
    if tools:
        store.cache_toolkit_tools(toolkit, tools)
    _ensure_toolkit_policy_rows(toolkit, claims["sub"])
    return {**item, "tools": len(tools), "note": f"{note}; {auth_note}"}


@app.patch("/api/integrations/catalog/{toolkit}")
def integration_catalog_toggle(
    toolkit: str,
    req: ToolkitEnableRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    claims = _verify_admin(authorization)
    name = (toolkit or "").strip().lower()
    try:
        store = get_store()
        store.set_toolkit_enabled(name, req.enabled, actor=claims["sub"])
        if req.enabled:
            _ensure_toolkit_policy_rows(name, claims["sub"])
    except MemberError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"toolkit": name, "enabled": req.enabled}


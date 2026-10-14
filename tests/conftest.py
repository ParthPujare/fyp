"""Shared pytest fixtures. Tests run fully offline (MockLLM, no network)."""

from __future__ import annotations

import asyncio
import os
import uuid
from typing import Any

# HERMETIC TESTS (Mk13). A developer's `.env` may hold the real, shared `DATABASE_URL`; pydantic
# loads `.env` even when the shell variable is unset, and the store factories would then connect
# the whole suite to that shared database (this actually happened once: migrations + demo seed
# rows were written to a live Neon DB by a test run). An explicit empty environment variable wins
# over `.env` and means "no shared database". This must run before any `app` module reads
# settings. Postgres-backed tests build their own pools from TEST_DATABASE_URL instead.
os.environ["DATABASE_URL"] = ""
# HERMETIC TESTS (Mk16). Same hazard for the platform extractor key: a real
# `OPENROUTER_API_KEY` in `.env` would make every memory test hit the network
# via `get_system_provider()`. Blank both names; tests that need a system
# provider inject a stub via monkeypatch instead.
os.environ["OPENROUTER_API_KEY"] = ""
os.environ["SYSTEM__OPENROUTER_API_KEY"] = ""

import httpx
import pytest
from fastapi import FastAPI
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.agent.tools import ToolExecutor
from app.config import Settings, get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.llm.base import LLMProvider
from app.ops.store import TraceStore
from app.services import crm, devops, finance, hr


def serve_request(app: FastAPI, request: httpx.Request) -> httpx.Response:
    """Drive an ASGI app synchronously from a MockTransport handler (offline)."""

    async def _run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        resp = await transport.handle_async_request(request)
        await resp.aread()
        return httpx.Response(
            status_code=resp.status_code,
            headers=resp.headers,
            content=resp.content,
            request=request,
        )

    return asyncio.run(_run())


def make_service_client(apps_by_port: dict[int, FastAPI]) -> httpx.Client:
    """httpx client routing :8002-8005 to in-process FastAPI apps (offline).

    Mk18 Phase C: dept services require the operator's JWT, so the client
    attaches a freshly minted admin token when the caller didn't set one
    (real runs always carry the per-run user token via ToolExecutor).
    """

    def _fallback_token() -> str:
        from app.auth.jwt import create_token
        from app.auth.users import USERS
        from app.config import get_settings as _gs

        _s = _gs()
        return create_token(USERS["admin@acme.com"], _s.jwt.secret, _s.jwt.algorithm, _s.jwt.expiry_minutes)

    def handler(request: httpx.Request) -> httpx.Response:
        if "authorization" not in {k.lower() for k in request.headers}:
            request.headers["Authorization"] = f"Bearer {_fallback_token()}"
        return serve_request(apps_by_port[request.url.port], request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def make_runner(
    *,
    provider: LLMProvider,
    executor: ToolExecutor,
    trace: TraceStore,
    settings: Settings | None = None,
    enforcer: PolicyEnforcer | None = None,
    audit: AuditLog | None = None,
    checkpointer: Any = None,
    store: Any = None,
    episodic: Any = None,
    approvals: Any = None,
    recall: Any = None,
):
    """Run agent against in-process services. A checkpointer is provided by
    default (in-memory, offline) so threads + HITL interrupts work in tests."""

    if checkpointer is None:
        checkpointer = InMemorySaver()

    def _run(claims: dict[str, Any], message: str, thread_id: str | None = None):
        return run_agent(
            claims,
            message,
            provider=provider,
            executor=executor,
            trace=trace,
            settings=settings,
            enforcer=enforcer,
            audit=audit,
            checkpointer=checkpointer,
            store=store,
            episodic=episodic,
            approvals=approvals,
            recall=recall,
            thread_id=thread_id,
        )

    return _run


@pytest.fixture
def enforcer(member_store) -> PolicyEnforcer:
    return PolicyEnforcer()


@pytest.fixture
def audit_log(tmp_path) -> AuditLog:
    return AuditLog(tmp_path / "audit.jsonl")


@pytest.fixture
def dept_apps(tmp_path) -> dict[int, FastAPI]:
    return {
        8002: crm.create_app(tmp_path / "crm.sqlite"),
        8003: hr.create_app(tmp_path / "hr.sqlite"),
        8004: devops.create_app(tmp_path / "devops.sqlite"),
        8005: finance.create_app(tmp_path / "finance.sqlite"),
    }


@pytest.fixture
def trace_store(tmp_path) -> TraceStore:
    return TraceStore(tmp_path / "trace.sqlite")


@pytest.fixture
def memory_store(tmp_path):
    from app.memory.store import SqliteBaseStore

    return SqliteBaseStore(tmp_path / "memory.sqlite")


@pytest.fixture
def episodic(tmp_path):
    from app.memory.episodic import EpisodicStore

    return EpisodicStore(tmp_path / "memory.sqlite")


@pytest.fixture
def approvals(tmp_path):
    from app.memory.approvals import ApprovalStore

    return ApprovalStore(tmp_path / "memory.sqlite")


@pytest.fixture
def recall_service(episodic, enforcer):
    from app.memory.recall import RecallService

    return RecallService(episodic, enforcer, get_settings().memory)


@pytest.fixture
def executor(dept_apps) -> ToolExecutor:
    return ToolExecutor(client=make_service_client(dept_apps), settings=get_settings())


@pytest.fixture
def runner_factory(executor, trace_store, enforcer, audit_log, tmp_path):
    from app.memory.approvals import ApprovalStore
    from app.memory.episodic import EpisodicStore
    from app.memory.recall import RecallService
    from app.memory.store import SqliteBaseStore

    _memory_path = tmp_path / "memory.sqlite"
    _store = SqliteBaseStore(_memory_path)
    _episodic = EpisodicStore(_memory_path)
    _approvals = ApprovalStore(_memory_path)

    def _make(
        provider: LLMProvider,
        settings: Settings | None = None,
        checkpointer: Any = None,
        store: Any = None,
        episodic: Any = None,
        approvals: Any = None,
        recall: Any = None,
    ):
        store = store if store is not None else _store
        episodic = episodic if episodic is not None else _episodic
        approvals = approvals if approvals is not None else _approvals
        recall = recall if recall is not None else RecallService(episodic, enforcer, get_settings().memory)
        return make_runner(
            provider=provider,
            executor=executor,
            trace=trace_store,
            settings=settings,
            enforcer=enforcer,
            audit=audit_log,
            checkpointer=checkpointer,
            store=store,
            episodic=episodic,
            approvals=approvals,
            recall=recall,
        )

    return _make


@pytest.fixture
def settings_with_budget() -> Settings:
    return Settings(guardrails={"tool_budget": 3, "max_steps": 10})


@pytest.fixture
def settings_with_max_steps() -> Settings:
    return Settings(guardrails={"max_steps": 3, "tool_budget": 10})


@pytest.fixture
def settings_with_timeout() -> Settings:
    return Settings(guardrails={"run_timeout_seconds": 0.05, "call_timeout_seconds": 5.0})


@pytest.fixture(scope="session", autouse=True)
def _suite_never_touches_a_shared_database():
    """Fail loudly (instead of silently writing to someone's Neon DB) if isolation ever breaks."""
    from app.db import is_enabled

    assert not is_enabled(), (
        "tests must not run with DATABASE_URL active — that would read/write the shared database"
    )


@pytest.fixture(autouse=True)
def member_store(tmp_path, request):
    """Isolate the MemberStore per test (fresh, seeded tmp DB) so the suite never
    touches the real `data/members.sqlite` (plan_mk2 Phase 1 test isolation).

    `TEST_BACKEND=postgres` (with TEST_DATABASE_URL) runs the *whole suite* with the member
    store on a throwaway Postgres schema instead — the regression check that the Postgres
    store is a drop-in replacement (`make test-pg-full`)."""
    from app.auth import users as auth_users
    from app.auth.store import MemberStore
    from app.harness.audit import AuditLog

    previous = auth_users._STORE  # not get_store(): never construct a real store just to save it
    audit = AuditLog(tmp_path / "members_audit.jsonl")
    if os.environ.get("TEST_BACKEND") == "postgres" and TEST_URL:
        from app.auth.pg_store import PgMemberStore

        # its OWN schema (not the `pg` fixture's): tests that ask for `pg` expect a pristine one
        pool, _admin, _schema, cleanup = _private_pg_schema()
        request.addfinalizer(cleanup)
        store = PgMemberStore(pool, audit=audit)  # production cache TTLs: exercises the cache too
    else:
        store = MemberStore(tmp_path / "members.sqlite", audit=audit)
    auth_users.set_store(store)
    yield store
    auth_users.set_store(previous)


# ------------------------------------------------------------------ Postgres (Mk13)
# Postgres-backed tests run only when TEST_DATABASE_URL points at a disposable database
# (`make test-pg` starts one in Docker); otherwise they skip and the suite stays offline.
TEST_URL = os.environ.get("TEST_DATABASE_URL")


def _private_pg_schema():
    """Create a throwaway schema + a pool confined to it; returns (pool, admin, schema, cleanup)."""
    import psycopg

    from app.db import make_pool

    schema = "t_" + uuid.uuid4().hex[:12]
    admin = psycopg.connect(TEST_URL, autocommit=True)
    admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = make_pool(TEST_URL, kwargs={"options": f"-c search_path={schema}"}, max_size=8, name=schema)

    def cleanup() -> None:
        pool.close()
        admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        admin.close()

    return pool, admin, schema, cleanup


@pytest.fixture
def pg():
    """(pool, admin_conn, schema): a pool whose connections are confined to a private schema."""
    if not TEST_URL:
        pytest.skip("set TEST_DATABASE_URL (make test-pg) for Postgres tests")
    pool, admin, schema, cleanup = _private_pg_schema()
    yield pool, admin, schema
    cleanup()

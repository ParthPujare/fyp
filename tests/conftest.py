"""Shared pytest fixtures. Tests run fully offline (MockLLM, no network)."""

from __future__ import annotations

import asyncio
from typing import Any

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
    """httpx client routing :8002-8005 to in-process FastAPI apps (offline)."""

    def handler(request: httpx.Request) -> httpx.Response:
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
def enforcer() -> PolicyEnforcer:
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

"""Shared pytest fixtures. Tests run fully offline (MockLLM, no network)."""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

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
):
    """Run agent against in-process services."""

    def _run(claims: dict[str, Any], message: str):
        return run_agent(
            claims,
            message,
            provider=provider,
            executor=executor,
            trace=trace,
            settings=settings,
            enforcer=enforcer,
            audit=audit,
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
def executor(dept_apps) -> ToolExecutor:
    return ToolExecutor(client=make_service_client(dept_apps), settings=get_settings())


@pytest.fixture
def runner_factory(executor, trace_store, enforcer, audit_log):
    def _make(provider: LLMProvider, settings: Settings | None = None):
        return make_runner(
            provider=provider,
            executor=executor,
            trace=trace_store,
            settings=settings,
            enforcer=enforcer,
            audit=audit_log,
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

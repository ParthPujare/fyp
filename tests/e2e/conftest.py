"""Browser (Playwright) fixtures for the dashboard UI.

The dashboard and the auth service run in THIS process on random localhost ports, so the
suite's isolation (DATABASE_URL="", the per-test tmp member store from tests/conftest.py)
applies to every request the page makes. Each test also points the dashboard at temp
components and a temp policy.csv copy: nothing here touches data/, the repo policy.csv,
or any shared database. The gateway URL points at a closed port, so chat calls fail fast
instead of reaching a real gateway that happens to be running.

Browser: the installed Google Chrome by default (no browser download needed). Override:
  E2E_BROWSER_CHANNEL=msedge                     installed Microsoft Edge
  E2E_BROWSER_CHANNEL=bundled                    Playwright's Chromium (`uv run playwright install chromium`)
  uv run pytest tests/e2e --headed --slowmo 300  watch it run
"""

from __future__ import annotations

import shutil
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import uvicorn


def pytest_collection_modifyitems(session, config, items):
    """Run the browser tests LAST in any mixed run.

    pytest-playwright's `playwright` fixture is session-scoped and keeps an asyncio loop
    running in the main thread until the session ends; every later `asyncio.run()` (e.g.
    `serve_request` in tests/conftest.py) would then fail with "cannot be called from a
    running event loop". Stable sort: everything else keeps its order.
    """
    items.sort(key=lambda item: "e2e" in item.nodeid.replace("\\", "/").split("/"))


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _serve(app: Any) -> tuple[uvicorn.Server, threading.Thread, str]:
    """Start `app` on an already-bound random port in a daemon thread; wait until it is up."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError(f"test server for {app.title!r} did not start")
        time.sleep(0.05)
    return server, thread, f"http://127.0.0.1:{port}"


@pytest.fixture(scope="session")
def servers():
    from app.auth.service import app as auth_app
    from app.dashboard.app import app as dashboard_app

    started = [_serve(auth_app), _serve(dashboard_app)]
    yield {"auth": started[0][2], "dashboard": started[1][2]}
    for server, thread, _url in started:
        server.should_exit = True
        thread.join(timeout=5)


@dataclass
class Dash:
    """What a test needs: the page URL plus the temp stores behind it (to seed and assert)."""

    url: str
    members: Any
    approvals: Any
    trace: Any
    audit_path: Path
    policy_path: Path


@pytest.fixture
def dash(servers, tmp_path, member_store) -> Dash:
    from langgraph.checkpoint.memory import InMemorySaver

    import app.dashboard.app as d
    from app.config import get_settings
    from app.harness.audit import AuditLog
    from app.harness.enforcer import POLICY_PATH, PolicyEnforcer
    from app.memory.approvals import ApprovalStore
    from app.memory.edit import MemoryEditor
    from app.memory.episodic import EpisodicStore
    from app.memory.recall import RecallService
    from app.memory.store import SqliteBaseStore
    from app.ops.store import TraceStore
    from app.skills.registry import SkillRegistry

    policy = tmp_path / "policy.csv"
    shutil.copy(POLICY_PATH, policy)
    skills_dir = tmp_path / "skills"
    shutil.copytree(d.settings.skills.skills_dir, skills_dir)

    memory_db = tmp_path / "memory.sqlite"
    store = SqliteBaseStore(memory_db)
    episodic = EpisodicStore(memory_db)
    enforcer = PolicyEnforcer()
    audit_path = tmp_path / "audit.jsonl"
    components = {
        "trace": TraceStore(tmp_path / "trace.sqlite"),
        "enforcer": enforcer,
        "audit": AuditLog(audit_path),
        "store": store,
        "episodic": episodic,
        "approvals": ApprovalStore(memory_db),
        "checkpointer": InMemorySaver(),
        "skills": SkillRegistry.open(skills_dir),
        "memory_editor": MemoryEditor(store, d.settings.memory),
        "recall": RecallService(episodic, enforcer, d.settings.memory),
    }

    # `_policy_csv()` reads get_settings(); the login proxy reads the module snapshot.
    targets = {id(d.settings.services): d.settings.services, id(get_settings().services): get_settings().services}
    saved = [(svc, svc.auth_url, svc.gateway_url, svc.policy_path) for svc in targets.values()]
    saved_components = d._components
    for svc in targets.values():
        svc.auth_url = servers["auth"]
        svc.gateway_url = f"http://127.0.0.1:{_free_port()}"  # closed: chat fails fast, offline
        svc.policy_path = policy
    d._components = components
    yield Dash(
        url=servers["dashboard"],
        members=member_store,
        approvals=components["approvals"],
        trace=components["trace"],
        audit_path=audit_path,
        policy_path=policy,
    )
    d._components = saved_components
    for svc, auth_url, gateway_url, policy_path in saved:
        svc.auth_url, svc.gateway_url, svc.policy_path = auth_url, gateway_url, policy_path


# ---- Playwright configuration ---------------------------------------------------------


def _channel(launch_args: dict[str, Any]) -> str | None:
    import os

    chosen = os.environ.get("E2E_BROWSER_CHANNEL") or launch_args.get("channel") or "chrome"
    return None if chosen == "bundled" else chosen


@pytest.fixture(scope="session")
def browser_type_launch_args(browser_type_launch_args):
    args = dict(browser_type_launch_args)
    channel = _channel(args)
    if channel:
        args["channel"] = channel
    else:
        args.pop("channel", None)
    return args


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args):
    # Reduced motion: no boot intro or entrance animations, so assertions never race them.
    return {**browser_context_args, "reduced_motion": "reduce", "viewport": {"width": 1280, "height": 860}}


@pytest.fixture(scope="session", autouse=True)
def _browser_available(browser_type_launch_args):
    """Skip (with the fix) only when no usable browser exists on this machine."""
    from playwright.sync_api import Error, sync_playwright

    try:
        with sync_playwright() as p:
            p.chromium.launch(**browser_type_launch_args).close()
    except Error as exc:
        pytest.skip(
            "no browser for the e2e tests — install Chrome, or set E2E_BROWSER_CHANNEL=msedge, "
            f"or run `uv run playwright install chromium` and set E2E_BROWSER_CHANNEL=bundled ({exc})"
        )


@pytest.fixture
def page(page):
    """Every e2e page fails its test on any uncaught JavaScript error."""
    from playwright.sync_api import expect

    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    # 10 s, not Playwright's 5 s: the first test of a run pays Chrome start-up + lazy imports.
    page.set_default_timeout(10_000)
    expect.set_options(timeout=10_000)
    yield page
    assert not errors, f"uncaught JavaScript errors: {errors}"

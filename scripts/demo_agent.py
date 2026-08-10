"""Demo: watch the Milestone 2 agent loop do real things (fully offline, no pytest).

Usage:
    uv run python scripts/demo_agent.py

Runs the actual LangGraph agent against the 4 dept services in-process with a
scripted MockLLM, and prints, for each scenario:
    - every tool call the agent made + its outcome (allowed/denied/timeout)
    - the scope-gate denials and deny-retry guardrail hits
    - the trace event tree written for one run

Scenarios:
    1. alice  -> allowed CRM search (agent really executes against seeded data)
    2. alice  -> cross-dept HR delete -> scope-gate DENY (tool never hits the service)
    3. bob    -> deny-storm on hr_delete_employee -> bounded by guardrails
    4. trace  -> dump one run's event tree from the TraceStore
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from app.agent.runner import run_agent
from app.agent.tools import TOOL_REGISTRY, ToolExecutor
from app.auth.users import USERS
from app.config import Settings, get_settings
from app.llm.mock import MockLLM
from app.ops.store import TraceStore
from app.services import crm, devops, finance, hr

B = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def serve_request(app, request: httpx.Request) -> httpx.Response:
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


def make_service_client(apps: dict[int, object]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return serve_request(apps[request.url.port], request)

    return httpx.Client(transport=httpx.MockTransport(handler))


def claims_for(username: str) -> dict:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


def header(title: str) -> None:
    print(f"\n{B}{'=' * 70}\n{title}\n{'=' * 70}{RESET}")


def outcome_color(outcome: str) -> str:
    return {"allowed": GREEN, "denied": RED, "timeout": YELLOW}.get(outcome, YELLOW)


def show_run(result, label: str) -> None:
    print(f"\n{DIM}{label}{RESET}")
    print(f"  reply        : {B}{result.reply}{RESET}")
    if result.tool_calls:
        print("  tool calls   :")
        for c in result.tool_calls:
            color = outcome_color(c["outcome"])
            print(
                f"    - {c['name']}({c['args']})  "
                f"{color}{c['outcome']}{RESET}  {DIM}{c['latency_ms']}ms{RESET}"
            )
    else:
        print(f"  tool calls   : {DIM}(none){RESET}")
    if result.guardrail_hits:
        print(f"  guardrails   : {YELLOW}{[h['type'] for h in result.guardrail_hits]}{RESET}")
    else:
        print(f"  guardrails   : {DIM}(none){RESET}")


def main() -> None:
    settings: Settings = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="fyp_demo_"))
    print(f"{DIM}temp dir: {workdir}{RESET}")

    apps = {
        8002: crm.create_app(workdir / "crm.sqlite"),
        8003: hr.create_app(workdir / "hr.sqlite"),
        8004: devops.create_app(workdir / "devops.sqlite"),
        8005: finance.create_app(workdir / "finance.sqlite"),
    }
    executor = ToolExecutor(client=make_service_client(apps), settings=settings)
    trace = TraceStore(workdir / "trace.sqlite")
    n_tools = len(TOOL_REGISTRY)
    print(f"{DIM}tool registry: {n_tools} tools (crm/hr/devops/finance) • LLM: MockLLM (scripted){RESET}")

    # 1. Allowed tool call — alice runs a real CRM search against seeded data.
    header("1. alice (marketing_agent) — ALLOWED crm_search_customer")
    provider = MockLLM(sequence=[
        {"text": "searching...", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]},
        {"text": "Found Acme customers.", "tool_calls": []},
    ])
    run1 = run_agent(
        claims_for("alice@acme.com"),
        "find customer acme",
        provider=provider,
        executor=executor,
        trace=trace,
    )
    show_run(run1, "alice: 'find customer acme'")

    # 2. Cross-dept denial — alice asks to delete an HR record.
    header("2. alice — CROSS-DEPT hr_delete_employee -> scope-gate DENY")
    provider = MockLLM(sequence=[
        {"text": "deleting...", "tool_calls": [{"name": "hr_delete_employee", "args": {"id": 1}}]},
        {"text": "done", "tool_calls": []},
    ])
    show_run(
        run_agent(claims_for("alice@acme.com"), "delete employee 1", provider=provider, executor=executor, trace=trace),
        "alice: 'delete employee 1'",
    )

    # 3. Deny-storm — bob keeps requesting the same blocked tool; guardrails bound it.
    header("3. bob (hr_agent) — DENY-STORM on hr_delete_employee -> bounded by guardrails")
    provider = MockLLM({"delete the employee": {"text": "deleting...", "tool_calls": [
        {"name": "hr_delete_employee", "args": {"id": 1}},
    ]}})
    show_run(
        run_agent(claims_for("bob@acme.com"), "delete the employee", provider=provider, executor=executor, trace=trace),
        "bob: 'delete the employee' (tool budget 8, max steps 10)",
    )

    # 4. Trace tree for run 1 — evidence of what the agent actually did.
    header("4. Trace event tree (run 1) — evidence of what the agent did")
    for e in trace.get_run_tree(run1.run_id):
        color = DIM if e["type"] in ("run_start", "run_end") else ""
        print(f"  {e['seq']:>3}  {color}{e['type']:<11}{RESET} {e['payload']}")
    ok, issues = trace.verify_integrity(run1.run_id)
    print(f"\n  trace integrity: {GREEN}OK{RESET}" if ok else f"  trace integrity: {RED}FAILED {issues}{RESET}")

    print(f"\n{B}Demo complete. Every run above is also in the trace store ({workdir}/trace.sqlite).{RESET}")


if __name__ == "__main__":
    main()

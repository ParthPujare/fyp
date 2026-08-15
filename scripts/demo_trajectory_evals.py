"""Demo: Milestone 7 — trajectory viewer + evals harness (offline).

Usage:
    uv run python scripts/demo_trajectory_evals.py

Runs the real LangGraph agent in-process (MockLLM, no services, no API keys)
against temp SQLite files and prints, per scenario:
    1. alice's allowed CRM search — the run's trajectory filtered by source
       (`llm` → PROMPT + REASONING, `memory` → CONTEXT_INJECTION, `tool` →
       tool I/O incl. the result the model saw) + the deterministic eval suite.
    2. carol invokes the central `triage_incident` skill but the nested
       `devops_create_deployment` is DENIED for her role — the eval's
       `rbac_contract` check is justified by a matching audit DENY, and a
       scripted judge verdict is traced as an EVAL event.

With live services running, every `/chat` run lands in `data/trace.sqlite` —
open the dashboard (:8006) → Ops tab to browse trajectories (filterable by
source) and per-run eval scores.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from langgraph.checkpoint.memory import InMemorySaver

from app.agent.runner import run_agent
from app.agent.tools import ToolExecutor
from app.auth.users import USERS
from app.config import get_settings
from app.harness.audit import AuditLog
from app.harness.enforcer import PolicyEnforcer
from app.llm.mock import MockLLM
from app.memory.episodic import EpisodicStore
from app.memory.recall import RecallService
from app.memory.store import SqliteBaseStore
from app.ops.store import TraceStore
from app.services import crm, devops, finance, hr

B = "\033[1m"
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
DIM = "\033[2m"
RESET = "\033[0m"


def _serve(app, request: httpx.Request) -> httpx.Response:
    async def run() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        resp = await transport.handle_async_request(request)
        await resp.aread()
        return httpx.Response(
            status_code=resp.status_code,
            headers=resp.headers,
            content=resp.content,
            request=request,
        )

    return asyncio.run(run())


def claims_for(username: str) -> dict:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


def header(title: str) -> None:
    print(f"\n{B}{'=' * 70}\n{title}\n{'=' * 70}{RESET}")


def print_filtered(trace: TraceStore, run_id: str, *sources: str) -> None:
    filt = {s for s in sources}
    tree = [e for e in trace.get_run_tree(run_id) if e["source"] in filt]
    if not tree:
        print(f"  {DIM}no events for {sorted(filt)}{RESET}")
        return
    for e in tree:
        label = e["type"]
        payload = e["payload"]
        if e["type"] in ("prompt", "reasoning"):
            text = payload.get("text", "")
            if not text and e["type"] == "reasoning":
                text = "<empty>"
            msg = text if e["type"] == "reasoning" else f"{len(payload.get('messages', []))} messages"
            print(f"  [{e['seq']}] {YELLOW}{label}{RESET} ({e['source']}) {DIM}{msg[:120]}{RESET}")
        elif e["type"] == "context_injection":
            print(
                f"  [{e['seq']}] {YELLOW}{label}{RESET} ({e['source']}) kind={payload.get('kind')} "
                f"scopes={payload.get('scopes')} chars={payload.get('chars')}"
            )
        elif e["type"] == "tool_call":
            p = payload
            print(
                f"  [{e['seq']}] {YELLOW}{label}{RESET} ({e['source']}) {p.get('tool')} -> "
                f"{p.get('outcome')} sanitized={p.get('sanitized')} "
                f"{DIM}result: {str(p.get('result_summary', ''))[:100]}{RESET}"
            )
        elif e["type"] == "skill_use":
            print(
                f"  [{e['seq']}] {YELLOW}{label}{RESET} ({e['source']}) skill={payload.get('skill')} "
                f"steps={payload.get('steps')} outcome={payload.get('outcome')}"
            )
        else:
            print(f"  [{e['seq']}] {YELLOW}{label}{RESET} ({e['source']}) {payload}")


def print_evals(trace: TraceStore, run_id: str) -> None:
    for e in trace.get_run_tree(run_id):
        if e["type"] != "eval":
            continue
        p = e["payload"]
        mark = GREEN if p["passed"] else RED
        print(
            f"  EVAL {mark}{p['suite']}: {'PASS' if p['passed'] else 'FAIL'}{RESET} "
            f"score={p['score']}"
        )
        for c in p.get("checks", []):
            cm = GREEN if c["passed"] else RED
            print(f"    {cm}{'PASS' if c['passed'] else 'FAIL'}{RESET} {c['name']} "
                  f"({c['score']}) {DIM}{c['detail'][:]}{RESET}")


def main() -> None:
    settings = get_settings()
    workdir = Path(tempfile.mkdtemp(prefix="fyp_trajectory_demo_"))
    print(f"{DIM}temp dir: {workdir}{RESET}")

    store = SqliteBaseStore(workdir / "memory.sqlite")
    episodic = EpisodicStore(workdir / "memory.sqlite")
    trace = TraceStore(workdir / "trace.sqlite")
    enforcer = PolicyEnforcer()
    audit = AuditLog(workdir / "audit.jsonl")
    recall = RecallService(episodic, enforcer, settings.memory)
    checkpointer = InMemorySaver()

    apps_by_port = {
        8002: crm.create_app(workdir / "crm.sqlite"),
        8003: hr.create_app(workdir / "hr.sqlite"),
        8004: devops.create_app(workdir / "devops.sqlite"),
        8005: finance.create_app(workdir / "finance.sqlite"),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return _serve(apps_by_port[request.url.port], request)

    executor = ToolExecutor(client=httpx.Client(transport=httpx.MockTransport(handler)))

    def run(claims, message, thread_id, sequence, judge_provider=None):
        return run_agent(
            claims, message, thread_id=thread_id,
            provider=MockLLM(sequence=sequence), executor=executor,
            trace=trace, enforcer=enforcer, audit=audit,
            store=store, episodic=episodic, recall=recall,
            checkpointer=checkpointer, settings=settings,
            judge_provider=judge_provider,
        )

    # 1. alice's allowed CRM search — trajectory by source + deterministic evals.
    header("1. alice: allowed CRM search — trajectory filterable by source")
    seq = [
        {"text": "searching", "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}],
         "reasoning": "need to find the Acme customer record"},
        {"text": "Found the Acme account.", "tool_calls": []},
    ]
    alice = run(claims_for("alice@acme.com"), "find customer acme", thread_id="t1", sequence=seq)
    print(f"  run {B}{alice.run_id[:8]}{RESET} reply: {alice.reply}")
    print(f"{DIM}trajectory source=llm (PROMPT + REASONING){RESET}")
    print_filtered(trace, alice.run_id, "llm")
    print(f"{DIM}trajectory source=memory (CONTEXT_INJECTION){RESET}")
    print_filtered(trace, alice.run_id, "memory")
    print(f"{DIM}trajectory source=tool (tool I/O — sanitized result the model saw){RESET}")
    print_filtered(trace, alice.run_id, "tool")
    print(f"{DIM}deterministic evals{RESET}")
    print_evals(trace, alice.run_id)

    # 2. carol's skill is allowed but the nested devops call is denied —
    #    rbac_contract is justified; judge verdict traced as an EVAL event.
    header("2. carol: triage_incident allowed, nested devops DENIED — eval holds")
    triage = [
        {"text": "", "tool_calls": [{"name": "triage_incident", "args": {
            "service": "api", "version": "v2", "environment": "prod",
            "status": "deployed", "deployed_by": "carol@acme.com"}}],
         "reasoning": "triage the api incident via the skill"},
        {"text": "Done.", "tool_calls": []},
    ]
    judge_provider = MockLLM(sequence=[
        {"text": '{"response_quality": 4, "injection_resistance": 5, "skill_faithfulness": 4}'},
    ])
    carol = run(claims_for("carol@acme.com"), "triage the api incident", thread_id="t2",
                sequence=triage, judge_provider=judge_provider)
    print(f"  run {B}{carol.run_id[:8]}{RESET} reply: {carol.reply}")
    print(f"{DIM}trajectory source=tool+guardrail (nested deny){RESET}")
    print_filtered(trace, carol.run_id, "tool", "guardrail")
    decisions = [e["decision"] for e in audit.entries() if e["run_id"] == carol.run_id]
    print(f"  audit decisions : {decisions}")
    print(f"{DIM}deterministic + judge evals (judge = scripted MockLLM verdict){RESET}")
    print_evals(trace, carol.run_id)

    ok, issues = trace.verify_integrity()
    print(f"\n  trace integrity: {GREEN}OK{RESET}" if ok else f"  trace integrity: {RED}FAILED {issues}{RESET}")
    print(f"\n{B}Demo complete. Memory + trace + audit live under {workdir}.{RESET}")
    print(
        f"{DIM}With live services running, /chat runs land in data/trace.sqlite — browse them "
        f"on the dashboard (:8006) → Ops → filter by source.{RESET}"
    )


if __name__ == "__main__":
    main()
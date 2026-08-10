# Session 003 — LangGraph Agent & Guardrails

## Metadata
- Date: 2026-08-10
- Plan step: Weeks 3–4 — LangGraph agent + HTTP tools + guardrails node (max steps, tool budget, deny-retry, timeout) (ref_docs/plan.md §11 row 2)
- Scope: Build the bounded, traced LangGraph agent and wire it into the gateway as `POST /chat`.
- Status: complete
- State at start: Milestone 1 done (git HEAD `352b889`): auth :8000, gateway `/hello` :8001, 4 dept services :8002–8005, `app/ops` TraceStore, MockLLM/LiteLLM; 32 offline tests green. `langgraph`/`langchain-core` not installed.

## Objective
Deliverable: bounded agent loop; deny-storm test passes (MockLLM in CI); gateway :8001 grows from `/hello` into the real chat endpoint. Tool calls hit the 4 dept services over HTTP. Guardrails: max steps, tool-call budget, deny-retry, timeout — deterministic graph logic, traced per run.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 08:00 | explore | Read INDEX/INSTRUCTIONS/session_002 + plan §11 + proposed_architecture. Confirmed Milestone 2 = LangGraph agent + HTTP tools + guardrails, gateway /chat. |
| 08:05 | decision | Asked 5 scope questions (agent framework, denial stub before Casbin, chat stateless vs threaded, tracing now, service endpoints). User chose all recommended: LangGraph now, JWT-scope gate stub, stateless single-turn, trace now, generic CRUD only. |
| 08:10 | implement | `uv add langgraph langchain-core` → installed fine on Python 3.14 (langgraph 1.2.10). Added `GuardrailSettings` (max_steps/tool_budget/call+run timeout) to `app/config.py` + `.env.example`. |
| 08:15 | fix | **Env collision**: a generic `AGENT=1` env var (set by the terminal) collided with pydantic-settings nested parsing of the `agent` settings block → Settings() failed to validate. Renamed the block to `guardrails` (env `GUARDRAILS__*`); documented in .env.example. |
| 08:20 | implement | `app/agent/tools.py`: ToolSpec + 16-tool TOOL_REGISTRY mapped to existing generic CRUD endpoints (crm_search_customer→GET /customers?q=, crm_create_campaign→POST /campaigns, hr_delete_employee→DELETE /employees/{id} scope hr:delete, finance_transfer→POST /transfers, etc.) + ToolExecutor (httpx, per-call timeout, ToolResult with latency/outcome). |
| 08:25 | implement | `app/agent/enforce.py`: `scope_allowed` — required_scope in JWT scopes (admin `*` passes). Documented as the stub Casbin replaces in wk 5–6. |
| 08:30 | implement | `app/agent/guardrails.py`: GuardrailTracker (steps, tool budget, denied-tool set, wall-clock deadline, hits list) + make_tracker. |
| 08:35 | implement | MockLLM: added stateful `sequence` mode (pop one response per generate()) so multi-step scripts (call A → B → answer) work; keyed `script` mode kept for deny-storm (same call repeats). |
| 08:40 | implement | `app/agent/graph.py`: LangGraph StateGraph (agent → execute_tools → should_continue → finalize) using the existing LLMProvider protocol (no langchain chat-model wrapper). Scope gate + deny-retry in execute_tools; guardrail hits traced. |
| 08:45 | implement | `app/agent/runner.py`: `run_agent(claims, message, *, provider, executor, trace, settings)` — stateless single-turn, full trace (run_start → nodes → tool_calls → guardrails → run_end). |
| 08:50 | implement | Gateway: `POST /chat {token, message}` via a `get_runner` FastAPI dependency (test-overridable), keeps `/hello`. |
| 08:55 | test | New suites test_tools/test_agent/test_chat. First failures: `httpx.ASGITransport` is async-only in httpx 0.28 (no sync handle_request). Built `serve_request` helper that drives the ASGI app with `asyncio.run` and returns a fresh sync Response (body fully read). |
| 09:00 | fix | Two wrong test assertions: CRM search matches email too (not just company); scope-gate denial surfaces in `tool_calls[0].outcome=="denied"`, not in the scripted MockLLM reply text. Fixed assertions. |
| 09:05 | verify | 55/55 tests green offline, ruff clean. Live smoke (`make run`): login alice → /chat returns echo reply + run_id; bad token → 401; `data/trace.sqlite` written and `verify_integrity()` True. |

## What was done
- `pyproject.toml`: + `langgraph`, `langchain-core` (uv.lock updated).
- `app/config.py`: + `GuardrailSettings` (`max_steps=10`, `tool_budget=8`, `call_timeout_seconds=30`, `run_timeout_seconds=120`), nested under `settings.guardrails`.
- `.env.example`: + `GUARDRAILS__*` block (named to dodge the `AGENT=1` env collision).
- `app/agent/__init__.py`, `tools.py` (ToolSpec, 16-tool registry, tool_schema, ToolExecutor), `enforce.py` (scope gate stub), `guardrails.py` (GuardrailTracker), `graph.py` (LangGraph StateGraph + AgentDeps), `runner.py` (run_agent + AgentResult).
- `app/llm/mock.py`: + sequence mode.
- `app/gateway/app.py`: + `POST /chat` with `get_runner` dependency (Annotated Depends, avoids B008).
- `tests/conftest.py`: + `serve_request` (sync ASGI driver), `make_service_client` (httpx MockTransport routing :8002–8005 → in-process apps), fixtures `dept_apps`, `trace_store`, `executor`, `runner_factory`, budget/max-steps/timeout settings.
- `tests/test_tools.py` (10): registry↔service routes, scope gate, HTTP calls, call timeout.
- `tests/test_agent.py` (9): single/multi-step runs, cross-dept denial, deny-storm bounded (8 denials, budget + deny_retry hits), deny-storm never touches service, tool budget forced-final, max-steps, run timeout, trace written + integrity.
- `tests/test_chat.py` (4): /chat 401, reply shape, real agent path via dependency override.
- `README.md`, `ref_docs/plan.md` (§4/§11), `ref_docs/proposed_architecture.md` (status banner + §2 topology): status updated to Milestone 2.

## Decisions locked in this session
- **LangGraph now**, using custom graph nodes over the existing `LLMProvider` protocol — no langchain chat-model wrapper; MockLLM/LiteLLM unchanged.
- **Scope gate stub** (JWT `scopes` contains `required_scope`) stands in for Casbin until wk 5–6; the `scope_allowed(tool, claims)` signature is the future Casbin swap point.
- **Stateless single-turn /chat**; conversation threads/checkpointer deferred to the memory milestone (wk 7–8).
- **Deny-retry policy**: first denial returns the permission error to the LLM; any repeat of that tool in the same run is short-circuited with a routing hint (deny-storm can't burn the loop budget).
- **Guardrail settings named `GUARDRAILS__*`**, not `AGENT__*`, to avoid colliding with a common `AGENT=1` env var.
- **Generic CRUD only** for tools this milestone (no new service endpoints).

## Verification performed
- `uv run pytest` → **55 passed** (was 32), fully offline (MockLLM + in-process httpx.MockTransport/ASGI driver, no sockets/API keys).
- `uv run ruff check app tests` → All checks passed.
- Live smoke (`bash scripts/run_dev.sh`): login alice/bob → `POST /chat` returns `{reply, run_id, guardrail_hits, tool_calls}`; bad token → 401; `data/trace.sqlite` contains run_start → node → run_end; `TraceStore.verify_integrity()` → True. Processes killed; `data/` removed.

## Blocker / open questions
- None. Notes for later: `httpx.ASGITransport` is async-only in httpx 0.28 — tests route through `tests/conftest.serve_request` (asyncio.run wrapper). The tool registry is the seed for wk 5–6 `filter_tools`; `risk_level` already marked (read/write/critical) for the wk 9–10 HITL gate.

## Next step (for the next session)
- Execute **Weeks 5–6 (Milestone 3)**: Casbin core — `model.conf` + `policy.csv` (RBAC with domains), replace the scope-gate stub, `filter_tools` (per-role schema filtering), `intercept` (runtime enforcement), and the JSONL **audit log** (allow/deny decisions, matching trace conventions). Unauthorized tool physically blocked.

## Handoff notes
- `app/agent/enforce.py:scope_allowed` is the deliberate Casbin swap point — keep its call shape (`(tool, claims) → bool`) when wiring `enforcer.enforce(sub, dom, obj, act)` in wk 5–6.
- Tool registry (`app/agent/tools.py`) already carries `required_scope` + `risk_level`; add `filter_tools` (visible-schema filtering per role) on top of it next milestone.
- Guardrails config lives in `settings.guardrails` (not `settings.agent`) — see .env.example note.
- `/chat` is stateless; `run_agent` rebuilds the graph per call. When memory lands (wk 7–8), add the checkpointer + thread_id and move `/chat` from single-turn to thread-backed.
- Test infra to reuse: `tests/conftest.serve_request` + `make_service_client` (in-process services), `runner_factory` fixture, and the settings fixtures for guardrail variations.

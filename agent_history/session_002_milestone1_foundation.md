# Session 002 — Milestone 1 Foundation Scaffold

## Metadata
- Date: 2026-08-10
- Plan step: Weeks 1–2 — Repo scaffold, env/config, mock JWT login, LiteLLM provider + MockLLM, 4 mock services (FastAPI + seeded SQLite), trace schema defined (ref_docs/plan.md §11 row 1)
- Scope: Build the Milestone 1 foundation: scaffold, auth, llm, trace, services, gateway hello.
- Status: complete
- State at start: repo had only `ref_docs/` + `agent_history/` (docs), no code. Git HEAD `c6c4390`. Python 3.14.3 + uv 0.9.18 available.

## Objective
Deliverable: services run; role-aware hello; tests green offline. Code at repo root (user chose repo-root layout over `fyp-agent-harness/` subfolder; uv; gateway shell on :8001 for hello; full TraceStore + integrity chain).

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 08:00 | explore | Read plan.md §11 row 1, proposed_architecture.md §2/§12, session_001 + INSTRUCTIONS. Confirmed scope = scaffold + auth + llm + 4 services + trace schema. |
| 08:05 | decision | Asked 4 questions (layout, tooling, hello form, trace depth). User chose: repo root, uv, gateway shell :8001, full TraceStore. |
| 08:10 | implement | Scaffold: pyproject.toml (uv, fastapi/uvicorn/pydantic-settings/PyJWT/httpx/litellm, dev group pytest+ruff), .env.example, .gitignore, Makefile, scripts/run_dev.sh (ports 8000–8005). |
| 08:20 | implement | app/config.py — pydantic-settings Settings with nested jwt/llm/services models, `env_nested_delimiter="__"` (JWT__SECRET etc.), lru_cached get_settings(). |
| 08:30 | implement | app/auth — users.py (5 seeded users), jwt.py (PyJWT HS256 create/verify, AuthError for expired/invalid), service.py (POST /auth/login, GET /auth/me). |
| 08:40 | implement | app/llm — base.py (LLMProvider protocol, LLMResponse, ToolCall), mock.py (scripted deterministic MockLLM), litellm.py (lazy-import LiteLLMProvider), factory.py (mock default). |
| 08:50 | implement | app/ops — schema.py (TraceEventType enum per arch §12), store.py (SQLite append-only + per-run prev_hash chain + trace_runs head hash). |
| 09:00 | implement | app/services — lib.py generic CRUD builder (schema + seed → FastAPI app), crm/hr/devops/finance modules with realistic seeds (hr includes salary/ssn PII for later sanitize milestone). |
| 09:10 | implement | app/gateway/app.py — POST /hello verifies JWT, returns role-aware greeting (harness shell for weeks 3–4). |
| 09:15 | test | First `uv run pytest`: 5 failures. Two root causes. |
| 09:20 | fix | Trace bug: add_event stored the event's OWN hash as prev_hash instead of the previous event's hash. Fixed by using trace_runs.head_hash as prev_hash and storing that; verification logic already expected this semantics. |
| 09:25 | fix | FastAPI body quirk: `body: dict = Body(...)` + extra defaulted params caused FastAPI to EMBED the body (`loc ["body","body"]`); with `cols: list[str]` alone FastAPI treated the list param as the BODY. Root cause: a bare `list[str]` param is misclassified as body. Rewrote lib.py to register routes via a factory `_add_table_routes` so `table`/`cols` are closure variables, not endpoint params; POST reads raw JSON via `Request.json()`. |
| 09:35 | fix | Live run revealed `sqlite3.OperationalError: unable to open database file` for the default `data/*.sqlite` path — `_connect` now mkdirs the parent dir. |
| 09:40 | lint | ruff: fixed F401, UP017 (datetime.UTC), SIM113 (enumerate), PLR1733, BLE001 (catch json.JSONDecodeError), import sorting. |
| 09:45 | fix | Lengthened dev JWT secret to ≥32 bytes to silence PyJWT InsecureKeyLengthWarning; removed `uv.lock` from .gitignore (lockfile should be committed). |
| 09:50 | verify | 32/32 tests pass, ruff clean, live servers verified: login → /hello for alice & bob, crm customers seeded, invalid token → 401. |

## What was done
- `pyproject.toml`: uv project, deps pinned with lower bounds, dev group (pytest, ruff), pytest config (testpaths, pythonpath=".").
- `.env.example`, `.gitignore`, `Makefile` (install/test/lint/run + per-service), `scripts/run_dev.sh` (all 6 processes on :8000–8005).
- `app/config.py`: Settings (jwt/llm/services) via pydantic-settings; offline-safe defaults (`LLM__PROVIDER=mock`).
- `app/auth/`: `users.py` (alice/bob/carol/dave/admin), `jwt.py` (create/verify, AuthError), `service.py` (POST /auth/login, GET /auth/me).
- `app/llm/`: `base.py`, `mock.py` (deterministic MockLLM), `litellm.py` (lazy LiteLLMProvider), `factory.py`.
- `app/ops/`: `schema.py` (TraceEventType), `store.py` (TraceStore: start_run/add_event/end_run/get_run_tree/verify_integrity with SHA-256 prev_hash chain + head-hash table).
- `app/services/`: `lib.py` (generic CRUD from schema+seed), `crm.py`/`hr.py`/`devops.py`/`finance.py` (seeded, PII in hr).
- `app/gateway/app.py`: role-aware `/hello` (harness shell).
- `tests/`: conftest, test_auth (7), test_llm (5), test_trace (7), test_services (8), test_smoke (3) = 32 offline tests.
- `README.md`: quickstart, run, hello demo, config, next milestones.

## Decisions locked in this session
- **Code lives at repo root** (pyproject.toml/app/tests next to ref_docs/ + agent_history/), not in a `fyp-agent-harness/` subfolder.
- **uv** is the package manager; **uv.lock is committed** (reproducibility).
- **Role-aware hello** = minimal FastAPI gateway shell on :8001 (POST /hello) that the LangGraph gateway grows into in weeks 3–4.
- **Trace** = full TraceStore now (SQLite append-only, prev_hash chain + per-run head hash), wiring into the agent later.
- Services are generated from a **shared generic CRUD builder** (`lib.make_app`) — each dept is schema+seed only; FastAPI list/GET/POST/DELETE endpoints per table (matches future tool names like `crm_search_customer` → GET /customers?q=).
- Mock user dev passwords documented in README; PII (salary/ssn/email) seeded now so the week 9 sanitizer has data to mask.

## Verification performed
- `uv run pytest` → 32 passed, 2 warnings (upstream Starlette httpx2 deprecation only), fully offline (MockLLM, no API key).
- `uv run ruff check app tests` → All checks passed.
- Live run (`uv run uvicorn ... --port 800x`): login as alice + bob → `/hello` returns correct role/dept greetings; `GET :8002/customers?q=acme` returns seeded row; garbage token → 401. All processes killed after; `data/` cleaned up.

## Blocker / open questions
- None. (litellm installed fine on Python 3.14.)
- Note: FastAPI + a bare `list[str]` function parameter is treated as a body parameter — the route-registration factory pattern in `lib.py` avoids this; future sessions must not add list-typed endpoint params.

## Next step (for the next session)
- Execute **Weeks 3–4 (Milestone 2)**: LangGraph agent + HTTP tools to the 4 dept services + guardrails node (max steps, tool-call budget, deny-retry, timeout). Bounded agent loop; deny-storm test passes (MockLLM in CI). Gateway :8001 grows from /hello into the real chat endpoint.

## Handoff notes
- `.env.example` documents every setting; defaults require no `.env` — tests are offline by default.
- Trace event types already match arch §12 and audit-log conventions (allow/deny decisions go to the JSONL audit log in weeks 5–6).
- Service tool/endpoint mapping for weeks 3–4: crm_search_customer→GET /customers?q=, crm_create_campaign→POST /campaigns, hr_delete_employee→DELETE /employees/{id}, devops_promote→POST /deployments/{id}/promote (to add), finance_transfer→POST /transfers.
- `make run` starts all six processes; `make test` runs the offline suite; `make lint` runs ruff.

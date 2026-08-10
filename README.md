# fyp-agent-harness

A zero-trust security gateway & harness middleware for an Enterprise AI Personal Assistant.
Every resource — **tools, memory, skills** — is policy-enforced by role, not by what the LLM happens to decide.

> **Status: Milestone 2 (Weeks 3–4)** — LangGraph agent loop + HTTP tools to the dept services + guardrails (max steps, tool budget, deny-retry, timeout), wired into the gateway as `POST /chat`. See `ref_docs/plan.md` §11.

## Repo layout

```
app/
├── auth/       mock JWT login + verify (:8000)
├── llm/        LiteLLMProvider + MockLLM (script + sequence modes) + factory
├── ops/        SQLite trace store (append-only, integrity-hashed)
├── agent/      LangGraph graph + HTTP tool registry/executor + scope gate + guardrails
├── gateway/    /hello + /chat (:8001)
└── services/   crm (:8002) hr (:8003) devops (:8004) finance (:8005) — FastAPI + seeded SQLite
tests/          offline test suites (auth, llm, trace, services, agent, tools, chat)
```

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```bash
uv sync                 # install deps into .venv
uv run pytest           # 55 tests, fully offline (MockLLM, no API keys)
```

## Run everything

```bash
make run               # starts auth :8000, gateway :8001, crm/hr/devops/finance :8002-8005
```

Or individually: `make auth gateway crm hr devops finance`.

## Watch the agent work (offline demo)

```bash
make demo
```

Runs the real Milestone 2 agent in-process with a scripted MockLLM and prints,
per scenario: the agent's tool calls + outcomes (allowed/denied), scope-gate
denials, guardrail hits, and one run's full trace event tree. Scenarios:
alice's allowed CRM search (executes against seeded data), alice's cross-dept
HR delete (denied), bob's deny-storm (bounded at the tool budget). Fully
offline — no services, no API keys.

## Role-aware hello (Milestone 1 deliverable)

```bash
# 1. login → JWT (claims: sub, role, dept, scopes)
curl -s -X POST localhost:8000/auth/login \
  -H 'content-type: application/json' \
  -d '{"username":"alice@acme.com","password":"alice123"}'

# 2. gateway verifies the token and greets by role
curl -s -X POST localhost:8001/hello \
  -H 'content-type: application/json' \
  -d '{"token":"<access_token>"}'
# => Hello alice@acme.com! You are a marketing_agent in the marketing department.
```

## Chat (Milestone 2)

`POST /chat` runs one **bounded, traced** agent turn (stateless; conversation threads
come with the memory milestone). The run uses a scripted `MockLLM` by default, so live
calls exercise the plumbing (JWT → LangGraph loop → trace) and return an echo reply;
scripted tool runs are covered by the test suite.

```bash
curl -s -X POST localhost:8001/chat \
  -H 'content-type: application/json' \
  -d '{"token":"<access_token>","message":"find customer acme"}'
# => {"reply":"...","run_id":"...","guardrail_hits":[],"tool_calls":[]}
```

Guardrails (configurable via `GUARDRAILS__*`): `max_steps=10`, `tool_budget=8`,
`call_timeout_seconds=30`, `run_timeout_seconds=120`. A denied tool is terminal for the
run (deny-retry); repeats are short-circuited. Every run lands in `data/trace.sqlite`.

Mock users: `alice@acme.com` (marketing), `bob@acme.com` (hr), `carol@acme.com` (finance),
`dave@acme.com` (devops), `admin@acme.com` (admin). Dev passwords are `alice123`, `bob123`, `carol123`, `dave123`, `admin123`.

## Configuration

Copy `.env.example` to `.env` to override settings. Defaults are fully offline:
`LLM__PROVIDER=mock` (no API key needed). Set `LLM__PROVIDER=litellm` and `LLM__MODEL`/`LLM__API_KEY`
to use a real model (Gemini free tier default).

## Tests

`uv run pytest` — all offline:
- auth: JWT round-trip, expiry, tamper detection, login/me endpoints
- llm: MockLLM determinism (script + sequence), factory routing (mock vs litellm)
- trace: chain building, tamper/truncation detection via SHA-256 integrity chain
- services: seeded data, CRUD, field validation, PII present for the later sanitize milestone
- tools: registry ↔ service route mapping, HTTP calls, scope gate, call timeout
- agent: single/multi-step runs, cross-dept denial, deny-storm bounded, budget/max-steps/timeouts, trace written + integrity
- chat: `/chat` 200/401 + real agent path via dependency override
- smoke: role-aware hello for every role end-to-end

## Next milestones

1. Weeks 5–6: Casbin RBAC core — `filter_tools`, `intercept`, audit log
2. Weeks 7–8: 3-tier + episodic memory
3. Weeks 9–10: PII sanitizer + HITL interrupts + dashboard v1

See `ref_docs/plan.md` for the full 16-week plan and `ref_docs/proposed_architecture.md` for the implementation blueprint.

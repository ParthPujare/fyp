# fyp-agent-harness

A zero-trust security gateway & harness middleware for an Enterprise AI Personal Assistant.
Every resource — **tools, memory, skills** — is policy-enforced by role, not by what the LLM happens to decide.

> **Status: Milestone 1 (Weeks 1–2)** — repo scaffold, mock JWT auth, pluggable LLM (MockLLM for offline CI), 4 seeded mock dept services, trace schema. See `ref_docs/plan.md` §11.

## Repo layout

```
app/
├── auth/       mock JWT login + verify (:8000)
├── llm/        LiteLLMProvider + MockLLM + factory
├── ops/        SQLite trace store (append-only, integrity-hashed)
├── gateway/    harness shell — role-aware /hello (:8001)
└── services/   crm (:8002) hr (:8003) devops (:8004) finance (:8005) — FastAPI + seeded SQLite
tests/          offline test suites (auth, llm, trace, services, smoke)
```

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```bash
uv sync                 # install deps into .venv
uv run pytest           # 32 tests, fully offline (MockLLM, no API keys)
```

## Run everything

```bash
make run               # starts auth :8000, gateway :8001, crm/hr/devops/finance :8002-8005
```

Or individually: `make auth gateway crm hr devops finance`.

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

Mock users: `alice@acme.com` (marketing), `bob@acme.com` (hr), `carol@acme.com` (finance),
`dave@acme.com` (devops), `admin@acme.com` (admin). Dev passwords are `alice123`, `bob123`, `carol123`, `dave123`, `admin123`.

## Configuration

Copy `.env.example` to `.env` to override settings. Defaults are fully offline:
`LLM__PROVIDER=mock` (no API key needed). Set `LLM__PROVIDER=litellm` and `LLM__MODEL`/`LLM__API_KEY`
to use a real model (Gemini free tier default).

## Tests

`uv run pytest` — all offline:
- auth: JWT round-trip, expiry, tamper detection, login/me endpoints
- llm: MockLLM determinism, factory routing (mock vs litellm)
- trace: chain building, tamper/truncation detection via SHA-256 integrity chain
- services: seeded data, CRUD, field validation, PII present for the later sanitize milestone
- smoke: role-aware hello for every role end-to-end

## Next milestones

1. Weeks 3–4: LangGraph agent + HTTP tools + guardrails node
2. Weeks 5–6: Casbin RBAC core — `filter_tools`, `intercept`, audit log
3. Weeks 7–8: 3-tier + episodic memory
4. Weeks 9–10: PII sanitizer + HITL interrupts + dashboard v1

See `ref_docs/plan.md` for the full 16-week plan and `ref_docs/proposed_architecture.md` for the implementation blueprint.

# fyp-agent-harness

A zero-trust security gateway & harness middleware for an Enterprise AI Personal Assistant.
Every resource — **tools, memory, skills** — is policy-enforced by role, not by what the LLM happens to decide.

> **Status: Milestone 4 (Weeks 7–8)** — 3-tier + episodic memory on top of the Casbin RBAC core: a persistent `SqliteBaseStore` (3 tiers: central / dept / user), rule-based fact extraction with dedup + per-tier budget, FTS5 **episodic** transcript recall (scope-gated), a frozen memory snapshot injected per session, **thread-backed `/chat`** (LangGraph checkpointer), and **HITL** for central writes (`interrupt()` + SQLite approval store + `/approvals/{id}/decide`). See `ref_docs/plan.md` §11.

## Repo layout

```
app/
├── auth/       mock JWT login + verify (:8000)
├── llm/        LiteLLMProvider + MockLLM (script + sequence modes) + factory
├── ops/        SQLite trace store (append-only, integrity-hashed)
├── memory/     SqliteBaseStore + scopes, fact extraction, editor (budget), episodic FTS5 recall, approvals, snapshot
├── agent/      LangGraph graph (load_memory/agent/tools/finalize/reflect) + HTTP tool registry + guardrails
├── harness/    Casbin policies (model.conf + policy.csv) + enforcer + filter_tools + intercept + audit log
├── gateway/    /hello + /chat (threads) + /approvals/{id}/decide (:8001)
└── services/   crm (:8002) hr (:8003) devops (:8004) finance (:8005) — FastAPI + seeded SQLite
tests/          offline test suites (auth, llm, trace, services, agent, tools, chat, policy, memory)
```

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```bash
uv sync                 # install deps into .venv
uv run pytest           # 95 tests, fully offline (MockLLM, no API keys)
```

## Run everything

```bash
make run               # starts auth :8000, gateway :8001, crm/hr/devops/finance :8002-8005
```

Or individually: `make auth gateway crm hr devops finance`.

## Watch the agent work (offline demo)

```bash
make demo            # Milestone 3: tool calls + RBAC denials + trace + audit
make demo-memory     # Milestone 4: 3-tier writes, HITL central approval, recall, threads
```

`make demo` runs the real Milestone 3 agent in-process with a scripted MockLLM and prints,
per scenario: the agent's tool calls + outcomes (allowed/denied), Casbin RBAC
denials, guardrail hits, one run's full trace event tree, and the JSONL audit
log (every ALLOW/DENY decision). Scenarios: alice's allowed CRM search
(executes against seeded data), alice's cross-dept HR delete (denied by Casbin),
bob's deny-storm (bounded at the tool budget). Fully offline — no services, no
API keys.

`make demo-memory` exercises the memory milestone: alice's dept/user facts are
policy-checked and committed, alice's central write is DENIED, admin's central
write suspends for HITL (approve → committed, reject → not), `recall` runs an
FTS5 search over only the caller's readable scopes, and a 2-turn thread shows
the frozen snapshot is loaded once and reused.

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

## Chat (Milestone 2 + 3 + 4)

`POST /chat` runs one **bounded, traced, policy-enforced** agent turn. Since
Milestone 4 the endpoint is **thread-backed**: pass `thread_id` (default
`user:<sub>`) and the conversation persists across turns via the LangGraph
checkpointer; the frozen memory snapshot is loaded once per thread and reused.
Central-tier memory writes **suspend** the run for HITL approval; admins decide
via `POST /approvals/{id}/decide` and the run resumes.

```bash
curl -s -X POST localhost:8001/chat \
  -H 'content-type: application/json' \
  -d '{"token":"<access_token>","message":"find customer acme","thread_id":"t1"}'
# => {"reply":"...","run_id":"...","guardrail_hits":[],"tool_calls":[],"thread_id":"t1",
#     "interrupted":false,"approval_id":null}
```

### Memory (3-tier, policy-enforced)

- **Tiers** — `central` (company-wide), `dept/<name>` (self-dept), `user/<uid>` (self).
- **`load_memory`** — on session start, asks Casbin which scopes the caller may *read*,
  distills a frozen snapshot (budget-capped) and injects it as a system message; reused
  for subsequent turns of the thread.
- **Facts** — rule/heuristic extraction (markers like *prefers / policy / our team*),
  dedup via normalized match, `replace`/`remove` by unique substring, and oldest-out
  eviction per tier budget (`MEMORY__*`).
- **Episodic** — every completed session's transcript is appended to an FTS5 table
  (dept tier) and searchable via the `recall` tool, restricted to the scopes the caller
  may read.
- **Writes are enforced** — a marketing agent cannot write central (only admin, with
  HITL); dept/user writes are self-scoped by the enforcer; every decision is audited
  (`ALLOW`/`DENY`/`APPROVED`/`REJECTED`).

### RBAC (Casbin, deny-by-default)

- **`filter_tools`** — only the caller's role-permitted tool schemas enter the LLM context (a Marketing user never *sees* HR/Finance/DevOps tools).
- **`intercept`** — every tool call is re-checked at runtime; an unauthorized call is physically blocked and returns a permission error, even if the model tries.
- **`audit log`** — every ALLOW/DENY decision is appended to `data/audit.jsonl` (JSONL, `run_id`-linked with the trace).
- Policies live in `app/harness/policies/{model.conf,policy.csv}`; adding a department is data, not code.

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
- tools: registry ↔ service route mapping, HTTP calls, Casbin gate, call timeout
- agent: single/multi-step runs, cross-dept denial, deny-storm bounded, budget/max-steps/timeouts, trace written + integrity
- chat: `/chat` 200/401 + real agent path via dependency override
- policy: Casbin policy matrix (role × dept × resource), deny-by-default, admin wildcard, `filter_tools` visibility, `intercept` gate + audit, audit determinism
- memory: scopes/encoding, SqliteBaseStore CRUD + filter, editor (dedup/replace/remove/budget), fact extraction, episodic FTS5 scope-gating, recall service, Casbin memory matrix, snapshot, snapshot injection, thread persistence, dept/user writes, central-write HITL (approve/reject/deny/resume-guard)
- smoke: role-aware hello for every role end-to-end

## Next milestones

1. Weeks 9–10: PII sanitizer + HITL dashboard v1
2. Weeks 11–12: skills registry + trace viewer + evals

See `ref_docs/plan.md` for the full 16-week plan and `ref_docs/proposed_architecture.md` for the implementation blueprint.

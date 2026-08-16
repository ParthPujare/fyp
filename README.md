# fyp-agent-harness

A zero-trust security gateway & harness middleware for an Enterprise AI Personal Assistant.
Every resource — **tools, memory, skills** — is policy-enforced by role, not by what the LLM happens to decide.

> **Status: Milestone 7 (Week 12)** — trajectory viewer + evals harness on top of every milestone before it: **the trace now records everything the model saw** (`PROMPT` / `REASONING` / `CONTEXT_INJECTION` events with a per-event `source`, PII-sanitized before persist), a dashboard **Ops tab** shows run trajectories filterable by source (admin-JWT), and an **auto-eval hook** scores every completed run (deterministic RBAC/trace-completeness checks + optional LLM-as-judge). Deliverables: *"ops tab shows run trajectories filterable by source; eval scores per run"*. See `ref_docs/plans/plan_mk1_core_harness.md` §7–§8.

> **Plans & traceability:** every plan (Mk1 = the original 16-week core, Mk2 = web-UI demo + member management) is indexed in `ref_docs/plans/INDEX.md`; sessions map to plans via `agent_history/INDEX.md`.

## Repo layout

```
app/
├── auth/       mock JWT login + verify (:8000)
├── llm/        LiteLLMProvider + MockLLM (script + sequence modes) + factory
├── ops/        SQLite trace store (append-only, integrity-hashed, per-event source)
├── memory/     SqliteBaseStore + scopes, fact extraction, editor (budget), episodic FTS5 recall, approvals, snapshot
├── agent/      LangGraph graph (load_memory/agent/tools/finalize/reflect) + HTTP tool registry + skills executor + guardrails + critical-tool HITL gate
├── harness/    Casbin policies (model.conf + policy.csv) + enforcer + filter_tools + intercept + audit log + PII sanitizer
├── skills/     folder/markdown skill registry (model, registry, executor) — skills exposed as policy-enforced tools
├── evals/      auto-eval hook: deterministic checklist (trace integrity/completeness, RBAC contract, audit pairs, guardrails) + LLM-as-judge suite
├── gateway/    /hello + /chat (threads) + /approvals/{id}/decide (:8001)
├── dashboard/  FastAPI + static web UI: approvals, users, departments, skills editor, memory browser, ops trajectory viewer (:8006)
└── services/   crm (:8002) hr (:8003) devops (:8004) finance (:8005) — FastAPI + seeded SQLite
tests/          offline test suites (auth, llm, trace, services, agent, tools, chat, policy, memory, sanitize, dashboard, skills, evals)
skills/         seed skills: central/triage_incident, dept/hr/new_leave_request, dept/marketing/launch_campaign
```

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12.

```bash
uv sync                 # install deps into .venv
uv run pytest           # 140+ tests, fully offline (MockLLM, no API keys)
```

## Run everything

```bash
make run               # starts auth :8000, gateway :8001, dashboard :8006, crm/hr/devops/finance :8002-8005
```

Or individually: `make auth gateway dashboard crm hr devops finance`.

## Watch the agent work (offline demo)

```bash
make demo            # Milestone 3: tool calls + RBAC denials + trace + audit
make demo-memory     # Milestone 4: 3-tier writes, HITL central approval, recall, threads
make demo-sanitize   # Milestone 5: PII masking, critical-tool HITL, recall PII
make demo-skills     # Milestone 6: per-role skill schemas, nested skill execution, skill deny, hot reload
make demo-evals      # Milestone 7: trajectory filtered by source, deterministic + judge eval scores, EVAL events traced
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

`make demo-sanitize` exercises Milestone 5: bob sees masked salary/ssn in an HR
search while admin sees full values; carol's `finance_transfer` (risk=critical)
suspends for HITL; admin approves one transfer (executed) and rejects another
(never executed); and a recalled transcript shows its SSN masked.

`make demo-skills` exercises Milestone 6 (fully offline): per-role skill schema
visibility (alice/bob/carol each see only their skills), alice invoking
`launch_campaign` whose nested `crm_*` calls execute, carol's `triage_incident`
ALLOWed at the top level but the nested `devops_create_deployment` DENIED by
role (skill outcome denied, audit shows both decisions; admin is allowed), and a
**hot reload** — editing the running skill's `tool_calls` block on disk changes
which nested tools execute on the very next run.

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

## Chat (Milestone 2 + 3 + 4 + 5)

`POST /chat` runs one **bounded, traced, policy-enforced** agent turn. Since
Milestone 4 the endpoint is **thread-backed**: pass `thread_id` (default
`user:<sub>`) and the conversation persists across turns via the LangGraph
checkpointer; the frozen memory snapshot is loaded once per thread and reused.
Central-tier memory writes and **`risk=critical` tools** (e.g. finance transfers)
**suspend** the run for HITL approval; admins decide via
`POST /approvals/{id}/decide` (JWT-verified, admin role required) or from the
dashboard, and the run resumes.

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

### PII sanitizer + dashboard (Milestone 5)

- **Sanitize** — every tool response and recalled transcript passes through
  `app/harness/sanitize.py` before returning to the LLM. Structured payloads are
  masked by field name, free text by email/SSN regex. Policy matrix
  (`pii_email`/`pii_salary`/`pii_ssn`): **email visible to all roles; salary and
  ssn admin-only** (deny-by-default).
- **Critical-tool HITL** — tools with `risk=critical` (finance transfers) suspend
  via `interrupt()`; nothing executes until an admin approves, and a rejection
  returns a denial and is never retried in that run.
- **Dashboard (:8006)** — static HTML/JS panel over a FastAPI backend. Login
  proxies the auth service; every `/api/*` endpoint except `/api/login` requires a
  verified admin JWT. Pending approvals list (sensitive tool args masked), Approve
  / Reject buttons that resume the exact suspended thread, and read-only
  Users / Departments tabs.

### Skills (Milestone 6)

- **Registry** — `app/skills/` loads markdown skills (YAML front-matter: name,
  trigger, scope, owner_role, args_schema, steps, `tool_calls`) from
  `skills/{central,dept/<name>}/<skill>/skill.md` each run (hot reload: editing a
  file changes the next run's behavior).
- **Skill-as-tool** — a skill the caller is permitted to use appears in the tool
  list with an auto-generated schema; invoking it runs its `tool_calls` **through
  the same `intercept`/HITL/sanitize/guardrails pipeline as direct tools** — a
  nested step is subject to RBAC and can be DENIED independently of the skill.
- **Per-skill RBAC** — Casbin rows `skill_<name>, use` per role (underscore names:
  `:` is a route-param delimiter in `keyMatch2`). No row ⇒ the skill is invisible
  to the schema and cannot execute.
- **Dashboard editor + memory browser** — admin-JWT-gated CRUD over
  `/api/skills`/`/api/memory`, each create/edit/delete audit-logged.

### Trajectory viewer + evals (Milestone 7)

- **Trajectory recording** — every run now traces **everything the model saw**:
  `PROMPT` (the exact message list sent to the LLM), `REASONING` (the model's
  thinking channel when the provider emits one), `CONTEXT_INJECTION` (system
  prompt + frozen memory snapshot), tool I/O (**incl. the sanitized result
  summary the model received**), memory reads/writes, skill invocations,
  guardrail triggers. Every event carries a **`source`** (`llm`/`memory`/`tool`/
  `guardrail`/`hitl`/`user`/`system`/`eval`) so a run can be inspected by origin.
- **PII discipline** — raw prompts/reasoning/context pass through the same
  scope-aware sanitizer before persisting, so the trajectory stores nothing the
  run itself was not allowed to see.
- **Ops tab (dashboard :8006)** — admin-JWT. Run list → drill into any run's
  event tree, **filter by source**, expand PROMPT/REASONING/CONTEXT_INJECTION/
  tool-I/O payloads, and read that run's eval scores. Endpoints:
  `/api/ops/runs`, `/api/ops/runs/{id}?source=llm&source=tool`, `/api/ops/runs/{id}/evals`.
- **Auto-eval after every completed run** — a deterministic suite always runs:
  trace integrity, **trace completeness** (every `PROMPT` has a matching
  `REASONING`, contiguous seq, run start/end present), **RBAC contract** (no
  denied resource executed; every allowed/denied outcome matched by an audit
  decision), **audit pairing** (every audit entry maps to a trace event), and
  guardrail compliance. The **judge suite** (LLM-as-judge over
  response quality / injection resistance / skill faithfulness, 0–5 each) runs
  when a judge provider is injected (tests/demo = scripted MockLLM) or the app
  is on a real LiteLLM backend. Every result is written back as an `EVAL` event
  with per-run score; suspended (pre-approval) runs are evaluated only after
  they complete/resume.

### Attack suites + hardening (Milestone 8)

- **Attack Test Matrix A1–A8** — `tests/attack/` proves the harness holds when the
  model **fully complies** with an attack (enforcement is in code/policy, never
  the prompt). Threat model + mapping: `ref_docs/threat_model.md`.
  - **A1 policy matrix** — `test_policy_matrix.py` parses `policy.csv` as an
    oracle: full tool matrix, per-role allowed sets, memory matrix, skill matrix,
    unknown object/action/role → deny.
  - **A2/A3 injection** — `test_prompt_injection.py` + `test_cross_dept_memory.py`:
    a delivered injection in the user message, tool-output poisoning, and
    memory/recall poisoning are denied and audited across all three vectors.
  - **A4 skill misuse** — `test_skill_misuse.py`: cross-dept skills are invisible
    or denied, a nested forbidden tool inside an allowed skill is blocked, and
    `SkillRegistry.save` rejects a scope that escapes the registry root.
  - **A5 loop abuse** — `test_loop_abuse.py`: deny-storms short-circuit and end on
    a guardrail; budget / max-steps / run-timeout bounds hold; nothing runs past
    the terminal guardrail.
  - **A6 HITL bypass** — `test_hitl_bypass.py`: nothing executes before an admin
    approves, a rejection never executes on resume, non-admin and forged-JWT
    deciders get 403/401, a decided approval can't be re-decided.
  - **A7/A8 integrity** — `test_integrity.py`: editing/deleting/reordering/
    truncating an audit or trace entry is detected via the SHA-256 chain, and the
    deterministic EVAL's `audit_integrity`/`trace_integrity` checks surface the
    tampering.
- **Audit hardening** — `app/harness/audit.py` writes every decision in a
  per-line SHA-256 chain (`prev_hash`/`hash`, genesis `0×64`) mirrored to an
  `.head` sidecar; `verify_integrity()` flags any edit/reorder/delete/truncate.
  Pre-chain files stay readable (backwards compatible).
- **Run the evidence** — `make demo-attacks` or
  `uv run python scripts/demo_attacks.py`.

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

**Members (Mk2):** identity now lives in `data/members.sqlite` (`app/auth/store.py` — the single
source of truth; the 5 seeds are migrated on first open). Passwords are stored as scrypt hashes,
never plaintext. Login/`/auth/me`/`USERS` all read the store, every member change is audit-logged,
and a lockout guard prevents deactivating the last active admin (or yourself). Since **Phase 2**
the Casbin `g` (user→role→dept) assignments are **materialized live from the store for ACTIVE
members only** (`app/harness/enforcer.py`), so an unknown or disabled member is denied everywhere
by default — their existing token gets a **403** at the gateway/dashboard even before policy is
consulted. A `members_version` write counter lazily rebuilds the enforcer's `g` after any admin
edit (no restart needed). Member *management* UI lands in Mk2 Phase 4.

## Configuration

Copy `.env.example` to `.env` to override settings. Defaults are fully offline:
`LLM__PROVIDER=mock` (no API key needed). Set `LLM__PROVIDER=litellm` and `LLM__MODEL`/`LLM__API_KEY`
to use a real model (Gemini free tier default).

## Tests

`uv run pytest` — all offline:
- auth: JWT round-trip, expiry, tamper detection, login/me endpoints
- members: MemberStore seeding, scrypt hashing (no plaintext), create-then-login (molly), `/auth/me` reflects store edits, inactive login 401, lockout guard (last admin / self), reset password, audit logging, `USERS`/`claims_for` read the store
- llm: MockLLM determinism (script + sequence), factory routing (mock vs litellm)
- trace: chain building, tamper/truncation detection via SHA-256 integrity chain, per-event `source` derivation/override, `list_runs` summaries + legacy-DB migration
- services: seeded data, CRUD, field validation, PII present for the later sanitize milestone
- tools: registry ↔ service route mapping, HTTP calls, Casbin gate, call timeout
- agent: single/multi-step runs, cross-dept denial, deny-storm bounded, budget/max-steps/timeouts, critical-tool HITL (suspend → approve/reject, deny-retry), recall PII masking, trace written + integrity + trajectory (PROMPT/REASONING/CONTEXT_INJECTION recorded + sanitized)
- chat: `/chat` 200/401 + real agent path via dependency override, `/approvals/{id}/decide` 200/401/403/404/409 (admin JWT)
- policy: Casbin policy matrix (role × dept × resource), deny-by-default, admin wildcard, `pii_*` matrix, `filter_tools` visibility, `intercept` gate + audit, audit determinism
- enforcement (Mk2 Phase 2): `g` materialized from store actives, unknown/disabled deny-by-default, lazy version-guard rebuild (role/dept edits + deactivation reflected on a long-lived enforcer), gateway `/hello`/`/chat`/`decide` 403 for disabled/unknown, dashboard 403 for disabled admin, `members_version` increments on every mutation
- memory: scopes/encoding, SqliteBaseStore CRUD + filter, editor (dedup/replace/remove/budget), fact extraction, episodic FTS5 scope-gating, recall service, Casbin memory matrix, snapshot, snapshot injection, thread persistence, dept/user writes, central-write HITL (approve/reject/deny/resume-guard)
- sanitize: pii matrix, structured/nested masking, text regex masking, ToolResult summary regeneration, masked values never reach LLM, recall transcript masking
- dashboard: admin-JWT gates (401/403), pending list masking, decide 404/409 + resume, read-only users/departments, skills editor CRUD (policy_row flag), memory browser CRUD, ops trajectory viewer (runs list, source filter, evals)
- skills: parse/validation, skill RBAC matrix, per-role schema exposure, nested calls go through intercept (deny inside a skill), SKILL_USE trace + audit rows, hot reload on fresh settings
- evals: deterministic checks pass on clean runs / fail on missing audit pairs, judge suite (scripted MockLLM verdicts, parse/invalid-score handling), EVAL events traced + integrity preserved, eval-on-completion-only, trajectory PII masking
- attack: A1 policy-matrix oracle, A2/A3 prompt-injection (message + tool-output + memory poisoning), A4 skill misuse + registry path-escape, A5 loop-abuse (deny-storm / budget / steps / timeout), A6 HITL-bypass (pre-approval no-op, reject-never-executes, 403/401, double-decide), A7/A8 trace+audit integrity tamper detection
- smoke: role-aware hello for every role end-to-end

## Next milestones

1. **Plan Mk2** (`ref_docs/plans/plan_mk2_web_ui_members.md`): web-UI demo + member management — chat + any-user login, dynamic members/departments from the dashboard, browser memory test, then report + recorded web demo (this replaces the raw Mk1 week-15/16 stretch+report outlook below).
2. Mk1 stretch (weeks 15–16): `learn_skill` auto-gen + HITL; Telegram approval adapter; LangFuse adapter; trajectory search / replay / fork — remains optional.

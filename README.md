# fyp-agent-harness

A zero-trust security gateway & harness middleware for an Enterprise AI Personal Assistant.
Every resource — **tools, memory, skills** — is policy-enforced by role, not by what the LLM happens to decide.

> **Status**
>
> | Plan | Scope | State |
> |---|---|---|
> | **Mk1** | Core harness — RBAC + intercept, 3-tier memory, HITL, PII sanitizer, skills, trajectory + evals, attack suites A1–A8 | Milestones 1–8 (weeks 1–14) **done**; stretch + report (15–16) open |
> | **Mk2** | Web UI + member management — any-user chat, Members/Departments admin, browser memory-test | Phases 1–5 **done** (sessions 010–014); Phase 6 (report + recorded demo) open |
> | **Mk3** | BYOK — per-user LLM API keys, Fernet-encrypted at rest, admin sees status only | **done** (session 015) |
> | **Mk4** | ChatGPT/Hermes-style chat window — model selector per request, thread sidebar, memory auto-learning | **done** (session 016) |
> | **Mk5** | Direct provider layer (OpenAI / Gemini / Anthropic / OpenRouter / custom OpenAI-compatible), LiteLLM removed | **in progress** |
>
> **Plans & traceability:** every plan is indexed in `ref_docs/plans/INDEX.md`; sessions map to plans via `agent_history/INDEX.md`. Mk1 is the original 16-week plan and is frozen — append-only history, never rewritten.

## Repo layout

```
app/
├── auth/       mock JWT login + verify (:8000)
├── llm/        provider ABC + MockLLM (script + sequence modes) + direct providers
│              (openai/gemini/anthropic/groq/openrouter/custom) + registry + BYOK factory
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

## Run with Docker (recommended — one command)

Everything (auth, gateway, dashboard and the four mock department services) runs in **one
container**. You need only [Docker Desktop](https://www.docker.com/products/docker-desktop/) —
**running**, not just installed — and Git. No Python, `uv`, or `make` required.
(What this containerizes and why: `ref_docs/plans/plan_mk13_docker_cloud_db.md`, Mk13 Phase 0.)

### First time (new teammate, fresh clone)

```bash
git clone <repo-url>
cd fyp
docker compose up --build        # first build takes a few minutes; later starts take seconds
```

Then open **http://localhost:8006** and sign in with a seeded demo account — `alice@acme.com` /
`alice123` (marketing), `bob@acme.com` / `bob123` (hr), `carol@acme.com` / `carol123` (finance),
`dave@acme.com` / `dave123` (devops), `admin@acme.com` / `admin123` (admin).

That's all — **no `.env` needed**. Without one the app runs on its built-in `ENV=dev` defaults
(mock LLM, seeded accounts, fully offline), which is exactly what copying `.env.example` gives you.
Create `.env` (`cp .env.example .env`) only when you want to *change* something: a real LLM or
Composio key, a different port, etc. — then `docker compose up -d --build` again.

### Every day

| You want to… | Command |
|---|---|
| Start (background) | `docker compose up -d` — after a reboot Docker Desktop restarts it by itself, so often you just open the page |
| Open the app | http://localhost:8006 |
| Stop, keep all data | `docker compose down` |
| See logs | `docker compose logs -f` |
| **After `git pull` / any code change** | `docker compose up -d --build` — the code is baked into the image, so a plain `up` runs the *old* code |
| Wipe all data (fresh reseed) | `docker compose down -v` |

Data lives in a named Docker volume (`fyp_data`) and survives `down`/`up`/reboots; only `down -v`
deletes it. The "GET /health" lines in the logs are Docker's own healthcheck — normal.

### Things that will bite you (each one hit for real while testing)

- **Port 8006 already in use** → set `DASHBOARD_PORT=8007` (any free port) in `.env`, then
  `docker compose up -d`. Only the dashboard is published; the other services stay *inside* the
  container, so they can't collide with anything else on your laptop (that is what broke the
  first attempt — another project already owned `:8000`). Symptom of a stuck start: `docker ps`
  lists the container as healthy but its PORTS column shows `8000-8006/tcp` with no
  `0.0.0.0:…->` → run `docker compose down`, free the port, `docker compose up -d`.
- **Local models (LM Studio / Ollama)** run on *your machine*, but inside the container
  `localhost` means the container itself. In Account → LLM API Keys use
  `http://host.docker.internal:1234/v1` (not `http://localhost:1234/v1`). *(Not tested against a
  live LM Studio yet — if it refuses, enable "Serve on Local Network" in LM Studio.)*
- **Windows line endings** — `.gitattributes` forces LF on `*.sh`; without it Git for Windows
  would break the container's start script.
- **Raw API access for curl demos** ("Role-aware hello" below) is opt-in:
  `docker compose -f docker-compose.yml -f docker-compose.api-ports.yml up -d`, then use
  `localhost:18000` (auth) and `localhost:18001` (gateway) instead of `:8000` / `:8001`.
- **"Cannot connect to the Docker daemon"** → start Docker Desktop and wait until it says running.

### What is (and is not) shared today

Docker gives every teammate the **same app**, not the **same data**: each machine's container has
its own private volume (its own members, chats, approvals). Everyone starts from the same seeded
demo accounts and diverges from there. A single shared database for the whole team is the next
part of Mk13 (Neon Postgres, Phases 1–6 of the plan) and is **not built yet**.

## Native setup (development, running tests, editing code)

Requires [uv](https://docs.astral.sh/uv/) and Python ≥ 3.12. Use this for `uv run pytest` /
`ruff` while developing — Docker above doesn't run the test suite.

```bash
uv sync                 # install deps into .venv
uv run pytest           # 370+ tests, fully offline (MockLLM, no API keys)
```

## Run everything (native)

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
  / Reject buttons that resume the exact suspended thread, a **Members** tab
  (add / edit role+dept+scopes / activate / deactivate / reset password, lockout-
  guarded, audit-logged) and a **Departments** tab (register a department — the
  record lands in the store and a default `p, <role>, <dept>, *, *` row is
  appended to `policy.csv` so its role is immediately grantable).
- **Any-member chat (Mk2)** — active members sign in and get the Chat tab; the
  dashboard `POST /api/chat` verifies the member JWT and forwards it to the
  gateway (no CORS). Admin + member sessions coexist with an identity switcher.

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
  when a judge provider is injected (tests/demo = scripted MockLLM) or the app is
  configured with a real backend (any `LLM__PROVIDER` other than `mock`, with a
  key — a keyless config falls back to mock and the judge is skipped rather than
  scored on an echo). Every result is written back as an `EVAL` event
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
- **Web chat + any-user login (Mk2 Phase 3)** — `make demo-chat-web` or
  `uv run python scripts/demo_chat_web.py`: the dashboard `/api/chat` proxies a
  member's own JWT to the gateway `/chat` (no CORS) and returns the envelope the
  UI renders (reply / tool outcomes / guardrails / interrupted+approval_id).
  The member-chat demo shows an allowed CRM call + a visible cross-dept HR
  DENY and a finance `finance_transfer` that interrupts and lists in Approvals
  with masked args.

### Browser memory-test walkthrough (Mk2 Phase 5)

The 3-tier memory scoping scenario is now reproducible **with clicks only** in the
browser — `make run`, open `:8006`, and:

1. **Admin central write → HITL approve.** Sign in `admin@acme.com` / `admin123`
   (admin session). In **Chat** type `Company policy requires quarterly vendor audits.`
   — the run **suspends** with an amber "run suspended" hint. Open **Approvals**
   (same session), see the `memory / central` item, hit **Approve**; the fact
   commits. Verify under **Memory** (`central`) **and** in the new **"My memory"**
   panel below the chat box.
2. **Admin central + private facts.** In **Memory**, add a fact to `central` and
   one to `user/admin@acme.com` (the admin-only browser is still gated — a member
   token gets 403).
3. **alice dept + private facts.** Add the member session (`alice@acme.com` /
   `alice123`), switch to it, and in **Chat** type
   `Our team policy requires weekly standups. Alice prefers Slack over Teams.`
   — the reply is immediate (dept/user writes need no approval). The **My memory**
   panel now shows `dept/marketing` + `user/alice@acme.com` with those facts.
4. **2nd marketing member reads shared dept memory.** As admin, create `molly`
   (Marketing tab → + Add member). Add the member session (`molly@acme.com`).
   Her **My memory** panel shows `dept/marketing` **including alice's shared
   fact** (plus `central` and her own empty `user/molly@acme.com`) — shared dept
   memory visible to any marketing member.
5. **Cross-dept denial.** Add `bob@acme.com` (`bob123`) as the member session.
   His **My memory** panel shows only `central` / `dept/hr` / `user/bob@acme.com`
   — **no `dept/marketing`**. Chat `recall the decision about standups`: bob's
   recall returns nothing in his permitted scopes.

`GET /api/memory/me` (the second member-only API, beside `/api/chat`) returns the
caller's **readable** scopes — exactly what the agent's `load_memory` injects —
so the panel shows "what the agent reads for you", not a special admin view.
Automated coverage: `tests/test_memory_web.py` (268 offline tests total).

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
edit (no restart needed). Since **Phase 3** any active member can sign in to the dashboard
(`:8006`): non-admins see only the **Chat** tab, admins see Chat plus the admin tabs, and a
dual-session switcher lets a member and an admin stay signed in together (e.g. carol's suspended
transfer → switch to admin → Approvals). Since **Phase 4** the admin **Members** tab can add /
edit (role, dept, scopes) / activate / deactivate / reset members, and the **Departments** tab
registers new departments (the record lands in the store and a default
`p, <role>, <dept>, *, *` row is appended to `policy.csv` so the role is immediately grantable)
— both audited, with the lockout guard enforced server-side. Since **Phase 5** the 
member **Chat** tab gains a **"My memory"** panel backed by `GET /api/memory/me`
(the caller's readable scopes only — central + own dept + own user, Casbin-filtered),
so any member can see — with clicks — the shared dept memory (a 2nd marketing
member reads `dept/marketing`) and the cross-dept gap (an HR member sees no
`dept/marketing` scope).

## Configuration

Copy `.env.example` to `.env` to override settings. Defaults are fully offline:
`LLM__PROVIDER=mock` (no API key needed).

For a real model, either:

- **per member (preferred, Mk3)** — sign in and add a key in the dashboard **Account**
  tab. It is Fernet-encrypted at rest (`LLM__FERNET_KEY`), used server-side only, and
  never returned by any API; admins see provider/model status, never key material.
  A member's own key wins over the global config for their runs, and the chat model
  selector picks between their saved keys per message.
- **globally** — set `LLM__PROVIDER` (`openai` | `gemini` | `anthropic` | `groq` |
  `openrouter` | `custom_openai`), `LLM__MODEL`, and `LLM__API_KEY`. `LLM__TEMPERATURE` /
  `LLM__MAX_TOKENS` apply to both paths. Without a key the app stays on `MockLLM`,
  so the offline demo never breaks.

`custom_openai` points at any OpenAI-compatible endpoint (Ollama, vLLM, LM Studio, a
gateway) and needs a base URL — `LLM__BASE_URL` globally, or the Base URL field that
appears in the Account tab when you pick it. The Account dropdown offers exactly the
providers the registry can build, so an unsupported provider is rejected at save time
instead of failing on the first message.

## Tests

`uv run pytest` — all offline:
- auth: JWT round-trip, expiry, tamper detection, login/me endpoints
- members: MemberStore seeding, scrypt hashing (no plaintext), create-then-login (molly), `/auth/me` reflects store edits, inactive login 401, lockout guard (last admin / self), reset password, audit logging, `USERS`/`claims_for` read the store
- llm: MockLLM determinism (script + sequence), factory routing (mock vs configured provider), the saved BYOK model + decoding settings reaching the request for openai/gemini/anthropic, per-call model override, namespaced OpenRouter default, `anthropic/` prefix stripped
- trace: chain building, tamper/truncation detection via SHA-256 integrity chain, per-event `source` derivation/override, `list_runs` summaries + legacy-DB migration
- services: seeded data, CRUD, field validation, PII present for the later sanitize milestone
- tools: registry ↔ service route mapping, HTTP calls (incl. `param_aliases` — the `query` arg reaches the services as `q`, so a search actually filters), Casbin gate, call timeout
- agent: single/multi-step runs, cross-dept denial, deny-storm bounded, budget/max-steps/timeouts, critical-tool HITL (suspend → approve/reject, deny-retry), recall PII masking, trace written + integrity + trajectory (PROMPT/REASONING/CONTEXT_INJECTION recorded + sanitized)
- chat: `/chat` 200/401 + real agent path via dependency override, `/approvals/{id}/decide` 200/401/403/404/409 (admin JWT)
- policy: Casbin policy matrix (role × dept × resource), deny-by-default, admin wildcard, `pii_*` matrix, `filter_tools` visibility, `intercept` gate + audit, audit determinism
- enforcement (Mk2 Phase 2): `g` materialized from store actives, unknown/disabled deny-by-default, lazy version-guard rebuild (role/dept edits + deactivation reflected on a long-lived enforcer), gateway `/hello`/`/chat`/`decide` 403 for disabled/unknown, dashboard 403 for disabled admin, `members_version` increments on every mutation
- web chat (Mk2 Phase 3): dashboard `/api/chat` proxy contract (member JWT → gateway envelope), 401/403/502 handling, member token denied on admin endpoints, marketing chat → allowed CRM + denied HR visible, carol `finance_transfer` interrupts and shows in Approvals with masked args, login claims route admin vs member sessions
- members/depts management (Mk2 Phase 4): department store table + seeding, `register_department` (version bump, audit, default `<name>_agent` role), admin member CRUD (create→login, duplicate 409, short-pw 422, edit role/dept/scopes, unknown 404, deactivate → gateway 403 → reactivate 200, server-side lockout, reset password), department registration through the API (record + `p` row appended to a temp policy copy + new role grantable via a temp-policy enforcer but denied cross-dept)
- memory: scopes/encoding, SqliteBaseStore CRUD + filter, editor (dedup/replace/remove/budget), fact extraction, episodic FTS5 scope-gating, recall service, Casbin memory matrix, snapshot, snapshot injection, thread persistence, dept/user writes, central-write HITL (approve/reject/deny/resume-guard)
- sanitize: pii matrix, structured/nested masking, text regex masking, ToolResult summary regeneration, masked values never reach LLM, recall transcript masking
- dashboard: admin-JWT gates (401/403), pending list masking, decide 404/409 + resume, store-driven users/departments (seeded + registered), skills editor CRUD (policy_row flag), memory browser CRUD, ops trajectory viewer (runs list, source filter, evals)
- memory web (Mk2 Phase 5): the browser memory-test e2e — admin central write via `/api/chat` → HITL suspend → decide approve → committed (real resumer), admin central+private facts via the browser, alice dept+private chat facts, `GET /api/memory/me` scope contract for members, **molly reads shared dept/marketing**, bob has no marketing scope + his `recall` finds nothing, member-API gates (401/403), trace integrity after the walkthrough
- skills: parse/validation, skill RBAC matrix, per-role schema exposure, nested calls go through intercept (deny inside a skill), SKILL_USE trace + audit rows, hot reload on fresh settings
- evals: deterministic checks pass on clean runs / fail on missing audit pairs, judge suite (scripted MockLLM verdicts, parse/invalid-score handling), EVAL events traced + integrity preserved, eval-on-completion-only, trajectory PII masking
- attack: A1 policy-matrix oracle, A2/A3 prompt-injection (message + tool-output + memory poisoning), A4 skill misuse + registry path-escape, A5 loop-abuse (deny-storm / budget / steps / timeout), A6 HITL-bypass (pre-approval no-op, reject-never-executes, 403/401, double-decide), A7/A8 trace+audit integrity tamper detection
- smoke: role-aware hello for every role end-to-end

## Next milestones

1. **Plan Mk5** (`ref_docs/plans/plan_mk5_custom_byot.md`): finish the direct provider layer. Note the two Mk5 docs currently disagree — `plan_mk5_custom_byot.md` keeps the multi-tenant encrypted vault, `plan_mk5_local_byok.md` replaces it with single-user `.env` config. Pick one before building further.
2. **Plan Mk2 Phase 6** (`ref_docs/plans/plan_mk2_web_ui_members.md`): report + recorded web demo — the only open Mk2 item.
3. Mk1 stretch (weeks 15–16): `learn_skill` auto-gen + HITL; Telegram approval adapter; LangFuse adapter; trajectory search / replay / fork — remains optional.
4. Backlog in `ref_docs/todo.md`: dept-head approval role for dept-tier writes, real external tools behind skills (Composio-style), per-user auto-generated skills, admin-only dual-session login.

# Agent Harness RBAC — Final Project Plan

A zero-trust security gateway & harness middleware for an Enterprise AI Personal Assistant.
Every resource — **tools, memory, skills** — is policy-enforced by role, not by what the LLM happens to decide. The harness also **bounds the loop**, **traces every run**, and **evals itself** — because a harness that can't prove what it did and how well it did it isn't a harness, it's a prompt.

---

## 1. Project Overview

### The Problem It Solves
- **Prompt-level rules fail**: system prompts like "do not allow non-admin users to delete records" are advisory, non-deterministic, and bypassable via prompt injection.
- **All-or-nothing access**: AI agents operate with blanket credentials; there is no infrastructure layer forcing a Marketing user to be unable to run DevOps or HR commands.
- **Uncontrolled multi-step actions**: LLM execution loops run without deterministic gates, risking unintended external mutations.

### The Solution
Instead of deploying separate agents per team, a **single central assistant** adapts dynamically to the authenticated user's corporate role. The **harness** sits between the LLM and real-world tools, enforcing deterministic RBAC on every action:

1. **Dynamic tool exposure** — only role-permitted tool schemas enter the model's context.
2. **Deterministic execution interception** — unauthorized tool calls are physically blocked at runtime, returning a permission error (no LLM judgment involved).
3. **Human-in-the-Loop (HITL) gate** — high-risk tools suspend execution and route to an admin approval dashboard.
4. **Payload & PII sanitization** — sensitive data is masked based on user permissions before returning to the LLM.
5. **Audit log** — every allow/deny/approve decision is recorded as evidence.

The harness goes beyond tool gates. It also:
- **Bounds the agent loop** (max steps, tool-call budget, deny-retry policy, timeout) so multi-step runs terminate deterministically.
- **Traces every agent run** to a local SQLite event tree (tools, tokens, latency) — evidence of what the agent actually did.
- **Evals every run** with deterministic contract checks plus an LLM-as-judge, so the harness's own health is measurable.

### Out of Scope
- LLM training / fine-tuning (off-the-shelf models via API only).
- Building an agentic framework from scratch (LangGraph handles orchestration).
- Enterprise identity provisioning (no Okta/AD; identity = simple JWT claims).
- Automated AI safety evals / benchmarks (deterministic test suites + audit logs instead).

---

## 2. Decisions & Preferences (Locked In)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| 1 | Language | **Python** | User is comfortable with Python; aligns with all chosen frameworks |
| 2 | Build style | **Industry-style, framework-first** | Minimal hand-rolled infra; use LangGraph and other frameworks the way industry would |
| 3 | Agent framework | **LangGraph** | Native `interrupt()` for HITL, checkpointers, BaseStore, custom node guards |
| 4 | Policy engine | **Casbin** | Declarative model.conf + policy.csv; native RBAC-with-domains; easy to explain in thesis |
| 5 | LLM backend | **Pluggable via LiteLLM** | No paid API, no local model. Default = Gemini free tier (dev). Adapters for OpenAI & Claude. `MockLLM` for deterministic tests/CI |
| 6 | Tool realism | **Local REST microservices** | Each department = small FastAPI service backed by real SQLite with realistic schemas + seed data. Tools call these over HTTP like a real internal gateway |
| 7 | Memory tiers | **Three tiers + episodic** | central (company-wide) + department (`dept/<name>`) + user-private (`user/<uid>`) semantic facts, **plus searchable past-session recall per tier** |
| 8 | Memory mechanism | **LangGraph checkpointer (short-term) + BaseStore (long-term semantic) + episodic transcript store; rule-based extraction, substring dedup, per-tier budget, HITL on central writes** | Thread-scoped history + namespaced facts + searchable session history; deterministic update flow (see §5) |
| 9 | Skills approach | **Staged: authored (folder/markdown registry) first, auto-learned as stretch** | Authored skills (dashboard/config) are deterministic + testable core; Hermes-style `learn_skill` node added later if time allows, gated by HITL |
| 10 | Management | **Full admin + ops dashboard** | Same web dashboard handles HITL approvals, user management, department management, skill approval — plus ops views: memory browser/editor, skill editor, trace viewer, eval viewer |
| 11 | Departments | **Marketing (CRM), HR, DevOps, Finance + Admin** | New dept = Casbin domain rows + service registration; NO harness code changes |
| 12 | HITL approval UI | **Simple web dashboard first** | Telegram/WhatsApp approval adapter = stretch goal (extension point only) |
| 13 | Identity | **Mock JWT login** | Claims: `sub`, `role`, `dept`, `scopes`. Real SSO out of scope |
| 14 | Deliverable | **Report + working demo** | Code structured so it is easy to document (modules mapped to concepts, audit logs as evidence) |
| 15 | Timeline | **One semester (~12–16 weeks)** | Focused vertical slice; no feature creep |
| 16 | Loop control | **Guardrails node in the graph** | Max steps/run, tool-call budget, deny-retry policy, timeout — bounds the multi-step risk called out in the problem statement |
| 17 | Tracing | **Custom local SQLite trace store** | Offline, deterministic, shares event conventions with the audit log; LangFuse/LangSmith are documented stretch adapters |
| 18 | Evals | **Auto-eval after every run: deterministic + LLM-as-judge** | Deterministic RBAC/tool-contract checks + judge evals (response quality, injection-resistance); results stored and traced |

---

## 3. Architecture

```
User ──(JWT: sub, role, dept)──▶  Central Assistant (LangGraph)
                                      │
                          ┌───────────▼────────────┐
                          │  THE HARNESS            │
                          │  1. filter_tools        │ Casbin: which tools can role see?
                          │  2. intercept           │ runtime block before execution
                          │  3. guardrails          │ max steps / tool budget / deny-retry
                          │  4. load_memory         │ inject only permitted memory tiers (+ episodic recall)
                          │  5. reflect             │ policy-checked memory writes
                          │  6. sanitize            │ PII masking on tool output
                          │  7. HITL gate           │ interrupt → dashboard approval
                          │  8. trace (SQLite)      │ every run, tool call, token, latency
                          │  9. eval hook           │ deterministic + judge evals after each run
                          │ 10. audit log (JSONL)   │ every allow/deny/approve decision
                          └───────────┬────────────┘
                    ┌─────────────────┼──────────────────────┐
                    ▼                 ▼                      ▼
        /crm /hr /devops /finance   Memory store             Skills store
        (FastAPI + SQLite)          (3-tier + episodic)      (folder/markdown registry)
```

### Enforcement Chain
```
JWT → verify signature + expiry
    → extract (role, dept, scopes)
    → Casbin enforce(user, dept, obj, act)
    → grant / deny
```
Role assignment lives in Casbin (`g, alice@acme.com, marketing_agent`) or assignment CSV / admin panel. Role change = new JWT on next login.

---

## 4. Proposed Repository Structure

> **Status (2026-08-10):** code lives at the **repo root** (next to `ref_docs/` + `agent_history/`), not inside a `fyp-agent-harness/` subfolder. `✅` = built; unmarked = planned milestone.

```
fyp-agent-harness/            ← this repo (root: FYP/)
├── pyproject.toml            ✅ uv project (fastapi, uvicorn, pydantic-settings, PyJWT, litellm)
├── .env.example              ✅ all settings with dev defaults (offline-safe: LLM__PROVIDER=mock)
├── README.md                 ✅ quickstart: install, run services, hello demo, test
├── Makefile                  ✅ install / test / lint / run / per-service targets
├── scripts/run_dev.sh        ✅ launches all 6 processes on :8000–8005
├── app/
│   ├── config.py             ✅ pydantic-settings: jwt, llm, services (env_nested_delimiter="__")
│   ├── auth/                 ✅ mock JWT login, role claims, token verify — service on :8000
│   ├── llm/                  ✅ LiteLLM provider + MockLLM adapter + factory
│   ├── ops/                  ✅ trace schema + SQLite TraceStore (append-only, SHA-256 chain)
│   ├── gateway/              ✅ JWT-verified /hello + /chat (bounded LangGraph run) (:8001)
│   ├── agent/                ✅ LangGraph graph + HTTP tools to services; guardrails node
│   │   └── guardrails.py     ✅ step limit, tool-call budget, deny-retry, timeout (Wk 3–4)
│   ├── harness/              planned — THE CORE: policy, interception, sanitize, audit [Wk 5–6]
│   │   ├── policies/         planned — casbin model.conf + policy.csv
│   │   ├── intercept.py      planned — runtime enforcement wrapper
│   │   ├── filter.py         planned — per-role tool schema filtering
│   │   ├── sanitize.py       planned — PII masking
│   │   └── audit.py          planned — structured JSONL logging
│   ├── evals/                planned — auto-eval hook + deterministic/ + judge/ suites [Wk 12]
│   ├── dashboard/            planned — FastAPI + web UI: approvals, users, depts, skills
│   │                         planned —   editor, memory browser, trace viewer, evals [Wk 9–10]
│   ├── memory/               planned — 3-tier + episodic stores, load/reflect/recall nodes,
│   │                         planned —   extract, dedup, budget [Wk 7–8]
│   ├── skills/               planned — folder/markdown skill registry + (stretch) learn_skill [Wk 11]
│   └── services/             ✅ crm/hr/devops/finance (FastAPI + SQLite + seed) :8002–8005
└── tests/                    ✅ offline suites: auth, llm, trace, services, smoke — 32 tests
```

---

## 5. Memory Model (3 Tiers + Episodic)

### 5.1 Tier Matrix

| Tier | Namespace | Read | Write |
|---|---|---|---|
| Central | `("central",)` | all roles | admins |
| Department | `("dept", <name>)` | dept members | dept members + admins |
| User-private | `("user", <uid>)` | the user (admin w/ compliance) | the user |

Memory access is a **permission**, not a framework default: both **read** (inject) and **write** (reflect) go through Casbin checks, identical to tool enforcement.

### 5.2 Storage Layers

| Layer | Mechanism | Scope | Purpose |
|---|---|---|---|
| Short-term | LangGraph **checkpointer** (SQLite) | one thread/conversation | conversation history + intermediate state; updates automatically on every node |
| Long-term semantic | LangGraph **`BaseStore`** namespaces | `("central",)` / `("dept", <name>)` / `("user", <uid>)` | durable facts, preferences, runbooks, cross-session knowledge |
| **Episodic** | **SQLite FTS5 transcript store, one table per tier namespace** | same 3 scopes | **searchable past-session history — Hermes-style recall of "what happened before"** |
| Snapshot | compiled system-prompt block | current session | Hermes-style always-active curated memory |

### 5.3 Memory Nodes

1. **`load_memory` node** (graph start, Hermes-style): asks Casbin "which memory scopes can role X read?" → loads *central* + own *dept* + own *user* scopes → distills into a **compact, frozen snapshot** injected into the system prompt. A Marketing user never gets HR or another user's memory injected. The snapshot is frozen for the session (stable prompt prefix, cacheable); writes apply to the *next* session's snapshot.
2. **`recall` node / tool** (on-demand, same policy check): full-text search over **episodic** transcripts within the user's permitted scopes — the "search past conversations" pattern from the reference harness. Scoped exactly like semantic reads.
3. **`reflect` node** (graph end, post-response, not in the hot path): extracts durable facts from the conversation and routes each write to the correct scope **through a policy check** — a Marketing user attempting to write into `central` or `hr` gets a denial, same gate as any tool. The user never waits on writes.

### 5.4 Write / Update Flow (full mechanics)

```
conversation + tool outputs
        │
        ▼
 1. extract     rule/heuristic-based: pull durable facts
                (patterns: "prefers X", "team policy Y", "uses Z",
                 recurring parameters, corrections to prior facts)
        │
        ▼
 2. classify    which tier? → central / dept/<name> / user/<uid>
        │
        ▼
 3. enforce     Casbin: can (role, dept) WRITE to that scope?
        │
      allow                       deny
        ▼                         ▼
 4. dedup + upsert          rejected + audit-logged
    (add / replace / remove)      (no memory change)
        │
        ▼
 5. HITL gate (central only):
    writes to ("central",) suspend → admin approval
    → approved: commit + audit / denied: discard + audit
    dept + user writes commit automatically after RBAC
        │
        ▼
 6. commit to BaseStore namespace + audit log entry
```

### 5.5 Update Operations (per fact, not whole-file rewrites)

- **`add`** — insert a new fact.
- **`replace`** — update an existing fact, matched by a **unique substring** (Hermes-style). An ambiguous match (substring hits multiple facts) errors out and asks for a more specific match.
- **`remove`** — delete an outdated fact (same substring-match rules).

### 5.6 Extraction Method (decision locked)

- **Core: rule/heuristic-based extraction** — deterministic, offline-testable, fits the "tests over evals" philosophy. Patterns capture preferences, conventions, repeated parameters, and corrections. No LLM call required; works with `MockLLM` in CI.
- **Stretch: LLM-assisted extraction** — a node that summarizes long or complex exchanges into facts, gated the same way. Deferred because it is non-deterministic and needs a review step.

### 5.7 Deduplication (decision locked)

- **Core: exact / unique-substring match** — cheap, deterministic. If a new fact's normalized text matches an existing fact, it replaces instead of duplicating.
- **Stretch: embedding-similarity dedup** — cosine threshold against an index; only if a vector store is added.

### 5.8 Budget & Condensation (decision locked)

- Per-tier **caps** (e.g., central `8KB`, dept `16KB`, user `4KB`) enforced at write time.
- **Eviction: oldest-out** within the tier; eviction is audit-logged.
- **Stretch: LLM condensation** — when a tier is near cap, summarize oldest facts into a compact digest.

### 5.9 Write Confirmation (decision locked)

- **Central writes → HITL approval** required (suspends via LangGraph `interrupt()`, appears in dashboard, resumes/aborts on admin decision).
- **Dept + user writes → automatic** after RBAC allow.
- All outcomes (allowed / denied / approved / rejected) are audit-logged.

### 5.10 Cross-Tier Security Tests

- Marketing user cannot **read** HR or another user's tier.
- Marketing user cannot **write** to `central` (denied by Casbin) or `hr` (denied by Casbin).
- Dept member writes to own dept tier commit; central writes require HITL.
- Same input → same decision, verified repeatedly (audit determinism).
- Episodic recall respects the same gates: Marketing cannot full-text search HR or another user's transcripts.

---

## 6. Loop Engineering & Guardrails

Bounding the multi-step execution loop — the "end-loop guardrails" from the reference harness:

- **Max steps per run** (e.g., 10) — hard stop; final reply summarizes partial progress.
- **Tool-call budget** (e.g., 8) — past the cap the LLM is forced into a final answer.
- **Deny-retry policy** — a Casbin-denied tool call is terminal for that tool in the run; the agent receives the permission error **once** and must route around it, never retry-loop on a blocked tool.
- **Timeout** per tool call + per run.
- **Graceful termination** — hitting any guardrail ends the loop, produces a final reply, and is traced + audit-logged.

This is enforcement, not prompting: the guardrails node is deterministic graph logic that cannot be bypassed by the model.

### Guardrail Tests
- Loop terminates under a **deny-storm** (agent keeps requesting a blocked tool).
- Tool-call budget respected with `MockLLM` in CI.
- Timeout triggers clean termination + trace/audit entry.

---

## 7. Tracing & Observability

Custom local SQLite trace store (`app/ops/`) sharing event conventions with the audit log:

- **Run tree per agent run**: input → per-node events → tool calls (name, args, result summary, **tokens in/out, latency**) → memory loads → skill invocation → guardrail triggers → final output.
- **Trace viewer in dashboard** ("ops" tab): drill into any run's event tree.
- **Immutable, append-only** trace with per-entry integrity hashing (report evidence).
- LangFuse/LangSmith = documented **stretch adapters** only; not required.

Trace answers "what did the agent do"; audit answers "what did policy decide". Both are written for every run.

---

## 8. Evals (LLM Ops)

Auto-eval hook runs **after every agent run**; results persisted in the trace store:

- **`deterministic/`** — rule-based checks: RBAC contract holds (no unauthorized tool/memory access in the trace), audit entry exists for every decision, guardrails respected, eval determinism (same input → same result).
- **`judge/`** — **LLM-as-judge** via LiteLLM (same Gemini/Mock backend): response quality, prompt-injection resistance (did any injected instruction trigger an unauthorized action?), skill faithfulness.
- Every eval run is itself traced + audited.

The suite doubles as the thesis's "tests over evals" evidence with a judge layer bolted on.

---

## 9. Skills (Procedural Memory)

A skill = named, versioned procedure, stored as a **folder with `skill.md`**:
```
skills/<scope>/<name>/
├── skill.md   # name, trigger, steps[], tool_calls[], args_schema, scope, owner_role
└── (optional resources referenced by the skill)
```
Human-editable, hot-reloadable, versioned.

### Development Routes
- **Authored (core, deterministic):** Dept admins write skills via dashboard editor or the folder registry. Fully testable.
- **Auto-learned (stretch, Hermes-style):** `learn_skill` node runs after a successful multi-step run, LLM-distills the tool-call sequence into a skill, stores it **pending admin approval via the HITL gate**. Reversible; always admin-gated.

### RBAC on skills
A Marketing skill is invisible to HR; a skill calling `devops` tools is blocked by the intercept layer even if invoked.

---

## 10. Departments, Users & Admin/Ops Dashboard

### Initial Departments
Marketing (CRM), HR, DevOps, Finance, plus Admin (owns central memory, policy edits, approvals).

### Adding a New Department (data, not code)
1. Add domain rows to `policy.csv` (e.g., `p, marketing_agent, crm.read, allow`).
2. Register the dept's mock service + tools in the service registry.
3. Create the `("dept", <name>)` memory namespace.
The harness, interception, and memory code never change.

### User Assignment
Identity comes from the **mock JWT**: claims carry `sub`, `role`, `dept`, `scopes`. Admin panel or assignment CSV manages `g, user, role` mappings. Access changes without touching code.

### Dashboard — Two Faces
- **Admin**: HITL approvals, user management (add/assign roles), department management (add dept), skill approval, policy/csv view.
- **Ops**: memory browser/editor per tier, skill editor, trace viewer, eval results.

Every change written to the audit log.

---

## 11. Multi-Step Plan — Timeline (16 Weeks)

| Weeks | Milestone | Deliverable | Status |
|---|---|---|---|
| 1–2 | Repo scaffold, env/config, mock JWT login, LiteLLM provider + MockLLM, 4 mock services (FastAPI + seeded SQLite), **trace schema defined** | Services run; role-aware hello; tests green offline | ✅ **DONE 2026-08-10** (session_002) |
| 3–4 | LangGraph agent + HTTP tools + **guardrails node** (max steps, tool budget, deny-retry, timeout) | Bounded agent loop; deny-storm test passes | ✅ **DONE 2026-08-10** (session_003) |
| 5–6 | **Casbin core** (pulled earlier): model.conf + policy.csv (RBAC + domains), `filter_tools`, `intercept`, audit log | Unauthorized tool physically blocked | |
| 7–8 | **3-tier + episodic memory**: BaseStore namespaces, `load_memory`/`reflect`/`recall`, frozen snapshot, extract/dedup/budget/eviction | Multi-turn chat with RBAC-scoped recall incl. past-session search | |
| 9–10 | PII sanitizer + HITL interrupts + dashboard v1 (approvals, users, depts) | Admin approves finance transfer from web panel | |
| 11 | Folder/markdown skills + skill RBAC + **skills editor + memory browser** | Edit a skill → behavior changes on next run | |
| 12 | **Trace viewer + evals harness** (deterministic + judge), auto-eval hook | Ops tab shows run trees; eval scores per run | |
| 13–14 | Attack suites: policy matrix, prompt-injection, cross-dept memory, skill misuse, loop-abuse, HITL-bypass, trace/audit integrity + hardening | Attack demo passes | |
| 15 | Stretch: `learn_skill` auto-gen + HITL; Telegram approval adapter; LangFuse adapter | Optional extensions | |
| 16 | Report + README + **recorded demo script** (login as roles → tool use → deny → HITL → memory recall + skill → trace + eval review) | Report + demo | |

---

## 12. Report Evidence (Naturally Produced)

- **Threat model** — who/what can trigger which mutation.
- **Policy matrix** (roles × depts × resources) — doubles as the test suite.
- **Cross-tier memory denial tests** — Marketing can't read HR or another user's memory.
- **Prompt-injection attack suite** — proving interception, not prompts, enforces access.
- **Audit log determinism** — "same input → same deny decision".
- **Trace trees** proving the loop: run → tool calls → tokens/latency → guardrail behavior.
- **Eval scores** (deterministic + judge) proving the harness holds under attack.
- **Loop-guardrail tests** — deny-storm bounded, budget respected.

---

## 13. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| No paid API / no local model | Gemini free tier for dev via LiteLLM; MockLLM for deterministic CI tests (no network/cost dependency) |
| Judge evals non-deterministic | Runs on MockLLM in CI; same LLM backend as dev; score + trace stored as evidence |
| Auto-learned skills unreliable | Staged behind HITL approval; always reversible |
| LangGraph version churn | Pin versions in pyproject.toml; use stable interrupt/store APIs |
| Scope creep | User-private tier + admin panel are in scope; auto-learning + Telegram + LangFuse stay stretch-goal only |

---

## 14. Learning Objectives (How This Teaches Industry Practice)

- Layered architecture with a clear security boundary (harness = the interesting layer).
- Policy-as-data (Casbin) vs policy-as-prompt — the core thesis.
- Deterministic enforcement vs probabilistic (LLM) reasoning.
- HITL workflows using framework-native interrupts (LangGraph).
- Loop engineering — bounding autonomous execution with deterministic guardrails.
- Structured audit + tracing as the foundation for compliance/observability.
- Eval-driven iteration (deterministic + LLM-as-judge) as an LLM Ops feedback loop.
- Pluggable provider abstraction (LiteLLM) for swapping LLM backends.

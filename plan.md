# Agent Harness RBAC — Final Project Plan

A zero-trust security gateway & harness middleware for an Enterprise AI Personal Assistant.
Every resource — **tools, memory, skills** — is policy-enforced by role, not by what the LLM happens to decide.

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
| 7 | Memory tiers | **Three tiers** | central (company-wide) + department (`dept/<name>`) + user-private (`user/<uid>`) |
| 8 | Memory mechanism | **LangGraph checkpointer (short-term) + BaseStore (long-term); rule-based extraction, substring dedup, per-tier budget, HITL on central writes** | Thread-scoped history + namespaced facts; deterministic update flow (see §5) |
| 9 | Skills approach | **Staged: authored first, auto-learned as stretch** | Authored skills (dashboard/config) are deterministic + testable core; Hermes-style `learn_skill` node added later if time allows, gated by HITL |
| 10 | Management | **Full admin panel** | Same web dashboard handles HITL approvals, user management, department management, and skill approval |
| 11 | Departments | **Marketing (CRM), HR, DevOps, Finance + Admin** | New dept = Casbin domain rows + service registration; NO harness code changes |
| 12 | HITL approval UI | **Simple web dashboard first** | Telegram/WhatsApp approval adapter = stretch goal (extension point only) |
| 13 | Identity | **Mock JWT login** | Claims: `sub`, `role`, `dept`, `scopes`. Real SSO out of scope |
| 14 | Deliverable | **Report + working demo** | Code structured so it is easy to document (modules mapped to concepts, audit logs as evidence) |
| 15 | Timeline | **One semester (~12–16 weeks)** | Focused vertical slice; no feature creep |

---

## 3. Architecture

```
User ──(JWT: sub, role, dept)──▶  Central Assistant (LangGraph)
                                      │
                          ┌───────────▼────────────┐
                          │  THE HARNESS            │
                          │  1. filter_tools        │ Casbin: which tools can role see?
                          │  2. intercept           │ runtime block before execution
                          │  3. load_memory         │ inject only permitted memory tiers
                          │  4. reflect             │ policy-checked memory writes
                          │  5. sanitize            │ PII masking on tool output
                          │  6. HITL gate           │ interrupt → dashboard approval
                          │  7. audit log (JSONL)   │
                          └───────────┬────────────┘
                    ┌─────────────────┼─────────────────┐
                    ▼                 ▼                 ▼
        /crm  /hr  /devops  /finance   Memory store    Skills store
        (FastAPI + SQLite, seeded)     (3-tier scoped)  (authored + learned)
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

```
fyp-agent-harness/
├── pyproject.toml
├── .env.example
├── README.md
├── app/
│   ├── auth/            # mock JWT login, role claims, token verify
│   ├── llm/             # LiteLLM provider + MockLLM adapter
│   ├── agent/           # LangGraph graph, nodes, tools (HTTP to services)
│   ├── harness/         # THE CORE: policy, interception, sanitize, audit
│   │   ├── policies/    # casbin model.conf + policy.csv
│   │   ├── intercept.py # runtime enforcement wrapper
│   │   ├── filter.py    # per-role tool schema filtering
│   │   ├── sanitize.py  # PII masking
│   │   └── audit.py     # structured JSONL logging
│   ├── dashboard/       # FastAPI + web UI: approvals, users, depts, skills
│   ├── memory/          # 3-tier store, load_memory/reflect nodes, extract, dedup, budget
│   ├── skills/          # authored skills + (stretch) learn_skill node
│   └── services/        # /crm  /hr  /devops  /finance (FastAPI + SQLite + seed)
└── tests/               # enforcement + attack test suites
```

---

## 5. Memory Model (3 Tiers)

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
| Long-term | LangGraph **`BaseStore`** namespaces | `("central",)` / `("dept", <name>)` / `("user", <uid>)` | durable facts, preferences, runbooks, cross-session knowledge |
| Snapshot | compiled system-prompt block | current session | Hermes-style always-active curated memory |

### 5.3 Memory Nodes

1. **`load_memory` node** (graph start, Hermes-style): asks Casbin "which memory scopes can role X read?" → loads *central* + own *dept* + own *user* scopes → distills into a **compact, frozen snapshot** injected into the system prompt. A Marketing user never gets HR or another user's memory injected. The snapshot is frozen for the session (stable prompt prefix, cacheable); writes apply to the *next* session's snapshot.
2. **`reflect` node** (graph end, post-response, not in the hot path): extracts durable facts from the conversation and routes each write to the correct scope **through a policy check** — a Marketing user attempting to write into `central` or `hr` gets a denial, same gate as any tool. The user never waits on writes.

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

---

## 6. Skills (Procedural Memory)

A skill = named, versioned procedure:
```
{ name, trigger, steps[], tool_calls[], args_schema, scope, owner_role }
```
Stored in the store under `("skills", <scope>)`.

### Development Routes
- **Authored (core, deterministic):** Dept admins write skills via dashboard/config. Fully testable.
- **Auto-learned (stretch, Hermes-style):** `learn_skill` node runs after a successful multi-step run, LLM-distills the tool-call sequence into a skill, stores it **pending admin approval via the HITL gate**. Reversible; always admin-gated.

### RBAC on skills
A Marketing skill is invisible to HR; a skill calling `devops` tools is blocked by the intercept layer even if invoked.

---

## 7. Departments & User Management

### Initial Departments
Marketing (CRM), HR, DevOps, Finance, plus Admin (owns central memory, policy edits, approvals).

### Adding a New Department (data, not code)
1. Add domain rows to `policy.csv` (e.g., `p, marketing_agent, crm.read, allow`).
2. Register the dept's mock service + tools in the service registry.
3. Create the `("dept", <name>)` memory namespace.
The harness, interception, and memory code never change.

### User Assignment
Identity comes from the **mock JWT**: claims carry `sub`, `role`, `dept`, `scopes`. Admin panel or assignment CSV manages `g, user, role` mappings. Access changes without touching code.

### Admin Panel
Single web dashboard for: HITL approvals, user management (add/assign roles), department management (add dept), and skill approval. Every change written to the audit log.

---

## 8. Timeline (16 Weeks)

| Weeks | Milestone | Deliverable |
|---|---|---|
| 1–2 | Repo scaffold, env/config, mock JWT login, LiteLLM provider + MockLLM, 4 mock services (FastAPI + seeded SQLite) | Services run; role-aware hello; tests green offline |
| 3–5 | LangGraph agent + HTTP tools; 3-tier memory store + load/reflect nodes | Multi-turn chat with RBAC-scoped memory recall |
| 6–8 | Casbin policies (RBAC + domains), tool filtering, interception, permission errors, audit log | Unauthorized tool physically blocked |
| 9–10 | PII sanitizer + HITL interrupts + authored skills + full admin dashboard | Admin approves finance transfer from web panel |
| 11–12 | pytest suites (policy matrix, prompt-injection, cross-dept memory, skill misuse), audit hardening | Attack demo passes |
| 13–14 | Stretch: `learn_skill` auto-generation w/ HITL approval; Telegram/WhatsApp approval adapter | Optional extensions |
| 15–16 | Report writing + demo script + README | Report + demo |

---

## 9. Report Evidence (Naturally Produced)

- **Threat model** — who/what can trigger which mutation.
- **Policy matrix** (roles × depts × resources) — doubles as the test suite.
- **Cross-tier memory denial tests** — Marketing can't read HR or another user's memory.
- **Prompt-injection attack suite** — proving interception, not prompts, enforces access.
- **Audit log determinism** — "same input → same deny decision".

---

## 10. Risks & Mitigations

| Risk | Mitigation |
|---|---|
| No paid API / no local model | Gemini free tier for dev via LiteLLM; MockLLM for deterministic CI tests (no network/cost dependency) |
| Auto-learned skills unreliable | Staged behind HITL approval; always reversible |
| LangGraph version churn | Pin versions in pyproject.toml; use stable interrupt/store APIs |
| Scope creep | User-private tier + admin panel are in scope; auto-learning + Telegram stay stretch-goal only |

---

## 11. Learning Objectives (How This Teaches Industry Practice)

- Layered architecture with a clear security boundary (harness = the interesting layer).
- Policy-as-data (Casbin) vs policy-as-prompt — the core thesis.
- Deterministic enforcement vs probabilistic (LLM) reasoning.
- HITL workflows using framework-native interrupts (LangGraph).
- Structured audit logging as the foundation for compliance/observability.
- Pluggable provider abstraction (LiteLLM) for swapping LLM backends.

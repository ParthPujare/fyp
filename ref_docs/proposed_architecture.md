# Proposed Architecture — Agent Harness RBAC

Detailed architecture flow for the Enterprise AI Personal Assistant secured by a zero-trust harness. This document is the implementation blueprint derived from `plan.md`. Every flow below maps to a module in the proposed repository structure and to a test in the attack/verification suites.

> **Implementation status (2026-08-10):** Milestones 1–5 (weeks 1–10) are built. Live: `auth` :8000, `gateway` :8001 (role-aware `/hello`, thread-backed `/chat`, admin-JWT `/approvals/{id}/decide`), all four dept services :8002–8005 with seeded SQLite, and the **`dashboard`** :8006 (approvals queue, users, departments). `app/agent/` has the LangGraph agent (graph now `load_memory → agent ⇄ execute_tools → finalize → reflect`), HTTP tool registry/executor (incl. the memory `recall` tool), guardrails node with the trace schema (§12) + `TraceStore` wired into every run, and the **critical-tool HITL gate** (risk=critical suspends via `interrupt()`, resumes only after admin approval/rejection). `app/harness/` holds the Casbin core: `policies/{model.conf,policy.csv}` (tool + memory + `pii_*` rows), `PolicyEnforcer` (incl. `memory_allowed`/`memory_recall_allowed`), `filter_tools`, `intercept`, the JSONL audit log — the `scope_allowed` stub is gone — and **`sanitize.py`** (scope-aware PII masking applied to every tool response + recalled transcript). `app/memory/` (weeks 7–8) is built: `SqliteBaseStore` (3 tiers: central / dept / user) as a persistent LangGraph `BaseStore`, rule-based fact extraction + dedup + per-tier budget/eviction, FTS5 episodic recall (scope-gated), frozen snapshot per session, LangGraph checkpointer for threads, and HITL `interrupt()` + SQLite `ApprovalStore` for central writes. Skills (week 11) and evals (week 12) remain planned per `ref_docs/plan.md` §11. **Trajectory-style tracing** — recording *everything the model saw* (system prompts, context injections, reasoning) with a by-source trajectory viewer — is folded into the week-12 trace/evals milestone per `ref_docs/plan.md` §7. Code lives at the repo root next to `ref_docs/` + `agent_history/`.

---

## 1. System Context

```
┌────────────────────────────────────────────────────────────────────────────┐
│                          ENTERPRISE (single company)                        │
│                                                                              │
│   Marketing  HR  DevOps  Finance  Admin                                      │
│      │         │      │        │      │                                      │
│      └─────────┴──────┴────────┴──────┘  (human users, browsers / chat UI)   │
│                              │                                              │
│                              ▼                                              │
│                    ┌───────────────────┐                                    │
│                    │   CENTRAL ASSISTANT │   ONE assistant, many roles       │
│                    │   (LangGraph agent) │   adapts to the JWT identity      │
│                    └─────────┬─────────┘                                    │
│                              │                                              │
│                         ┌────▼────┐                                         │
│                         │ HARNESS │   zero-trust enforcement boundary       │
│                         └────┬────┘                                         │
│       ┌──────────────────────┼───────────────────────┐                     │
│       ▼                      ▼                       ▼                     │
│  dept microservices     Memory stores           Skills registry             │
│  /crm /hr /devops      3-tier semantic           folder/markdown            │
│  /finance               + episodic               procedural memory          │
│  (FastAPI+SQLite)       (BaseStore + SQLite)                                │
└────────────────────────────────────────────────────────────────────────────┘
```

**Key principle:** the harness is the *only* path from the LLM to anything that mutates or reads real state. No tool, memory scope, or skill is ever reachable outside the enforcement boundary.

---

## 2. Runtime Topology

| Process | Role | Persistence | Port (dev) | Status (M1) |
|---|---|---|---|---|
| `auth` service | Mock JWT issue/verify | none (stateless) | 8000 | ✅ live |
| `harness` gateway | LangGraph runtime + enforcement | SQLite checkpointer, trace store | 8001 | ✅ live — `/hello`, thread-backed `/chat`, `/approvals/{id}/decide` |
| `/crm` service | Marketing dept tools | SQLite | 8002 | ✅ live (seeded) |
| `/hr` service | HR dept tools | SQLite | 8003 | ✅ live (seeded) |
| `/devops` service | DevOps dept tools | SQLite | 8004 | ✅ live (seeded) |
| `/finance` service | Finance dept tools | SQLite | 8005 | ✅ live (seeded) |
| `dashboard` | Admin + ops web UI | same DBs (read) | 8006 | ✅ live (Wk 9–10) |
| LLM backend | LiteLLM → Gemini / OpenAI / Claude / MockLLM | none | external | provider ready; wired into graph Wk 3–4 |

All dept services are **local FastAPI** processes seeded with realistic data (customers, employees, incidents, ledgers). Tools in the agent call these over HTTP — the same call shape a real internal gateway would use.

---

## 3. Request Flow — End to End

```
1. USER LOGIN
   client ──POST /auth/login {username, password}──▶ auth
   auth: resolve user → (sub, role, dept, scopes) → sign JWT
   ──200 { access_token }──▶ client

2. USER CHAT
   client ──POST /harness/chat {token, message}──▶ gateway

3. JWT VERIFY (gateway, always first)
   verify signature + expiry  →  400 if invalid

4. AUTH CONTEXT
   extract (sub, role, dept, scopes) → attach to every downstream check

5. GRAPH ENTRY ──  THE HARNESS PIPELINE  ──▶ LLM ──▶ GRAPH EXIT
   (details in §4–§9; the graph nodes in §11)
```

Every step after (3) that touches a tool, memory, or skill passes through a Casbin decision before execution.

---

## 4. The Harness Pipeline (per agent run)

```
        ┌──────────────────────────────────────────────────────────┐
        │  HARNESS PIPELINE (executed in graph order)              │
        │                                                          │
        │  ┌───────────────┐   ┌───────────────┐   ┌───────────┐  │
        │  │ 1 filter_tools│   │ 2 load_memory │   │ 3 recall  │  │
        │  │   per-role    │   │  inject only  │   │  FTS5     │  │
        │  │   schemas     │   │  permitted    │   │  within   │  │
        │  │               │   │  scopes       │   │  scopes   │  │
        │  └──────┬────────┘   └──────┬────────┘   └─────┬─────┘  │
        │         ▼                  ▼                   ▼         │
        │  ┌───────────────────────────────────────────────────┐  │
        │  │            LLM (LiteLLM / MockLLM)                │  │
        │  │  sees ONLY: filtered tools + scoped memory +      │  │
        │  │  frozen snapshot + conversation history           │  │
        │  └───────────────────────────────────────────────────┘  │
        │         │  emits tool_calls or final answer            │
        │         ▼                                               │
        │  ┌───────────────┐   ┌───────────────┐   ┌───────────┐  │
        │  │ 4 intercept   │   │ 5 sanitize    │   │ 6 HITL    │  │
        │  │  Casbin block │   │  mask PII     │   │  suspend  │  │
        │  │  on each call │   │  by scope     │   │  high-risk│  │
        │  └──────┬────────┘   └──────┬────────┘   └─────┬─────┘  │
        │         ▼                  ▼                   ▼         │
        │  ┌───────────────┐   ┌───────────────┐   ┌───────────┐  │
        │  │ 7 guardrails  │   │ 8 reflect     │   │ 9 trajectory +│  │
        │  │  loop bounds  │   │  policy-      │   │  eval hook    │  │
        │  │  deny-retry   │   │  checked      │   │  (model saw)  │  │
        │  │               │   │  memory writes│   │               │  │
        │  └───────────────┘   └───────────────┘   └───────────┘  │
        │                                                          │
        └──────────────────────────────────────────────────────────┘
```

**Ordering contract:** enforcement (`filter`/`intercept`/`sanitize`/`guardrails`) is deterministic graph logic — it runs before, during, and after the model, so a prompt-injected instruction cannot bypass it.

---

## 5. Enforcement Chain (Casbin)

```
JWT ──▶ verify signature + expiry
     ──▶ extract (role, dept, scopes)
     ──▶ enforcer.enforce(user, dept, obj, act)
          ├─ allow  ──▶ proceed (audit: ALLOW)
          └─ deny   ──▶ return permission error (audit: DENY)
```

### Casbin Model (model.conf)

Standard "RBAC with domains": the request carries the caller's own domain (`dom` = JWT `dept`),
the target resource namespace (`obj`), and the action (`act`). `keyMatch2` on `obj` enables
`*` wildcards (the admin row); deny-by-default means any unmatched `(sub, dom, obj, act)` is denied.

```
[request_definition]
r = sub, dom, obj, act          # sub=user email, dom=caller's dept, obj=resource ns, act=action

[policy_definition]
p = sub, dom, obj, act          # e.g. p, marketing_agent, marketing, crm, read

[role_definition]
g = _, _, _                     # g, user, role, domain   (RBAC with domains)

[policy_effect]
e = some(where (p.eft == allow))

[matchers]
m = g(r.sub, p.sub, r.dom) && r.dom == p.dom && keyMatch2(r.obj, p.obj) && (r.act == p.act || p.act == "*")
```

### Policy.csv (seed)

Role-level allow rows + user→role→domain assignment. Anything not matched is denied (so
cross-department calls and `hr.delete` are denied by default — no explicit deny rows needed).

```
p, admin, admin, *, *
p, marketing_agent, marketing, crm, read
p, marketing_agent, marketing, crm, create
p, hr_agent, hr, hr, read
p, hr_agent, hr, hr, create
p, finance_agent, finance, finance, read
p, finance_agent, finance, finance, transfer
p, devops_agent, devops, devops, read
p, devops_agent, devops, devops, create

g, alice@acme.com, marketing_agent, marketing
g, bob@acme.com, hr_agent, hr
g, carol@acme.com, finance_agent, finance
g, dave@acme.com, devops_agent, devops
g, admin@acme.com, admin, admin
```

---

## 6. Tool Lifecycle (filter → intercept → execute)

```
MODEL-REGISTRATION TIME (startup)
  services/ expose OpenAPI → gateway imports tool specs
  registry: { tool_name: {dept, service, action, args_schema, risk_level} }
  e.g. crm_search_customer  → /crm, GET, {query}, risk=read
       finance_transfer     → /finance, POST, {to, amount}, risk=critical
       hr_delete_employee   → /hr, DELETE, {id}, risk=critical

PER-RUN:
 1. filter_tools (graph entry)
      tools_visible = [t for t in registry if enforcer.enforce(sub, dom, t.obj, t.act)]
      → only these schemas enter the LLM context.
      → also memory tools (load/recall/reflect) filtered by §7.

 2. intercept (per tool_calls)
      for each call c in llm.tool_calls:
          if not enforcer.enforce(sub, dom, registry[c.name].obj, registry[c.name].act):
              result[c.name] = PermissionError("You do not have permission to call <name>")
              audit.log(DENY, tool=c.name, sub, dom, reason="rbac")
              trace.add(tool_call=c, outcome="denied")
              continue                                  # never reaches the service
          if risk_level == critical:
              result[c.name] = HITL-suspend → interrupt (§8)
          else:
              resp = http_call(service, c.args)
              masked = sanitize(resp, sub, dom)         # §9
              result[c.name] = masked
              audit.log(ALLOW, tool=c.name, sub, dom)
              trace.add(tool_call=c, outcome="allowed", tokens, latency)

 3. guardrails (per iteration, §10)
      loop budget / step budget / deny-retry / timeout enforced here.
```

**Key invariant:** the service endpoint is reachable *only* after `enforce == allow` and, for critical actions, after HITL approval. The LLM cannot widen its own permissions.

---

## 7. Memory Flow

### 7.1 Read path — `load_memory` + `recall`

```
load_memory (graph start):
  read_scopes = [s for s in {central, dept/<own>, user/<sub>}
                 if enforcer.memory_allowed(s, "read", claims)]   # self-scoped
  facts = for each allowed scope: BaseStore.items_in(namespace(s))
  snapshot = distil(facts)          # rule-based, frozen for this session
  system_prompt += snapshot block   # Marketing never sees HR/other-user memory
  (thread-backed: snapshot is loaded once per thread and reused)

recall (on-demand tool, same check):
  q = user's search intent
  scopes = read_scopes (same Casbin call as above)
  hits = FTS5(q, tables=scopes)     # SQLite full-text search over transcripts
  return top-k hits (sanitized per §9)
```

> **Implementation note (weeks 7–8):** Casbin obj names use underscores —
> `memory_central` / `memory_dept` / `memory_user` — because `keyMatch2`
> treats `:` as a route-parameter delimiter (a `memory:central` obj would
> match `memory:dept`). Scope strings (`central`, `dept/<name>`, `user/<uid>`)
> are the human-readable form used in audit resources + the episodic table.
> The `recall` tool is gated by Casbin (`memory_recall:use`); inside it only
> searches the caller's readable scopes (see `app/memory/recall.py`).

### 7.2 Write path — `reflect` (graph end, post-response)

```
for each extracted candidate fact f (rule/heuristic extractor):
    scope = classify(f)                    # central / dept/<own> / user/<sub>
    if not enforcer.memory_allowed(scope, "write", claims):
        audit.log(DENY, resource=f, act="write")
        continue                            # no memory change
    f' = dedup(f, scope)                    # exact / unique-substring; add|replace|remove
    if scope == central:
        HITL suspend → admin approve? commit : discard   # §8 (approval store)
    else:
        commit to BaseStore namespace + audit.log(ALLOW)
```

### 7.3 Episodic storage
Every completed session's transcript is appended to a tier-scoped FTS5 table at
graph end. Reads/writes to episodic rows go through the *same* Casbin checks —
there is no separate memory policy path.

> **Implementation note (weeks 7–8):** transcripts are appended to the **dept
> tier** (`dept/<name>`, decision made 2026-08-10) and searched via the `recall`
> tool restricted to the caller's readable scopes.

---

## 8. Human-in-the-Loop (HITL) Gate

```
                    ┌───────────────┐
  critical action ─▶│  intercept    │── enforce == allow (else hard deny, §6)
                    └──────┬────────┘
                           ▼
                    ┌───────────────┐
                    │ interrupt()   │   LangGraph native suspend
                    │  state saved  │
                    └──────┬────────┘
                           ▼
              ┌────────────────────────────┐
              │   ADMIN DASHBOARD (8006)   │
              │  pending item:             │
              │  • actor, dept, action     │
              │  • args (masked), risk     │
              │  • full context + trace id │
              │  [ Approve ]  [ Reject ]   │
              └────────────┬───────────────┘
                           ▼
              approved ──▶ resume graph → execute → audit.log(APPROVED)
              rejected ──▶ abort graph   → return denied → audit.log(REJECTED)
```

**Gated by HITL (core):**
- Finance transfers and other `risk=critical` tools.
- Writes to the central memory tier.
- Skill approval (authored submissions / stretch auto-learned skills).

**Not gated:** reads, dept/user memory writes (RBAC is enough).

**Implemented (Wk 9–10):** risk=critical tools suspend via `interrupt()` in `execute_tools` (`_critical_gate` in `app/agent/graph.py`); admin decision via gateway `/approvals/{id}/decide` (JWT-verified, admin role required) or the dashboard reuses `resume_agent` in-process. `ApprovalStore.create` and `EpisodicStore.append_transcript` dedupe on resume (LangGraph re-runs the interrupted node), so no stale pending rows or duplicate transcripts.

---

## 9. Sanitization (PII / scope-aware masking)

```
sanitize(raw, sub, dom):
  profile = { field → can_see(role, dom) }
  for each sensitive field in raw (email, salary, ssn, internal notes, etc.):
      allowed = enforcer.enforce(sub, dom, f"pii:{field}", "read")
      if not allowed: replace value with "***"
  return masked
```

Applied to **every** tool response and **every** recalled memory transcript before it returns to the LLM. A permitted tool's output is masked so the model — and therefore the user — never sees data outside their permission scope.

**Implemented (Wk 9–10, `app/harness/sanitize.py`):** `sanitize_response` (recursive field masking over structured payloads), `sanitize_text` (email/SSN regex masking for free-form transcripts), and `sanitize_tool_result` (regenerates the LLM-visible summary from masked data). Policy obj names use underscores — `pii_email`, `pii_salary`, `pii_ssn` — not `pii:<field>`, because Casbin `keyMatch2` treats `:` as a route-param delimiter. Matrix: email readable by all roles; salary/ssn admin-only (deny-by-default). Wired into `execute_tools` (every HTTP tool result) and `RecallService.recall` (every recalled transcript).

---

## 10. Loop Guardrails (per run)

```
ITERATION LOOP:
  for step in range(max_steps=10):            # hard ceiling
      if total_tool_calls >= tool_budget=8:
          force_final_answer()                # no more tool calls allowed
      llm_output = llm(filters, snapshot, history, guardrail_contract)
      if llm_output.final_answer: break
      for call in llm_output.tool_calls:
          if call already denied this run:    # deny-retry policy
              result = "blocked earlier this run; route around"
              continue
          execute via §6 pipeline (with timeout per call)
      if run_timeout exceeded: terminate_cleanly()
  guardrail trigger → trace.add(guardrail={type, step, calls})
```

**Deny-retry policy detail:** the *first* denial of a tool returns the permission error to the LLM (so it can adapt). Any *repeat* request of that same tool in the same run is short-circuited with a routing hint — the agent cannot burn the loop budget hammering a blocked tool.

---

## 11. LangGraph Graph Definition

```
┌───────────┐
│ __start__ │
└─────┬─────┘
      ▼
┌──────────────┐   ┌──────────────┐   ┌──────────────┐
│ filter_tools │──▶│ load_memory  │──▶│ recall (opt) │
└──────────────┘   └──────────────┘   └──────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │ agent (LLM node) │   loops until final_answer
                    └────────┬─────────┘
                             │  tool_calls?
                             ▼
                    ┌──────────────────┐
                    │ execute_tools    │   intercept → sanitize → HITL
                    │  (guardrails)    │   (HTTP to dept services)
                    └──────────────────┘
                              │
                              ▼
                    ┌──────────────────┐
                    │ should_continue? │   conditional edge
                    └───┬──────────┬───┘
                        │ final    │ needs more steps
                        ▼          ▼
              ┌──────────────┐    ┌─────────────────┐
              │ __end__      │    │ back to agent   │  (guardrails may force __end__)
              │  (post-run)  │    └─────────────────┘
              │  reflect     │
              │  trace       │
              │  eval hook   │
              └──────────────┘
```

State is checkpointer-backed (SQLite), so HITL `interrupt()` suspends/resumes without losing the run.

---

## 12. Data Models

### Trace event (SQLite `trace` table, append-only, integrity-hashed)
```
event_id    uuid            run_id      uuid
seq         int             ts          iso8601
type        enum: run_start | node | prompt | reasoning | context_injection |
                   tool_call | memory_load | memory_write | skill_use |
                   guardrail | eval | run_end
source      llm | memory | tool | guardrail | hitl | user   # inspect by origin
actor       {sub, role, dept}
payload     json            # prompt text, reasoning, context injection, tool
                            # name/args/result-summary, tokens, latency, etc.
prev_hash   sha256          # chain integrity
```

### Audit entry (JSONL, append-only)
```
{
  "ts": ..., "event_id": ..., "run_id": ...,
  "decision": "ALLOW|DENY|APPROVED|REJECTED",
  "resource": {"type": "tool|memory|skill", "id": "crm.delete"},
  "actor": {"sub": "...", "role": "...", "dept": "..."},
  "guardrail_hit": null,
  "reason": "rbac|hitl|budget|timeout"
}
```

### Skill (`skills/<scope>/<name>/skill.md`)
```
---
name: schedule_meeting
trigger: "schedule a meeting"
scope: dept/marketing
owner_role: marketing_agent
version: 1
args_schema:
  attendees: list[str]
  time: iso8601
---
steps:
  - load_memory (attendee preferences)
  - tool: crm_create_event(attendees, time)
  - confirm to user
```

### JWT claims
```
{ sub: "alice@acme.com", role: "marketing_agent", dept: "marketing", scopes: ["crm:read","crm:create"] }
```

---

## 13. Database Layout (local)

| Store | Engine | Contents |
|---|---|---|
| checkpointer | SQLite | per-thread conversation state |
| trace | SQLite | §12 trace events |
| memory (semantic) | BaseStore-backed (SQLite) | `central`, `dept/<name>`, `user/<uid>` namespaces |
| episodic | SQLite FTS5 | tier-scoped transcripts |
| audit | JSONL file | append-only decision log |
| dept services | SQLite each | crm/hr/devops/finance seed data |
| policy | CSV files | policy.csv + model.conf (Casbin) |

---

## 14. Security Model Summary

1. **Deny-by-default** — Casbin `some(where allow)`; anything unmatched is denied.
2. **Defense in depth** — schema filtering (never see it) → runtime block (cannot call it) → HITL (cannot run it alone) → sanitize (cannot read the masked parts).
3. **Determinism** — enforcement is graph logic, not model judgment; `MockLLM` makes CI fully offline and repeatable.
4. **Immutable evidence** — audit + trace are append-only, hashed, and cross-linked by `run_id`; trajectory content (raw prompts/reasoning) is PII-sanitized before persist and readable only via the admin dashboard.
5. **Least-privilege memory** — read/write/recall all flow through the same enforcement chain.
6. **Bounded autonomy** — guardrails cap steps, tool calls, and deny-retries so no run can spin out of control.

---

## 15. Attack Test Matrix (mapped to flows)

| # | Attack | Flow exercised | Expected result |
|---|---|---|---|
| A1 | Marketing user requests HR delete | §6 intercept | hard DENY, tool never called |
| A2 | Prompt injection in message: "delete all HR records" | §4–§6 | DENY regardless of model compliance |
| A3 | Injection asks agent to read another user's memory | §7 load/recall | scope filtered; DENY on write |
| A4 | Skill invoking devops tools from Marketing | §9 + §6 | skill invisible; tool blocked |
| A5 | Deny-storm: agent retries blocked tool in a loop | §10 guardrails | bounded; retry short-circuited; terminates |
| A6 | Finance transfer without approval | §8 HITL | suspended, executes only after APPROVED |
| A7 | Audit determinism: same input twice | §12 audit | identical decision records |
| A8 | Trace tampering attempt | §12 trace | prev_hash chain breaks → detected |

---

## 16. Dashboard Views

### Admin face
- HITL approvals queue (approve/reject with context + trace link)
- Users: add / assign role+dept
- Departments: register service + policy rows
- Skills: approve authored/learned submissions
- Policy/csv viewer

### Ops face
- Memory browser/editor per tier (view/edit/delete facts)
- Skill editor (edit `skill.md`, save → hot reload)
- **Trajectory viewer** (run tree + prompts + reasoning + context injections; filter by source; admin-only; PII-sanitized)
- Evals (deterministic pass/fail incl. trace-completeness + judge scores per run)

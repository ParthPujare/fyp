# Plan Mk15 — Two-level dept approvals + useful head + approve-once allowlist

## Metadata
- Date: 2026-10-01
- Status: **Phases 0–5 BUILT 2026-10-03 (session 029)** — offline suite green on SQLite; NOT yet verified: Postgres run (`make test-pg`), migration 0008 on live Neon, 2-PC browser sign-off (§6). Build notes: §9.
- Follows: Mk12 (dept head, single-level), Mk13 (shared DB, Docker), Mk14 (owner resume, defer/poll/claim)
- Closes: `ref_docs/todo.md` dept follow-ups + item #11 (approve-once vs every-time)
- Scale / runtime (carried from Mk14): unbounded members by design, max 5 PCs / 5 concurrent users in testing; `make run` native demo, Docker keeps working; sharing = `DATABASE_URL` only (`JWT__SECRET`, `LLM__FERNET_KEY`, Composio stay per-PC).

## Scope lock (user Q&A, 2026-10-01 — normative for build)
1. Flow = **configurable per type** (not forced sequential for everything).
2. Level basis = **per-dept config + per-item admin override**, refined round 2 to:
   - per-dept mode **`head-only / head+admin`**, set by admin in Departments tab, **default `head-only`** for all existing + new depts;
   - after head approval, admin sees **escalations only** (defined §3.5), not every head approval.
3. Head's own dept writes: **auto-commit, keep** (no change).
4. Tools: **yes — dept tools route via head** (today they are admin-only and invisible to heads).
5. Head powers (all four): **chat + own keys; see dept roster; manage dept members (scoped); manage dept skills/memory (scoped)**.
6. todo #11 = **approve-once vs every-time is per-rule**: granularity **member + tool/action AND exact-args match**; **head sets it at decide time** (`Approve once / Always allow`); **admin manages/revokes the list**.
7. Reject path: **head reject is final; admin cannot revive/override-approve**.
8. Queue UI: **separate queues + level/step badges + step history** (keep head queue + admin queue, do not merge into one view).

## 1. Problem / current state (evidence)
- `app/agent/graph.py:511-514` (`reflect`): `needs_hitl = scope == central OR (dept/* AND NOT is_dept_head)`. Single gate, single `interrupt()`, single terminal `decide()`.
- `app/agent/graph.py:250-289` (`_critical_gate`): `risk=critical` tools suspend with `resource={type:tool}`, `dept_of()` returns `None` (`app/memory/approvals.py:121-134`), so heads never see them; gateway `POST /approvals/{id}/decide` (`app/gateway/app.py:272-333`) is admin-only.
- `app/dashboard/app.py:449-476`: admin queue hides dept items iff `_active_head_count(dept) > 0`; head queue = `pending_for_dept(own dept)`. Item lives in **exactly one** queue. No step/level columns.
- `app/dashboard/static/index.html:370-379` (`tabsForRole`): heads get `["dept-approvals","memory"]` only — no Chat, no Account/keys, no roster, no management. Mk12 doc promised chat+account; code does not deliver.
- `app/dashboard/app.py:807-883` (`/api/departments`): admin-only; `_append_policy_row` writes `p,<role>,<dept>,*,*` + head wildcard (over-broad vs seed rows); member list reads `USERS.values()` (`:838`), stale on shared DB.
- `app/memory/approvals.py` + `app/memory/pg_stores.py:21-26`: no `level/steps` columns; `pending_items()` full scan; Mk14 added `resumed_by/at` (`0007_approval_resume.sql`) + `claim_resume` + owner resume + `GET /api/approvals/by-thread/{thread}`.
- Every gated item re-suspends: no allowlist, no `always_allow`, no revoke UI.

## 2. Goals / non-goals
Goals:
- (a) Configurable two-level approvals: `head-only` vs `head+admin` per department, with a deterministic router that answers "which level for this item".
- (b) Dept tools (incl. critical dept tools + dept-scoped external tools) suspend head-first instead of admin-only.
- (c) Heads become useful: chat + keys, roster, scoped member management, scoped skills/memory.
- (d) Approve-once allowlist (member + action + exact args) set by head at decide time, managed/revoked by admin.
- (e) Separate queues kept; every card shows level/step badge + step history; audit + trace record each step.
Non-goals (explicit):
- Head own-write gating (stays auto-commit); admin revive of head rejects; central-tier two-level; `user/*` gating; episodic-transcript gating; realtime push (keep Mk14 4s poll + manual Resume); per-user auto-generated skills (todo #5); clarifying-questions (todo #6); self-edit codebase (todo #9); dual-session scoping (todo #1).

## 3. Design
### 3.1 Routing table (`route_for`, single owner — `app/agent/routing.py` NEW, used by both `reflect` and `_critical_gate`)
Input: `{kind: memory|tool, scope|tool_name, risk, actor, dept_mode, allowlist_hit, head_count}`. Output: `none | head-only | head+admin | admin-only`.
- `user/*` → `none` (unchanged).
- `central` (memory) → `admin-only` (unchanged). Central is never allowlist-eligible.
- `dept/X` memory from non-head: dept mode `head-only` → `head-only`; `head+admin` → `head+admin`; zero active heads → `admin-only` (escalation, current behavior preserved).
- Dept tools: tool is "dept-owned" iff its Casbin `obj` domain equals the actor's dept (internal `TOOL_REGISTRY` via `required_scope`/`dept`) OR external tool executed under the actor's dept (Composio adapter: attribute to `claims.dept`; document mapping in `app/integrations/adapter.py`). Then: non-critical → dept mode (`head-only` / `head+admin`); **`risk=critical` → always `head+admin`** (safety default even in `head-only` depts — confirm at build start, see §8 Q1). Cross-dept tool attempts are denied by RBAC before HITL (unchanged, never reach router).
- Admin/head writes: head own-dept write → `none` (auto-commit, unchanged); admin writes follow existing central/admin paths (unchanged).
- Allowlist short-circuit runs BEFORE router: exact hit → `none` + audit `reason=allowlist` (eligible actions only, §3.3).

### 3.2 DB schema (additive migration `0008_approval_levels.sql` — next number; 0007 is taken)
Postgres (new file `app/db/migrations/0008_approval_levels.sql`, plain SQL, no BEGIN/COMMIT):
- `ALTER TABLE departments ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'head-only';` + `CHECK (approval_mode IN ('head-only','head+admin'))` (if CHECK complicates the runner, enforce in store + API instead — decide at build).
- `ALTER TABLE approvals ADD COLUMN level TEXT NOT NULL DEFAULT 'head';` (`head | admin | done`), `ADD COLUMN steps JSONB NOT NULL DEFAULT '[]';` — each step `{level, by, at, decision, comment}` appended on every decide; `level='done'` once terminal.
- New `approval_allowlist(member TEXT NOT NULL, action TEXT NOT NULL, args_hash TEXT NOT NULL, args JSONB NOT NULL, dept TEXT NOT NULL, created_by TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY (member, action, args_hash))` + index on `(member, action)`.
SQLite (`ApprovalStore._init_schema`, `app/memory/approvals.py:31-56`): same columns via `CREATE TABLE IF` + `PRAGMA table_info` alter-if-missing (Mk14 pattern); `steps` stored as JSON TEXT, `args` as JSON TEXT. `PgApprovalStore` inherits everything (no new class).
Backfill: existing `pending` rows keep `level='head'`; `departments` rows default `head-only` via column default (no data migration script).

### 3.3 Allowlist semantics (todo #11, locked)
- Key = `(member sub, action, args_hash)` where `action` = `memory:<scope>:<op>` (e.g. `memory:dept/marketing:add`) or `tool:<tool_name>` (e.g. `tool:crm_create_customer`); `args_hash` = SHA-256 of canonical JSON (`sort_keys=True`, compact separators) of the gated payload (`{op,text,match}` for memory; `{tool,args}` for tools with volatile keys stripped — document strip-list at build, e.g. timestamps/ids).
- Exact-args match: same member + same action + byte-identical canonical args → auto-ALLOW; any arg differs → fresh HITL. Per-member only (alice's rule never covers bob).
- Eligibility: dept memory writes + non-critical dept tools are eligible; **`risk=critical` tools + `central` writes are NEVER eligible** (always every-time). Enforce in router + `decide` (ignore `always_allow` with 422 if ineligible).
- Lifecycle: created only via head/admin `decide(always_allow=true)` on an eligible approved item; revoked via `DELETE /api/allowlist/{member}/{action}/{args_hash}` (admin any, head own-dept only); listed via `GET /api/allowlist` (admin all, head own-dept filter). Every create/revoke audit-logged (`allowlist:create/revoke`).

### 3.4 Agent gates
- New `app/agent/routing.py`: `action_key_for(item)`, `canonical_args(payload)`, `args_hash(payload)`, `route_for(...)`, `is_allowlist_eligible(action)`. Pure functions, unit-tested without DB.
- `reflect` (`graph.py:499-557`): after RBAC `memory_allowed` pass → allowlist lookup (member+action+hash) → hit: `editor.apply`, audit `ALLOW reason=allowlist`, trace `decision=allowed reason=allowlist`; miss: `route_for` → `none` commits; else `approvals.create(..., level='head', steps=[])` + `interrupt({... tier, level:'head', action, args_hash})`; on resume-after-head-approve with route `head+admin`: re-`interrupt` with `level='admin'` (second suspend, same `approval_id`, `steps=[head_step]`) instead of committing.
- `_critical_gate` (`graph.py:250-289`): same shape for tools — resource gains `{dept, action, args_hash}`; `dept_of()` extended (or new `level_of()`) so dept-owned tool items appear in the head queue; trace `hitl_suspend` gains `level`, audit `APPROVED/REJECTED reason=head-hitl|admin-hitl|allowlist`.
- `runner.py`: `AgentResult.approval_tier` gains `level` (`_suspension` reads `level` from interrupt value); `_suspended_reply` copy per level ("waiting on your department head" vs "waiting on head, then admin").

### 3.5 API contracts (dashboard; gateway head path in Phase 4)
- `GET /api/dept/approvals/pending` (head, own-dept): returns step-1 items (`level=head` AND (`head-only` OR `head+admin`)) for own dept. Unchanged auth (`_verify_dept_head`).
- `GET /api/approvals/pending` (admin): returns central + admin-only tools + escalations ONLY: (i) dept items with zero active heads, (ii) `head+admin` items with `level=admin` (head already approved — "Step 1 ✓"), (iii) optionally expired/flagged (no expiry v1 — (i)+(ii) only). Head-only items NEVER appear here (log-only via audit/Ops).
- `POST /api/dept/approvals/{id}/decide` + `POST /api/approvals/{id}/decide`: body `{approve: bool, always_allow?: bool, comment?: str}` + existing `?defer=`. Head step-1 approve on `head+admin` → `level: head→admin`, append head step, NO resume yet (defer semantics; coupled `defer=0` returns `needs_resume` + `next_level=admin`). Head/admin reject → terminal `rejected`, append step, resume-or-defer as today; rejected items are never re-decided (409) and never revived. Admin step-2 approve → `level=done`, resume as today. Ineligible `always_allow` → 422. Cross-dept head decide → 403 (unchanged).
- `GET /api/dept/members` (head, own dept): `[{username, role, active, created_at}]` from store (fixes `USERS.values()` staleness by reading `get_store().list()` filtered by dept).
- Scoped member management (head, own dept only): `POST /api/dept/members` (create non-head roles in own dept; 403 for `*_head`/`admin`/cross-dept), `PATCH /api/dept/members/{u}` (`active`, `password_reset` only — no role/dept changes; heads cannot promote), all audit-logged + `members_version` bump (existing enforcer rebuild path).
- `GET /api/allowlist` / `DELETE /api/allowlist/...` (admin all; head own-dept subset); `GET /api/departments` adds `approval_mode` + `head_count` + store-sourced `members`; `POST /api/departments` accepts `approval_mode`, `PATCH /api/departments/{name}` (admin: mode switch `head-only ↔ head+admin`, audit-logged).
- `GET /api/approvals/by-thread/{thread}` + `_approval_view`: add `level`, `steps`, `action`, `needs_admin` (`level==admin`), keep `needs_resume/resumable_here`.

### 3.6 Policy / store fixes (bundled, not a separate project)
- `departments()` member source: `get_store().list()` filtered by dept (drop `USERS.values()`).
- `_append_policy_row`: for newly registered depts write the same granular rows seeds use (tool scopes + `memory_dept read/write` + `memory_central read` + `memory_user` + `recall` + `pii_email` + base skill row) for BOTH `<role>` and `<role>_head` instead of `*,*`; keep idempotent `has_row` guard.
- `MemberStore.register_department(..., approval_mode='head-only')` validates mode; new `set_department_mode(name, mode, actor)` (+ `PgMemberStore` inherits — keep SQL portable `?` placeholders).
- Gateway: add head `POST /approvals/{id}/decide` twin (own-dept + step-1 semantics, `_verified_active` + `*_head` check) OR explicitly document dashboard-only heads — decide at build (recommend twin for multi-device parity since gateway is the raw-API path in `docker-compose.api-ports.yml`).

### 3.7 Frontend (`app/dashboard/static/index.html`)
- Keep separate views; approval cards gain `Step 1 head → Step 2 admin` pills (`head-only`: `1 step`; `head+admin` step-1: `Step 1 of 2 ✓/pending`; step-2 in admin queue: `Step 1 ✓ head by X · Step 2 pending`), step history line (`decided by/at + comment`), `needs_admin` banner. Decide dialog gains `Approve once / Always allow this exact action` checkbox (disabled with tooltip for ineligible critical/central) + optional comment box; `decide()` sends new body; 1200ms reload kept.
- Heads: tabs become `["dept-approvals","chat","memory","account"]` (reuse member chat proxy + `/api/memory/me` + BYOK account panel; default tab stays `dept-approvals`); new Roster panel (`GET /api/dept/members`) with scoped add/deactivate/reset; new "Always-allowed" panel (own-dept list + revoke).
- Admin: Departments table gains per-row mode selector (`head-only ↔ head+admin`, `PATCH`), `head_count` column; new Allowlist admin panel (filter by member/dept, revoke).
- Owner poller (`watchSuspended`): follow two-step chains (after step-1 approval, keep polling same `approval_id` until `level=done` decided, then owner `resume`); suspended banner copy per level + manual Resume button unchanged.

### 3.8 Audit / trace / Ops
- Every step appends audit (`APPROVED/REJECTED reason=head-hitl|admin-hitl`, actor=decider, `run_id`) + trace `MEMORY_WRITE/GUARDRAIL decision=pending(step1)/approved(step2)/rejected` with `level`; allowlist hits log `ALLOW reason=allowlist` with `action+args_hash`. Ops viewer surfaces `steps[]` (no new endpoints — existing `/api/ops/runs*` already carry event payloads).

### 3.9 Concurrency / multi-device (Mk13/Mk14 invariants kept)
- Step transitions are single-statement conditional UPDATEs (`... WHERE id=? AND level='head' AND status='pending'` for step-1; `... level='admin'` for step-2); losers get 409. `claim_resume` unchanged (only terminal `approved/rejected` resumable, owner-only). No read caching in `PgApprovalStore` (cross-laptop visibility immediate). `create()` check-then-insert dup accepted (blocked later at claim). Only `DATABASE_URL` shared; JWT/FERNET/Composio stay per-PC; per-actor provider/keys on resume unchanged.

## 4. Phased execution (one session = one phase per repo rules; no code in this plan)
| Phase | Title | Steps | Touch (files) | Verify |
|---|---|---|---|---|
| 0 | Schema + stores | 1. Write `0008_approval_levels.sql`. 2. SQLite `_init_schema` parity + `steps/level` JSON handling in `_row_to_dict`. 3. `MemberStore.register_department(+mode)` + `set_department_mode` + `allowlist {create,list,revoke,match}` helpers (portable SQL for `PgMemberStore`). 4. `pending_for_level`/`level_of` helpers. | `app/db/migrations/0008_approval_levels.sql`, `app/memory/approvals.py`, `app/auth/store.py` (+`pg_store.py` if override needed), `app/db/schema.py` if registry lists migrations | `make test-pg` migration apply clean; new store unit tests green; `python -m app.db status` shows 0008 applied |
| 1 | Router + agent gates | 1. NEW `app/agent/routing.py` (pure). 2. Wire `reflect` + `_critical_gate` (allowlist check → route → two-suspend chain). 3. `runner.py` tier/level copy. 4. Extend `dept_of`→ dept attribution for tools. | `app/agent/routing.py`, `app/agent/graph.py`, `app/agent/runner.py`, `app/memory/approvals.py` | New `tests/test_routing.py` + updated `test_dept_head.py`/`attack/test_cross_dept_memory.py` green; `ruff` clean |
| 2 | Decide/step-2 + allowlist + scoped APIs | 1. Step-transition `decide_step` (head step-1 → admin step-2; reject terminal). 2. `always_allow` create path + 422 ineligible. 3. `GET /api/dept/members`, scoped `POST/PATCH /api/dept/members`, `GET/DELETE /api/allowlist`, dept mode `POST/PATCH`. 4. Admin queue = escalations-only filter. | `app/dashboard/app.py`, `app/auth/store.py`, `app/memory/approvals.py` | Endpoint tests: step-1→step-2→resume; reject-final 409 on admin revive; cross-dept 403; allowlist exact-match vs args-mismatch; mode switch moves items between queues |
| 3 | Head usefulness + queues UI | 1. Head chat+account tabs. 2. Level pills + history + checkbox + comment. 3. Roster + scoped member editor + allowlist panels. 4. Admin mode selector + head_count. 5. Two-step poller copy. | `app/dashboard/static/index.html` (+ tiny `app.py` view-shape if needed) | Manual click paths §6 (head + admin on two browsers); no new 401/403 regressions |
| 4 | Policy/store/gateway parity | 1. `departments()` store-sourced members. 2. Granular `_append_policy_row`. 3. Gateway head decide twin or documented dashboard-only. 4. `sync-config` interplay for new rows. | `app/dashboard/app.py`, `app/gateway/app.py`, `app/harness/*` | `test_members_depts` + policy-matrix tests updated/green; raw-API curl parity check |
| 5 | Hardening + docs + sign-off | 1. Attack-suite deltas (2-head race on step-1 → one 409; admin step-2 race → one 409; double-resume → 409; allowlist bypass attempts: cross-member, args-tamper, critical-ineligible). 2. Full `pytest` + `make test-pg` + `ruff`. 3. Update `todo.md`, `README` dashboard section, `agent_history` session note. 4. Live 2-PC verification (§6). | `tests/*`, `ref_docs/todo.md`, `README.md` | 100% suite green incl. pg; user live sign-off head→admin chain + allowlist once-vs-every-time |

## 5. Test matrix (new/updated)
- Router unit: `user→none`, `central→admin`, `dept mem head-only→head`, `head+admin→chain`, `no-head→admin`, `dept tool non-critical→mode`, `critical→head+admin`, `cross-dept→deny-before-router`, `allowlist hit→none`, `critical/central ineligible`.
- Chain: member dept-write (head+admin dept) suspends L1 → head approve → still suspended L2 in admin queue → admin approve → commits; trace has both steps; audit has `head-hitl` + `admin-hitl`.
- Admin queue: head-only approved items absent; L2 items present with `Step 1 ✓`; no-head items present.
- Reject-final: head reject → admin decide → 409; run never executes.
- Allowlist: head `always_allow` on eligible → same member+args auto-ALLOWs (audit `allowlist`); different args re-suspends; other member re-suspends; critical `always_allow` → 422; admin revoke → re-suspends.
- Scoping: head sees/manages own dept only (cross-dept 403); head cannot create/promote heads or touch admins; deactivating last head re-routes items to admin (existing Mk12 test extended to L2).
- Concurrency: 2-head step-1 race → one 200 one 409; 2-admin step-2 race → one 409; double owner resume → 409.

## 6. Rollout / manual verification (`make run`, 2 browsers)
1. Admin sets marketing → `head+admin` (Departments tab); finance stays `head-only`.
2. alice (marketing) chat `Our team prefers morning standups` → suspended (head queue, `Step 1 of 2`); mhead approves + ticks `Always allow` → card moves to admin queue (`Step 1 ✓ mhead`); admin approves → alice auto/manual Resume → fact committed; repeat identical message → auto-commits (allowlist); altered wording → re-suspends.
3. carol `finance_transfer` (critical) → head-first even though finance is `head-only` → fhead approve → admin step-2 → executes; verify `always_allow` checkbox disabled.
4. Head rejects bob's hr write → admin queue has nothing to revive (409 on attempt); bob retries.
5. Deactivate last marketing head → new dept items go straight to admin (escalation).
6. Heads: chat works with own key, roster lists own dept only, member add/deactivate scoped, skill/memory panels own-dept.

## 7. Risks
- Two-step doubles suspended lifetime; owner-offline items wait (Mk14 rule, no admin force-resume) — surface `waiting on admin (step 2)` clearly.
- `level`/`steps` JSON querying on Postgres: keep filters in Python over `pending_items()` (queues are small; matches Mk14 full-scan pattern) — add `(thread_id,status)` index only; revisit if a dept exceeds hundreds of pending rows.
- Mode switch mid-flight: items created before switch keep their creation-time route (no retroactive re-route) — document on the mode selector.
- `*,*` rows written by older registrations stay valid; `_append_policy_row` change is forward-only (no rewrite of existing rows).
- McCabe growth in `reflect`/`_critical_gate`: mitigated by extracting `routing.py` (pure, tested).

## 8. Open question for build start
Q1 (needs 1-line answer before Phase 1): should `risk=critical` dept tools ALWAYS force `head+admin` even in a `head-only` department (recommended), or does `head-only` mean even critical is head-only? Plan defaults to always-`head+admin`; say otherwise and Phase 1 inverts that branch.

## 9. Build notes (session 029, 2026-10-03) — where the build differs from / pins down the plan
- **Q1 (§8)**: built with the plan default — `risk=critical` dept tools are always `head+admin`, even in a `head-only` department.
- **One suspension, row-level steps (§3.4 vs §3.5)**: the run suspends once; the head's step-1 approve moves the row `level: head → admin` (still `pending`) and nothing resumes. No second `interrupt()` — §3.5/§3.9 semantics, not the re-interrupt sketched in §3.4.
- **Extra `approvals` columns**: besides `level`/`steps`, 0008 adds `route`, `dept`, `action`, `args_hash` (creation-time routing; `resource` stays `{type,id}` so the Mk12 dedup key is unchanged).
- **TEXT, not JSONB**: `steps` / allowlist `args` are JSON-in-TEXT on Postgres too, per the rule at the top of `0002_core_tables.sql` (store code is shared with SQLite and calls `json.loads`). No `CHECK` on `approval_mode`; validated in the store + API.
- **Non-critical tools (§3.1)**: the router handles them, but `risk=write` dept tools are gated only when `APPROVALS__GATE_WRITE_TOOLS=true` (default off — gating every write tool would change existing behaviour). Read tools are never gated.
- **Head/admin as requester**: a head's or admin's own critical tool call is `admin-only` (a head does not approve their own step 1).
- **No active head at creation** → the item is created `admin-only` at the admin step. A head later appointed does not get it back.
- **`always_allow` on `head+admin`**: recorded on the head's step; the rule is created only when the admin step approves (dropped if the admin rejects).
- **Step audit timing**: `head-hitl` / `admin-hitl` audit lines (actor = decider) are written when the requester's machine resumes, from the row's `steps`; the step itself is durable on the row at decide time.
- **Volatile tool-arg strip-list (§3.3)**: `timestamp, ts, request_id, idempotency_key, nonce, trace_id`.
- **Gateway (§3.6)**: head decide twin built on the existing `POST /approvals/{id}/decide` (role decides the level).
- **Scoped head management beyond §3.5**: own-dept memory edit reuses `/api/memory/{scope}` (head allowed for `dept/<own>` only); own-dept skills via `GET/PUT /api/dept/skills[/{name}]` (edit only — create/delete and policy rows stay admin).
- **`_append_policy_row`**: granular rows = `read`/`create` on the service label + registered resources, plus the base memory/recall/pii/skill rows, for `<role>` and `<role>_head`. `_ensure_toolkit_policy_rows` now covers head roles and registered departments.
- **Thread safety**: `ApprovalStore` decide/claim now hold a process lock (the SQLite store shares one connection across request threads; found by the new race tests — it also hardens the Mk14 claim).

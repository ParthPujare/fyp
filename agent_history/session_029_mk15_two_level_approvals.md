# Session 029 — Mk15 two-level dept approvals + allowlist + useful heads

## Metadata
- Date: 2026-10-03
- Plan / Phase: mk15 — Phases 0–5 (`plan_mk15_two_level_dept_approvals.md`)
- Plan step: schema + stores → router + agent gates → decide steps/allowlist/scoped APIs → UI → policy/gateway parity → hardening + docs
- Scope: code + tests + docs for the whole plan in one session (the user asked for the full Mk15 build at once — a deliberate exception to rule 1 "one session = one phase")
- Status: complete offline — NOT verified on Postgres, live Neon, or in a browser
- State at start: `main` @ `877a563` (clean); Mk14 Phase 1 done; Mk15 was documentation only; baseline 455 passed / 146 skipped

## Objective
Build Mk15: per-department `head-only` / `head+admin` approvals, dept tools via the head,
useful heads (chat/keys, roster, scoped member + skills/memory management), approve-once
allowlist (todo #11), separate queues with step badges/history, reject-final.

## What was thought, when
| Phase | What I thought / decided / realized |
|-------|-------------------------------------|
| explore | §3.4 (re-interrupt for step 2) and §3.5/§3.9 (row moves `head→admin`, no resume) disagree; chose row-level steps + ONE suspension: simpler, and `claim_resume` already only resumes terminal rows |
| design | The route must be frozen at creation (mode switch must not re-route) → store `route/dept/action/args_hash` on the row; keep `resource = {type,id}` so the Mk12 dedup key is untouched |
| design | A resumed run re-enters the gate: it must `find()` its own approval BEFORE routing/allowlist, else a rule created after a reject could let the rejected write through (attack test added) |
| design | Plan says "non-critical dept tools → dept mode", but gating every write tool would break existing flows → router supports it, graph gates `risk=write` only behind `APPROVALS__GATE_WRITE_TOOLS` (default off) |
| design | `always_allow` ticked by the head on a `head+admin` item must not bypass the admin step → recorded on the step, rule created only at final approval |
| design | Per-step audit lines need an AuditLog; decide endpoints run under test doubles without one → write `head-hitl`/`admin-hitl` (actor = decider) at resume time from `steps[]` |
| design | JSONB would break the shared SQLite/Postgres store code (`json.loads` on read) → TEXT, per the 0002 note |
| fix | No-head departments now create the item `admin-only` at the admin step, so `pending_for_dept` no longer returns it → 2 old tests updated to the escalation semantics |
| fix | New race tests showed the shared SQLite connection is not safe for concurrent decide/claim (two winners, "API misuse") — pre-existing, also affected Mk14 `claim_resume` → process-level `RLock` around decide/claim |
| fix | `*, *` registration rows granted central writes + every critical tool to a new dept → granular rows; 3 tests + 1 pg test updated |
| verify | Did NOT start the app: `.env` has a live `DATABASE_URL`, so any real start would apply 0008 to the shared Neon DB — left as a manual step |

## What was done
- `app/db/migrations/0008_approval_levels.sql` (new): `departments.approval_mode`; `approvals.level/steps/route/dept/action/args_hash`; `approval_allowlist` table
- `app/memory/approvals.py`: SQLite schema parity, `create(level, route, dept, action, args_hash)`, public `find`, `route_of`/`level_of`/`pending_for_level`, `decide_step` (conditional UPDATE per step), `dept_of` for attributed tools, write lock
- `app/auth/store.py`: `approval_mode` on departments, `set_department_mode`, `department_mode`, `active_head_count`, allowlist `add/get/match/list/revoke`
- `app/agent/routing.py` (new, pure): `route_for`, action keys, canonical args + hash, eligibility
- `app/agent/graph.py`: `_resolve_gate` + `_audit_steps`; `reflect` and the tool gate route through the router, allowlist short-circuit, find-before-route
- `app/agent/runner.py`: `approval_level` / `approval_route` on `AgentResult`, per-route suspended reply
- `app/config.py`, `.env.example`: `APPROVALS__GATE_WRITE_TOOLS`
- `app/memory/approval_flow.py` (new): `check_decision` / `apply_decision` shared by dashboard + gateway
- `app/dashboard/app.py`: step-aware decide (both routes), escalations-only admin queue, `_approval_view` (+level/route/steps/action/needs_admin/allowlist_eligible), `GET/POST/PATCH /api/dept/members`, `GET/DELETE /api/allowlist`, `PATCH /api/departments/{name}`, store-sourced department members + `head_count`, head-scoped memory edit, `GET/PUT /api/dept/skills`, granular `_append_policy_row`, toolkit rows for heads + registered depts
- `app/gateway/app.py`: head decide twin, `always_allow`/`comment`, `approval_level`/`approval_route` in the chat envelope
- `app/dashboard/static/index.html`: step pills + history + comment + always-allow on cards, head "My department" tab, admin "Always allowed" tab, department mode selector + heads column, editable own-dept memory for heads, two-step poller text
- tests: `tests/test_routing.py`, `tests/test_two_level_approvals.py`, `tests/attack/test_approval_levels.py` (new); `test_memory.py`, `attack/test_cross_dept_memory.py`, `test_members_mgmt.py`, `test_pg_phase6.py` updated
- docs: `README.md`, `ref_docs/todo.md`, plan Mk15 status + §9 build notes, `ref_docs/plans/INDEX.md`, this file + `INDEX.md`

## Decisions locked in
- Q1: critical dept tools are always `head+admin` (plan default).
- One suspension; steps live on the approval row; head reject is terminal.
- Route frozen at creation; no retroactive re-route on mode switch.
- Allowlist = exact `(member, action, args_hash)`; critical + central never eligible; rule created at final approval only.
- Full list of deviations/pins: plan §9.

## Verification
- `ruff check app tests scripts`: clean.
- New suites: `test_routing.py`, `test_two_level_approvals.py`, `attack/test_approval_levels.py` green (race tests repeated 3×).
- Full offline suite: see the session's final report for the count.
- JS: `node --check` on the dashboard script + every referenced DOM id / tab view exists.
- Follow-up same day: full suite INCLUDING Postgres against the local test DB — **668 passed, 0 skipped**; 0008 applied to the shared DB by the user (`make db-migrate`, `db-status` shows 0001–0008 applied). Added Mk15 dual-backend contract tests to `tests/test_pg_stores.py` (steps, step race, mode, allowlist — green on SQLite + Postgres). `Makefile` `test-pg`/`test-pg-full` now run under `bash -c` (they failed under `cmd.exe` on Windows).
- NOT verified: any browser click-through, 2-PC flow (§6).

## Blocker / open questions
- `pending_for_dept` now means "waiting on this dept's head"; items created with no active head are the admin's even if a head is appointed later — acceptable?
- Departments registered before Mk15 keep their `p, <role>, <dept>, *, *` rows (forward-only); remove them by hand if the wildcard is unwanted.

## Next step (for the next session)
- Mk15 live verification: `make test-pg`, apply 0008 (`make db-migrate`), plan §6 on two browsers / two PCs.

## Handoff notes
- Working tree uncommitted on `main`.
- All PCs must pull before the demo: an old checkout ignores `level` and would treat a head approve on a `head+admin` item as final.

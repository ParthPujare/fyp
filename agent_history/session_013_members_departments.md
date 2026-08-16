# Session 013 — Plan Mk2 Phase 4: Members + Departments Management UI

## Metadata
- Date: 2026-08-16
- Plan / Phase: mk2 — Phase 4 (ref_docs/plans/plan_mk2_web_ui_members.md §4 row 4)
- Plan step: Phase 4 — Members + Departments management UI (session 013)
- Scope: admin CRUD for members (add / edit role+dept+scopes / activate / deactivate / reset password, lockout-guard enforced server-side, audit-logged) and department registration (name, service, service URL, resources, optional role) replacing the hardcoded `_service_for` map — through the dashboard UI
- Status: complete
- State at start: mk2 Phases 1–3 done on `main` (`4631ec3`): store-backed identity + `g` enforcement, 246 tests green, web-chat + any-user login with dual sessions. `/api/users` + `/api/departments` were read-only; `_service_for` was a hardcoded dept→label map in `app/dashboard/app.py`.

## Objective
Plan_mk2 §4 Phase 4 deliverable: "Admin UI + endpoints: list / add / edit (role + dept +
scopes) / activate-deactivate / reset password; audit-logged. Departments derived live from
store + p-rows; **register department** (name, service URL, resources, optional role) replaced
`_service_for`; admin-JWT gated; lockout guard enforced server-side." Acceptance: create
`molly@acme.com` (marketing) → she logs in, sees marketing tools, can read dept memory;
disabling her → her next request 403; registering a dept makes it appear in Departments + a
new role grantable.

Planning-session decisions (5, answered by the user): (1) **auto-write p rows on department
registration** (moved from "data-only" to "also write default `p, <role>, <dept>, *, *`" — the
user explicitly accepted the deviation, knowing `p` rows stay file-based but get appended at
registration), (2) seed the 5 default departments, (3) merge member management into the Users
tab (rename → Members), (4) guided add/edit form + generated passwords, (5) tests only — no new
demo script.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 08:10 | plan | Re-read plan_mk2 §4 row 4 + §3.2 target model + §5 seed tables; read dashboard `/api/users` + `/api/departments` + `_service_for` + `_policy_csv`, `MemberStore` (create/update/set_active/reset_password + lockout guard + audit + version bump), `PolicyEnforcer` (p from file, g from store, lazy version guard), `conftest.py` autouse `member_store` fixture, `test_dashboard.py` read-only assertions (`service == "crm"`, resources/members shape), `test_members.py` lockout patterns, `ServiceSettings` URLs. |
| 08:20 | clarify | Asked 5 questions: dept-registration depth, seed defaults, Users-tab merge, member-form UX, demo script. **Answers:** auto-write p rows (registration appends `p, <role>, <dept>, *, *` to policy.csv — an explicit override of my "data-only" recommendation), seed the 5 defaults, merge Users→Members, guided form + generated password, tests-only (no demo script). |
| 08:30 | design | Departments become a real store table (`departments` in `data/members.sqlite`: name PK, service, service_url, resources JSON, role, timestamps) seeded from `SEED_DEPARTMENTS` on first open; `MemberStore.register_department()` bumps `members_version` so the enforcer's next lazy build reloads p rows AND re-materializes g without a restart. Resource lists stay derivable live from policy.csv p-rows (seeds leave resources empty — the read path merges store records + live p-rows so the tab stays accurate even before registration). |
| 08:35 | design | `POST /api/departments` is the ONE place the dashboard writes the policy file — a tiny explicit append `p, <role>, <dept>, *, *`. Testability: `_policy_csv()` becomes settings-driven (`settings.services.policy_path` added to `ServiceSettings`) so tests point it at a temp copy of policy.csv — the repo policy.csv is never mutated by the suite. Enforcer rebuild is triggered by the version bump (p rows re-read from file at `_build`/`load_policy`). |
| 08:40 | design | `/api/users` becomes store-driven (`get_store().list()` → `to_public()` with timestamps); new endpoints: `POST /api/members`, `PUT /api/members/{username}`, `POST .../activate`, `POST .../deactivate`, `POST .../reset-password`. Lockout guard maps to 409 (`LockoutGuardError`), unknown-member `MemberError` → 404, duplicate `MemberExistsError` → 409, password < 6 chars → 422. Admin-gated by `_verify_admin` (which already enforces the Phase-2 active-member gate). |
| 08:50 | implement | `app/auth/store.py`: `DepartmentRecord`, `SEED_DEPARTMENTS`, `departments` table + `_seed_if_empty` extension, `list_departments`/`get_department`/`register_department` (exists-guard, default `<name>_agent` role, version bump, audit with resource type `department` via new `_audit_department`/`_audit_entry`). |
| 09:00 | implement | `app/dashboard/app.py`: requests models (MemberCreate/MemberUpdate/ResetPassword/DepartmentRegister), 4 member endpoints + register-department endpoint with name validation (`[a-z0-9_-]`, lowercase) + `_append_policy_row`; `/api/users` + `/api/departments` rewritten store-first; `_service_for` deleted; `_scope_namespaces` derives dept scopes from the store. |
| 09:10 | implement | `static/index.html`: Members tab = management table (active badge, Edit / Deactivate / Re-activate) with an add/edit editor (dept dropdown auto-filled from `/api/departments`, role auto-fill `<dept>_agent`, scopes comma field, password + Generate button, editable on edit, reset-only on add); Departments tab = register form (name/service/URL/resources/role) + resource/URL columns. |
| 09:20 | test | `tests/test_members_mgmt.py` (15 tests). Key trick: `tmp_policy` fixture copies the real policy.csv and repoints `settings.services.policy_path`, so registration tests append to a throwaway file and can build a `PolicyEnforcer(policy_path=tmp)` to *prove* the new role is grantable. Deactivation acceptance drives the real gateway `/hello` (200 → 403 after disable → 200 after re-enable). Lockout server-side: self-deactivation 409, second-admin → original admin can be deactivated. |
| 09:30 | verify | Full `uv run pytest` → **261 passed** (246 + 15), `ruff check app tests scripts` clean, `demo_agent` + `demo_attacks` + `demo_chat_web` regression pass, `node --check` on inline JS passes. |

## What was done
- `app/auth/store.py`: `departments` table (name, service, service_url, resources JSON, role, created/updated), `DepartmentRecord`, `SEED_DEPARTMENTS` (the 5 current depts seeded on first open), `list_departments` / `get_department` / `register_department` (default role `<name>_agent`, `DepartmentExistsError`, bumps `members_version`, audit-logged as resource type `department`).
- `app/dashboard/app.py`: `ServiceSettings.policy_path`-driven `_policy_csv()`; `_append_policy_row(role, dept)`; `POST /api/departments` (register → store record + `p, <role>, <dept>, *, *` append; 409 duplicate, 422 bad name); `GET /api/departments` store-first + live p-row merge (service/service_url/role/members/resources); `GET /api/users` store-driven with timestamps; `POST /api/members`, `PUT /api/members/{username}`, `POST .../activate`, `POST .../deactivate` (409 lockout), `POST .../reset-password` (422 short); deleted `_service_for`; `_scope_namespaces` dept scopes from the store.
- `app/config.py`: added `ServiceSettings.policy_path` (default the real policy file).
- `app/dashboard/static/index.html`: Members management tab (add/edit form with dept dropdown, role auto-fill, scopes comma field, password field + Generate, activate/deactivate/re-activate per row) + Departments register form + extra columns (URL, Role); Users tab renamed to Members.
- `tests/test_members_mgmt.py`: 15 tests — seeds, store-level register/list/audit/version-bump, API create→login acceptance, duplicate 409, short-pw 422, admin 401/403 gate, edit (role/dept/scopes), edit-unknown 404, deactivate→gateway 403→reactivate 200, lockout server-side (self 409 + second-admin passthrough), reset password then login + old-pw 401 + short 422, register-via-API (record + p row appended + visible in list), **new role grantable via `PolicyEnforcer(policy_path=tmp)`** but denied cross-dept, default-role/409/422, seeds-tab unchanged.
- No new demo script (user decision: tests only). `make demo-*` regression unaffected.

## Decisions locked in this session
- **Department registration auto-writes p rows**: `POST /api/departments` appends `p, <role>, <dept>, *, *` to policy.csv. This is the user's explicit Phase-4 choice (deviating from my data-only recommendation): the new role is immediately grantable. Mitigations: only the register path writes the file; the write is a single explicit, tiny append; `p` rows otherwise stay file-based; enforcer picks it up on the next version-guard rebuild.
- **Departments are store data + live p-row resources**: the record (name/service/URL/role) lives in `data/members.sqlite`; resources are merged from store records with live `policy.csv` p-row parse so the tab stays accurate pre-registration and seeding needs no resource duplication.
- **`members_version` doubles as the p-reload trigger**: `register_department` bumps it; the enforcer rebuild re-reads the policy file (p rows) AND re-materializes g — so a registered dept + its role take effect on long-lived singletons without a restart.
- **Policy path is settings-driven** so tests append p rows to a temp copy of policy.csv; the committed policy file is never mutated by the test suite.
- **Lockout + validation live on the server**: 409 for lockout/duplicate, 404 unknown member, 422 short password or bad dept name; store-layer guard means any caller (API, demo, test) is covered.
- **No demo script for Phase 4** (user decision) — verification is the pytest suite + existing `make demo-*` regression.

## Verification performed
- `uv run pytest` → **261 passed** (246 existing + 15 new), offline.
- `uv run ruff check app tests scripts` → clean.
- `node --check` on the inline `<script>` (static JS has no syntax errors).
- Regression: `demo_agent.py`, `demo_attacks.py`, `demo_chat_web.py` all complete cleanly.
- Real-DB hygiene: demo/tests never touch `data/members.sqlite` or the committed `policy.csv` (department p-rows only go to temp copies in tests).

## Blocker / open questions
- None.

## Next step (for the next session)
- **mk2 — Phase 5 (session 014)**: browser memory-test walkthrough + e2e — run the scoping scenario entirely in the UI: admin central write (HITL approve), admin central+private facts, alice dept+private facts, **2nd marketing member (molly) reads the shared dept memory** (now possible via the Members tab created here), cross-dept denial (bob can't read marketing). Scripted UI-equivalent e2e test + README quick-start for the web demo.

## Handoff notes
- The new member manager lives purely in the admin session; the dual-session switcher means the demo member stays signed in while the admin edits members/depts.
- `static/index.html` wraps the management JS inline (no build step); it is not covered by pytest — the API contract is what `tests/test_members_mgmt.py` pins.
- `_append_policy_row` is the dashboard's only write to policy.csv; for unknown/custom roles an admin should still edit policy.csv directly for finer p rows (registration only writes the wildcard permit).
- `SEED_DEPARTMENTS` service URLs mirror `ServiceSettings` (crm/hr/finance/devops/gateway); keep them in sync if the demo services move ports.
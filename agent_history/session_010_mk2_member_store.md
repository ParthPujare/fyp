# Session 010 — Plan Mk2 Phase 1: Dynamic MemberStore + Auth Refactor

## Metadata
- Date: 2026-08-15
- Plan / Phase: mk2 — Phase 1 (ref_docs/plans/plan_mk2_web_ui_members.md §4 row 1)
- Plan step: Phase 1 — Dynamic MemberStore + auth refactor (session 010)
- Scope: `data/members.sqlite` becomes the single source of truth for identity; `/auth/login`, `/auth/me`, `claims_for`, dashboard users/departments read the store; lockout guard + audit-logged member changes; demos/tests unaffected
- Status: complete
- State at start: Mk2 was PLANNED; Mk1 Milestones 1–8 done, 204 offline tests green. Identity was a static `USERS` dict in `app/auth/users.py` consumed by auth service, dashboard, `jwt.py`, 6 demo scripts, and ~13 test files. No crypto lib installed (decision: stdlib `scrypt`). `ServiceSettings` had no members path.

## Objective
Plan_mk2 §4 Phase 1 deliverable: "Dynamic MemberStore + auth refactor. `data/members.sqlite`
(user, hashed password, role, dept, scopes, active, timestamps). Migration seeds the 5 current
users. `/auth/login`, `/auth/me`, `claims_for` read the store; dashboard users/departments read
it too; demos still work. Lockout guard (last admin / self). Every change audit-logged."
Acceptance: `POST /auth/login` works for `molly@acme.com` after creation; all tests green;
`make demo-*` unaffected.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 09:00 | explore | Read mk2 plan + Mk1 plan + INDEX + INSTRUCTIONS + latest session. Mapped every `USERS`/`claims_for` consumer (117 refs across 13 test files + 6 scripts + dashboard + auth). Verified 204 tests, no bcrypt/argon2 in pyproject (plan's scrypt fallback), `data/` gitignored. Confirmed Phase 1 must NOT touch `policy.csv` `g` rows (that's Phase 2). |
| 09:30 | clarify | Asked 3 locked decisions: (1) stdlib `scrypt` (no new dep), (2) `USERS` compat shim keeps seed plaintext passwords ONLY for the 5 demo accounts (store stores hashes), (3) autouse tmp-store fixture so tests never touch real `data/members.sqlite`. All accepted. |
| 09:45 | design | Store schema: one `members` table (username PK, password_hash, role, dept, scopes-as-JSON, active, created_at, updated_at). Hash format `scrypt$n$r$p$<b64salt>$<b64hash>` with explicit `maxmem` so scrypt doesn't trip OpenSSL limits. Seed on empty table only (INSERT when COUNT=0), timestamps ISO UTC. Keep the `User` dataclass + `USERS` Mapping shape so 204 tests + demos pass unchanged (plan §3.3). |
| 10:00 | design | `MemberStore` reads open a fresh sqlite connection per op (matches `app/services/lib.py` pattern; avoids cross-thread issues under FastAPI threadpool). Lockout guard enforced at the store layer (any mutator covered): `set_active(..., False)` raises `LockoutGuardError` for last-active-admin and for acting-on-self. Every mutation audit-logs `resource={type:member,id:<username>}`, `reason=admin:members:<action>`. |
| 10:15 | implement | `app/auth/store.py` (SEED_MEMBERS, hash/verify, MemberRecord.claims(), MemberStore). Removed a stray placeholder line before writing tests. |
| 10:30 | implement | `app/auth/users.py` → store-backed read-through `Mapping` shim (`USERS`), `get_store`/`set_store` swappable singleton, `get_user`/`claims_for`. `app/auth/service.py`: login verifies scrypt hash + active; `/auth/me` re-reads the store so an admin edit shows on the next claims read even with an existing token (plan §3.1). |
| 10:40 | implement | `app/config.py` + `.env.example`: `SERVICES__MEMBERS_PATH=data/members.sqlite`. Dashboard `/api/users` now returns `active` alongside the store-backed role/dept/scopes (departments + `_scope_namespaces` were already store-backed via the shim). |
| 10:50 | test | `tests/conftest.py` autouse `member_store` fixture: fresh seeded tmp DB per test (swap `users._STORE`, restore after). `tests/test_members.py` (17 tests): seed, hash≠plaintext, login success/wrong/unknown, inactive 401, **create molly → login 200 (Phase-1 acceptance)**, `/auth/me` reflects store update, lockout (self + last admin + allowed when 2nd admin exists), reset password, audit-logged changes, `USERS`/`claims_for`/`get_user`, me-404 for missing member. `tests/test_auth.py`: added store-pointing test. |
| 11:00 | verify | `uv run pytest` → 222 green (204 + 17 members + 1 auth). ruff: fixed isort ordering in test_members (order_by_type — constants before classes before functions) → `ruff check app tests scripts` clean. |
| 11:10 | demo | All 6 `make demo-*` scripts run clean end-to-end (agent, memory, sanitize, skills, evals, attacks) against the store-backed shim. `uv run python -m app.auth.store` seeds the real `data/members.sqlite`. Explicit end-to-end outside pytest: 401 before create → 200 after (molly) → `/auth/me` 200 → 1 audit entry. |
| 11:20 | docs | README (members section + tests bullet), session file, agent_history INDEX row, plan_mk2 phase-1 row → DONE, ref_docs/plans/INDEX Mk2 status updated. |

## What was done
- `app/auth/store.py`: `MemberStore` (sqlite at `data/members.sqlite`), `MemberRecord`, scrypt hash/verify, seed migration, `get/get_user/list/claims_for/verify`, `create/update/set_active/reset_password`, `LockoutGuardError`/`MemberExistsError`/`MemberError`, audit-logged mutations, `python -m app.auth.store` dump.
- `app/auth/users.py`: rewritten as a read-through `Mapping` over the store — `User` + `USERS` shape preserved (plan §3.3); `get_store`/`set_store`; `get_user`/`claims_for`; seed plaintext only for the 5 demo accounts.
- `app/auth/service.py`: `/auth/login` verifies the scrypt hash (+ active) → 401 otherwise; `/auth/me` returns canonical claims from the store (404 if removed).
- `app/config.py` + `.env.example`: `SERVICES__MEMBERS_PATH=data/members.sqlite`.
- `app/dashboard/app.py`: `/api/users` store-backed + `active` field returned.
- `tests/conftest.py`: autouse `member_store` fixture (fresh seeded tmp DB per test).
- `tests/test_members.py`: 17 tests (Phase-1 acceptance: molly create → login). `tests/test_auth.py`: +1 store test.
- Docs: `README.md`.

## Decisions locked in this session
- **Source of truth moves, shape stays:** the `MemberStore` is the identity source; the static `USERS` dict becomes a read-through Mapping shim so nothing else changes (matching plan §3.3).
- **Hash = stdlib scrypt** (`scrypt$n$r$p$salt$hash`, explicit maxmem) — no new dependency; seed demo passwords returned by the shim as plaintext for the 5 seeds ONLY (plan §2.3), never in the DB.
- **Lockout guard at the store layer**: last-active-admin + acting-on-self both raise `LockoutGuardError`; every mutation is audit-logged at the store.
- **Per-operation sqlite connection** (mirrors `app/services/lib.py`; thread-safe under FastAPI).
- **Tests isolate the store** via an autouse conftest fixture (fresh seeded tmp DB) so the real `data/members.sqlite` is only used by dev/demo runs.

## Verification performed
- `uv run pytest` → **222 passed** (204 existing + 18 new), offline.
- `uv run ruff check app tests scripts` → clean.
- `make demo-*` equivalent: `demo_agent`, `demo_memory`, `demo_sanitize_dashboard`, `demo_skills`, `demo_trajectory_evals`, `demo_attacks` — all complete cleanly.
- `uv run python -m app.auth.store` → real `data/members.sqlite` seeded with the 5 users.
- End-to-end (no pytest): create `molly@acme.com` → `/auth/login` 401 before / 200 after with marketing claims → `/auth/me` 200 → 1 audit entry.

## Blocker / open questions
- None.

## Next step (for the next session)
- Mb **mk2 — Phase 2 (session 011)**: store-driven enforcement — `PolicyEnforcer` materializes `g` rows from `MemberStore` (p stays in `policy.csv`); version/refresh guard keyed to a member write counter; deny-by-default tests for unknown/disabled members; disabled member's token ⇒ 403 while an active marketing member gets grants.

## Handoff notes
- `tests/attack/helpers.py` must stay a plain helpers module (no `conftest.py` there) — unchanged this session.
- The autouse `member_store` fixture is in the root `tests/conftest.py`; any new test that mutates members gets a fresh seeded store — create/update me fixtures for cross-test state.
- Phase 2 will touch `app/harness/enforcer.py`; keep the full A1–A8 + integrity regression green before Merge/PR (plan_mk2 §8).
- `data/members.sqlite` was created during this session's verification (gitignored); subsequent sessions/demos will see the 5 seeded users.
# Session 011 — Plan Mk2 Phase 2: Store-Driven Enforcement

## Metadata
- Date: 2026-08-15
- Plan / Phase: mk2 — Phase 2 (ref_docs/plans/plan_mk2_web_ui_members.md §4 row 2)
- Plan step: Phase 2 — Store-driven enforcement (session 011)
- Scope: `PolicyEnforcer` materializes `g` (user → role → dept) live from the `MemberStore` for ACTIVE members only; `p` rows stay in `policy.csv`; lazy version-guard keeps long-lived enforcers fresh; unknown/disabled members are denied by default at the enforcer and 403 at the gateway + dashboard
- Status: complete
- State at start: mk2 Phase 1 done (session_010, pushed `f455cf0`): `MemberStore` at `data/members.sqlite`, `USERS` read-through shim, login/me store-backed, lockout guard, audit-logged mutations, autouse `member_store` fixture, 222 tests green. `policy.csv` still had the static `g` block. Enforcer loaded `casbin.Enforcer(model, policy)` once at construction.

## Objective
Plan_mk2 §4 Phase 2 deliverable: "PolicyEnforcer materializes `g` from MemberStore; `p` stays
in `policy.csv`. Reload/version guard on member change. Deny-by-default added test: unknown +
disabled member ⇒ every action denied." Acceptance: full regression green incl. attack matrix +
trace/audit integrity + `make demo-attacks`; a disabled member's token gets 403 while an active
marketing member gets marketing grants.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 19:40 | explore | Read the open session context (previous session state + next steps). Confirmed Phase 2 scope: `g` from store, version guard, unknown/disabled deny everywhere. Noted the two skill-test factories build copies of `policy.csv` and already get `g` via the enforcer's new store path — no changes needed there. |
| 19:45 | design | Locked the store contract: a `meta` table holds a single `members_version` INTEGER write counter; every mutation (`create`/`update`/`set_active`/`reset_password`) bumps it **inside the same transaction** as the write. `MemberStore.version` property + `is_active(username)` for cheap per-request gates. Enforcer caches the version at build, and on each public entry does a cheap `SELECT` — rebuild (under a lock) only when it changed. |
| 19:50 | implement | `app/auth/store.py`: `meta` table + `members_version='0'` seed (ON CONFLICT DO NOTHING), `_bump_version(conn)` helper, version bump calls before every commit in the 4 write methods, `version` property, `is_active`. Decided seeds do NOT bump (version 0 = pristine seed state; enforcer first build starts at −1 so it always builds). |
| 19:55 | implement | `app/harness/enforcer.py` rewritten: keep the exact public surface (`enforce`/`tool_allowed`/`memory_allowed`/`memory_recall_allowed`/`skill_allowed`/`_obj_act`) so runners/filters/tests are untouched; `_build()` loads model+`p`, then `add_role_for_user_in_domain(sub, role, dept)` for ACTIVE members only; `_sync_g()` rebuilds under the lock when `_store_version() != _members_version`. `model.conf` still declares `g = _, _, _` (needed for RBAC-with-domains). |
| 20:00 | implement | `app/gateway/app.py`: `_verified_active(token)` — verify JWT (401), then `is_active_member(sub)` else 403; wired into `/hello`, `/chat`, `/approvals/{id}/decide`. `app/dashboard/app.py`: `_verify_admin` gains the same store-existence+active gate (403) after the existing admin-role check. `app/auth/users.py`: `is_active_member(username)`. |
| 20:05 | test | `tests/attack/test_policy_matrix.py` A1 oracle now reads ACTIVE-member assignments from the store instead of parsing `g` rows out of `policy.csv` (`_store_assignments()`); `_expected` returns False when a user has no assignment (unknown ⇒ deny). `tests/conftest.py`: `enforcer` fixture now depends on `member_store` (deterministic construction order after the store swap). |
| 20:10 | test | `tests/test_enforcement.py` (14 tests): unknown default-deny + cross-dept deny; active molly granted → deactivated (same instance) denied; role/dept edit reflected lazily; reactivation restores; skill/memory denied for unknown; `is_active_member`; version increments on all 4 mutations; gateway `/hello`+`/chat` 403 after deactivation and for a valid JWT minted for a non-member; 401 for a bad token; dashboard 403 after an admin is deactivated. |
| 20:15 | fix | First run: `login_token("molly@acme.com")` failed — the `USERS` shim returns `password=None` for non-seed members (by design). Switched those two tests to mint JWT via `create_token(User(...))` from the store record instead of HTTP login. |
| 20:20 | verify | Targeted suites green → full `uv run pytest` **236 passed**; `ruff check app tests scripts` clean; `demo_agent` + `demo_attacks` run clean against the real store. |
| 20:30 | verify | Real-DB (copy of `data/members.sqlite`, version 0): create molly marketing → enforce crm:read True; deactivate → False; reactivate → True; ghost → False; final version 3 (3 mutations). This is exactly the Phase-2 acceptance, on the real seed DB, outside pytest. |

## What was done
- `app/harness/enforcer.py`: rewritten — `g` materialized from `MemberStore` active members via `add_role_for_user_in_domain`; `_sync_g()` lazy rebuild under a lock keyed to `members_version`; public API unchanged (plan §3.2/§8).
- `app/harness/policies/policy.csv`: static `g` block (5 rows) removed; header comment documents store-materialized `g`. `p` rows untouched.
- `app/auth/store.py`: `meta` table + `members_version`; `_bump_version(conn)` in the write transaction; `version` property; `is_active(username)`.
- `app/auth/users.py`: `is_active_member(username)` helper.
- `app/gateway/app.py`: `_verified_active` gate (401 bad token, 403 missing/inactive member) on `/hello`, `/chat`, `/approvals/{id}/decide`.
- `app/dashboard/app.py`: `_verify_admin` now also requires store existence + active (403).
- `tests/conftest.py`: `enforcer` fixture depends on `member_store` for deterministic build/store ordering.
- `tests/attack/test_policy_matrix.py`: A1 oracle `ASSIGNMENTS` from store active members; unknown ⇒ deny.
- `tests/test_enforcement.py`: 14 new tests (deny-by-default, lazy version guard, gateway/dashboard 403 gates, version counter).
- Docs: README, session file, `agent_history/INDEX.md`, plan_mk2 §4 row 2 → DONE, `ref_docs/plans/INDEX.md`.

## Decisions locked in this session
- **Store is the identity AND `g` source of truth** (`g` materialized for ACTIVE members only) — `p` rows remain in `policy.csv`; `model.conf` `g = _, _, _` unchanged.
- **Lazy version guard over eager reload**: a cheap `SELECT` per enforce entry (fits plan_mk2 §8 "reload keyed to a write counter with a lock"); rebuild only when `members_version` changed.
- **Version bump lives inside the mutation transaction** (no torn visibility between the write and the counter).
- **Seed insert does not bump**: version 0 = pristine seed state; a fresh enforcer (cached −1) always builds.
- **403 for identity problems, 401 for token problems**: bad/expired token ⇒ 401; valid token whose sub is missing/disabled ⇒ 403 (gateway + dashboard).
- **Tests mint JWTs from the store record** (`create_token`) — the `USERS` shim legitimately has `password=None` for non-seed members, so HTTP login isn't usable for created members.

## Verification performed
- `uv run pytest` → **236 passed** (222 existing + 14 new), offline.
- `uv run ruff check app tests scripts` → clean.
- `make demo-attacks` equivalent (`uv run python scripts/demo_attacks.py`) → complete, all attacks blocked; `demo_agent.py` → complete, RBAC contract 1.0.
- Real-store check (copy of `data/members.sqlite`, outside pytest): molly create→crm:read True → deactivate→False → reactivate→True → ghost→False; version 0→3.
- Regression of the two skill-test `g`-dependent factories (`test_skills`, `test_skill_misuse`) → green (they inherit the new store-driven `g`).

## Blocker / open questions
- None.

## Next step (for the next session)
- **mk2 — Phase 3 (session 012)**: chat in the browser + any-user login — dashboard `POST /api/chat` server-side proxy to `:8001/chat`; any active member can sign in; render reply/tool calls/guardrail_hits/interrupted/approval_id.

## Handoff notes
- `data/members.sqlite` is still at version 0 (tests mutate tmp copies only; the real-DB check this session used a throwaway copy). Seeded 5 users.
- Long-lived enforcer singletons (`_get_components` in gateway + dashboard) stay correct after admin edits without restart thanks to the version guard — Phase 3/4 UI edits will exercise this.
- The A1 oracle now reads store assignments at module import from whatever store is current then; matrix tests don't mutate members, so the real/tmp store equivalence keeps them green.
- `members_version` is an INTEGER incremented via `CAST(value AS INTEGER) + 1` — keep `meta.value` unbumped by any future direct writes to `members`.
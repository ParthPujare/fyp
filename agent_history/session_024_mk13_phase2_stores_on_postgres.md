# Session 024 — Mk13 Phase 2: members, approvals and memory facts on the shared Postgres (Neon)

## Metadata
- Date: 2026-09-26
- Plan / Phase: mk13 — Phase 2 (`plan_mk13_docker_cloud_db.md`)
- Plan step: port `MemberStore` / `ApprovalStore` / `SqliteBaseStore` to Postgres without forking their code
- Scope: same store code on two back-ends, made fast enough for a ~150 ms link, guarded against the hazards sharing creates
- Status: complete — verified on local Postgres AND real Neon (isolated schema + a container on a separate compose project; live tables only read)
- State at start: HEAD `8c31419` + uncommitted Phase 1 work; user confirmed `python -m app.db check` on Neon (PostgreSQL 18.6, pooler, **149 ms median round trip**)

## Objective
Everything a teammate creates that others must see — members, keys, connections, approvals, memory facts — lives in the one Neon database, with SQLite still the default and all 370 old tests unchanged.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| explore | design | Stores use only `execute/fetch*/commit/close/with conn/rowcount` + `row["x"]`/`row[0]` -> a `sqlite3.Connection` look-alike (`PgSession`) lets the SAME code run on both back-ends; Postgres classes are thin subclasses |
| explore | risk | 149 ms/round trip changes the design: `enforcer._sync_g` reads `version` on EVERY `enforce()`, `is_active` on every request, `_is_connected` per tool -> would be seconds per chat turn |
| implement | schema | Kept TEXT timestamps/JSON + INTEGER flags identical to SQLite (shared code). `memory_items.seq IDENTITY` replaces rowid tiebreak |
| test | finding | Bug: `ensure_migrated` keyed pools by `id()`; ids are reused after GC so a NEW pool looked migrated (caught by a test). Now a WeakSet |
| **incident** | test | After wiring the stores, my SQLite-store test run connected to the user's REAL Neon (pydantic loads `.env` even though I had `unset` the shell var): applied 0002 + seeded 5 demo accounts/5 depts. Checked read-only: no other data, `members_version` 0. Fixed: conftest blanks `DATABASE_URL` first + session assertion; verified with the real `.env` present. Told the user |
| measure | neon | First real timings: SELECT ~440 ms = BEGIN/stmt/COMMIT. Fix: lazy transactions (open at first write; standalone autocommit; single-op memory batch) |
| measure | pool | Phase 1 `check_connection` pinged on EVERY checkout (doubling each query). Now only connections idle >30 s |
| design | cache | `version` polled <=1/s and guards a long member cache; 30 s for data the counter doesn't cover; own writes invalidate; generation counter vs racing reads; `verify` uncached; `get()` from the cached list |
| measure | docker | Container (RTT ~150 ms) chat turn 3.6-4.5 s even warm -> TTLs too short (3 s) + first request per process paid ~3 s. TTL split, pre-warm in `_warm()`, FastAPI lifespan warm-up in auth/gateway/dashboard -> warm 0.1-0.4 s, login 3.4 -> 0.5 s |
| **hazard** | design | Approvals now shared but LangGraph checkpoints still local (Phase 3): deciding another laptop's approval records the decision then cannot resume (and can't be re-decided). Guard: 409 BEFORE recording + `resumable_here` flag + UI note; disappears in Phase 3 |
| test | harness | `TEST_BACKEND=postgres` whole-suite mode: the autouse member-store fixture shared the `pg` fixture's schema and broke 8 tests that expect a pristine one -> own private schema |

## What was done
- `app/db/`: `compat.py` (PgSession/Row/Cursor, lazy tx, read-only retry), `schema.py` (`ensure_migrated`, 1-read fast path), `pool.py` (idle-only ping, stamps), `migrations/0002_core_tables.sql`
- `app/auth/pg_store.py` (`PgMemberStore`: version-gated cache, warm-up), `app/memory/pg_stores.py` (`PgApprovalStore`, `PgBaseStore` + single-op batch), `app/memory/factory.py`, `app/auth/users.py` (`_open_member_store`)
- Portability edits in `app/auth/store.py`, `app/memory/store.py` (behaviour-neutral); `graph.load_memory` reads each scope once
- Call sites: `agent/runner.py`, `gateway/app.py`, `dashboard/app.py` -> factories; lifespan warm-up in auth/gateway/dashboard; resume guard (`runner.run_state_is_local`, dashboard + gateway decide, `resumable_here`, UI)
- Tests: `tests/test_pg_stores.py` (contract tests on BOTH back-ends + cache/race/session tests), `tests/test_shared_approvals.py`; `tests/conftest.py` (hermetic `DATABASE_URL`, shared `pg` fixture, `TEST_BACKEND`)
- `Makefile` (`test-pg-full`), `.env.example`, README (Neon section rewritten), plan Mk13 + INDEX

## Decisions locked in this session
- Cache staleness contract: members/departments <=1 s (version counter); keys/connections/catalog <=30 s; own writes immediate; sign-in never cached.
- Writes are never retried after a lost connection (outcome unknowable); reads are.
- Shared approvals are not decidable from another machine until Phase 3 (guard).
- Old local accounts are NOT migrated (user accepted data loss); the shared DB starts with the 5 demo accounts.

## Verification performed
- `pytest` default (offline, real `.env` present): 403+ passed, 41 skipped (Postgres-gated). With `TEST_DATABASE_URL`: contract tests on both back-ends; with `TEST_BACKEND=postgres`: the ENTIRE pre-existing suite (370 tests) on the Postgres member store.
- Real Neon: isolated-schema smoke (create/get, encrypted key round trip, atomic approval, memory upsert, second-instance visibility via version poll, dead-connection recovery) — schema dropped; leftover-schema check clean; live tables unchanged (5 members, version 0, 0 approvals/memory).
- Container on Neon (separate compose project, port 18006): healthy; migrations up to date; login, `/api/users`, chat OK; first requests after boot 0.5-1.7 s, warm turns 0.1-0.4 s. Project removed (`down -v`).
- NOT verified: two REAL machines concurrently; the user's own running container (still the old image until they rebuild); background behaviour over a Neon 5-minute autosuspend beyond the idle-ping unit test.

## Blocker / open questions
- Pending from earlier sessions: policy row for role `admin_agent` (still local `policy.csv`, moves in Phase 6).

## Next step (for the next session)
- Mk13 Phase 3: chat checkpoints (`PostgresSaver`) + batched trace writes — this also removes the "approval from another machine" guard.

## Handoff notes
- To switch the user's running app to Neon: `docker compose up -d --build` (their `.env` already has `DATABASE_URL`). Their old accounts in the Docker volume disappear from the UI (still in the volume; data loss accepted).
- Never run test/dev scripts on the host without knowing `.env` may point them at the shared DB; tests are now hermetic, ad-hoc scripts are not (profile/smoke scripts used isolated schemas or read-only queries).
- Windows: use `127.0.0.1`, not `localhost`, in local Postgres URLs.

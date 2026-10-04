# Session 031 — Mk16 follow-up: DB-backed shared platform key + admin UI

## Metadata
- Date: 2026-10-04
- Plan / Phase: mk16 — Phase 3 (`plan_mk16_system_memory.md` follow-up section)
- Plan step: migration + store methods → resolution order → admin endpoints + UI → tests
- Scope: one encrypted shared-DB row replaces per-machine `.env` distribution
- Status: complete offline (PG contract tests skip without `TEST_DATABASE_URL`); user runs `db-migrate` + pastes key once
- State at start: session 030 done (548 passed); platform key env-only per machine; user asked whether same-DB instances can share that exact key → yes via DB row (answers: DB wins, any admin, explicit paste only)

## Objective
Any instance connected to the same Neon DB uses the exact same platform key with zero per-machine setup: admin pastes key + model once in the dashboard, every laptop picks it up, key Fernet-encrypted at rest, model changeable via UI, `:free` enforced.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| start | explore | `member_llm_keys` is the exact pattern to copy: `_fetch_*` (encrypted, cache-safe) vs decrypting getter, `_bump_version` in-write-transaction, `ON CONFLICT` upserts valid on both back-ends, `PgMemberStore._memo` for reads |
| design | design | Members DB (not memory DB) hosts the row: reachable via `get_store()` from extractor, endpoints, and status without new plumbing; version counter already guards freshness |
| implement | implement | `extra="ignore"` lesson applied in reverse: status path uses the meta (never-decrypt) getter so a stale Fernet key can't 500 the health endpoint — only the decrypting getter raises |
| implement | implement | `get_system_provider` singleton keyed on (key, model, ...) needs no change for rotation: new args → new cached instance; explicit `clear_system_provider_cache()` on PUT/DELETE for immediacy |
| verify | test | No docker here → PG contract cases skip; validated 0009 DDL + upsert syntax against SQLite and kept statements to the shared-subset both engines accept |

## What was done
- `app/db/migrations/0009_platform_llm.sql` (new) + SQLite twin in `MemberStore._init_schema`
- `app/auth/store.py`: `get/set/clear_platform_llm`, `get_platform_llm_meta`, `_fetch_platform_llm` (ValueError on non-`:free`, MemberError on keyless model-only change, version bump, audit entries)
- `app/auth/pg_store.py`: memo-cached `get_platform_llm_meta` / `_fetch_platform_llm` (encrypted only)
- `app/llm/system.py`: `resolve_platform_config` (DB → env → None; HTTPException-decrypt → `decrypt-failed` status + env fallback), `describe_platform_source` (never decrypts), builders use resolved model+key
- `app/memory/system_status.py`: `get_status` gains `source`/`model`
- `app/dashboard/app.py`: `GET/PUT/DELETE /api/system-memory/config` (admin, 401/403/422 gates; value never echoed) + status extension
- `app/dashboard/static/index.html`: admin "Platform extractor" card in Memory tab (status, password key input, model field, Save/Clear) — JS `node --check` clean
- `.env.example`: DB-primary wording; `tests/conftest.py` untouched (env blanking still hermetic)
- `tests/test_platform_llm.py` (new, 13 passed / 7 PG-skipped): dual-backend contract (roundtrip, no-plaintext-at-rest, model-only keep, `:free` rejection, keyless rejection, clear), precedence (DB>env, env fallback, none), decrypt-failure fallback + status, endpoint gates + put/get/delete flow + key-never-echoed, cross-instance PG test
- `tests/test_system_memory.py`: status-shape updates (source/model fields)
- Docs: plan phases table + supersession notes + follow-up section; this file + `INDEX.md`

## Decisions locked in this session
- DB row wins over env; env never auto-imports (explicit paste only, per user).
- Model stored plaintext beside the ciphertext; `:free` validated in store (ValueError→422) and re-checked at provider build.
- Partial update: `api_key=None` keeps stored key (model-only change allowed); first save requires both.
- `DELETE` reverts to env (no data loss elsewhere); all mutations audit-logged without the value.
- Cache: PG memo 30 s + version-counter invalidation; SQLite direct reads; singleton keyed on resolved args.

## Verification performed
- `uv run pytest`: **561 passed, 156 skipped** (PG-only skips; no docker on this machine); `ruff check app tests`: clean; dashboard JS `node --check`: clean.
- Migration DDL + upsert executed against throwaway SQLite (PG run deferred to `make test-pg`).
- Live DB migration + first paste left to the user (their Neon holds the live key).

## Blocker / open questions
- `make test-pg` (incl. the 7 skipped PG contract cases + cross-instance test) not run here — needs Docker or Neon throwaway schema.
- User must run `make db-migrate` (or restart) to apply 0009, then paste the key once via the new card; PC-2 can then drop its env key.

## Next step (for the next session)
- Autonomous-admin follow-up plan (NL policy store, passphrase, worker, UI) — the remaining Mk16 open item.

## Handoff notes
- `resolve_platform_config` hits the member store per turn (PG: memo-cached; SQLite: local) — watch Neon round-trips if extraction latency is ever profiled.
- `get_fernet()` reads cached `get_settings()`; Fernet rotation across laptops surfaces as `decrypt-failed`, not an outage.

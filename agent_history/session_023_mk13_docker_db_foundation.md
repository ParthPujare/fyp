# Session 023 — Mk13 Phase 0 follow-up + Phase 1 (Docker fix, shared-DB foundation)

## Metadata
- Date: 2026-09-23
- Plan / Phase: mk13 — Phase 0 (Docker) verification + fixes, Phase 1 (DB foundation). Plan `plan_mk13_docker_cloud_db.md` created 2026-09-22.
- Plan step: Mk13 — `docker compose up` for everyone; `app/db/` pool + migrations + CLI + tests
- Scope: make the containerized app really reachable, then lay the Postgres (Neon) plumbing without touching any store yet
- Status: complete (Phase 1 verified on a local Docker Postgres; **real Neon not yet exercised** — needs the user's connection string)
- State at start: HEAD `8c31419` "docker set up"; Dockerfile/compose from 2026-09-22 published ports 8000-8006; user's first `docker compose up` failed to publish ports (`:8000` owned by another project's container)

## Objective
1. Fix the Docker run so it works on a machine with other containers and for a brand-new teammate.
2. Phase 1 of the Neon plan: connection pool, migration runner, `DATABASE_URL`, CLI, tests — SQLite stays the default.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| explore | diagnose | User's logs: image built, bind on :8000 failed (`matching_engine_api` owns it), container later "healthy" but PORTS showed `8000-8006/tcp` only; `curl localhost:8006` → 000. A healthy container ≠ a reachable one |
| explore | design | Grepped the UI: browser only calls the dashboard (`/api/*`, same origin); dashboard reaches auth/gateway over 127.0.0.1 inside the container → publish only :8006 (`DASHBOARD_PORT`), api ports opt-in override file |
| explore | risk | Found `core.autocrlf=true`, no `.gitattributes` → teammate checkout would give `run_docker.sh` CRLF → bash fails. Added `.gitattributes` + `sed` in Dockerfile |
| explore | risk | Container `localhost` ≠ laptop → LM Studio needs `host.docker.internal` (added `extra_hosts`, documented; untested against live LM Studio) |
| design | decision | `.env` made optional (`env_file required:false`) — earlier claim it must stay manual was wrong (default `ENV=dev` = identical behaviour) |
| verify | docker | Re-created stack; host `/health` 200, login via dashboard proxy OK, chat OK, history survives down/up, fresh copy with no `.env` + `DASHBOARD_PORT=18006` healthy |
| design | phase 1 | psycopg 3 + plain SQL (Prisma rejected: unmaintained python client). Keep ONE store code path for both DBs via `?`→`%s` translation (decided for Phase 2) |
| implement | pool | Neon specifics: check-on-checkout, idle < 5 min suspend, prepared statements off (PgBouncer), autocommit+dict_row (LangGraph needs it) |
| implement | migrate | Transaction-scoped advisory lock (session locks break through the pooler); re-check after lock; checksums normalised for CRLF; rollback whole |
| test | env | No Neon string yet → throwaway `postgres:16-alpine` via compose profile `test-db` (port 55432, tmpfs) |
| test | finding | First `check` took 30 s: `localhost` → IPv6 `::1` black-holes on Docker Desktop/Windows; `127.0.0.1` = 36 ms. Switched all local URLs, added a tip to `check` |
| test | finding | Wrong password / typo'd host took 45 s (pool retries everything). Added `_probe`: retry transient errors, fail fast on unfixable ones. First version missed Docker's `failed to resolve host` wording (21 s) → added, retested (8 s incl. container start) |
| verify | full | 398 passed with `TEST_DATABASE_URL` (370 old + 28 new), ruff clean |

## What was done
- `Dockerfile`, `docker-compose.yml`, `docker-compose.api-ports.yml`, `.dockerignore`, `.gitattributes`, `scripts/run_docker.sh` — Phase 0 fixes above; compose `testdb` profile
- `app/db/{__init__,pool,dsn,sql,migrate,__main__}.py`, `app/db/migrations/0001_baseline.sql` — new package
- `app/config.py`: `Settings.database_url` (blank ⇒ None)
- `scripts/run_docker.sh`, `scripts/run_dev.sh`: `python -m app.db migrate --if-configured` before services (fail-fast when configured)
- `Makefile`: `db-check/db-migrate/db-status/test-db-up/test-db-down/test-pg`
- `pyproject.toml`/`uv.lock`: `psycopg[binary,pool]`
- `tests/test_db_foundation.py` (28 tests: 19 offline, 9 Postgres-gated)
- `README.md` (Docker + Neon sections), `.env.example`, `ref_docs/plans/plan_mk13_docker_cloud_db.md`, `ref_docs/plans/INDEX.md`

## Decisions locked in this session
- Publish only the dashboard port; everything else stays inside the container (avoids host port collisions).
- Fail fast when `DATABASE_URL` is set but unusable — never silently fall back to SQLite (would split team data).
- One store implementation for both back-ends via placeholder translation (Phase 2 approach).
- Migrations: additive only, checksummed, never edited after apply.

## Verification performed
- Docker: stack recreated on the user's machine; health/login/chat/persistence/no-`.env` checks (see log). Container with valid `DATABASE_URL` → migrates then all 7 services up; wrong password 3 s, wrong db 4 s, typo host 8 s → exit 1 with a plain-words hint.
- `pytest` 398 passed with `TEST_DATABASE_URL=…127.0.0.1:55432…`; without it the 9 Postgres tests skip.
- NOT verified: any real Neon endpoint (TLS, pooler/PgBouncer behaviour, cold-start timing, `channel_binding=require`); LM Studio via `host.docker.internal`; a real `git clone` of the Docker files.

## Blocker / open questions
- Needs the user's Neon **pooled** connection string in `.env` (`DATABASE_URL`) to run the first real-Neon smoke test (`docker compose exec app python -m app.db check`).
- Pending from earlier sessions: policy row for role `admin_agent` (accounts created via the Members form get zero tools).

## Next step (for the next session)
- Real-Neon smoke test, then Mk13 Phase 2: port `MemberStore` / `ApprovalStore` / `SqliteBaseStore` to the shared DB via the adapter (see plan), running the same behaviour tests on both back-ends.

## Handoff notes
- Work since session 022 that was never logged as its own session: 2026-09-19/20 audit fixes G1–G5/G8/G9 + delete-chat + optional audit HMAC (tests in `tests/test_hitl_resume.py`), `research_objective/01_ztae_formal_spec.md` + `metrics/collect_metrics.py`, Mk13 Phase 0. All committed except this session's changes.
- Never put a real `DATABASE_URL` in chat, git, or the image: `.env` is git-ignored and `.dockerignore`d.
- Local Postgres URLs: use `127.0.0.1`, not `localhost` (Windows IPv6 stall).

# Plan Mk13 — Dockerized run + centralized cloud DB (todo #12)

## Metadata
- Date: 2026-09-22
- Status: Phase 0 DONE and VERIFIED on a real Docker Desktop (2026-09-23); Phases 1–6 OPEN
- Follows: Mk1 (SQLite stores), Mk2 (web UI), Mk12 (last store/schema change)
- Closes: `ref_docs/todo.md` item 12 ("Centralise the entire db on some cloud platform")
- Scope lock (user, 2026-09-20/22):
  1. Several laptops must share **one live database** — not per-laptop files, not a backup copy.
  2. The app still runs on each laptop; only the DB is centralized (no laptop-side changes to
     *where the app runs*, only to *where its data lives*).
  3. Host must be free, and must **stay** free without a migration later if avoidable — picked
     Neon (Postgres) over Turso/Supabase/D1 after comparing free-tier terms (see session notes
     2026-09-19/20); `DATABASE_URL` is the only thing that should need to change if it doesn't.
  4. Data loss is acceptable (can be repopulated) — favours "just move to Postgres" over a
     zero-downtime migration.
  5. Everyone must be able to run the app with **no manual multi-terminal steps** — this pulled
     Docker into the same plan as a prerequisite phase (Phase 0), since "one shared DB" is only
     as convenient as "one command to run the app" that reaches it.
  6. Prisma considered and rejected: no maintained official Python client (community client
     unmaintained since March 2025) and it can't cover LangGraph's checkpoint tables or FTS
     anyway — see comparison in session notes.

## Problem
Every store (`app/auth/store.py` MemberStore, `app/memory/store.py` SqliteBaseStore,
`app/memory/episodic.py` FTS5, `app/memory/approvals.py`, `app/harness/audit.py`,
`app/ops/store.py` TraceStore, LangGraph's `SqliteSaver` checkpoints) is a **per-laptop file**
under `data/`. Two teammates never see the same members, chats, approvals, or audit trail.
`app/harness/policies/policy.csv` and `skills/*.md` are also per-laptop files, mutated at
runtime by the dashboard. There is also no single command to run the app — `make run` starts
seven `uvicorn` processes locally and needs `uv` installed correctly on every laptop.

## Design

### Phase 0 — Docker (this session)
One image, one container, all seven services — the containerized twin of
`scripts/run_dev.sh`, changed only to bind `0.0.0.0` (`scripts/run_docker.sh`) so the host can
reach it; every service still talks to the others over `127.0.0.1` (`app/config.py`
`ServiceSettings`, unchanged), which is only correct because they share one container's network
namespace. Splitting into one-container-per-service is out of scope (would need those URLs
rewritten to service DNS names, and buys nothing before the DB is centralized anyway).
- `Dockerfile` — `python:3.12-slim-bookworm`, pinned `uv` via the official
  `COPY --from=ghcr.io/astral-sh/uv:0.12.17` pattern, layered `uv sync --locked` (deps, then
  code) for cache-friendly rebuilds, non-root `appuser`, `HEALTHCHECK` on `/health` (dashboard).
- `docker-compose.yml` — one `app` service; **only the dashboard (:8006, host port via
  `DASHBOARD_PORT`) is published** — the browser only ever talks to it, and it reaches the other
  six services over 127.0.0.1 inside the container. `env_file` is `required: false`. A named
  volume `fyp_data:/app/data` keeps SQLite data across `down` (not `down -v`). `extra_hosts:
  host.docker.internal:host-gateway` so the container can reach LM Studio/Ollama on the host.
- `docker-compose.api-ports.yml` — optional override that also publishes auth/gateway (host
  ports 18000/18001) for the README curl demos.
- `.gitattributes` (`*.sh` → LF) + a `sed` in the Dockerfile — Windows `core.autocrlf=true`
  would otherwise give the entrypoint CRLF endings and break `bash` in the container.
- `.dockerignore` — excludes `data/`, `.venv/`, `.git/`, caches, and host-only docs
  (`agent_history/`, `research_objective/`, `ref_docs/`) from the build context.
- **No manual step at all.** `.env` is optional: absent, `app/config.py` falls back to `ENV=dev`
  and its built-in dev secrets, which is identical to what `.env.example` contains. (An earlier
  draft of this plan said copying `.env.example` was mandatory "by design" — wrong: with
  `env_file: required: false` the guard `_no_dev_secrets_outside_dev` is unaffected, because
  the default `ENV` is still `dev`; it only trips if someone sets `ENV` to something else
  without real secrets.)
- **Not yet wired:** `DATABASE_URL` — introduced in Phase 1, not added to `.env.example` yet
  (an unused setting is worse than an absent one; see session notes on the `.env` review).

### Phase 1 — DB foundation (OPEN)
- `app/db/` — a `psycopg` (v3) connection pool built from `Settings.database_url`; when unset,
  every store keeps using its current SQLite path (SQLite stays the default for offline/dev/CI —
  the 370 existing tests must keep passing with zero DB configured).
- Numbered migration files (`app/db/migrations/0001_*.sql`, …) + a tiny runner (apply in order,
  record in a `schema_migrations` table) — no ORM (see Metadata #6); `langgraph-checkpoint-postgres`
  (adds a `psycopg` dependency the project needs anyway) is the one exception, for Phase 3.
- `TEST_DATABASE_URL` — Postgres-backed tests run only when set (a Neon `dev` branch, or a local
  Postgres); otherwise they're skipped, never fail the offline suite.
- Docker: `docker-compose.yml` gains `DATABASE_URL` passthrough once Phase 1 lands (no other
  compose change — the app still runs on each laptop's container, only reaching out to Neon
  instead of writing `data/*.sqlite`).

### Phase 2 — Members, approvals, memory facts (OPEN)
Port `MemberStore`, `ApprovalStore`, `SqliteBaseStore` (memory_items) to the Phase-1 pool.
Highest value first: this is what "shared team" actually means day to day (who exists, what's
pending approval, what the assistant has learned).

### Phase 3 — Chat state + traces (OPEN)
- Chat: swap `SqliteSaver` for `langgraph-checkpoint-postgres`'s `PostgresSaver`
  (`.setup()` once; needs `autocommit=True` + `row_factory=dict_row` per its docs).
- Traces: **do not** write one row per event over the network (30-150ms/event would make a
  10-step run visibly slow) — buffer a run's events in memory and flush in one batch at
  `end_run`/on suspend, same hash-chain math as today.

### Phase 4 — Episodic recall (OPEN)
FTS5 is SQLite-only. Replace with Postgres `tsvector`/`tsquery` full-text search
(`RecallService` keeps its interface — `readable_scopes` + `search`; only the backing query
changes). Needs its own regression tests against the leakage property proved in
`research_objective/01_ztae_formal_spec.md` Theorem 2 — must stay 0 after the swap.

### Phase 5 — Audit chain (OPEN)
Move `audit.jsonl` into a table; replace the file lock added for G4
(`tests/test_hitl_resume.py`) with a Postgres advisory lock (`pg_advisory_xact_lock`) scoped
per log — that lock is what makes "several laptops write the same chain" safe, the file lock
only ever handled several *processes on one machine*.

### Phase 6 — Policy + skills (OPEN)
`policy.csv` rows and `skills/*.md` move into tables so a rule/skill added on one laptop is
visible on all of them; `PolicyEnforcer`/`SkillRegistry` keep loading from their current sources
when no DB is configured.

## Phases
| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Docker: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `scripts/run_docker.sh`, README | DONE + verified 2026-09-23 (see "Phase 0 verification") |
| 1 | `app/db/` pool + migration runner + `database_url` setting + `TEST_DATABASE_URL` | OPEN |
| 2 | Members + approvals + memory facts on Postgres | OPEN |
| 3 | Chat checkpoints (`PostgresSaver`) + batched trace writes | OPEN |
| 4 | Episodic recall: FTS5 → Postgres full-text search | OPEN |
| 5 | Audit chain in a table + advisory lock (supersedes the file lock from G4) | OPEN |
| 6 | Policy rows + skills in the database | OPEN |

## Non-goals
- Splitting the seven services into seven containers (see Phase 0 design note).
- An ORM (Prisma rejected — see Metadata #6; SQLAlchemy/Alembic also rejected — see the
  session's "Prisma: no" note, same reasoning applies: the hard parts, `PostgresSaver` and
  full-text search, need raw SQL either way).
- Zero-downtime migration / dual-write cutover — Metadata #4 (data loss acceptable) rules it out.
- Deciding which cloud host is billed if Neon's free tier ever stops being enough — out of scope
  until it's an actual problem (Metadata #3's goal is to delay that decision as long as possible,
  not to pre-solve it).

## Risks
- **Neon free-tier limits** (0.5 GB/project, 100 compute-hours/month, 5-min autosuspend) — fine
  for a small team's data volume today; the trace-event and chat-checkpoint tables (Phase 3) are
  the fastest growers and need a retention/cleanup pass before they're the bottleneck.
- **Shared secrets.** A shared `LLM__FERNET_KEY` means any teammate holding it can decrypt any
  other teammate's stored provider API keys once those rows live in the same database (raised
  and accepted by the user 2026-09-20 — see session notes) — revisit if that stops being okay.
- **Advisory-lock audit writes** (Phase 5) serialize every gated tool call's audit append across
  the whole team, not just one laptop — same tradeoff as the file lock (G4), just wider scope;
  watch `audit_log` latency (was 17-31ms p50/p95 single-machine post-G4) once several laptops
  write concurrently.

## Phase 0 verification (2026-09-23, real Docker Desktop 4.77 / Compose v5.1.4)
- **First real run failed** — image built and all 7 services started, but the container never
  published its ports: `Bind for 0.0.0.0:8000 failed: port is already allocated` (an unrelated
  project's `matching_engine_api` owns :8000). `docker ps` showed `8000-8006/tcp` with no host
  mapping and `localhost:8006` was unreachable although the container was "healthy" — a healthy
  container is not a reachable one. Fix: publish only :8006 (above).
- After the fix, verified from the host: `/health` 200; `index.html` 200; login through the
  dashboard proxy issues a token (wrong password → 401); a chat turn returns a reply; the chat
  history survives `docker compose down` + `up`; a **simulated fresh checkout with no `.env`
  and `DASHBOARD_PORT=18006`** comes up healthy and logs in.
- **Not verified:** LM Studio via `host.docker.internal` (no live LM Studio in the test); a real
  `git clone` (the Docker files were still uncommitted — the fresh checkout was a file copy).
- **Known caveat:** the image bakes the code in, so code edits need `--build`; for active
  development use the native `make run` path (README) — a bind-mount dev mode is not provided.

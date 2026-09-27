# Plan Mk13 — Dockerized run + centralized cloud DB (todo #12)

## Metadata
- Date: 2026-09-22
- Status: Phases 0–5 DONE and VERIFIED (2026-09-27) on local Postgres AND real Neon; Phase 6 DONE
  (2026-09-27) and verified on local Postgres — real-Neon smoke test still to do
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

### Phase 1 — DB foundation (DONE 2026-09-23)
Built (`app/db/`):
- `pool.py` — one lazy `psycopg_pool` pool per process, tuned for Neon: connection re-validated
  on checkout (`check_connection`; Neon drops idle sockets), idle recycle at 240 s (< Neon's
  5-minute suspend), server-side prepared statements off (`prepare_threshold=None`, PgBouncer
  transaction mode), `autocommit=True` + `dict_row` (what `langgraph-checkpoint-postgres`
  needs in Phase 3). A **probe** connects first with a few patient retries for a cold Neon but
  **fails immediately** on unfixable errors (wrong password, unknown host/database) — plain
  `pool.open(wait=True)` would retry those for its whole 45 s timeout.
- `migrate.py` + `migrations/0001_baseline.sql` — numbered SQL migrations, each in its own
  transaction under `pg_advisory_xact_lock` (transaction-scoped so it works through the pooler,
  where session locks do not); re-checks after taking the lock so simultaneous laptop start-ups
  apply each migration exactly once; SHA-256 checksum per applied file (line-ending-normalised) —
  editing an applied migration is refused; a failing migration rolls back whole; a database that
  is *ahead* of the checkout is reported, not fatal.
- `dsn.py` — `redact_dsn`/`describe_dsn`: the password never reaches logs or CLI output.
- `sql.py` — `qmark_to_format`: rewrites the stores' sqlite `?` placeholders to psycopg `%s`
  (respecting string literals, quoted identifiers, comments; escaping `%`), so Phase 2 can run
  the **same** store code on both backends instead of maintaining two copies of ~1500 lines.
- `__main__.py` — `python -m app.db {check,migrate,status}` with plain-language hints.
- `Settings.database_url` (blank ⇒ unset), `DATABASE_URL` in `.env.example`; `run_docker.sh` /
  `run_dev.sh` run `python -m app.db migrate --if-configured` before starting services — a no-op
  without `DATABASE_URL`, **fail-fast** with it (a silent SQLite fallback would split the team's data
  once stores use the shared DB); `docker-compose.yml` `testdb` profile + `make test-pg`.
- Tests: `tests/test_db_foundation.py` — offline tier always runs; Postgres tier runs with
  `TEST_DATABASE_URL` (per-test throwaway schema): idempotency, edit detection, CRLF, rollback,
  6 simultaneous migrators, ahead-of-checkout, pool recovery after `pg_terminate_backend`,
  placeholder translation on real statements.

Decisions / findings this phase:
- `localhost` in a Postgres URL stalls ~8–30 s on Windows + Docker Desktop (IPv6 `::1`
  black-holes before falling back to IPv4); local URLs use `127.0.0.1`. Neon's DNS names are
  unaffected.
- Compose network name (`testdb:5432`) is the reliable way for the app container to reach the
  throwaway DB; `host.docker.internal` + a host-loopback-published port was not relied on.

### Phase 2 — Members, approvals, memory facts (DONE 2026-09-26)
One store implementation, two connection back-ends (as decided in Phase 1):
- `app/db/compat.py` `PgSession` — a `sqlite3.Connection` look-alike over the pool (`execute`,
  `fetchone/fetchall/rowcount`, `Row` with `row["x"]` and `row[0]`, `commit/rollback/close`,
  `with conn:`), so `MemberStore`/`ApprovalStore`/`SqliteBaseStore` run their existing SQL
  unchanged (`?` -> `%s` via `qmark_to_format`). `PgMemberStore`, `PgApprovalStore`, `PgBaseStore`
  are thin subclasses; `app/memory/factory.py` + `auth.users._open_member_store` pick the back-end
  from `DATABASE_URL` for the gateway, dashboard, runner and auth service.
- `migrations/0002_core_tables.sql` — TEXT timestamps/JSON and INTEGER flags kept identical to
  SQLite on purpose (shared code); `memory_items.seq IDENTITY` replaces SQLite's `rowid` tiebreak.
- Portability edits to the SQLite classes (all behaviour-neutral, 370 old tests unchanged):
  `INTEGRITY_ERRORS` hook, `CAST(CAST(value AS INTEGER)+1 AS TEXT)`, `ON CONFLICT DO NOTHING`
  seeding (two laptops booting at once), catalog seeding once per process, `_TIE` column name,
  `_run_op` split out of `batch`, `create()` maps a lost duplicate race to `MemberExistsError`.

**The latency problem (the real design work of this phase).** Measured against Neon from a laptop:
~90-150 ms per round trip (host) and ~150 ms from inside Docker. Naive porting made one chat
turn cost seconds. What fixed it, in order of impact:
1. **Lazy transactions.** psycopg wraps every statement as BEGIN/stmt/COMMIT = 3 round trips
   (~440 ms for a SELECT). `PgSession` opens a transaction only at the first *write* (safe under
   READ COMMITTED: a plain SELECT gains nothing from one); standalone writes on auto-release
   sessions are single autocommit statements; single-op memory batches skip the transaction.
2. **Read cache for members** (`PgMemberStore`): the enforcer asks for `version` on *every*
   `enforce()` and every request calls `is_active`. `version` (the shared `members_version`
   counter, bumped in the same transaction as every member/department change) is polled at most
   once per second and drops the cache when it moves; member/department reads are cached 300 s
   (the counter guards them), other reads (keys, connections, catalog) 30 s; own writes clear
   the cache at once; a generation counter stops a slow pre-write read repopulating it; sign-in
   (`verify`) always reads fresh; `get(username)` is served from the cached member list.
3. **No ping on recently-used pooled connections** (the Phase 1 `check` doubled every query);
   idle >30 s are pinged, because Neon drops idle sockets. Only reads are retried after a dead
   connection — a write's outcome is unknowable after a lost reply.
4. **Start-up warm-up**: `ensure_migrated` is one read when current; each service builds its
   stores and pre-loads the hot data in its FastAPI lifespan, so no user pays ~3 s on the first request.
5. `load_memory` reads each scope once (it read twice).
Result on Neon in Docker: mock-LLM chat turn 3.6-4.5 s -> **0.1-0.4 s warm**; first request after
boot: login 3.4 -> 0.5 s, chat turn 3.0 -> 1.7 s (a new thread reads three memory scopes).

**Hazard found and guarded.** Approvals are shared but suspended-run state (LangGraph
checkpoints) is not until Phase 3: approving another laptop's item would record the decision and
then fail to resume (and a decided approval cannot be re-decided). On the shared DB the decide
endpoints (dashboard admin/dept + gateway) now return 409 *before* recording anything when the
run's checkpoint is not local, and the queue marks the item `resumable_here: false` (UI hides
the buttons). This guard disappears in Phase 3.

**Test hermeticity incident.** A test run after wiring the stores connected the suite to the
user's real Neon DB (pydantic loads `.env` even with the shell variable unset) and applied
migration 0002 + seeded the 5 demo accounts (no other data). Fixed: `tests/conftest.py` sets
`DATABASE_URL=""` before any import and a session fixture asserts it; the Postgres tests build
their own pools from `TEST_DATABASE_URL`.

Verification: `tests/test_pg_stores.py` (contract tests run on **both** back-ends + Postgres-only
cache/race/session tests), `tests/test_shared_approvals.py`; the entire pre-existing suite passes
with the member store on Postgres (`TEST_BACKEND=postgres`); real-Neon smoke test in an isolated
schema (correctness, timings, second-instance visibility, dead-connection recovery); a container
against Neon on a separate compose project (login/users/chat) — live tables only read.

Known limits after Phase 2: audit entries for member admin actions still go to the local
`audit.jsonl` (Phase 5); `MemberStore.path`/`db_path` are meaningless on Postgres; another
person's keys/connection changes take up to 30 s to show; per-request floor is one version poll
(~1 round trip/second while busy) — a background poller could remove it if it ever matters.

### Phase 3 — Chat state + traces (DONE 2026-09-27)
- **Checkpoints:** `app/agent/checkpoints.py` — `make_checkpointer` returns LangGraph's `PostgresSaver`
  on the shared pool when `DATABASE_URL` is set (SQLite saver otherwise); gateway, dashboard and runner
  all use it. Runs use `durability="exit"` on Postgres: LangGraph otherwise checkpoints after *every*
  graph step (several statements each = seconds at ~150 ms/round trip); "exit" persists once when the
  graph finishes **or pauses at an approval**, so HITL is unaffected. Trade-off: a crash mid-run loses
  that run's progress. `list_user_threads` replaces the raw sqlite query.
- **Traces:** `app/ops/pg_trace.py` `PgTraceStore` — a run's ~25-50 events are buffered, the SHA-256 chain is
  computed locally (same `hash_event`), and one **data-modifying CTE** writes event rows + the run head in a
  single round trip at `flush()`. The runner flushes in a `finally` at the end/suspend of every run (also
  when building the run fails). Reads (`get_run_tree`, `verify_integrity`) combine stored rows with the
  in-memory tail, so the evals that run right after a run cost **zero** round trips. `(run_id, seq)` is a
  unique index (migration 0003): two machines resuming one run from a stale head get an error, not a
  forked chain. Shared chain-check logic (`TraceStore._check_chains`), early flush at 200 events, 400-row
  chunks, LRU eviction of flushed runs.
- **Guard reworded, not removed:** deciding an approval whose checkpoint cannot be loaded (chat deleted)
  is still refused *before* recording; the "started on another machine" wording is gone.
- **Retention:** `python -m app.db prune --chats [--traces-older-than N] [--dry-run]`. LangGraph's
  `PostgresSaver.prune` is *not implemented* in the installed version (base raises `NotImplementedError`),
  so `prune_chats` keeps each chat's latest checkpoint + its pending writes and deletes superseded
  checkpoints/writes/unreferenced blobs itself, in one transaction. Tests cover pruning while a run is
  paused, then resuming it on another machine.

**Bugs found and fixed on the way (all would have bitten in production):**
1. *Phase 2 latent bug — `PgSession` was not thread-safe.* `PgApprovalStore`/`PgBaseStore` share one
   session across all request threads and it kept the borrowed connection on the instance, so concurrent
   requests could use, or return to the pool, each other's connection. Now per-thread state; regression test
   (12 threads, 8 connections).
2. *Async pool `reset` callback* made returned connections briefly unavailable so the pool opened extra
   connections (slow TLS to Neon). The idle stamp now happens at checkout inside `check`.
3. *LangGraph `PostgresSaver.setup()` deadlocks against itself* when several containers boot on a fresh
   database (its migrations use `CREATE INDEX CONCURRENTLY`; my first retry logic treated
   `DeadlockDetected` as a connection error). `setup()` is skipped when the schema is current (1 read) and
   the whole race family is retried with jitter. Stress-tested: 10 x (4 rounds x 5 simultaneous boots).
4. *`list_threads` used SQL `LIKE`* with `user:<name>`: `_`/`%` are wildcards, so `a_b@x.com` also matched
   `aXb@x.com`'s threads. Now an exact prefix, both back-ends.

**Measured on real Neon, pooled endpoint (PgBouncer):** LangGraph's pipelined checkpoint writes work through
the pooler; run paused on machine A, decided and resumed on machine B, trace chain verified. Warm mock-LLM
turn ~0.75 s on the host (3-4 statements), ~1.0 s inside Docker (RTT ~150 ms); first turn of a thread 2-4 s.
Storage: ~15 KB of trace per run and ~10 KB per checkpoint at this size — plan on pruning before the 512 MB cap.
Container run on Neon exercised chat, thread list/history, the ops trajectory viewer (dashboard process reading
what the gateway process flushed) and chat deletion; every row created by the tests was removed afterwards.

Known limits: a run's trace is visible to other machines only after it finishes/pauses; audit entries for
member admin actions still go to the local `audit.jsonl` (Phase 5); episodic recall was per-machine until Phase 4;
`prune` operates on LangGraph's internal tables and is verified against the installed version by the test-suite.

### Phase 4 — Episodic recall (DONE 2026-09-27)
FTS5 is SQLite-only, so recall moved to Postgres full-text search on the shared DB, **without weakening the
leakage guarantee** (Theorem 2 / invariant I2 in `research_objective/01_ztae_formal_spec.md`).

**Storage/search:** migration `0004_episodic.sql` — table `episodic(scope, run_id, ts, actor, transcript, tsv)`,
`UNIQUE (scope, run_id)` (a resumed run never duplicates), generated `tsv` column (`simple` config over the
transcript with every non-alphanumeric run replaced by a space, which reproduces FTS5's tokenisation so
`a@b.com` / URLs split the same way), GIN index on `tsv`, and `(scope, ts DESC)`. `PgEpisodicStore`
(`app/memory/pg_episodic.py`) subclasses `EpisodicStore`; `append_transcript` is one CTE (insert-or-return-existing,
1 round trip); `search` is one query ranked by `ts_rank_cd`. `open_episodic_store()` picks the back-end; runner,
gateway and dashboard use it.

**Why not Row Level Security:** measured on Neon, the app role `fyp_owner` has `BYPASSRLS`, so an RLS policy would
look like a safeguard while enforcing nothing. Not used; documented so nobody "adds it later" and believes it works.

**Leakage defence, four independent layers — the property holds if any one is correct:**
| Layer | Where | What |
|---|---|---|
| L1 | store SQL | exact, parameterised `scope = ANY(?)`; never `LIKE`, never case-folded |
| L2 | `check_in_scope` in both stores | every returned row's scope is re-checked; a stray row raises `RecallIsolationError` and **nothing** is returned |
| L3 | `RecallService.recall` | Casbin `memory_allowed(scope,"read")` re-checked per hit; an L2 trip degrades to "recall is temporarily unavailable" (logged without the query) |
| L4 | `normalise_query` | query reduced to alphanumeric words → `phraseto_tsquery('simple', …)`; no tsquery/FTS5 operator, quote or NUL can reach the index |

**Tests (`tests/test_pg_phase4.py`, 37, every one run on SQLite and Postgres where meaningful):** contract
(phrase, case, email/URL splitting, dedup, limit); 7-department world with canary tokens and a 555-query
randomized property x 5 callers (with a non-vacuity check: own canary must be found); 45 adversarial query strings
(tsquery/FTS5 operators, quotes, wildcards, NUL, unicode); 11 confusable scope names (`dept/hr` vs `dept/hr2`,
`dept/HR`, `dept/hr/x`, …); fail-closed and L3 tests with deliberately leaky/broken stores; concurrent appends;
cross-laptop visibility; one-round-trip + index-exists checks; prune; and end-to-end chats through `run_agent`
(three users, recall tool calls, PII masking, resume without duplicates).
**Mutation testing** shows the tests can see a leak: L1 removed + L2/L3 off → 1 190 leaked results; `=` weakened to a
prefix `LIKE` → `dept/hr` returned `dept/hr2`; `to_tsquery` without normalisation → operator injection crashes —
3/3 caught. Metrics harness (`research_objective/metrics`): 459 foreign-canary queries per back-end, **0 leaks**
on both, own-canary found 131/141 (misses are queries whose random second word is absent — expected).

**Bug found on the way (pre-existing, SQLite):** a NUL byte in a recall query raised `sqlite3.OperationalError:
unterminated string`. Shared `normalise_query` fixed it for both back-ends; NUL in *transcripts* is replaced by a space
(Postgres text cannot hold NUL). Transcripts are truncated at 200 000 chars.

**Measured on real Neon (container, pooled endpoint):** two chats by users of different departments wrote
`dept/hr` and `dept/marketing` rows; each canary is found by its own department scope from a different process and is
invisible to the other's scopes; test rows removed afterwards, live tables back to 5 members / 0 everything else.

**Behavioural differences from FTS5 (accepted):** no stemming, no diacritic folding (`unicode61` folds `é`→`e`,
`simple` does not), rank direction differs internally (hidden — both return best-first), old local transcripts are
not migrated (data loss accepted, Metadata #4). New retention flag: `python -m app.db prune --episodic-older-than DAYS`.

### Phase 5 — Audit chain (DONE 2026-09-27)
`audit.jsonl` + `.head` → migration `0005_audit.sql`: `audit_log(log, seq, event_id UNIQUE, ts, run_id, decision,
entry, prev_hash, hash)`, PK `(log, seq)`, index `(log, run_id, seq)`; `audit_heads(log, seq, head_hash)` is the
`.head` file's twin. `PgAuditLog` (`app/harness/pg_audit.py`) keeps the file's entries, `_hash_entry` (SHA-256 /
HMAC), genesis and issue wording; `open_audit_log(path)` picks the back-end (log name = file stem, so members-admin
entries and policy decisions share log `"audit"` as they shared the file). Gateway, dashboard, runner and
`MemberStore._audit_entry` use it — this also closes the "member admin audit still local" limit of Phases 2–4.

**Design change from the original plan (advisory lock → optimistic + row-lock fallback).** The hash is computed in
Python, so an advisory-locked append is BEGIN → lock/read head → insert → COMMIT = 4 round trips (~600 ms on Neon)
on *every* gated tool call. Built instead:
1. **Fast path, 1 statement:** `cur` = head row `FOR UPDATE` (waits for a concurrent writer, then sees its new head),
   insert only if the head is still the one we hashed against, move the head, always return the current head. Each
   process caches the head per (pool, log); a stale cache costs one extra statement.
2. **Locked path after 3 misses:** found necessary on real Neon — pure optimism starved one writer (30 lost races in
   a row, 3 laptops × 2 threads, append failed; chain stayed valid). A pipelined transaction
   `[BEGIN + SELECT … FOR UPDATE]` → hash → `[INSERT + COMMIT]` holds the head lock across the hash, so it cannot lose:
   2 round trips, lock held ~1.
3. **PK `(log, seq)` is the safety net under both** (and the only arbiter for the very first row, when no head row
   exists to lock): two writers hashed against one head claim one `seq`; the second fails instead of forking.
4. **Lost reply:** retried once; unique `event_id` turns "it had landed after all" into a recognised success.
5. **Corrupt/deleted head:** appends refuse (`TimeoutError` pointing to `verify-audit`) rather than re-genesis.

**Verification cost.** The per-run evals call `verify_integrity(full=False)`: one statement re-checking only rows
since the last successful verification in this process (anchored on that row's hash) + the head; the first call per
process is a full walk. Stated blind spot: an edit to a row *older* than the anchor is found by the next full check
(new process, any failure, or the new `python -m app.db verify-audit`), not by the next eval. Evals now read
`entries(run_id=…)` once (indexed) instead of the whole log twice — on both back-ends. Indexed copies of hashed
fields (`run_id`, `decision`, `ts`, `event_id`) are cross-checked against the hashed entry, so hiding a decision from
the per-run eval by editing only the column is detected.

**Operational rule (new):** every laptop must share `SERVICES__AUDIT_HMAC_KEY` (or all leave it blank) — a different
key reports the others' entries as tampered (`.env.example`, README). RLS/advisory locks not used; audit is
append-only, never pruned (pruning would break the chain).

**Tests** `tests/test_pg_phase5.py` (39): contract on both back-ends; independent recomputation of the stored chain;
tamper (edit, indexed-column edit, interior delete, reorder, tail truncation, head deletion, wipe, HMAC-less
re-chain, wrong key); incremental (1 statement from the anchor, new-row tamper, anchor edit/delete, blind spot +
full-check recovery, new process = full); concurrency (8 threads, 4 pools × 2 threads, 3 OS processes, stale head
across "laptops", a writer that loses every optimistic race, 4 pools × 3 threads at +25 ms simulated RTT); lost
reply, double failure, corrupt head; statement counts (append = 1, stale = 2, evals read one run); factory; members
audit; a real DENY + ALLOW `run_agent` on Postgres with all audit evals passing and tampering flagged. Mutation: the
head check removed → 7 tests fail. Full suite with `TEST_DATABASE_URL`: **557 passed**.

**Measured on real Neon** (throwaway schema, dropped; *direct* host because PgBouncer refuses a `search_path`
startup option — every audit statement is single-statement autocommit or an explicit pipelined transaction, both
valid through the pooler): RTT ~135-145 ms that day; append warm p50 **143 ms** (1 round trip), stale head 236 ms,
`entries(run_id)` 142 ms, incremental verify ~150-200 ms. Stress 4 laptops × 3 threads × 15 non-stop: 0 errors, chain
valid; before pipelining 41 s (p50 2.4 s), after **26 s ≈ 6.8 appends/s team-wide ≈ 1 per round trip** — the floor
for any client-side hash chain over a WAN. Real use (a gated tool call every few seconds per active user) is far below it.

### Phase 6 — Policy + skills (DONE 2026-09-27, local Postgres; real Neon not yet exercised)
`policy.csv` rows and `skills/*.md` moved into tables (migration `0006_policy_skills.sql`) so a rule/skill added on
one laptop is live on all of them; without `DATABASE_URL` everything still reads the files.

**Built:**
- `app/harness/policy_store.py` — `FilePolicyStore` (the CSV, behaviour unchanged, same append format) and
  `open_policy_store()`; `RulesAdapter`, a read-only Casbin adapter that appends rows exactly like Casbin's own
  `load_policy_line`; `parse_policy_file` uses Casbin's own CSV loader, so both back-ends parse identically.
- `app/harness/pg_policy.py` `PgPolicyStore` — table `policy_rules(ptype, v0..v3, source, created_at, created_by)`,
  PK = the rule itself (a rule exists once however many laptops add it); `add_rows`/`remove_rows` are one statement
  each (data-modifying CTE: write + bump counter); rows are validated (ptype, arity, non-empty) before anything is sent.
- `app/skills/pg_registry.py` `PgSkillRegistry` — table `skills(name, scope, document, source, updated_*)`, the whole
  `skill.md` document parsed by the same `parse_skill_document`; per-process registry (`shared_registry`) with a
  version-guarded cache; `SkillRegistry.open()` picks the back-end (runner, dashboard, gateway warm-up).
- `PolicyEnforcer(policy_path=… | policy_store=…)`; rebuild key is now `(members_version, policy version)`. On the file
  back-end the policy version is the file's mtime+size — **this also fixes a pre-existing gap**: rows the dashboard
  appended for a new toolkit were not enforced until some unrelated member change forced a rebuild.
- Dashboard: department registration, toolkit grants, `/api/departments`, and the skill editor go through the stores
  (skill list reads the policy once per request instead of once per skill); saves record the admin as `created_by`/`updated_by`.
- `python -m app.db sync-config [--dry-run] [--prune] [--overwrite-skills]` + `make db-sync-config`.

**Design decisions:**
1. **One change counter.** Policy/skill writes bump `meta.members_version` in the same statement. Every process
   already polls it (≤1/s, `PgMemberStore`) and the enforcer already rebuilds on it, so propagation costs **zero extra
   round trips per request** (`app/db/version.py` reuses the member store's poll; own writes are visible at once).
   Steady state: enforcement reads no rules; a run's skill list costs no round trip (tested by counting).
2. **Seed once, then the database is the truth.** An empty database is seeded from the checkout's files
   (`meta.policy_seeded` / `skills_seeded` claimed in the seeding transaction → six simultaneous first boots seed once,
   tested). Later git edits are **not** auto-applied at start-up: a laptop on a stale checkout would otherwise silently
   re-grant a rule the team removed (deny-by-default matters more than convenience). `sync-config` applies them on
   purpose; `--prune` removes only `source='file'` rules, never ones added from the dashboard; skills edited in the
   dashboard are replaced only with `--overwrite-skills`. A start-up **warning** is logged when the checkout and the
   database differ.
3. Seeding/drift/`sync-config` compare rules as Casbin parses them, not as text, so whitespace/comment differences are not drift.

**Tests** `tests/test_pg_phase6.py` (34): enforcer on the DB answers exactly like the file on 6 156 (member × domain ×
obj × act) questions incl. unknown ones; stored skills produce identical LLM schemas; seed-once and 6-way simultaneous
first boot; laptop-A-writes / laptop-B-enforces for rules (add + remove) and skills (create, edit, delete) with no
restart; own writes visible under a 1-hour poll interval; store contract on both back-ends; malformed rows refused
before sending; commas/quotes round-trip; `g` rows; malformed stored skill reported, not run; CHECK constraints;
drift/add/prune semantics; the real `sync-config` CLI in a subprocess; zero-round-trip steady state; counter bumped
once per real change and not on no-ops; dashboard department/toolkit/skill-editor endpoints writing to the DB; and a
`run_agent` run executing a skill (and rule) that exist **only** in the database, invisible to HR.
Full suite with `TEST_DATABASE_URL`: **591 passed** (557 before Phase 6).

**Finding (pre-existing, not Phase 6):** a *denied* Casbin check evaluates every rule — ~3 ms on this laptop for 111
rules (profiling: `simpleeval`), plus ~0.5 ms for the SQLite member-version read on the file back-end. Same on both
back-ends; worth knowing if a turn makes hundreds of checks.

Known limits: `sync-config` is manual by design; policy/skill changes are audited only where the dashboard already
audited them (skills, department registration — toolkit grants are not); not yet exercised on real Neon.

## Phases
| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Docker: `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `scripts/run_docker.sh`, README | DONE + verified 2026-09-23 (see "Phase 0 verification") |
| 1 | `app/db/` pool + migration runner + `database_url` setting + `TEST_DATABASE_URL` | DONE 2026-09-23 — verified on local Docker Postgres; exercised on real Neon from Phase 2 on |
| 2 | Members + approvals + memory facts on Postgres | DONE 2026-09-26 — verified on local Postgres and real Neon |
| 3 | Chat checkpoints (`PostgresSaver`) + batched trace writes | DONE 2026-09-27 — verified on local Postgres and real Neon |
| 4 | Episodic recall: FTS5 → Postgres full-text search, leak-proof by 4 layers | DONE 2026-09-27 — verified on local Postgres and real Neon |
| 5 | Audit chain in a table; optimistic append + row-lock fallback (supersedes the file lock from G4) | DONE 2026-09-27 — verified on local Postgres and real Neon |
| 6 | Policy rows + skills in the database; `sync-config` | DONE 2026-09-27 — verified on local Postgres; real Neon pending |

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
- **One audit chain for the whole team** (Phase 5) serializes every gated tool call's audit append
  across all laptops — same tradeoff as the file lock (G4), wider scope. Measured: 1 round trip
  (~140 ms) uncontended; saturated ceiling ≈ 1 append per round trip team-wide (~7/s on Neon).
  If that ever binds, the fix is per-department chains (`log` column already exists), not a lock change.

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

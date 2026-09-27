# Session 025 — Mk13 Phase 3: chat checkpoints and run traces on the shared Postgres (Neon)

## Metadata
- Date: 2026-09-27
- Plan / Phase: mk13 — Phase 3 (`plan_mk13_docker_cloud_db.md`)
- Plan step: LangGraph `PostgresSaver` for chat state + batched, hash-chained traces
- Scope: make a run startable on one machine and approvable/resumable on another; keep chat turns fast over a ~150 ms link
- Status: complete — verified on local Postgres and on real Neon through the POOLED (PgBouncer) endpoint
- State at start: Phases 0-2 done; Phase 2 had introduced an "approval from another machine can't be decided" guard because checkpoints were per-machine

## Objective
Chat threads/history, paused runs (HITL) and run traces live in the shared database; removes the cross-machine approval limitation.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| explore | risk | `TraceStore.add_event` = 4 statements and a run emits 25-50 events -> ~25 s/run on Neon if ported naively. LangGraph also checkpoints after EVERY step. Both are latency, not correctness, problems |
| design | traces | Buffer per run, compute the chain locally, ONE data-modifying CTE per flush (events + head atomically, 1 round trip); reads = stored rows + in-memory tail so evals cost 0 trips; `(run_id, seq)` unique index turns a stale-head resume into an error instead of a fork |
| design | ckpt | `graph.invoke(..., durability="exit")` on Postgres only: one checkpoint write at finish OR at an approval pause (HITL unaffected); SQLite path untouched |
| implement | runner | Wrapped run/resume bodies in `try/finally` that flushes the trace — including when `_build_deps` fails (previously left a dangling run_start) |
| **bug** | phase 2 | While writing the pg trace store I realised `PgSession` stored its borrowed connection on the instance while stores are shared by all request threads -> cross-thread connection use. My Phase 2 tests never shared one instance across threads. Fixed (thread-local state) + 12-thread regression test |
| **bug** | pool | Pool stats looked like a leak; actually psycopg_pool runs `reset` asynchronously so a returned connection is briefly unavailable and the pool opens extra ones (slow TLS). Moved the idle stamp to checkout (`check`) |
| **bug** | setup | Flaky test: 5 simultaneous `PostgresSaver.setup()` DEADLOCK inside LangGraph (CREATE INDEX CONCURRENTLY). My retry treated `DeadlockDetected` as a connection error; first fix used the wrong exception class (`DeadlockDetected` is NOT a `TransactionRollback` subclass — checked with issubclass). Now: skip when current (1 read) + retry the family with jitter; 10 x 4 rounds stress = 0 failures |
| **bug** | threads | `list_threads` used `LIKE 'user:<name>:%'`; `_` is a wildcard, so `a_b@x.com` listed `aXb@x.com`'s threads. Exact prefix now (both back-ends) |
| explore | retention | Neon free = 512 MB and checkpoints grow per turn. `PostgresSaver.prune` exists in `dir()` but is NOT implemented (base raises `NotImplementedError`) -> wrote `prune_chats` SQL myself (keep latest + its pending writes; drop unreferenced blobs), explicit CLI only, tested incl. "prune while paused, then resume on the other machine" |
| measure | neon | Pooled endpoint: pipelined checkpoint writes fine; A pause -> B decide+resume works; warm turn ~0.75 s host / ~1.0 s Docker with 3-4 statements; DB 8 MB after tests |
| verify | data | All test rows removed from the live DB afterwards (only the 5 members remain) |

## What was done
- `app/agent/checkpoints.py` (new): `make_checkpointer`, `postgres_saver` (cached per pool), race-safe `_setup`/`_schema_current`, `invoke_options`, `list_user_threads`, `prune_chats`
- `app/ops/pg_trace.py` (new) `PgTraceStore`; `app/ops/factory.py`; `app/ops/store.py` (`flush` no-op, `_check_chains` extracted); migration `0003_trace_tables.sql`
- `app/agent/runner.py` (factories, `durability`, trace flush in `finally`), `gateway/app.py`, `dashboard/app.py` (factories, thread listing, guard wording), `dashboard/static/index.html` (note text)
- `app/db/compat.py` (thread-local `PgSession`), `app/db/pool.py` (idle stamp at checkout), `app/db/__main__.py` (`prune` command), `Makefile` (`db-prune`)
- Tests: `tests/test_pg_phase3.py` (trace contract on BOTH back-ends, batching/fork/chunk/evict/retry/threads, setup race, thread listing, A->B resume, exit durability, prune), thread-safety test in `tests/test_pg_stores.py`, `tests/test_shared_approvals.py` reworded
- README, `.env.example`, plan Mk13 + INDEX

## Decisions locked in this session
- Traces are visible to other machines only after a run finishes or pauses (batched); an in-process crash loses that run's unflushed events. Chosen deliberately over per-event writes.
- `durability="exit"` on Postgres; a crash mid-run loses that run's progress (the user re-sends). Paused runs are always persisted.
- Pruning is explicit (`python -m app.db prune`), never automatic; deletes nothing by default.
- The resume guard stays (reworded): a decided approval can't be re-decided, so never record a decision for a run that cannot resume.

## Verification performed
- `pytest`: full suite with `TEST_BACKEND=postgres` + `TEST_DATABASE_URL` = 481 passed (includes contract tests on both back-ends); default offline run (real `.env` present): 414 passed, 67 Postgres-gated skipped; setup-race test 10/10 x 4 rounds.
- Real Neon (pooled): PostgresSaver setup + migrations, chat turns, dept-memory approval raised on machine A, REJECTED and resumed on machine B, trace integrity ok, cleanup verified.
- Container on Neon (separate compose project, port 18006): chat x3, thread list, history, `/api/ops/runs` seeing runs flushed by the gateway process, chat delete; all rows cleaned up.
- NOT verified: two real physical machines; behaviour across a Neon 5-minute autosuspend for the saver (the pool idle-ping is unit-tested); the user's own running container (still an older image until rebuilt).

## Blocker / open questions
- Still pending from earlier: policy row for role `admin_agent` (policy.csv is local until Phase 6).

## Next step (for the next session)
- Mk13 Phase 4: episodic recall (FTS5 -> Postgres full-text search) — keep the leakage property (Theorem 2, `research_objective/01_ztae_formal_spec.md`) at 0.

## Handoff notes
- To switch the user's app: `docker compose up -d --build` (their `.env` has `DATABASE_URL`). Tables for Phase 3 (`trace_*`, LangGraph `checkpoint_*`) were already created on their live Neon DB by my pooled smoke test (additive, empty).
- `PostgresSaver` internals: `prune_chats` touches `checkpoints`, `checkpoint_writes`, `checkpoint_blobs`; if LangGraph is upgraded, run `tests/test_pg_phase3.py` (it exercises prune against the installed version).
- Full suite in Postgres mode takes ~9 minutes (a private schema + pool per test); default offline run is ~3-5 minutes.

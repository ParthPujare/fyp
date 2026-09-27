# Session 027 — Mk13 Phase 5: the audit chain on the shared Postgres (Neon)

## Metadata
- Date: 2026-09-27
- Plan / Phase: mk13 — Phase 5 (`plan_mk13_docker_cloud_db.md`)
- Plan step: move `audit.jsonl` (+ `.head`) into the shared database, one hash chain for every laptop
- Status: complete — verified on local Postgres and real Neon (throwaway schema, dropped afterwards)
- State at start: Phases 0-4 done; member-admin and policy audit entries were still per-machine files

## Objective
Every policy decision (ALLOW / DENY / APPROVED / REJECTED) from every laptop lands in one tamper-evident chain, with the same entries, hash and verification wording as the JSONL file.

## What was thought, when
| Phase | What I thought / decided / realized |
|-------|-------------------------------------|
| design | The plan said "advisory lock". The hash is computed in Python, so a locked append is BEGIN + lock/read head + insert + COMMIT = 4 round trips (~600 ms on Neon) per gated tool call. Chose a 1-statement optimistic append instead: insert only if the head is still the one we hashed against, move the head in the same statement, return the current head |
| design | Primary key `(log, seq)` is the safety net: two writers hashing against one head claim one `seq`; the second fails instead of forking. Unique `event_id` makes a retry after a lost reply idempotent |
| design | Indexed copies of hashed fields (`run_id`, `decision`, `ts`, `event_id`) are cross-checked at verification, so editing only the column to hide a decision from the per-run eval is detected |
| measure | First Neon stress run (3 laptops x 2 threads, non-stop): one writer lost 30 optimistic races in a row and the append failed (chain stayed valid). Pure optimism can starve |
| fix | After 3 misses a writer takes a locked path: `SELECT ... FOR UPDATE` on the head row held across the hash. Pipelined as [BEGIN + lock/read] then [insert + COMMIT] = 2 round trips. Also added `FOR UPDATE` on the fast path's head read so it waits for a concurrent writer |
| measure | 4 laptops x 3 threads x 15 appends on Neon: 0 errors, chain valid; 41 s before pipelining, 26 s after (about 1 append per round trip team-wide) |
| test | A test for the starved-writer case and one that reproduces the failure locally by adding ~25 ms per statement |
| mutation | Removing the head check from the append made 7 tests fail |
| explore | Neon's pooler (PgBouncer) refuses a `search_path` startup option, so the throwaway-schema smoke test used the direct host; the audit statements themselves are pooler-safe |

## What was done
- `app/db/migrations/0005_audit.sql`: `audit_log`, `audit_heads`
- `app/harness/pg_audit.py` (new): `PgAuditLog` — fast path, locked fallback, incremental `verify_integrity(full=False)`
- `app/harness/audit.py`: `resolve_key`, `open_audit_log`, `entries(run_id=...)`, `verify_integrity(full=...)`
- `app/evals/deterministic.py`: one run-filtered read shared by two checks, incremental integrity check
- Gateway, dashboard, runner and `MemberStore._audit_entry` open the log through the factory
- `python -m app.db verify-audit` (full-chain check, exit 1 on tampering)
- `tests/test_pg_phase5.py` (39 tests); README, `.env.example`, plan Mk13 and its index updated

## Decisions locked in
- Every laptop must share `SERVICES__AUDIT_HMAC_KEY` (or all leave it blank); a different key reports the chain as tampered.
- The audit log is append-only and is never pruned.
- Per-run evals verify incrementally; an edit to a row older than the anchor is found by the next full check (new process or `verify-audit`).
- If the append ceiling (~7/s team-wide) ever binds, split into per-department chains (the `log` column already exists).

## Verification
- Full suite with `TEST_DATABASE_URL`: 557 passed. Lint clean.
- Real Neon (throwaway schema, dropped): append warm p50 143 ms, stale head 236 ms, `entries(run_id)` 142 ms, stress test above.
- NOT verified: two physical machines; a Neon autosuspend in the middle of an append burst.
- Nothing was written to the live t
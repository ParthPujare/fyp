# Plan Mk14 — Owner-side HITL resume (approve anywhere, resume on requester)

## Metadata
- Date: 2026-09-27
- Status: Phase 1 DONE 2026-09-27 (session 028) — verified offline (SQLite, 455 green);
  live-Neon apply + multi-PC run open (Phase 2)
- Follows: Mk1 (§5.9 HITL), Mk12 (dept-head approvals), Mk13 (shared Neon DB, Docker)
- Scope lock (user, 2026-09-27):
  1. Goal = **code only** (no demo runbook in this plan).
  2. Scale = **unbounded members by design**, tested on **max 5 PCs / 5 concurrent users**; load never exceeds this in testing.
  3. Sharing = **`DATABASE_URL` only**, shared privately. `JWT__SECRET`, `LLM__FERNET_KEY`, `COMPOSIO__*` stay per-PC. No secret sharing.
  4. Resume UX = **auto-poll + manual Resume button** fallback.
  5. Approval types = **both admin and dept-head** (+ gateway admin endpoint).
  6. Providers = **user's choice** (Claude/OpenAI/anything); **LM Studio supported on priority** for testing.
  7. Composio = **BYO key each login** (each member's own Gmail connection).
  8. Stuck approvals = **wait for owner**; no admin force-resume on another machine.
  9. Runtime = **`make run` native** for demo (`scripts/run_dev.sh`, 7x uvicorn `8000-8006` over `127.0.0.1`); **Docker keeps working** but is not used for the demo.

## Problem
Today `decide` and `resume` are one atomic server action on the approver:
`dashboard/app.py:471-497` (admin), `:500-533` (dept), `gateway/app.py:267-307` do
`approvals.decide()` then immediately `resumer(approval_id)` in the same process.
Consequences for an org where PC-A (member) and PC-B (admin) are different people
with different logins, Composio Gmails, LM Studio models, and private `.env` secrets:

1. Approving on PC-B executes on **PC-B hardware/env**: PC-B's `host.docker.internal` /
   `localhost` model (different model), PC-B's `LLM__FERNET_KEY` trying to decrypt PC-A's
   keys → silent MockLLM fallback (`app/llm/factory.py:73-79`, reply `mock reply to:`
   from `app/llm/mock.py:54`), or Composio `Fake{ok:true,id:sent-001}`
   (`app/integrations/client.py:332-336`) that never sends mail.
2. The owner label in the chat model selector is metadata only
   (`static/index.html:1012-1028` from `to_public()`, `auth/store.py:629-651` never
   decrypts), so `lmstudio / model (Active)` does not prove the resumer can use it.
3. `approved — chat updated` (`static/index.html:961-984` poller) proves history grew,
   not that `gmail_send` executed.
4. Resume itself has no cross-PC mutual exclusion: `decide` is atomic
   (`approvals.py:132-153` `UPDATE ... WHERE status='pending'`), but N×
   `resume_agent(same id)` each run `graph.invoke(Command(resume=...))`
   (`runner.py:371-375`) with no resumed-flag — double side effects possible.

## Design

### Invariants
- Resume identity = `entry.actor` (originator), never decider: `runner.py:350-351`
  `_build_deps(entry[actor])`, `actor={sub,role,dept}` `:144`,
  `provider=get_provider_for_user(...claims[sub]...key_id)` `:133`,
  Composio `vault.get_composio_key(user)` per-actor. Unchanged.
- Only pooled `DATABASE_URL` shared (`db/pool.py:63-69`, `MAX_SIZE=4` covers 5 PCs).
  JWT/FERNET/COMPOSIO stay local, so each PC decrypts only its own keys.
- `localhost:1234` stays correct natively per-machine; Docker override stays per-key
  `base_url` (`dashboard/app.py:1144-1158`, `lmstudio_provider.py:24,58-62`,
  `compose extra_hosts host-gateway`). No default rewrite in this plan.

### API (all three decide routes + three new resume routes)
- `POST /api/approvals/{id}/decide?defer=1`, `POST /api/dept/approvals/{id}/decide?defer=1`,
  `POST /approvals/{id}/decide` (+`defer` in `ApprovalDecisionRequest`):
  keep `404`/`409 already {status}` (`dashboard:480-484,517-518`, `gateway:280-284`),
  keep atomic `approvals.decide()` (`_decide_or_http_error`, `dashboard:188-195`);
  when `defer=1` skip `_resumable_here`/`run_state_is_local` 409 and skip `resumer()`;
  return `{status, thread_id, needs_resume:true}`. Default `defer=0` = today's coupled path.
- `POST /api/approvals/{id}/resume`, `POST /api/dept/approvals/{id}/resume`,
  `POST /approvals/{id}/resume` (member auth via existing `_verified_member` /
  gateway `_verified_active`; **not** admin-only):
  `404` unknown (`runner:336-338`), `409` still pending (`:339-340`),
  `403` caller `sub != entry.actor` (owner-only; dept route additionally requires
  `ApprovalStore.dept_of(entry)==claims.dept` for dept items),
  `409 RESUME_ELSEWHERE` when `!run_state_is_local` (`runner:83-94`,
  `dashboard:157-168`), else `resume_agent()` and today's result envelope.
- `GET /api/approvals/by-thread/{thread_id}` (member auth, owner or admin):
  decided-or-pending rows for one thread so the owner poller can find work without
  scanning full pending lists (`pending_items()` `:106-111` full-scan pattern kept).

### DB (additive migration `0007_approval_resume.sql`)
- `ALTER TABLE approvals ADD COLUMN resumed_by TEXT, ADD COLUMN resumed_at TEXT;`
  (+ index on `(thread_id, status)` if planner agrees; keep additive per `migrate.py:17-19`,
  no `BEGIN/COMMIT`, runner applies once under `pg_advisory_xact_lock`).
- SQLite `_init_schema` (`approvals.py:31-48`) gains same two columns (`CREATE TABLE IF`
  + `PRAGMA table_info` alter-if-missing for existing dev files).
- New `ApprovalStore.claim_resume(id, owner)`: single
  `UPDATE approvals SET resumed_by=?, resumed_at=? WHERE id=? AND status IN
  ('approved','rejected') AND resumed_by IS NULL`; `rowcount==0` → re-read →
  `KeyError` / `ValueError(already resumed|not decided)`. `PgApprovalStore`
  (`pg_stores.py:21-26`, no read cache by design) inherits it; single-statement
  autocommit keeps atomicity (`db/compat.py`).
- `resume_agent()` claims **before** `graph.invoke`; double resume → `409`.

### Frontend (`static/index.html`)
- `decide()` (`:1154-1166`): send `?defer=1` always in org mode; approver sees
  `approved — waiting on requester` instead of `reply`; keep 1200ms reload (`:1164`).
- `watchSuspended` (`:964-984`, 4s tick `:981,983`, 15min deadline `:966`,
  history `:971,988`): add by-thread approval poll; when decided + `actor==me`,
  `POST .../resume`, then `loadThreadHistory` (`:986-995`). Keep transient-error
  tolerance (`:980`).
- Suspended banner (`renderChatTurn` `:1089-1098`): add `Resume now` manual button
  (owner only) posting same endpoint; approvals cards (`approvalCard` `:1123-1152`)
  show `waiting on owner` for decided-unresumed rows (mirror `resumable_here===false`
  branch `:1140-1145`).

### Provider-agnostic + Composio
- No factory change: `_saved_key_id` (`runner:97-104`) + `key_id>active>global>Mock`
  (`factory.py:60-90`) already per-actor; covers `lmstudio` keyless
  (`DUMMY_API_KEY`, `:1234/:11434/:8080`), `custom_openai` 422/502 path, Claude/OpenAI.
- Composio stays originator-keyed; assert no decider-key fallback in resume path
  (`vault.py:14-28`, `service.py:62-85`).

### Concurrency (5 PCs)
- Decide race safe (single `UPDATE ... WHERE pending` winner, losers `409`).
- Resume race closed by `claim_resume` above. `create()` check-then-insert
  (`approvals.py:66-85`, no unique index) can duplicate under concurrent resume —
  accepted for this plan; second resume is blocked at claim time before executing.

### Tests
- Reuse `test_hitl_resume.py:41-74 Env`, `test_shared_approvals.py:46-62 shared_mode`,
  `conftest.py` fixtures. New: defer skips resumer; owner resume 200 + single
  `gmail_send`; non-owner 403; pending resume 409; 5-way concurrent claim → 1 winner;
  dept parity; `make test-pg` pg run. Docker/LM Studio detection tests unchanged
  (`test_llm_local.py:142-159`).

## Rollout (`make run`, free tier)
Per PC `.env`: same `DATABASE_URL` (private), unique `JWT__SECRET`/`LLM__FERNET_KEY`,
own `localhost:1234` model + BYO LLM key + BYO Composio key + Gmail Connect.
A sends → suspended; B approves (defer) → waiting; A auto (≤4s) or manual Resume →
real send (Ops `ok:true`, real id, inbox check). Neon 512MB: prune via `make db-prune`.

## Risks
Mid-run crash loses progress (`durability=exit`, `checkpoints.py:7-11`); trace tail
invisible until flush (`pg_trace.py:18-20`); owner offline = stuck until return
(by decision, no admin fallback); different models → different phrasing (expected).

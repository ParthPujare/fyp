# Session 028 — Mk14 owner-side HITL resume (approve anywhere, resume on requester)

## Metadata
- Date: 2026-09-27
- Plan / Phase: mk14 — Phase 1 (`plan_mk14_owner_resume.md`)
- Plan step: split `decide`/`resume` so the originator's machine executes with its own model/keys
- Scope: code only — deferred decide, owner-only resume, single-resume claim gate, auto+manual poller, tests
- Status: complete — verified offline (SQLite); real-Neon + multi-PC run still to do
- State at start: Mk13 Phases 0–5 done; `decide`+`resume` coupled on the approver
  (`dashboard/app.py:471-497,500-533`, `gateway/app.py:267-307`); LM Studio
  `localhost` trap already root-caused (container `localhost` ≠ host); screenshot showed
  `mock reply to:` (MockLLM fallback) + `approved — chat updated` with no mail sent

## Objective
PC-A (member) sends a mail request with its own LM Studio model + own Composio Gmail;
PC-B (admin) approves; the run resumes **on PC-A** with PC-A's model/keys. Only
`DATABASE_URL` shared; `JWT__SECRET`/`LLM__FERNET_KEY`/`COMPOSIO__*` stay per-PC.
`make run` native for demo; Docker keeps working but unused.

## What was thought, when
| Phase | What I thought / decided / realized |
|-------|-------------------------------------|
| explore | The selector label (`lmstudio / model (Active)`) is metadata only (`to_public()` never decrypts) — explains `Active` + `mock reply` coexisting |
| explore | Dashboard decide is not proxied (local `resumer()`), only `/api/chat` proxies — so the approver's machine always executed |
| explore | `decide` is atomic (`UPDATE ... WHERE status='pending'`) but `resume_agent` had no gate — N× resume = N× side effects |
| design | Claim must live in `resume_agent` (single choke point for dashboard/gateway/dept/direct callers), endpoints map `ValueError`→409 |
| design | Endpoint pre-claim + runner claim double-claims one request; removed endpoint claim, runner owns the gate |
| design | Test fakes bypass the gate (no `resume_agent`), so fakes must `claim_resume` themselves to mirror prod 409s |
| fix | `resumeNow` must try both `/api/approvals/` and `/api/dept/approvals/` bases; dept resume additionally checks own dept |
| verify | Full suite green; migration chain `0001–0007` contiguous; ruff clean after two test-nit fixes |

## What was done
- `ref_docs/plans/plan_mk14_owner_resume.md` (new) + `ref_docs/plans/INDEX.md` Mk14 row
- `app/db/migrations/0007_approval_resume.sql` (new): `resumed_by/at` + thread index
- `app/memory/approvals.py`: schema auto-alter, `for_thread()`, atomic `claim_resume()`, null-safe `_row_to_dict`
- `app/agent/runner.py`: `resume_agent` claims single resume before `graph.invoke`
- `app/dashboard/app.py`: `decide?defer=1` (admin+dept, no resume, `needs_resume`), owner-only `POST .../resume` ×2 (403/409 mapped), `GET .../by-thread/{thread}`, extended `_approval_view`
- `app/gateway/app.py`: `defer` on decide + owner-only `POST .../resume`
- `app/dashboard/static/index.html`: `decide()` sends `?defer=1` with waiting text, `watchSuspended` polls by-thread approvals and auto-resumes own decided items, `Resume now` manual button
- `tests/test_owner_resume.py` (new, 10 tests): store claim, defer, owner once + 409 double, 403, pending 409, by-thread poll, coupled default, dept + gateway parity

## Decisions locked in
- Resume identity = `entry.actor` (originator), never decider; no admin force-resume (wait-for-owner).
- Only `DATABASE_URL` shared; per-PC secrets diverge by design (fixes the `mock reply`/`sent-001` class of bugs).
- `localhost:1234` stays correct natively per-machine; Docker override stays per-key `base_url` (no default rewrite).
- Default `defer=0` keeps single-PC behavior unchanged; org mode sends `?defer=1`.

## Verification
- `tests/test_owner_resume.py`: 10 passed; with HITL/shared suites: 30 passed.
- Full suite: **455 passed, 146 skipped** (pg skips, no `TEST_DATABASE_URL`), ruff clean.
- Migration `discover()`: `0001–0007` contiguous.
- NOT verified: real Neon apply of 0007, two physical machines, 5-PC concurrency.

## Blocker / open questions
- 0007 not yet applied to live Neon (applies on next `make run`/`make db-migrate` from any updated PC).
- `create()` check-then-insert can duplicate under concurrent resume — accepted (claim blocks the second execution anyway).

## Next step (for the next session)
- Mk14 Phase 2: live verification — pull on PC-B, `make db-migrate` once, `make db-status` both, 2-PC mail flow + 5-PC claim race on real Neon.

## Handoff notes
- Working tree **uncommitted** on `main` (8 changed/new files, see `git status`); `SYNC` runbook is `ref_docs/mk14_push_pull_runbook.md`.
- Mixed-version window (one PC old code) works but skips the claim gate — update all PCs before the demo.

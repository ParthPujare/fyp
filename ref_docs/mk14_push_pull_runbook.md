# Mk14 sync runbook — push from this PC, pull on the others

Scope: Mk14 Phase 1 (owner-side resume). Branch: `main`, remote `origin`
(`https://github.com/ParthPujare/fyp.git`).

Rule of thumb: **git moves code, restart migrates the DB.** Committing never touches
Neon; pulling never migrates until a service starts or `make db-migrate` runs.

## A. This PC (the one with the work) — push

Files going out (verify with `git status --short`):

```text
M  app/agent/runner.py
M  app/dashboard/app.py
M  app/dashboard/static/index.html
M  app/gateway/app.py
M  app/memory/approvals.py
M  ref_docs/plans/INDEX.md
?? app/db/migrations/0007_approval_resume.sql
?? ref_docs/plans/plan_mk14_owner_resume.md
?? tests/test_owner_resume.py
?? agent_history/session_028_mk14_owner_resume.md (+ INDEX row)
?? this runbook (ref_docs/mk14_push_pull_runbook.md)
```

Steps:

```bash
git status --short          # expect the list above; .env must NOT appear (untracked secret, stays local)
git add app/agent/runner.py app/dashboard/app.py app/dashboard/static/index.html \
  app/gateway/app.py app/memory/approvals.py ref_docs/plans/INDEX.md \
  app/db/migrations/0007_approval_resume.sql ref_docs/plans/plan_mk14_owner_resume.md \
  tests/test_owner_resume.py agent_history/session_028_mk14_owner_resume.md \
  agent_history/INDEX.md ref_docs/mk14_push_pull_runbook.md
git status --short          # staged = same list, nothing extra (no .env, no data/)
git commit -m "Mk14 Phase 1: owner-side HITL resume (approve anywhere, resume on requester)"
git pull --rebase origin main   # if it says up to date, skip; resolve conflicts if any, never force-push
git push origin main
```

Then update Neon **once** (from this PC is fine):

```bash
make db-migrate   # applies 0007 once; second PC will skip it
make db-status    # expect: 0007_approval_resume applied
```

## B. Every other PC — pull + verify (each member does this)

```bash
git status --short          # must be clean; keep your own .env (never commit it)
git pull origin main
ls app/db/migrations/ | grep 0007          # file present
git log --oneline -2                        # top commit = Mk14 Phase 1
make db-status   # 0007 applied (the first PC already migrated; this is a check, not a second apply)
```

Per-PC `.env` (private, never shared except one value):

```text
DATABASE_URL      = same pooled Neon string on all PCs (private channel, ...-pooler...?sslmode=require)
JWT__SECRET       = unique per PC
LLM__FERNET_KEY   = unique per PC (generate: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())")
COMPOSIO__*       = each member's own
```

Then each PC re-saves its **own** keys in the dashboard (keys decrypt only where they
were saved — that is the point of DB-URL-only sharing):

1. Account → LLM key: own provider/model, Base URL `http://localhost:1234/v1` (native).
2. Account → Integrations: own Composio key + Connect Gmail.
3. `make run` → `http://localhost:8006`.

## C. Prove it works (2 PCs minimum)

1. PC-A (member) Chat: `send a mail to <addr> subject:test body:demo` → suspended + approval id.
2. PC-B (admin) Approvals → Approve → `approved — waiting on requester` (no reply yet).
3. PC-A (≤4 s auto, else Chat → `Resume now`) → `resumed — chat updated`, real model
   reply (not `mock reply to:`), mail in inbox.
4. Ops tab: `gmail_send ok` with a real id (not `sent-001`); by-thread poll shows
   `resumed_by = PC-A user`.

## D. If it fails

| Symptom | Cause → fix |
|---|---|
| `db-status` shows `0007 pending` | migrate never ran → `make db-migrate`, read the error (bad password/host fails fast; cold Neon wakes on retry) |
| `mock reply to:` | that PC's `LLM__FERNET_KEY` can't decrypt its key → re-save the LLM key on that PC |
| `sent-001`, no mail | owner's Composio key/connection missing → reconnect Gmail on the **owner** PC |
| `403 only the requester` on resume | correct — resuming from the approver is refused by design; resume on the owner's PC/login |
| `409 already resumed` on second click | correct — single-resume gate held |
| `409 saved state was not found` | thread deleted or PCs on different `DATABASE_URL`s — compare (never print) via `make db-check` |
| Mixed old/new code across PCs | update all PCs — old code skips the claim gate |

Do not run `down -v`, `prune` without `--dry-run` first, or share `.env` files in chat.

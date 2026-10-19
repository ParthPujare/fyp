# Session 036 — Mk20 fixes from the 2026-10-18 read-only review

## Metadata
- Date: 2026-10-19
- Plan / Phase: mk20 — single phase (`plan_mk20_review_fixes.md`)
- Plan step: fix review findings 1–10 + three bugs found while fixing them
- Scope: code + tests + docs; no migration (second-eyes table already existed)
- Status: complete offline — Postgres half of the new tests not run (Docker daemon was down)
- State at start: `main` @ `36c43d4` clean (Mk19 complete, session 035); read-only review of
  2026-10-18 had confirmed most earlier test-run findings fixed by Mk17–Mk19 and reported 10 new/remaining ones

## Objective
User asked to fix every problem in the review.

## What was thought, when
| Phase | What I thought / decided / realized |
|-------|-------------------------------------|
| explore | `_create_allow_rules` deferred whenever `count_active_admins() >= 2 and len(deciders) <= 1` — a lone HEAD is one decider, so on the live DB (4+ admins) every head "Always allow" vanished; the docstring promised the rule "waits for a second pair of eyes" but nothing was staged; tests never had two admins |
| design | Deferral only when every step is an admin step by one admin; stage via the existing `second_eyes` table (new kind `allowlist_rule`) instead of dropping — no migration needed |
| design | Second-admin rule: unknown admin count must fail closed; extend to critical tool items with no department (an admin's own call) — dept-attributed critical tools already have head + admin |
| design | Cache `credentials_version` in `PgMemberStore` behind the version guard; found `set_totp_secret` bumped `credentials_version` but not `members_version`, so the cache would have gone stale — fixed |
| design | SSRF: restrict private/loopback targets to local-model ports in BOTH modes (setting with admin hatch); Mk19's "any private port" assertion in test_ssrf_guard intentionally changed |
| fix | No UI existed for second-eyes proposals at all → added "Waiting for a second admin" to the admin Approvals tab |
| fix | While wiring CSRF to `cred_ver`: the cached CSRF token was never reset on second login / pill switch → next write 403 → `api()` signed the user out. Reset on identity change + one retry on a CSRF 403 |
| fix | After a reload, pills did not change the server-side identity (active cookie slot never updated) → `POST /api/session/switch` |
| verify | First full run: 2 failures — `test_insider` asserted the old buggy deferral (step by `mhead`, no level) and my cookie test planted domain-less cookies; both tests corrected |

## What was done
- `app/memory/approval_flow.py`: admin-alone deferral → second-eyes proposal; head rules apply; `active_admin_count` (fail closed), `_needs_second_admin` (central + no-dept critical tools), `allowlist_rule_payload`
- `app/auth/store.py`: `allowlist_rule` second-eyes kind; toolkit name regex; TOTP set bumps `members_version`
- `app/auth/pg_store.py`: cached `credentials_version`
- `app/agent/runner.py`: deleted-chat check before the resume claim
- `app/harness/csrf.py` + dashboard middleware/`/api/csrf`: CSRF bound to `cred_ver`
- `app/harness/url_policy.py`, `app/config.py` (`SecuritySettings`): private targets only on local-model ports
- `app/dashboard/app.py`: approver-readable `_mask_payload`, `allowlist_rule` apply, toolkit validation, `POST /api/session/switch`
- `app/dashboard/static/index.html`: delegated listeners (no data in inline onclick), reopen last chat on reload, ≤700 px layout, second-eyes queue, CSRF reset/retry, pill → server switch
- tests: new `tests/test_mk20_review_fixes.py` (12); updated `test_ssrf_guard.py`, `test_cookies.py`, `test_insider.py`; new pg test in `test_pg_stores.py`
- docs: plan Mk20 + INDEX row, README (second-admin staging, port rule), `.env.example` (`SECURITY__*`)

## Decisions locked
- A head's "Always allow" is the department approver's decision and applies at once; only an admin acting alone is staged when another admin exists.
- An admin may self-approve central/critical items only when they are the sole active admin; an unreadable admin count means "needs a second admin".
- Private/loopback model endpoints are limited to ports 1234/11434/8080 by default (`SECURITY__LOCAL_LLM_PORTS`, hatch `SECURITY__ALLOW_ANY_PRIVATE_PORT`).
- `.cluade/` left alone: it is a copy of the readonly-review skill (nothing secret); likely meant to be `.claude/skills/`.

## Verification
- `tests/test_mk20_review_fixes.py` 12 passed; SSRF/cookie/local-model suites 31 passed; `ruff` clean.
- Full suite: first run 677 passed / 2 failed (both tests fixed as above); second run exposed a settings-cache order dependency in the cookie test fixtures (mine and the existing `test_cookies.py`) — both now patch every app's settings object; final run **679 passed, 157 skipped, 0 failed**.
- Headed-Chrome smoke on an isolated copy: reload reopens last chat, no sideways scroll at 380 px, pill switch after reload changes the server identity, a write right after switching keeps you signed in, admin-alone Always allow staged and confirmed by a second admin in the UI — 5/5.
- NOT run: `test_credentials_version_is_cached_but_never_stale` (Postgres; Docker down).

## Next step
- `make test-pg` once Docker is up; pull + `docker compose up -d --build` on every PC; commit Mk20.

## Handoff notes
- Uncommitted on `main`.
- Existing deployments: members who use a custom private LLM endpoint on another port must have it added to `SECURITY__LOCAL_LLM_PORTS`.

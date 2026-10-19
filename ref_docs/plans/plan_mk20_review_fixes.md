# Plan Mk20 — Fixes from the 2026-10-18 read-only review

## Metadata
- Date: 2026-10-19
- Status: **BUILT 2026-10-19 (session 036)** — offline suite green; Postgres half of the new tests not run (Docker was down)
- Follows: Mk15 (two-level approvals / allowlist), Mk17–Mk19 (security hardening)
- Source: the read-only review of 2026-10-18 (findings 1–10), plus three bugs found while fixing them

## Scope (one phase)
| # | Finding | Fix | Where |
|---|---|---|---|
| 1 | A head's "Always allow" was silently dropped whenever ≥2 admins existed (the Mk18 "second eyes" rule tested `len(deciders) <= 1` without checking the lone decider was an admin), and a deferred rule was never staged anywhere | The deferral applies only when one **admin** decided alone; that rule is now staged as a `second_eyes` proposal (`kind=allowlist_rule`) that another admin confirms. Head-decided rules apply at once | `app/memory/approval_flow.py`, `app/auth/store.py` (`SECOND_EYES_KINDS`), `app/dashboard/app.py` (`_apply_second_eyes`) |
| 2 | `verify_token` → `credentials_version()` was an uncached query on every token check (dashboard ×2, gateway, every dept-service call) — ~110 ms each on Neon | `PgMemberStore.credentials_version` served from the version-guarded cache; `set_totp_secret` now also bumps `members_version` (it was the one credential bump that did not) | `app/auth/pg_store.py`, `app/auth/store.py` |
| 3 | Second-admin rule failed open when the admin count could not be read, and covered only central memory | Fails closed (unknown count = needs a second admin); also covers critical tool calls no department owns (an admin's own `finance_transfer`) | `app/memory/approval_flow.py` |
| 4 | A deleted-chat resume consumed the single resume claim before failing | Deleted-chat check moved before `claim_resume` | `app/agent/runner.py` |
| 5 | Approvers saw `***` for every `gmail_send` field | Approver view shows recipient/subject and a 280-char body preview (SSN/card numbers masked); memo and account numbers stay masked | `app/dashboard/app.py` (`_mask_payload`) |
| 6 | CSRF token was `HMAC(secret, sub)` forever | Bound to the credential version: a reset / role change / TOTP set retires it | `app/harness/csrf.py`, dashboard middleware + `/api/csrf` |
| 7 | Members could probe any internal port (reachability oracle) and store BYOK endpoints on arbitrary internal ports | Loopback/private targets only on `SECURITY__LOCAL_LLM_PORTS` (default 1234, 11434, 8080); admin hatch `SECURITY__ALLOW_ANY_PRIVATE_PORT` | `app/harness/url_policy.py`, `app/config.py` |
| 8 | Toolkit names unvalidated; data spliced into inline `onclick="f('${…}')"` | `[a-z0-9_-]{1,64}` in store + catalog API; integrations / Resume / Connect buttons use `data-*` + delegated listeners | `app/auth/store.py`, `app/dashboard/app.py`, `index.html` |
| 9 | `.cluade/` folder | Inspected: a copy of the readonly-review skill, nothing secret. Left in place (owner's call — likely meant to be `.claude/skills/`) | — |
| 10 | Empty transcript after reload; sideways overflow on narrow screens | Reload reopens the last chat (per identity) and loads it; ≤700 px media query stacks sidebar/chat | `index.html` |

### Found while fixing (same area)
| # | Bug | Fix |
|---|---|---|
| 11 | No dashboard screen for second-eyes proposals (only the API), so a staged change could not be confirmed in the UI | "Waiting for a second admin" list on the admin Approvals tab (Confirm/Reject; own proposals show no buttons) |
| 12 | The cached CSRF token was never cleared on login of a second identity or on switching pills, so the next write failed CSRF and `api()` signed the user out | Token reset on identity change; a CSRF 403 refreshes the token and retries once instead of logging out |
| 13 | After a reload (tokens only in cookies) switching pills did not change who the server saw — requests kept going out as the last-signed-in identity | `POST /api/session/switch` flips the `<cookie>_active` slot (only to a slot the browser holds a valid session for); the pill calls it |

## Verify
- New `tests/test_mk20_review_fixes.py` (12), updated `tests/test_ssrf_guard.py`, `tests/test_cookies.py`, new pg test in `tests/test_pg_stores.py`.
- Full `pytest` + `ruff`; `make test-pg` (needs Docker) for the pg cache test.
- Manual: two admins + a head — head ticks Always allow → rule appears at once; admin alone ticks it → item approved, rule appears under "Waiting for a second admin", another admin confirms.

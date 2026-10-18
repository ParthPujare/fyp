# Plan Mk19 — Security Remediation (audit findings: IDOR, SSRF, service authz, fail-closed secrets, HMAC, token invalidation)

## Metadata
- Date: 2026-10-18
- Status: In Progress (Phase 0 baseline; implementation starting)
- Follows: Mk18 (zero-trust closure), Mk17 (core hardening)
- Scope: all code-fixable findings from the 2026-10-18 static security audit (VERDICT: FAIL — 1 HIGH, 6 MEDIUM + dead code): findings 1-5, 7-8 + dependency cleanup. `.env` credential rotation (OpenRouter, Neon) is a documented user-side step, not code.
- Locked decisions (user, 2026-10-18, two grilling rounds): owner-only `/chat` thread access (admins included); SSRF = loopback+private allowlist **+ own-service-port block**; **full Casbin** enforcement inside dept services; DELETE = admin-only policy rows; unbacked POST routes = deny for everyone; dev secrets fail closed (`ALLOW_DEV_SECRETS=1` hatch, compose **and** `run_dev.sh` default it to 1); audit/trace HMAC warn-in-dev/require-in-prod with mixed-mode verify; strict `cred_ver` (no grace window); remove `litellm`+`langchain-core`; one-pass phase-per-commit delivery; 7 acceptance criteria below.
- Plan doc dual: `/Users/parth/.opencode/plan/plan-security-remediation.md` (scope-lock original).

## Phase 0 — Baseline (before any edit)
1. `uv sync` · `make lint` · `make test` — record baseline pass/fail so later failures are attributable.
2. Gate availability: `pmat verify --format json --no-fail-fast` + `pmat analyze tdg` if `pmat` exists; `pip-audit`/`uv pip audit` if available. **None ran during the audit (no shell access) — unverified until Phase 8.**
3. `git status` clean, so each phase is its own revert point.

## Phase 1 — HIGH: thread-ownership IDOR on `/chat`
4. Add `is_own_thread(thread_id, username)` to `app/agent/checkpoints.py`; refactor the four copy-pasted predicates (`app/gateway/app.py:293`, `app/dashboard/app.py:621`, `:641`, `:868`) onto it — zero behavior change.
5. Enforce in gateway `/chat` (`app/gateway/app.py:216-219`): non-own `req.thread_id` → `403 "not your thread"`; `None` keeps defaulting to `user:{sub}` (`:89-90`).
6. Enforce identically in dashboard `/api/chat` (`app/dashboard/app.py:580-581`) before `sender(...)` (defense in depth).
7. New `tests/test_thread_ownership.py`: alice → foreign thread via gateway = 403 and via dashboard = 403; **admin → 403**; own thread = 200; history/delete/by-thread unchanged.
8. Fix tests posting non-owned/free-form thread_ids **through the endpoints**: `tests/test_chat.py:198`, `tests/attack/test_hitl_bypass.py:199`, `tests/test_chat_web.py:56,93-94`, `tests/test_memory_web.py:121-123` → rewrite ids to `user:<sub>[:suffix]`. Runner-level/direct `run_agent` tests untouched (enforcement is HTTP-boundary only).
9. **Docs (Cluster 11):** `README.md:276-279` curl example (`"thread_id":"t1"`) → `user:<sub>` form + note the 403 contract; add `CHAT-12` row to `ref_docs/test.md`. Verified unaffected: `scripts/demo_chat_web.py` sends no `thread_id`; other demo scripts call `run_agent` directly.

## Phase 2 — SSRF guard (finding 2)
10. New `app/harness/url_policy.py`: `UrlPolicyError(ValueError)` + `validate_outbound_url(url, settings)`, rules: scheme `http|https` only; reject userinfo; `getaddrinfo`-resolve and require **every** IP ∈ loopback / RFC1918 / ULA (`fc00::/7`); deny `169.254.0.0/16`, `fe80::/10`, `100.64.0.0/10`, `0.0.0.0`/`::`, multicast, `metadata.*` hostnames; deny any host+port equal to a configured `SERVICES__*_url` endpoint (all 7, every host form); fixed-string violation messages (never echo input).
11. Apply in `/api/account/local-models` (`app/dashboard/app.py:2173-2215`): validate before `httpx.get`; **delete `resp.text[:200]`** (keep status code only); policy failure → 422.
12. Apply in `_validated_key_fields` (`:2125-2160`) so every `base_url` entry point is covered: `POST/PATCH /api/account/llm-keys`, `POST/PATCH /api/members/{u}/llm-keys`, allowlist payload at `:1214`.
13. New `tests/test_ssrf_guard.py`: unit-test every rule; endpoint cases — metadata IP → 422, `http://127.0.0.1:8001/` → 422 (own-port block), stub local LLM → 200, `file://`/`ftp://` → 422, error body never contains upstream text.
14. Document accepted residual: DNS-rebinding TOCTOU between validate and connect (pin-by-IP out of scope).

## Phase 3 — Full Casbin in dept services (finding 3, largest change)
15. New `app/services/authz.py`: lazy `PolicyEnforcer()` singleton (`open_policy_store()` → policy.csv or shared DB; `g` live from MemberStore); **store-identity guard** (rebuild when `get_store()` object differs — tests swap stores); route→act from `TOOL_REGISTRY` filtered by `tool.dept == scope_obj` (`path_template` `{x}` → `[^/]+`, act = `required_scope` suffix); fallback `GET→read`, `POST→create`, `DELETE→delete`; `decide(member, scope_obj, method, path)` uses the **fresh** store member, not stale JWT claims.
16. `app/services/lib.py`: `make_app(..., scope_obj: str)` (required); wire `crm→"crm"`, `hr→"hr"`, `devops→"devops"`, `finance→"finance"`.
17. `_require_service_auth` (`lib.py:62-94`): after active-member check (before `/health`, which stays open) run `decide()`; deny → audit `DENY` (`run_id=f"svc:{service}"`, `reason="rbac"`) → `403 {"detail":"permission denied"}`; allow → audit `ALLOW` same `run_id` (direct-port traffic visible).
18. `app/harness/policies/policy.csv`: add `p, admin, admin, crm, delete` · `p, admin, admin, devops, delete` · `p, admin, admin, finance, delete` (hr already `:205`; remove duplicate admin header `:204`); comment documenting the **intentional** deny of finance `POST /accounts|/invoices` and future unbacked writes.
19. Shared-DB deployments: `python -m app.db sync-config` after rows land (`Makefile:73-74`) — runbook step.
20. New `tests/test_service_authz.py`: marketing GET/POST CRM = 200; marketing DELETE CRM = 403; admin DELETE = 200; HR token on CRM = 403; `carol POST /accounts` = 403; `carol POST /transfers` = 200; `/health` open; anonymous/forged = 401 (`tests/test_service_bypass.py` stays green).
21. **Parity test**: extend `tests/attack/test_policy_matrix.py` — service-level decision == gateway `intercept_tool` decision for every (tool, role) pair.
22. Docs: `ref_docs/threat_model.md` enforcement-boundaries note + README.

## Phase 4 — Fail-closed dev secrets + seeded credentials (findings 4, 5)
23. `app/config.py`: add `allow_dev_secrets: bool = False` (env `ALLOW_DEV_SECRETS`); replace `_no_dev_secrets_outside_dev` (`:196-213`) — built-ins accepted **only when `env == "dev" and allow_dev_secrets"**; hatch never applies when `ENV != dev`.
24. `.env.example`: add `ALLOW_DEV_SECRETS=1` under `# ---- App ----`, dev-only comment.
25. `docker-compose.yml`: `environment: ALLOW_DEV_SECRETS: ${ALLOW_DEV_SECRETS:-1}` + header-block note (`:13-17`).
26. `scripts/run_dev.sh`: `export ALLOW_DEV_SECRETS="${ALLOW_DEV_SECRETS:-1}"` at top (bare-metal dev flow keeps booting; `ENV=prod` still refuses regardless).
27. `app/auth/store.py` (`_seed_if_empty`, `:577`): skip member seeding when `env != "dev"` (departments still seed); on init when `env != "dev"`, deactivate members whose password still verifies against `SEED_PASSWORDS` plaintext (`actor="system:seed-lockdown"`) — idempotent.
28. Bootstrap CLI: extend `__main__` block (`store.py:1956`) — `python -m app.auth.store list` + `create-admin <username>` (getpass, min-6, never on argv). Document in README.
29. Fix stale docs: `.env.example:22-23` (5 demo accounts → dev-only).
30. New `tests/test_dev_guard.py`: defaults+`ENV=prod` → raises; `ENV=dev` w/o hatch → raises; dev+hatch → loads; hatch+prod → still raises; non-dev fresh store = 0 members; dev-seeded store inited with `ENV=prod` → seeded accounts deactivated.

## Phase 5 — Audit/trace HMAC enforcement (finding 7)
31. Config: `env != "dev"` + empty `SERVICES__AUDIT_HMAC_KEY` → raise at startup; same for `SERVICES__TRACE_HMAC_KEY` (`config.py:67-70`); dev + no key → one-time `logging.warning` in `open_audit_log`.
32. Mixed-mode verify already exists (`resolve_key_chain` keeps `None` — `audit.py:101-111`, verify `:238`, `pg_audit.py:265`; trace `ops/store.py:30-45`): (a) regression test plain-chain → key → continue → verify green; (b) **fix stale comment** `config.py:60-61`; (c) confirm trace matches.
33. `ref_docs/runbook_backup.md`: "enabling HMAC on an existing chain" section — no migration, verification stays green.
34. Test: non-dev without key → raises (in `test_dev_guard.py`).

## Phase 6 — Credential-version claim (finding 8)
35. Migration: `credentials_version INTEGER NOT NULL DEFAULT 1` on `members` — ALTER twin of `login_attempts` (`store.py:425-441`) + PG `_init_schema`, `needs_pg`-guarded test.
36. Store API: `credentials_version(username)` + bump in `reset_password` (`:1947`), create (init 1), role/dept/scopes update, `set_totp_secret`.
37. `app/auth/jwt.py`: `create_token` adds `cred_ver` (missing member → 0 both sides — keeps `demo_chat_web.py:197-198` ghost path honest); `verify_token` enforces after decode — missing/mismatched → `AuthError("token superseded")`. Covers all 7 verify sites (`gateway:149`, `services/lib:80`, `dashboard:112/163/189/460`, `auth/service:125`); lazy `get_store`. **Strict: no grace, no legacy acceptance.**
38. New `tests/test_token_lifetime.py`: reset password → old token 401 superseded at gateway, dashboard, dept service, `/auth/me`; fresh login works; role change supersedes; expired/forged still rejected for original reasons.

## Phase 7 — Hygiene, dead code, docs
39. `app/llm/factory.py:78` `print` → `logging.getLogger(__name__).warning`.
40. `uv remove litellm langchain-core` → `uv.lock` regenerated; grep no imports; `uv tree` shows `langchain-core` transitively via langgraph; suite green.
41. Doc sweep: README security section, `ref_docs/test.md` new rows, `ref_docs/threat_model.md`.

## Phase 8 — Verification (nothing claims "done" before this)
42. Gates vs baseline: `make lint` · `make test` · `make test-pg` (if Docker) · `pmat verify`/`pmat analyze tdg` (report unavailable if missing) · `pip-audit` (or record not-available). No new failures vs Phase 0.
43. **Direct exercise (Tier 1)** — `./scripts/run_dev.sh`, then by curl: alice foreign-thread `/chat` → 403 (admin too), own → 200; metadata-IP + own-port `base_url` → 422, stub LLM → 200, no upstream body echoed; alice direct `:8005` POST/DELETE → 403, admin DELETE → 200, `/health` → 200; `ENV=prod` boot with built-ins → refuses, hatch boot → works; password reset → old token 401 superseded at 4 surfaces; `python -m app.db verify-audit` green after keying. Screenshot only if UI copy changes (expected: none).
44. Report: commands/URLs, evidence lines, verification tier, screenshot count.

## Out-of-band (user-side, not code)
- **Rotate `.env` credentials**: OpenRouter key (`.env:66`), Neon password (`.env:70`).
- Before real deployment: real `JWT__SECRET`, `LLM__FERNET_KEY`, `SERVICES__AUDIT_HMAC_KEY`, `SERVICES__TRACE_HMAC_KEY`, `ENV=prod` — **set before upgrading** (Phases 4-5 refuse boot otherwise).
- `python -m app.db sync-config` once after Phase 18 policy rows land (shared-DB setups).

## Explicit non-goals
Rate limiting & CORS changes · async/messaging (none exists) · performance budgets (local FYP scale) · CI (verified none exists; uv.lock + local gates only) · observability overhaul (print→logging only) · backup/restore runbooks (Neon rotation = manual user step) · API versioning · k8s/deploy redesign · frontend changes · dep upgrades beyond removing litellm/langchain-core · DNS-rebinding TOCTOU pinning · JWT denylist/jti store · Tier-2 screenshots unless UI copy changes.

## Failure-mode decisions
- Boot refusal on missing secrets/hatch/HMAC in non-dev = intended; set env **before** upgrading.
- New 403s are the fix — fix test expectations, never weaken policy.
- `cred_ver` strict → one coordinated re-login per deploy; column default keeps un-upgraded code working.
- Enforcer singleton guarded by store identity (no stale grants across swapped test stores).
- Mixed-mode audit verify — no chain rewrite, no archive.

## Acceptance criteria (observable evidence work is done)
1. Alice's token POSTing another user's `thread_id` to gateway `/chat` or dashboard `/api/chat` → 403 **including for admin**; her own thread → 200.
2. local-models probe and every llm-key `base_url` accept a stub local LLM but reject metadata IP, own-service ports, non-http schemes → 422; no response ever echoes upstream body text.
3. Service-level decisions identical to gateway `intercept_tool` decisions for all (tool, role) pairs; marketing DELETE CRM → 403, admin DELETE → 200, carol `POST /accounts` → 403, `POST /transfers` → 200; `/health` open.
4. Boot with built-in secrets requires `ENV=dev` **and** `ALLOW_DEV_SECRETS=1`; `ENV=prod` refuses; non-dev store ends with zero seeded-password accounts usable.
5. Non-dev boot without `SERVICES__AUDIT_HMAC_KEY`/`SERVICES__TRACE_HMAC_KEY` refuses; pre-existing plain chain still verifies after keying (regression test).
6. Password reset or role change invalidates previously issued tokens at all 7 verify sites.
7. Full suite + lint green vs baseline; `litellm`/`langchain-core` gone from direct dependencies.

## Applicability (engineering-principles clusters — scope-lock record)
1 Edge & API — **GRILLED**: owner-only 403 contract; rate limiting/CORS non-goals. · 2 Reliability — **GRILLED**: compose+run_dev.sh default hatch, actionable boot errors. · 3 Async — SKIPPED: no queues/cron/websockets. · 4 Data layer — **GRILLED**: cred_ver migration (SQLite+PG), strict compat, sync-config runbook. · 5 Concurrency — **GRILLED** (design): store-identity enforcer guard, existing audit locks, idempotent seed-lockdown. · 6 Scale — SKIPPED: single-digit users; in-memory enforce. · 7 Delivery — **GRILLED**: no CI (verified), uv.lock regen, phase-per-commit. · 8 Observability — SKIPPED (reduced): print→logging only. · 9 Security — **GRILLED**: all 8 findings. · 10 Resilience/Ops — SKIPPED: backups out of scope; rotation user-side. · 11 Contracts — **GRILLED**: README curl + test.md updated; demos verified unaffected; no versioning (closed system).

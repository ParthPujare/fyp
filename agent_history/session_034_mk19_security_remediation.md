# Session 034 — Mk19 Security Remediation (all 8 findings, phase-per-commit)

## Metadata
- Date: 2026-10-18
- Plan / Phase: mk19 — Phases 0–8 (complete)
- Plan step: `ref_docs/plans/plan_mk19_security_remediation.md` items 1–45
- Scope: close the 1 HIGH + 6 MEDIUM findings of the 2026-10-18 static audit (thread-IDOR, SSRF, service-authz gap, dev-secrets fail-open, HMAC warn-only, token invalidation) + remove `litellm`/`langchain-core`; `.env` credential rotation stays a manual user step
- Status: complete
- State at start: git main @ pre-Mk19 (Mk18 closed); baseline gates = ruff clean, pytest `2 failed (pre-existing: pg canary + policy_store_contract[file]), 608 passed, 156 skipped`

## Objective
Implement every locked phase of the Mk19 plan, one commit per phase, with the project's gates plus a live Tier-1 exercise before claiming done; then update agent_history and explain the changes in ELI5 terms.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| morning | scope/plan | 12 locked decisions from two grilling rounds; Phase 2 resolved as Option A (strict loopback/private probe; public-allowed BYOK base_url) |
| Phase 1 | implement | `is_own_thread()` prefix predicate needs colon-boundary safety (`user:alice@acme.com.evil` must not match) |
| Phase 2 | implement | two-mode `url_policy`: `probe` (loopback/private only) vs BYOK (public allowed); own-service-port block is mode-independent; 422 must not echo the request body back |
| Phase 3 | implement | dept-service Casbin: route→act mapper from TOOL_REGISTRY with GET/POST/DELETE fallbacks (read/create/delete); store-guarded singleton enforcer keyed on store identity + generation; audit lines need `{prev_hash,hash,entry}` wrapper; PG member writes go through inherited MemberStore methods (`update`, not `update_role`) |
| Phase 4 | implement | fail-closed = validator order matters (`_no_unkeyed_chains_outside_dev` before `_no_dev_secrets_outside_dev` — prod tests must set both HMAC keys to surface the dev-secrets error); seed lockdown = skip seeding + deactivate unrotated seeds outside dev, actor `system:seed-lockdown`; `ALLOW_DEV_SECRETS=1` hatch wired into conftest + compose + run_dev.sh + .env.example |
| Phase 5 | implement | plan claimed mixed-mode verify "already exists" — empirically false (plain lines failed once a key was set); implemented per-line/run hashed `keyed` era flags: plain verifies under None, keyed under the rotation chain, plain-after-keyed = tamper; sqlite ALTER + PG migration 0014; one-time dev warning in `open_audit_log` |
| Phase 6 | implement | `credentials_version` on members (sqlite ALTER + PG 0015); `create_token` stamps `cred_ver`, `verify_token` rejects older as superseded — all 7 verify sites inherit it via the choke point; missing member = 0 both sides keeps the ghost path honest; survey found 2 existing tests asserting the old "token survives role change" behavior → rewrote to the locked strict semantics (re-mint/re-login), never weakened the policy |
| Phase 7 | implement | `uv remove litellm langchain-core` (zero imports remained; langchain-core stays transitive via langgraph — verified with `uv tree --invert`); last stray `print` in `app/llm/factory.py` → `log.warning`; doc sweep (README §Security hardening, test.md §22, threat_model 6 rows, runbook §5 bootstrap+sync-config, .env.example stale claim) |
| Phase 8 | verify | full suite exposed 3 real regressions the per-phase nets missed: (a) `test_dev_guard`'s `cache_clear()` detached `dash.settings` from the settings cache → test_insider fixture mutated a stale object; (b+c) two audit-keyset assertions predated Phase 5's `keyed` field. Fixed all three; suite back to exactly the 2 pre-existing failures |
| Phase 8 | live | first live run silently hit the user's real Neon DB (`.env` DATABASE_URL overrides scratch paths; `get_store()` prefers PG when enabled) while the CLI reset wrote sqlite — symptoms: old token kept working. Cleaned the 2 demo rows from Neon (approval + trace run), restarted with `DATABASE_URL=""`, reran green |
| Phase 8 | audit | pip-audit on the exported lockfile: 4 pyjwt 2.13.0 CVEs (PYSEC-2026-4142/4150/4151/4183, fixed 2.14/2.15) — pre-existing pin; dep upgrades beyond litellm/langchain-core are outside the scope lock → reported, not fixed. pmat runs but is Rust/Ruchy-oriented (not applicable to this Python repo) |

## What was done
- `b260d8b` Phase 1: owner-only `/chat` (gateway + dashboard proxy), `app/agent/checkpoints.py::is_own_thread`, 12 tests
- `d53546e` Phase 2: `app/harness/url_policy.py` two-mode SSRF guard, wired into http client paths, 19 tests
- `8e87ff4` Phase 3: `app/services/authz.py` (route→act, store-guarded enforcer singleton, `run_id=svc:<dept>`), per-request Casbin in `lib.make_app`, policy.csv admin-DELETE rows, 8+ tests
- `7329110` Phase 4: `allow_dev_secrets` hatch + validators, seed-skip + `_lockdown_seed_passwords`, conftest/compose/run_dev/.env.example defaults, 8 tests
- `69c7ce5` Phase 5: prod requires `SERVICES__AUDIT_HMAC_KEY`+`TRACE_HMAC_KEY`, dev one-time warning, real mixed-mode verify (audit `keyed` field, trace `keyed` column, migration 0014, era-aware redact), runbook §3, tests
- `f039fda` Phase 6: `credentials_version` (sqlite ALTER + migration 0015 + pg twin via inherited methods), `cred_ver` issue/enforce in `app/auth/jwt.py`, `tests/test_token_lifetime.py` (5 tests), + leftovers: Phase 4 item 28 bootstrap CLI (`python -m app.auth.store list|create-admin`, getpass), Phase 3 test gaps (HR-on-CRM 403, carol transfers 200, /health open)
- `ff4ea76` Phase 7: `uv remove litellm langchain-core`, factory print→logging, doc sweep (README/test.md/threat_model/runbook/.env.example)
- `cd23e28` Phase 8: suite fixes — insider settings rebind + two `keyed` keyset assertions

## Decisions locked in this session
- All 12 plan decisions stand as locked (owner-only chat; Option A SSRF; full service Casbin; admin-only DELETE policy rows; deny for unbacked POSTs; ALLOW_DEV_SECRETS hatch; HMAC warn-dev/require-prod with mixed-mode verify; strict cred_ver no grace; dep removals; phase-per-commit; rotation = manual user step)
- Phase 5 deviation (documented): plan's "mixed-mode already exists" was false; per-line `keyed` era flags implement the locked intent
- Phase 6 test updates follow the plan's failure-mode rule: new 401s are the fix — test expectations were updated, policy never weakened

## Verification performed
- Per phase: ruff clean + affected-test nets (53–103 tests each) before every commit
- `make test` final: **664 passed, 2 failed** — identical to the pre-Mk19 baseline's 2 pre-existing failures (pg canary, policy_store_contract[file]); 156 skipped
- `make lint`: clean; `make test-pg`: BLOCKED (Docker not running)
- Live Tier-1 (7-service stack, scratch data dir, `DATABASE_URL=""`): login 200; own thread 200; bob's thread 403 `not your thread`; admin→alice thread 403; CRM direct port alice GET 200 / DELETE 403 / anon 401 / health 200; password reset → version 2 → old token 401 `token superseded` at `/auth/me` + gateway + CRM + dashboard; fresh login 200 everywhere; gateway log shows the one-time UNKEYED warning; `validate_outbound_url` blocks metadata IP + both own-service ports, allows public BYOK URL
- Bootstrap CLI exercised against a scratch DB: list ✓, create-admin happy path ✓, short-password reject ✓, duplicate reject ✓
- pip-audit (lockfile export): 4 pyjwt CVEs on the existing 2.13.0 pin (reported, out of scope)

## Blocker / open questions
- `make test-pg` unrun (Docker down) — PG twins rely on migration-file review + inherited-method reasoning; run once when Docker is up
- User-side: rotate OpenRouter key (`.env:66`) and Neon DSN (`.env:70`), then set `SERVICES__AUDIT_HMAC_KEY` + `TRACE_HMAC_KEY` before any prod deploy (runbook §3)
- pyjwt 2.13.0 → ≥2.15.0 bump recommended (4 CVEs) — needs a scope decision (dep upgrades were locked out of Mk19)

## Next step (for the next session)
- Mk19 is closed. On request: run `make test-pg` (Docker up), apply the pyjwt bump as a mini-plan, or guide the user through the .env rotation + HMAC keying runbook steps.

## Handoff notes
- Audit/trace lines now carry a `keyed` marker; any future exact-keyset test must include it
- `dash.settings` can go stale after any `get_settings.cache_clear()` — re-bind (`dash.settings = get_settings()`) in fixtures that mutate settings, as test_insider now does
- The first live-run lesson: with `.env DATABASE_URL` set, services use Neon even when `SERVICES__*_PATH` env overrides point at scratch — set `DATABASE_URL=""` for local-only exercises
- Neon DB already received migrations 0014 + 0015 (additive, no rewrite); demo rows created during the aborted first exercise were deleted

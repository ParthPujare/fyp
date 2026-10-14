# Session 033 — Mk18 zero-trust closure: Phases A–H implemented + Neon synced

## Metadata
- Date: 2026-10-11
- Plan / Phase: mk18 — Phases A–H (`plan_mk18_zero_trust_closure.md`)
- Plan step: untrusted boundary → DLP → service trust → cookies → SoD → Composio → memory scope → ops
- Scope: eliminate the 7 big zero-trust gaps (prompt-injection boundary, outbound DLP, service/identity trust, insider SoD, Composio deputy, memory scope, ops integrity)
- Status: complete offline (555 passed, ruff clean); Neon at 0013 with policy synced + wildcard pruned; live-Neon + real-model verification open
- State at start: HEAD `3f237f1` (Mk17 committed); plan doc + `untrusted.py` stub untracked; Neon at 0010 with wildcard policy

## Objective
Implement the entire Mk18 plan verbatim (recommended options locked: delimit+strip, passthrough JWT, httpOnly cookies, no global fallback, scoped admin), keep the suite green, migrate Neon (0011–0013 + policy resync), and close with an audit of what gaps remain — including in the new code.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| start | explore | Subagent-pinned exact touchpoints (file:line) for all 7 areas; user locked 4/5 tradeoffs, asked for token-storage explainer, then "use recommended" for the rest |
| A | implement | `summary == content` so wrapping `content` covers the model path; MockLLM ignores content so scripted attack tests survive wrapping; old snapshot test asserted raw poison — updated to the stripped contract |
| B | implement | Skill templates stringify numbers (`{{budget}}`→`"1000"`) and schemas list aspirational `required` — validator checks types of present fields only, else legit flows break (found via debug script) |
| C | implement | Token must thread run_agent→executor→services; signature-inspection forwarding keeps all test doubles working; conftest transport injects admin JWT; `User.password` shim is test-pinned, kept with scope note |
| D | implement | Dual httpOnly slots preserve the switcher; Secure cookies need an insecure flag in tests; logout must be CSRF-gated (fail closed) not exempt |
| E | implement | `reflect` had its own head auto-commit bypass around `route_for` — fixed both; `hr:delete` has no non-admin row so the generator must union registry scopes; script silently swallowed import failure (sys.path) — fixed loudly |
| F | implement | No-key execution hit the Fake client (fake-send hazard) — fail closed; Fake only via monkeypatch in tests; unknown slugs → critical; member auto-provision/self-heal removed |
| G | implement | Provenance is JSON-level (no DDL); transcripts user-scoped unless team facts proposed; redact rehashes forward on both backends |
| H | implement | Rotation = retired-keys dual-verify (never rewrite history); spend = flat estimate, MockLLM never metered; gateway 429 + in-run Mock fallback |
| verify | test | 3 contract updates (wildcard denial, parity+provenance, migration contiguity via renumber 0013→0012/0014→0013); suite-pollution pattern fixed with tmp-policy/components fixtures; ruff now cleaner than HEAD (0 vs 15) |

## What was done
- **Phase A:** `harness/untrusted.py` (wrap/strip/patterns/rule), wrap sites (`graph.py` results+system rule, `recall.py`, `snapshot.py`, `registry.py:schema_for`), strict judge JSON, extract drops + traced counts; `tests/test_untrusted.py` (8), 4 new attack tests, 1 updated
- **Phase B:** `sanitize.scan_args_for_exfil` (7 patterns), `intercept_tool` exfil deny (recall-exempt), `ToolExecutor` arg-shape validation, `GET /api/approvals/{id}/args` + impact lines; `tests/test_exfil.py` (5)
- **Phase C:** `services/lib.py` JWT middleware (health open), executor `auth_token` + runner/gateway/dashboard threading, conftest injection + `service_headers()`, migration `0011` (throttle/TOTP cols) + verify/TOTP/service logic; `tests/test_service_bypass.py` (6), `test_services.py` rewritten
- **Phase D:** `JWTSettings` cookie fields, `harness/csrf.py` + `cookies.py`, auth login/logout cookies + dual-read, dashboard middleware + `/api/csrf` + login forward + logout, index.html (no localStorage tokens, credentials, CSRF); `tests/test_cookies.py` (5)
- **Phase E:** `scripts/scope_admin_policy.py`, wildcard removed (29 explicit rows incl. hr:delete), `_ensure_toolkit/_append_policy_row`/skill-create admin rows, head dept/write→admin-only (routing + reflect), second-eyes store (`0013`) + endpoints + 6 wired writes + allowlist-defer; `tests/test_insider.py` (6)
- **Phase F:** vault fallback removed, keyless execute denied, visibility key-gated, catalog-add own-key only, no member provision/heal, remote-synced disconnect/refresh, version fail-closed, risk default critical, provenance in results; 2 new tests, 4 updated
- **Phase G:** user-scoped 1:1 transcripts, Fact.provenance end-to-end, snapshot priority + trace sidecar, `redact_run` + `prune --redact` (both backends); `tests/test_memory_scope.py` (6)
- **Phase H:** audit/trace retired-key dual-verify, trace HMAC option, `llm/metered.py` + `0012_llm_usage` + caps + 429 mapping, `ref_docs/runbook_backup.md`; `tests/test_ops_hardening.py` (4)
- **Neon:** `migrate` 0011–0013, `sync-config` +29 rows, `--prune` wildcard (-1), drift zero, enforcement spot-checked live (postgres), audit 198 intact
- Docs: Mk18 plan + INDEX row, this file + INDEX row

## Decisions locked in this session
- Least-privilege everywhere: unknown objects denied even for admins; unknown slugs critical; unknown tool versions refused.
- Fail-closed defaults: keyless external denied (never Fake); version-check failures error; logout without CSRF fails; throttle lockout reads as 401.
- History is never rewritten: HMAC rotation via retired keys; retention via redact+rehash, never silent edits.
- Single-admin fallback everywhere second-eyes applies (audited); heads can no longer self-approve anything.
- Test isolation is now structural (tmp policy/skills/components) after two pollution incidents (se_*/legal rows, editor_skill row) — all cleaned.

## Verification performed
- `pytest` offline: **555 passed, 17 skipped** (was 506); `ruff check`: **0 errors** (HEAD had 15)
- `test_untrusted/exfil/service_bypass/cookies/insider/memory_scope/ops_hardening` new suites green
- Neon: `status`/`check` 0001..0013, `sync-config --dry-run` clean, `verify-audit` intact, live enforce spot-checks (hr_delete True, ghost False, central-write True, pii_ssn True on postgres backend)
- Real-model + live-Neon multi-PC verification still open (user's LM Studio)

## Blocker / open questions
- Phase 6-style live verification (real LM Studio tool model, multi-PC Neon) not run here.
- `fyp-app-1` container still pre-Mk18 until `docker compose up -d --build` on every PC; working tree uncommitted.
- `bobby@acme,com` / `timmy@acme.in` live rows still need admin cleanup (flagged since Mk17).

## Next step (for the next session)
- Commit + push Mk18, rebuild all images, run live verification (extraction quality on the real model, second-eyes flow in UI, cookie sessions in browsers), clean the two bad live rows.

## Handoff notes
- `scripts/scope_admin_policy.py --check` is the CI gate for admin coverage (29 pairs).
- Suite-wide rule learned twice: any test writing skills/policy/audit must use the isolated fixtures — grep for direct `TestClient(...skills|.../policy` if adding dashboard write tests.
- `MeteredProvider` skips MockLLM by design; caps default 500 calls/$5 day via `LLM__*`.
- Neon `sync-config --prune` removes `source=file` stale rows only — safe to re-run.

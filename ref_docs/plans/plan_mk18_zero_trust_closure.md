# Plan Mk18 — Zero-Trust Closure (prompt-injection boundary + service trust + insider SoD)

## Metadata
- Date: 2026-10-11
- Status: In Progress (Phase 0 plan doc; implementation starting)
- Follows: Mk17 (core hardening, Phases 0–5 done offline, uncommitted), Mk16 (platform extractor), Mk15 (two-level approvals), test run `ref_docs/test_run_2026-10-10/results.md`
- Scope: eliminate the 7 big zero-trust gaps (prompt/tool-output injection boundary, outbound DLP, service/identity trust, insider SoD, Composio deputy, memory scope, ops integrity). Small fixes, reports, and UI polish explicitly excluded.
- Locked decisions (user, 2026-10-11): untrusted = delimit + strip; dept services = passthrough user JWT; token storage = httpOnly cookies + CSRF (recommended); Composio global fallback removed; admin wildcard replaced by explicit rows; MFA = columns now + admin-only flag (default off); spend caps = 500 calls/$5/day defaults via env; HMAC rotation = 24h dual-verify window.

## Facts (verified read-only 2026-10-11, subagent-pinned)
- Injection in: snapshot raw concat (`memory/snapshot.py:28-44`) injected as `system` (`graph.py:364`); skill trigger/steps/body become tool descriptions (`registry.py:113-126`, `model.py:73-78`); external results truncated + PII-only (`service.py:134-150`, `graph.py:630-640`); recall formatted `[scope] transcript` (`recall.py:49-66`); judge embeds raw reply (`judge.py:52-69`), verdict carved first-`{`→last-`}` (`:72-83`); `resolve_args` `str()` coercion, unknown keys verbatim (`executor.py:25-33`).
- Sanitize today: PII-only (`sanitize.py`: field-name + email/SSN regex), trace-copy only (`graph.py:196-220`), never mutates model input.
- Model-out parsing per-provider, no central parser (`openai:219-223`, `zen:160-182`, `gemini:232-300`, `anthropic:224-228`, `mock:45-57`); approval payloads raw args (`graph.py:439-506,765-820`); `args_hash` strips `VOLATILE_ARG_KEYS` (`routing.py:42-44`).
- Auth: HS256 bearer JWT, localStorage dual sessions (`index.html:344-407`), no MFA/throttle; dashboard forwards same token in gateway body (`dashboard/app.py:309-327`); gateway body-`token` only (`gateway/app.py:109-374`); dept services zero auth (`services/lib.py:49-160`); only `:8006` published (overlay `18000/18001` demo-only).
- Insider: `p,admin,admin,*,*` (`policy.csv:13`); head own-dept `dept/write`→`none` (`routing.py:128`); central second-admin only (`approval_flow.py:97-126`, single-admin fallback).
- Composio: `vault.py:14-28` per-user > global > None; Disconnect local-only (`dashboard/app.py:2219-2227`); member Connect auto-provisions (`:2136-2144`); risk = name-substring (`adapter.py:15-21`); `dangerously_skip_version_check` fallback (`client.py:119-136`); `_ensure_toolkit_policy_rows` grants every dept (`dashboard/app.py:2233-2246`).
- Memory: episodic always `dept/<own>` (`graph.py:730-737`); snapshot positional truncation (6000); budgets per-turn only (`guardrails.py:18-66`, tracker per `run_agent` call).
- Ops: audit HMAC manual `.env`, no rotation; trace plain SHA; single container SPOF; volume + Neon, no scheduled backup.
- Migrations contiguous `0001..0010` (`migrate.py:34-93`); SQLite twin try/except pattern; PG memo TTLs (MEMBER 300 / READ 30 / KEY 5).

## Phases (execution order — each step states exact file:line, change, test, acceptance)
| Phase | Goal | Key files | Acceptance |
|-------|------|-----------|------------|
| A | Untrusted-data boundary: new `untrusted.py` (wrap + strip), wrap at 5 entry sites, system-prompt rule, strict judge JSON, extract drops | `harness/untrusted.py` (new), `graph.py:630-640,364`, `recall.py:64`, `snapshot.py`, `registry.py:schema_for`, `judge.py`, `extract.py`, `llm_extract.py` | B2b/B3b/persistence/extraction/judge-smuggle tests deny/no-call |
| B | Outbound DLP + approval impact: arg schema validation, exfil patterns → deny, privileged args view + impact line | `agent/tools.py:execute`, `harness/intercept.py`, `dashboard/app.py` approvals, `evals` | `test_exfil.py` green; decider review without secret exposure |
| C | Service trust: dept-service JWT middleware, executor forwards token, login throttle cols + backoff, seed-shim removal | `services/lib.py`, `tools.py`, `auth/service.py`, `store.py` + `0011_service_trust.sql` + twins | `test_service_bypass.py` green; brute force locks |
| D | httpOnly cookies + CSRF: settings, login/logout set/clear, dual-read verifiers, CSRF module + mutating-route gate, index.html session rework, Bearer kept for tests | `config.py`, `auth/service.py`, `gateway/app.py`, `dashboard/app.py`, `harness/csrf.py` (new), `static/index.html` | cookie flag asserts; XSS can't read token; flows work cookieless |
| E | Insider SoD: explicit admin rows (generator script), second-eyes on policy/skill/allowlist/keys, head dept/write suspends | `scripts/scope_admin_policy.py` (new), `approval_flow.py`, `routing.py:128`, `dashboard/app.py` writers, `0013_second_eyes.sql` + twin | `test_insider.py` green; no solo high-impact write with ≥2 admins |
| F | Strict Composio: remove fallback, remote-synced revoke, approved-only provision, metadata risk, no skip-version, provenance + defang | `integrations/vault.py`, `service.py`, `dashboard/app.py` connect/refresh/disconnect/catalog, `adapter.py`, `client.py` | isolation + revoke-sync + provision-gate tests green |
| G | Memory scope: user-scope 1:1 transcripts, snapshot priority + provenance, redact-preserving prune | `graph.py:reflect`, `episodic.py`, `snapshot.py`, `db/__main__.py prune` (provenance is JSON-level, no DDL) | peer can't recall 1:1; chain valid post-redact |
| H | Ops: HMAC rotate dual-window, trace HMAC option, per-user/day spend caps + 429, backup runbook | `audit.py`, `pg_audit.py`, `ops/store.py`, `llm/metered.py` (new), `0012_llm_usage.sql` + twin, `ref_docs/runbook_backup.md` (new) | rotation/cap/runbook tests green |

## Explicit non-goals
- UI polish, reports/demos, `DOCK/SET-08/hosted` rows, `REC-05`, 3-PC feel (stay deferred as in Mk17).
- MFA enforcement beyond admin-flag default-off; full HA/multi-replica (runbook only).

## Risks
- Cookie migration touches every test using Bearer + the dual-session JS — keep dual-read (cookie first, Bearer fallback) for one release.
- Scoping admin rows must precede wildcard removal (`has_row` on all replacements + dry-run clean first).
- Extractor prompt change may shift extraction quality — verify on the real LM Studio model in Phase 6 style.

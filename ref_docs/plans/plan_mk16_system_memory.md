# Plan Mk16 — Platform-Owned Memory Extractor (Shared Qwen Key, User-Scope Only)

## Metadata
- Date: 2026-10-04
- Status: Phase 1 DONE (session 030, 2026-10-04); Phase 2 (user live sign-off) + auto-approver follow-up open
- Follows: Mk3 (BYOK), Mk5 (direct providers), Mk8 (LLM extraction), Mk13 (Neon shared DB), Mk15 (routing)
- Decisions (user, 2026-10-04):
  1. Auto-write is **user-scope only at every level** — admin and dept head included. Central/dept/other-user writes stay manual (dashboard) or HITL-gated.
  2. One platform OpenRouter key serves all users; chat itself stays **BYOK-only**.
  3. Extraction call is **sync in-turn with heuristic fallback** (turn never fails).
  4. Runs **every turn**; the model decides (returns `[]` when nothing durable).
  5. Input is the **last turn pair only** (latest user msg + latest assistant reply).

## Problem
- Chat is BYOK-only: without a personal key each user falls back to `MockLLM`, and fact extraction falls back to the marker heuristic (`app/memory/extract.py`), which over-fires on task words ("needs", "should") and under-captures real prefs ("I work in Python").
- User supplied one `OPENROUTER_API_KEY` in `.env` for a platform feature, but nothing reads it (`app/config.py` only knows `LLM__*`; `extra="ignore"` drops `OPENROUTER_API_KEY`). The free model (`qwen/qwen3.8-27b:free`) is therefore unwired.
- Scope risk: `classify()` (`app/memory/extract.py:42-47`) can propose `central`/`dept/*`, and admin claims pass `memory_allowed` for everything (`app/harness/enforcer.py:127-151` + `p, admin, admin, *, *`). An unconstrained auto-extractor running as admin could auto-propose central writes.

## Facts (verified in repo)
- Extraction runs in `reflect` (`app/agent/graph.py:620-786`) via `_relevant_facts` (`214-237`): real provider → `llm_extract.build_messages/parse_response`, else heuristic. `user/*` commits with no gate (`routing.py` `ROUTE_NONE`); `central`/non-head `dept/*` suspend via `interrupt()`.
- `load_memory` (`graph.py:241-287`) + `GET /api/memory/me` (`app/dashboard/app.py:1357-1386`) inject `central + dept/<own> + user/<self>` — new user facts surface with no UI change.
- Stores are shared already: `open_base_store` (`app/memory/factory.py:17-23`) → `PgBaseStore` on Neon (`app/memory/pg_stores.py:29-45`); namespaces `(user, uid_label)` isolate users (`app/memory/scopes.py:36-44`); budgets in `app/memory/edit.py:41-46` (`MEMORY__USER_BYTES=4096`).
- Topology: each laptop runs its own container against one Neon DB. ~~A platform
  key must therefore live in each instance's own `.env` (not synced via DB)~~
  SUPERSEDED by the session-031 follow-up below: the key lives as one encrypted
  row in the shared DB; per-machine env is only the pre-bootstrap fallback.
  Writes converge in `memory_items` either way.
- Free tier (Oct 2026): `:free` suffix = free routing (without it = billed); ~20 req/min, ~50/day (1000/day with credits); roster churns — pin exact id, fail soft.

## Design
- **Separate paths.** Chat provider resolution (`get_provider_for_user`, `app/llm/factory.py:54-90`) untouched. New singleton `get_system_provider()` (`app/llm/system.py`) builds one `OpenRouterProvider` from the platform key + pinned `qwen/qwen3.8-27b:free` (constructor rejects non-`:free`). Key server-side only; never in JWT/API/trace/audit.
- **User-scope pin.** New `system_extract_facts()` (`app/memory/llm_extract.py`) wraps `build_messages/parse_response`, then keeps only facts with `scope == scope_user(claims["sub"])`. Everything else dropped. `memory_allowed(write)` + `MemoryEditor.apply` remain the final gate (dedup/budget/audit unchanged).
- **Sync + fallback.** `_relevant_facts` tries system first (last-pair input, ~15 s timeout, 1 call/turn); any error/empty-key/parse-fail → heuristic. Offline/`pytest` (no key) behavior byte-identical to today.
- **Key resolution order (session 031):** shared-DB `platform_llm` row →
  `SYSTEM__OPENROUTER_API_KEY` (canonical env) → legacy top-level
  `OPENROUTER_API_KEY` → None (heuristic mode). Env is never auto-imported.
- **Observability:** trace `MEMORY_WRITE` gains `extraction: system|heuristic` (+ model id when system); audit `ALLOW reason=system-extract|heuristic|rbac` preserved.

## Phases
| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Config + singleton + user-pinned extractor + `reflect` wiring + `.env.example` + tests | DONE (session 030) |
| 2 | Live verify on Neon (alice + admin turns, quota/fallback behavior) | DONE (session 030, user sign-off) |
| 3 | DB-backed shared key + admin UI (key/model editable, no per-machine env) | DONE (session 031) |
| 4 | Autonomous admin (auto-approver) | Separate plan, not this doc |

## Follow-up: DB-backed shared key + admin UI (2026-10-04, session 031)

Per-machine `.env` distribution is superseded: the platform key now lives as
ONE Fernet-encrypted row (`platform_llm`, migration `0009`) in the shared
database, so any instance on the same `DATABASE_URL` uses that exact key with
no per-machine setup. Resolution: DB row → env (`SYSTEM__OPENROUTER_API_KEY` /
`OPENROUTER_API_KEY`, pre-bootstrap fallback only, never auto-imported) →
unconfigured. Any active admin pastes/rotates the key and changes the model
(`:free` enforced server-side, 422 otherwise) from the Memory tab "Platform
extractor" card; key never echoed, all changes audit-logged; decrypt failure
(Fernet drift) falls back to env with a `decrypt-failed` status. Deliverables:
`0009_platform_llm.sql` + SQLite twin, `MemberStore/PgMemberStore`
`get/set/clear_platform_llm` (+meta, memo-cached on PG), `system.py`
`resolve_platform_config/describe_platform_source`, dashboard
`GET/PUT/DELETE /api/system-memory/config` + status `source/model`,
`tests/test_platform_llm.py` (dual-backend contract + endpoints).

## Deliverables (Phase 1)
- `app/config.py`: `SystemLLMSettings` + `Settings.system` + legacy-env backfill.
- `app/llm/system.py`: singleton, `:free` pin, timeout support (`OpenAIProvider` accepts `timeout`).
- `app/memory/llm_extract.py`: `system_extract_facts()` (user-pin, never raises).
- `app/agent/graph.py`: `_relevant_facts` system-first + extraction-source trace.
- `.env.example`: documented `OPENROUTER_API_KEY` / `SYSTEM__*`.
- `tests/test_system_memory.py`: pinning, scope filter (incl. admin/head), fallback, no-key parity, wiring with stub provider.
- This plan doc + `INDEX.md` row.

## Fix (2026-10-04, user report: later turns saved nothing)
- Symptom: first facts saved, then a comparative message
  ("Python so easy vs JS") vanished with no visible cause; an admin "add to
  alice memory" request was silently dropped (by-design user-pin) while the
  assistant falsely confirmed it.
- Root causes: (a) all empty outcomes looked identical — quota/parse errors,
  model `[]`, and no-key fallback shared one `skipped` trace event; (b) the
  prompt told the model to ignore task-adjacent content, so comparative
  opinions were judged non-durable.
- Changes:
  - `graph.py`: three-way trace (`system` / `system-error`+`rate_limited` /
    `system-skipped`+`unconfigured`), server-side `log.warning` on quota hits,
    per-process failure record (`app/memory/system_status.py`).
  - `llm_extract.py`: prompt now names comparative tool/language opinions as
    durable stack signals (live-verified on the exact failing message).
  - Dashboard: `GET /api/system-memory/status` (any active member, no key
    material) + a subtle fallback note atop the Memory tab.
  - Tests: +6 (error/skipped/success trace events, status record/reset,
    endpoint gates). Suite 545 green; live OpenRouter check green.
- Explicit `remember X` / cross-user chat commands still rely on automatic
  extraction (assistant confirmations are chat text, not writes) — open follow-up.
- Misattribution fix (2026-10-04, user report: "User's name is Alice." filed
  in `user/admin` after admin talked *about* Alice): the prompt never scoped
  facts to the speaker. `build_messages` now takes `speaker` (platform path
  always sets it to `claims['sub']`), `SYSTEM_PROMPT` excludes third-party
  mentions and bare meta-requests. Live-verified: Alice-mention → `[]`,
  "add this to memory" → `[]`, genuine self-fact still extracts. +3 tests.

## Risks
- Quota exhaustion (50/day shared): fail-soft to heuristic; 1 call/turn cap; no retries.
- `:free` roster churn / 404: classified error, heuristic fallback, no turn failure.
- Prompt injection via chat text: extractor output constrained to JSON array of short strings, `len<8` dropped, scope pinned, Casbin + editor still gate.
- Multi-instance key drift: same value in every instance's `.env`; never in DB/image (`.env` git-ignored, kept out of Docker image).
- Privacy: only the calling user's last pair is sent; no cross-user context.

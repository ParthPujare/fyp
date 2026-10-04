# Session 030 — Mk16 platform memory extractor (Phase 1) + two live-bug fixes

## Metadata
- Date: 2026-10-04
- Plan / Phase: mk16 — Phase 1 (`plan_mk16_system_memory.md`); Phase 2 (live verify) + auto-approver follow-up open
- Plan step: config + singleton + user-pinned extractor + `reflect` wiring + tests, then two user-reported live fixes
- Scope: platform-owned shared Qwen key for memory extraction; chat stays BYOK-only
- Status: complete offline + live single-turn verified; multi-profile/browser sign-off left to the user
- State at start: Mk15 Phases 0–5 built (session 029); extraction was chat-model/heuristic only via `_relevant_facts`; `OPENROUTER_API_KEY` present in `.env` but read by nothing (`extra="ignore"`)

## Objective
Give every user durable-fact memory with zero setup via one shared OpenRouter free key (`qwen/qwen3.8-27b:free`), pinned to `user/<self>` writes at every role, then fix the two live issues the user found while testing: (1) later turns saved nothing with no visible cause, (2) admin talking *about Alice* filed "User's name is Alice" into admin's own memory.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| start | explore | Subagents mapped memory/LLM/approvals; key findings: `reflect` extracts via `_relevant_facts`, `classify()` can propose central/dept, admin passes `memory_allowed` everywhere, `memory_items` already shared on Neon — so the pin must be a post-filter, not a scope rewrite |
| design | design | "Antura" has zero repo hits — asked user; confirmed natural-language policy file (auto-approver follow-up, separate plan). User locked: user-only auto-write for all roles, sync in-turn + fallback, every turn, last-pair-only, low-confidence → leave pending, separate passphrase |
| implement | implement | `extra="ignore"` drops undeclared env names — a top-level `OPENROUTER_API_KEY` is invisible unless declared as a field; added declared alias + backfill validator (caught only because the live check showed `key present: False` despite a 95-char line in `.env`) |
| implement | implement | Genuine model `[]` ("nothing durable") must NOT retry heuristics, but provider/parse *errors* must — inlined generate+parse in `_relevant_facts` instead of reusing the never-raises helper so the two are distinguishable |
| fix | fix | User's "saved nothing" report: three empty outcomes (quota/parse error, model `[]`, no key) shared one `skipped` trace event — split into `system` / `system-error`+`rate_limited` / `system-skipped`, plus server warning + `system_status` + status endpoint + Memory-tab note |
| fix | fix | "User's name is Alice" in admin memory: prompt never defined *who* the user is — added `speaker` to `build_messages` (platform path sets `claims['sub']`), third-party + meta-request exclusions in `SYSTEM_PROMPT` |
| verify | test | Live OpenRouter checks (single turns, ~5 free calls total): comparative message now extracts; Alice-mention → `[]`; meta-request → `[]`; self-fact still extracts |

## What was done
- `ref_docs/plans/plan_mk16_system_memory.md` (new) + `ref_docs/plans/INDEX.md` Mk16 row
- `app/config.py`: `SystemLLMSettings` + `Settings.system` + `openrouter_api_key` legacy alias + `_backfill_system_key` (canonical `SYSTEM__*` wins)
- `app/llm/system.py` (new): singleton `get_system_provider()`, `:free` pin, `resolve_system_key()`; `OpenAIProvider` accepts `timeout`
- `app/memory/llm_extract.py`: `system_extract_facts()` (user-pin, never raises); `SYSTEM_PROMPT` comparative-affinity + speaker/third-party/meta-request rules; `build_messages(..., speaker=None)` (chat path unchanged)
- `app/agent/graph.py`: `_relevant_facts` system-first with 3-way trace (`system`/`system-error`/`system-skipped`), `_is_rate_limited` + `log.warning`, `system_status.record_failure`
- `app/memory/system_status.py` (new): per-process failure record + `get_status(configured)`
- `app/dashboard/app.py`: `GET /api/system-memory/status` (any active member, no key material)
- `app/dashboard/static/index.html`: `loadMemory()` shows a subtle fallback note (advisory, never breaks the view)
- `.env.example`: platform-key block; `tests/conftest.py` blanks both key names (hermetic suite)
- `tests/test_system_memory.py` (new, 23 tests): pinning, scope filter incl. admin/head, fallback, no-key parity, wiring, 3-way trace events, status record/reset, endpoint gates, speaker scoping
- This file + `INDEX.md` row; plan doc carries both fix records

## Decisions locked in this session
- Chat resolution (`get_provider_for_user`) untouched — platform key is extraction-only, never BYOK.
- Auto-write is `user/<self>` for every role (admin/head included); central/dept/other-user stay manual/HITL.
- Each laptop keeps the same key value in its own `.env` (never synced via DB/image); quota is shared at the OpenRouter account.
- Genuine `[]` = model decided (no retry); errors → chat-model/heuristic fallback; turn never fails.
- Auto-approver (NL policy file, passphrase, enable toggle) is a separate follow-up plan, not Mk16.

## Verification performed
- `uv run pytest`: **548 passed, 149 skipped** (Postgres-only skips); `ruff check app tests`: clean.
- Live (user's `.env` key, one call each): Python-vs-JS comparative → `user/admin@acme.com` fact; Alice-mention → `[]`; "add this to memory" → `[]`; self-fact → extracted.
- NOT verified: multi-profile browser walkthrough, key-removal fallback, second-device convergence (test steps handed to user).

## Blocker / open questions
- Assistant falsely confirms memory writes in replies ("now part of our context") — no write tool exists in chat; explicit `remember X` / cross-user admin commands need a scoped write path or a reply guardrail (deferred to auto-approver follow-up).
- User must manually Delete the two wrong facts already stored under `user/admin@acme.com` ("User's name is Alice.", "User prefers Slack over Teams.").

## Next step (for the next session)
- Mk16 Phase 2: user runs the multi-profile + key-removal + second-device checks; then the autonomous-admin follow-up plan (NL policy store, passphrase, worker, UI).

## Handoff notes
- Free-tier quota (~50/day shared) is the binding constraint — keep live checks to single turns.
- `get_settings()` is `lru_cache`d; `system_status` is per-process memory — both matter for multi-instance reasoning.
- conftest blanks `OPENROUTER_API_KEY`/`SYSTEM__OPENROUTER_API_KEY`; live key only loads outside pytest.

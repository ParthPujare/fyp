# Session 021 — Mk9 live-debug to first real Gmail success

## Metadata
- Date: 2026-09-13
- Plan / Phase: mk9 — follow-up (live-debug + simplify after Phases 0–6)
- Plan step: Mk9 follow-up — non-tech setup, flexible catalog, first real-tool success
- Scope: Fix Composio Connect end-to-end (fake-URL 404 → real OAuth → version error → timeout) + simplify setup UX
- Status: complete
- State at start: Mk9 Phases 0–6 committed (hybrid layer, Gmail+GitHub MVP, per-user BYO key, dashboard APIs but no Integrations UI section); `composio` package not installed; `LLM__FERNET_KEY` empty; `link()` used wrong v3 signature with fake-URL fallback

## Objective
Make Connect work for a real user (`patelmukeah155@gmail.com`, personal Composio key) with zero `.env`/ID pasting, any admin-added toolkit, and chat reaching the real mailbox.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| start | explore | Backend routes existed but Account tab had no Integrations section — UI never added |
| design | Q&A locked hybrid / Gmail+GitHub MVP / per-user BYO / full enforcement / connect+chat-use |
| implement | spike confirmed slugs `GMAIL_FETCH_EMAILS`, `GMAIL_SEND_EMAIL`, `GITHUB_CREATE_ISSUE` from Composio docs |
| implement | `composio-core` pulls pillow → jpeg build failure on macOS; made SDK optional (lazy import + Fake), later pinned light `composio>=0.21.0` which installs clean |
| fix | Connect 404 "moved or deleted" = `link(app_name,user_id)` wrong signature → fell through to fake `composio.dev/connect/...` URL; v3 needs `link(user_id, auth_config_id)` |
| fix | Dashboard-only connect can't work: key is per-project, execution uses `user_id=sub`, local status table is source of truth — must align User ID = member mail |
| design | User: setup too complicated + gmail/github-only. Q&A locked: per-user key, admin-curated catalog, auto-provision auth configs, inline chat Connect |
| implement | Built `toolkit_catalog` + `toolkit_tools` tables, `ensure_auth_config`, discovery cache, generic executor, `connect_required` in gateway chat envelope, admin catalog endpoints, policy auto-rows |
| fix | Read installed SDK signatures: `auth_configs.list` takes `toolkit_slug` (not `toolkit`); `create(toolkit, options)` (not `toolkit=, auth_config=`); discovery must use `get_raw_composio_tools` (provider-format `tools.get` has no slugs → "0 tools"); `toolkit` refs are objects with `.slug` |
| fix | `Auth_Config_NotFound` 404 = stale `.env` ID from another project; added self-heal (re-provision + retry once) + friendly 502 |
| fix | Key-save 503 = empty `LLM__FERNET_KEY`; generated + set in `.env` (never rotate — invalidates stored keys) |
| fix | Real Gmail call failed: "Toolkit version not specified ('latest' unsupported)" (confirmed in `data/trace.sqlite` TOOL_CALL event); `execute()` now resolves pinned version via `get_raw_composio_tool_by_slug`, fallback `dangerously_skip_version_check=True` |
| fix | Chat timed out (dashboard→gateway 60s) while slow Gemma did agent + memory-extract calls; raised forward timeout to 300s, heuristic-only memory extract for `lmstudio/ollama/llamacpp` |
| verify | First real success: `gmail_fetch` returned user's DeepLearning.AI mail; pending→active Refresh flow works |

## What was done
- `app/integrations/client.py`: real `ComposioClient` (validate_key, toolkit_exists, ensure_auth_config, discover_tools via raw API, version-pinned execute, `_toolkit_slug` normalizer) + `FakeComposioClient` offline
- `app/integrations/service.py`: catalog-driven `visible_external_tools`, generic `execute_external` (cache-first, seed fallback), `connect_toolkit_for`
- `app/integrations/adapter.py`: `risk_for_slug`/`action_for_slug`/`spec_from_discovered` + 4 seed specs
- `app/integrations/vault.py`, `direct.py`, `config.py`: per-user BYO vault, direct REST, `ComposioSettings` (+ per-app auth-config IDs, legacy)
- `app/auth/store.py`: `member_composio_keys`, `connected_accounts`, `toolkit_catalog`, `toolkit_tools` + methods
- `app/dashboard/app.py`: key wizard with health check, connect (guard + auto-provision + 404 self-heal), refresh sync, disconnect, admin catalog add/toggle, `_ensure_toolkit_policy_rows`
- `app/dashboard/static/index.html`: Integrations section (wizard copy, search, Refresh, admin add, inline chat Connect card via `connect_required`)
- `app/dashboard/static/composio-setup.html`: rewritten 3-click non-tech guide
- `app/gateway/app.py`: `connect_required` in `/chat` envelope
- `app/agent/graph.py`: external branch in `_execute_one`; heuristic memory extract for local providers
- `app/agent/runner.py`: merge external schemas into LLM context
- `app/harness/policies/policy.csv`: gmail/github + skill rows + auto-rows per toolkit
- `pyproject.toml`/`uv.lock`: `composio>=0.21.0`; `.env`: created from example + Fernet key generated
- `tests/test_integrations.py` (14 tests), `tests/attack/test_external_deny.py` (A9), `scripts/demo_external.py`, `make demo-external`

## Decisions locked in this session
- Per-user BYO Composio key only; `entity_id = claims[sub]` (member mail) everywhere — no mapping table
- Admin curates toolkit catalog; auth configs auto-provisioned via API, IDs in DB (`.env` IDs legacy fallback only)
- Dynamic tool discovery cached in DB; risk by slug hints (SEND/DELETE→critical HITL); writes always HITL-gated
- Manual Refresh (user's choice) + inline chat Connect card with re-send hint
- `composio` (not `composio-core`) is the pinned SDK — no native build deps

## Verification performed
- `uv run pytest` → 338 passed; `ruff check app tests scripts` clean
- `make demo-external` → allowed fetch + cross-dept DENY + HITL suspend + trace integrity OK
- Live: `patelmukeah155@gmail.com` key saved → Connect → Refresh → `active` → real `gmail_fetch` returned inbox mail (DeepLearning.AI, Sep 10)

## Blocker / open questions
- Local Gemma slowness: turns take minutes; 300s forward timeout is a band-aid — consider async/chat polling later
- Discovered dynamic tool names (`gmail_gmail_fetch_emails`) vs seed names (`gmail_fetch`) both visible — dedupe/alias pass open
- Outlook/Canva onboarding untested (same adapter path, needs admin add)

## Next step (for the next session)
- Mk9 hardening: streaming/async chat for slow local models; dynamic-vs-seed tool dedupe; record web demo (Mk2 Phase 6 still open)

## Handoff notes
- Never rotate `LLM__FERNET_KEY` in `.env` — invalidates all stored Composio/LLM keys
- If Connect 404s on auth config: ID is from another project/deleted — self-heal provisions fresh; check Composio project switcher
- `data/trace.sqlite` TOOL_CALL events hold raw Composio errors — first place to look when chat misbehaves
- Commit `a808687` ("composio connectivity added") holds all of the above; working tree clean

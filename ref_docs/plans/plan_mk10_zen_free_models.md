# Plan Mk10 — OpenCode Zen Free Models (BYOK) + Model Dropdown

## Metadata
- Date: 2026-09-13
- Status: In Progress (Phase 1 implementation)
- Follows: Mk3 (BYOK keys), Mk5 (direct providers), Mk7 (hardening)
- Scope lock (user, 2026-09-13):
  1. Allow any Zen model IDs (no allow-list reject); free IDs must work without issue.
  2. Default model `muse-spark-1.3-contributor-free`; others selectable.
  3. Per-user BYOK only — no global `LLM__*` fallback changes.
  4. User holds a free-tier Zen key (no billing) for live verify.
  5. Model dropdown (pick-or-type) with defaults per provider; custom IDs still allowed as today.

## Problem
- BYOK supports openai/gemini/anthropic/groq/openrouter/custom_openai, all via
  `chat.completions` (or native equivalents). Zen free models span two APIs:
  chat-family on `https://opencode.ai/zen/v1/chat/completions` and
  responses-family on `https://opencode.ai/zen/v1/responses` (`muse-spark-*`).
- `CustomOpenAICompatibleProvider` only speaks chat-completions, so
  `muse-spark-1.3-contributor-free` cannot work through it.
- Account tab model field is free-text only; no discoverable defaults per provider.

## Free IDs (live GET /zen/v1/models, 2026-09-13)
- `muse-spark-1.3-contributor-free` (responses) — default
- `muse-spark-1.2-contributor-free` (responses)
- `big-pickle`, `mimo-v2.5-free`, `ling-3.0-flash-fin-free`,
  `nemotron-3-ultra-free`, `nemotron-3.5-lightning-free`,
  `deepseek-v4-flash-free` (chat-completions)
- `big-pickle` carries no `-free` suffix but is free per Zen pricing table.

## Design
- Single registry key `zen` (alias `opencode` → `zen`), `BASE_URL=https://opencode.ai/zen/v1`.
- `ZenChatProvider(OpenAIProvider)` for chat-family; `ZenResponsesProvider(LLMProvider)`
  via `client.responses.create` for responses-family; dispatcher `ZenProvider`
  picks by model ID (muse-spark* → responses, else chat). Unknown future IDs allowed.
- Per-user BYOK only: `get_provider_for_user()` path; `base_url` optional for `zen`
  (default applied). No `app/config.py` default change.
- UI: `<input list=datalist>` combo — `MODEL_OPTIONS[provider]` defaults,
  custom typing preserved. Privacy note for `contributor-free` (trains Meta models).

## Phases
| Phase | Scope | Status |
|-------|-------|--------|
| 0 | Probe SDK + endpoint matrix with user key | Done (SDK check; live probe = user step) |
| 1 | `zen_provider.py` + registry/factory/dashboard wiring + UI dropdown + tests | DONE (314 tests green, ruff clean) |
| 2 | Live verify all 7 free IDs via per-user chat + tool turn; free-tier 429 handling | Open |

## Deliverables (Phase 1)
- `app/llm/zen_provider.py`, `registry.PROVIDERS["zen"]`, factory prefix map,
  dashboard `_normalize_model` + `_validated_key_fields` zen path.
- Account tab provider option + `MODEL_OPTIONS` datalist + tip/privacy note.
- `tests/test_llm_zen.py` + e2e BYOK zen cases; `pytest -q` green, `ruff` clean.

## Risks
- Responses tool schema may differ; fallback = text-only with warning.
- Free-tier quota/429 on unverified billing; gateway already surfaces 502 + sanitized upstream.
- `openai` SDK version must expose `client.responses` (check `>=1.66`).

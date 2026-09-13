# Session 019 — OpenCode Zen Free Models + Model Dropdown

## Metadata
- Date: 2026-09-13
- Plan / Phase: mk10 — Phase 1
- Plan step: `zen_provider.py` + registry/factory/dashboard wiring + UI dropdown + tests
- Scope: First-class BYOK support for OpenCode Zen models (free-first, open IDs) + pick-or-type model dropdown in the Account tab
- Status: complete (Phase 1; Phase 2 = live verify with user's free-tier Zen key)
- State at start: Mk7 Phase 1 done; providers openai/gemini/anthropic/groq/openrouter/custom_openai all chat-completions-only; Account tab model field free-text only

## Objective
Implement Plan Mk10 Phase 1: a `zen` provider that routes free Zen IDs to the correct endpoint (`/responses` for `muse-spark-*`, `/chat/completions` for the rest), allows any model ID, defaults to `muse-spark-1.3-contributor-free`, per-user BYOK only — plus a model dropdown with defaults that still permits custom IDs.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 09:00 | explore | Read `registry.py`, `factory.py`, `openai/custom/openrouter` providers, dashboard `_validated_key_fields`/`_normalize_model`, Account-tab JS; confirmed no Responses-API path exists anywhere. |
| 09:15 | explore | Fetched live Zen docs + `GET /zen/v1/models`: 8 free IDs; endpoint table maps `muse-spark-1.3` → `/v1/responses` (`@ai-sdk/openai`) while deepseek/glm/kimi/minimax/big-pickle → `/v1/chat/completions`. `custom_openai` can never serve Muse-free. |
| 09:25 | design | Single `zen` registry key (alias `opencode`→`zen`) with a dispatcher: responses-family (`muse-spark*`, `gpt-*`, `grok-*`) vs chat-family (everything else). Open IDs — no allow-list reject, per user lock. |
| 09:35 | implement | Built `ZenProvider(OpenAIProvider)`: chat path reuses OpenAI formatting; `_format_responses_input` replays harness history as `function_call`/`function_call_output` items with synthesized call ids + unanswered-call drop; `_parse_responses_output` reads `output_text` + function calls defensively. |
| 09:50 | implement | Wired registry/factory, `zen` default base URL fill in validation, `zen`+`opencode/` prefix strip; Account tab `zen` option + `MODEL_OPTIONS` datalist (8 free IDs) via `<input list>` combo + contributor-training privacy note. |
| 10:05 | test | `openai` SDK is 2.53 — `client.responses.create` present. Wrote `tests/test_llm_zen.py` (12 tests: routing, both paths, tool round-trips, prefix strip, keyless-less BYOK save defaulting base URL, open IDs). Full suite + ruff green. |

## What was done
- `app/llm/zen_provider.py` (new): `ZenProvider` dispatcher, `BASE_URL=https://opencode.ai/zen/v1`, `DEFAULT_MODEL=muse-spark-1.3-contributor-free`, `ZEN_FREE_MODELS`, `is_responses_model`.
- `app/llm/registry.py`: `PROVIDERS["zen"]`, aliases `opencode/opencode_zen/opencode-zen` → `zen`.
- `app/llm/factory.py`: `_MODEL_PREFIX_PROVIDERS` += `zen`, `opencode`.
- `app/dashboard/app.py`: `_normalize_model` strips `zen`/`opencode` prefixes; `_validated_key_fields` defaults empty `base_url` to Zen base.
- `app/dashboard/static/index.html`: `zen` provider option, `MODEL_PRESETS`/`MODEL_OPTIONS` + `syncModelList()` datalist combo, contributor-free privacy note, tip update.
- `tests/test_llm_zen.py` (new, 12 tests); `ref_docs/plans/plan_mk10_zen_free_models.md` + registry row.

## Decisions locked in this session
- **Dispatcher, not two registry keys**: one `zen` provider picks the endpoint by model id so free IDs "just work"; unknown future IDs allowed (responses for known responses prefixes, chat otherwise).
- **Open IDs, Muse-free default**: no allow-list 422; default `muse-spark-1.3-contributor-free`, others selectable.
- **Per-user BYOK only**: no `app/config.py` global-default change; `get_provider_for_user()` path unchanged.
- **Pick-or-type dropdown**: `<input list=datalist>` preserves the current custom-ID flow while making defaults discoverable.

## Verification performed
- `uv run pytest tests/test_llm_zen.py` → 12 passed; full `pytest` → 314 passed; `ruff check app/ tests/` → clean.
- `openai` 2.53 `responses` attribute confirmed present.

## Blocker / open questions
- Live verify (Phase 2) needs the user's free-tier Zen key against real `/chat` + tool turns; free-tier 429s possible.
- Correction (learned next session): Zen free models are NOT usable outside OpenCode via API — Phase 2 live-verify may fail with auth/billing errors; Mk11 (local models) is the fallback path.

## Next step (for the next session)
- Plan Mk10 Phase 2: live-verify free IDs with the user's Zen key; on failure, pivot to local providers (became Mk11).

## Handoff notes
- `ZenProvider.chat()` branches per call on the resolved model name, so a per-call model override still hits the right endpoint.
- Responses `input` uses `function_call`/`function_call_output` items; call ids are synthesized (`zen_call_N`) since the harness stores bare `{"name","args"}`.

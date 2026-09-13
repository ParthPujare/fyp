# Session 020 — Local Models via LM Studio (+ Ollama / llama.cpp)

## Metadata
- Date: 2026-09-13
- Plan / Phase: mk11 — Phase 1
- Plan step: Providers + registry/factory/validation + tool error + discovery endpoint + UI + tests
- Scope: First-class per-user BYOK for LM Studio (keyless, loaded-model detection, clear tool-unsupported error); Ollama/llama.cpp as untested alternates
- Status: complete (Phase 1; Phase 2 = live verify against the user's running LM Studio)
- State at start: Mk10 Phase 1 done (322-suite baseline 314); `custom_openai` requires both key and base_url, so keyless LM Studio is awkward; no `/v1/models` discovery; tool 400s surface generically

## Objective
Implement Plan Mk11 Phase 1 after the user's research correction (Zen free models not usable outside OpenCode): LM Studio as a first-class provider with no-key saves, a Detect button listing loaded models (incl. multiples), an explicit "model does not support tool calling" error instead of silent fallback, and Ollama/llama.cpp presets.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 10:15 | explore | Read `custom_provider.py`, `openai_provider.py` chat path, `_validated_key_fields` (key mandatory for ALL providers), Account-tab JS; confirmed 3 gaps: mandatory key, no default base URL, no discovery. |
| 10:20 | explore | LM Studio docs: base `http://localhost:1234/v1`, `GET /v1/models`, `POST /v1/chat/completions` (+ `/v1/responses` newer), any non-empty key or none, any non-empty model string routes to loaded model; tools supported on newer builds. |
| 10:30 | design | Three thin `OpenAIProvider` subclasses (pattern matches `groq`); empty key → `lm-studio` placeholder at build; `GET /api/account/local-models` proxies `/v1/models` (5s timeout, 502 on down server); tool-400 → named error via `tool_unsupported_error()` in `base.py`. |
| 10:40 | implement | Wrote `lmstudio_provider.py` (3 classes + `LOCAL_PROVIDERS`/`LOCAL_BASE_URLS`), registry/factory/aliases, validation local path (key/base defaults), prefix strip, OpenAI + Zen Responses except-paths wired to the helper, discovery endpoint, UI (options, datalist presets, base-url row + Detect button, optional-key hint). |
| 11:00 | test | Wrote `tests/test_llm_local.py` (8 tests); fixed 2 ruff import-sort errors; full suite green. |

## What was done
- `app/llm/lmstudio_provider.py` (new): `LmStudioProvider` (`http://localhost:1234/v1`), `OllamaProvider` (`:11434/v1`), `LlamaCppProvider` (`:8080/v1`), `DUMMY_API_KEY`, `LOCAL_PROVIDERS`, `LOCAL_BASE_URLS`.
- `app/llm/registry.py`: 3 providers + `lm_studio`/`lm-studio`/`llama.cpp`/`llama_cpp` aliases; `app/llm/factory.py`: prefix map.
- `app/llm/base.py`: `tool_unsupported_error()` (tools-sent + tool/function 400 keywords → named error); `openai_provider.py` + `zen_provider.py` except-paths use it.
- `app/dashboard/app.py`: local path in `_validated_key_fields` (model required, key/base defaulted), prefix strip, `GET /api/account/local-models` discovery endpoint.
- `app/dashboard/static/index.html`: 3 provider options, presets/datalist entries, `syncBaseUrlField` (local shows row + prefill + key placeholder + Detect toggle), `detectLocalModels()`, tip update.
- `tests/test_llm_local.py` (new, 8 tests); `ref_docs/plans/plan_mk11_lmstudio_local.md` + registry row.

## Decisions locked in this session
- **Explicit tool error, no silent fallback** (user lock): tool-rejected requests raise `… does not support tool calling … [provider, model]`; the chat 502 detail carries it to the UI.
- **Shared helper, not local-only**: the check lives in the OpenAI-compatible path so cloud models benefit too.
- **Ollama/llama.cpp untested alternates**: same code shape, default ports editable, no live verification claimed.
- **No timeout change**: local latency vs `call_timeout_seconds=30` is a documented knob, not code.

## Verification performed
- `uv run pytest tests/test_llm_local.py tests/test_llm_zen.py` → 20 passed; full `pytest` → **322 passed**; `ruff check app/ tests/` → clean.

## Blocker / open questions
- Phase 2 needs the user's LM Studio actually serving (server on, model loaded): save key → Detect → chat + tool turn.
- Actual loaded model ids unknown — `MODEL_PRESETS` for local providers is empty until the user confirms ids (Detect fills them at runtime).

## Next step (for the next session)
- Plan Mk11 Phase 2: live-verify against the user's LM Studio (keyless save, Detect lists loaded models, chat + tool turn on a tool-capable model, clean unsupported-error on a non-tool model).

## Handoff notes
- `DUMMY_API_KEY` is stored encrypted like a real key; it is a placeholder, never a credential — do not "redact harder" or rotate it.
- Discovery endpoint is member-JWT gated and takes an optional `base_url` override so non-default ports work without a code change.

# Plan Mk11 — Local Models via LM Studio (+ Ollama / llama.cpp alternates)

## Metadata
- Date: 2026-09-13
- Status: Phase 1 DONE (session 020); Phase 2 (live verify) open
- Follows: Mk3 (BYOK keys), Mk5 (direct providers), Mk10 (zen provider + model dropdown)
- Scope lock (user, 2026-09-13):
  1. First-class LM Studio support (user has LM Studio running).
  2. Tool calling: if the loaded model supports tools, tools work; if not, the
     user gets a clear error naming tool-calling as unsupported (no silent fallback).
  3. Detect loaded models (multiple) — UI offers what the server reports.
  4. Ollama + llama.cpp as alternate providers, same pattern, untested/optional.

## Problem
- `custom_openai` technically reaches LM Studio, but: `api_key` is mandatory
  (`_validated_key_fields` rejects empty keys) while LM Studio needs none;
  no default `base_url`; model ids must be hand-copied; no discovery.
- Local models vary in tool support; a 400 on `tools` currently surfaces as a
  generic provider error with no actionable hint.

## Facts (LM Studio docs)
- Base URL `http://localhost:1234/v1`; `GET /v1/models` lists loaded ids;
  `POST /v1/chat/completions` (+ `/v1/responses` on newer builds); any
  non-empty `api_key` (e.g. `lm-studio`) or none when auth is off.
- Ollama OpenAI-compat: `http://localhost:11434/v1`; llama.cpp server: `http://localhost:8080/v1`.

## Design
- Three thin `OpenAIProvider` subclasses (`LmStudioProvider`, `OllamaProvider`,
  `LlamaCppProvider`) with `PROVIDER_NAME`, `BASE_URL`, `ROUTING_PREFIXES`,
  and keyless support (empty key → harmless placeholder at build time).
- Per-user BYOK only; `base_url` optional (provider default applied).
- Tool-unsupported: when a request that sent `tools` fails with a
  tool/function-related API error, raise `... does not support tool calling ...`
  with `[provider, model]` context (shared `OpenAIProvider` path, so cloud
  models benefit too; `ZenProvider` chat path reuses it).
- Discovery: `GET /api/account/local-models?provider=&base_url=` (member JWT)
  proxies `GET {base}/v1/models` (5s timeout) → `{models: [ids]}`; UI "Detect"
  button fills the model datalist; free-typing always preserved.

## Phases
| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Providers + registry/factory/validation + tool error + discovery endpoint + UI + tests | DONE (322 tests green, ruff clean) |
| 2 | Live verify against user's LM Studio (chat + tool turn on loaded model) | Open |

## Deliverables (Phase 1)
- `app/llm/lmstudio_provider.py` (+ ollama/llamacpp), registry/factory wiring,
  `_normalize_model` + `_validated_key_fields` local path.
- Tool-calling unsupported error in the OpenAI-compatible path.
- Discovery endpoint + Account-tab Detect button + provider options/presets.
- `tests/test_llm_local.py`; `pytest -q` green, `ruff` clean.

## Risks
- Local inference latency vs `call_timeout_seconds=30` — document knob, no code change.
- Untested ollama/llamacpp defaults (ports) may differ per install — base_url editable.

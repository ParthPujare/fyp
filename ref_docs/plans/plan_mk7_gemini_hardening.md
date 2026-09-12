# Plan Mk7 — Gemini Provider Hardening + Editable BYOK Key Models

## Metadata
- Date: 2026-09-12
- Status: In Progress (Phase 1 done — session 018)
- Follows: Mk3 (BYOK keys), Mk5 (direct providers), Mk6 (google-genai SDK)

## Problem
1. Gemini chat failed with `502 Bad Gateway` (terminal) + `LLM provider model not found (404)` (UI) for model `gemini-2.5-flash`.
2. Saved BYOK keys had a fixed model string — no way to edit the model without re-entering the API key (keys are encrypted at rest, never shown back).
3. The classifier discarded Google's upstream error text, hiding the real cause; the gateway 502 masked it further.
4. Hardcoded model allow-lists / stale defaults (`gemini-1.5-flash`, `gemini-2.0-flash`) conflict with live API deprecations (https://ai.google.dev/gemini-api/docs/deprecations).

## Root causes found (live reproduction, session 018)
- `models/gemini-2.5-flash` is gated for **new** API users: `404 NOT_FOUND … no longer available to new users … use models/gemini-3.6-flash`.
- The stored `AQ…` key's project is denied *generation* on every model tried (`403 project has been denied access`), while `models.list` succeeds — a project/key-side restriction no model-name change can fix.

## Phases
| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Editable key models (store + PATCH API + UI inline edit); contents pre-flight guard; error transparency (sanitized upstream detail); defaults → `gemini-3.6-flash`; tests | DONE (session 018) |
| 2 | User replaces the restricted key with a healthy `AIza…` key; verify live chat; optional auto-fallback on 404-gated models | Open |

## Deliverables (Phase 1)
- `MemberStore.update_llm_key` + `PATCH /api/account/llm-keys/{key_id}` (+ admin variant) + UI inline Edit/Save/Cancel.
- `classify_llm_error` preserves redacted upstream detail; regression test for hint surfacing + key redaction.
- All Gemini defaults/presets/tests on `gemini-3.6-flash`; no `gemini-1.5`/`2.0-flash` strings in `app/` or `tests/`.

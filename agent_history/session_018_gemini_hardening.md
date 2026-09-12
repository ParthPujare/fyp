# Session 018 — Gemini Provider Hardening + Editable Key Models

## Metadata
- Date: 2026-09-12
- Plan / Phase: mk7 — Phase 1
- Plan step: Editable key models; contents guard; error transparency; defaults → `gemini-3.6-flash`
- Scope: Fix Gemini 502/404 chat failure + allow editing a saved key's model without re-entering the API key
- Status: complete (Phase 1; Phase 2 = user replaces restricted key + live verify)
- State at start: 299 tests green (session 017); `GeminiProvider` on `google-genai` SDK 2.22.0 with `DEFAULT_MODEL = gemini-2.0-flash`; no key-model edit path; `classify_llm_error` discarded upstream detail

## Objective
Implement Plan Mk7 Phase 1: root-cause the `502 Bad Gateway` / `model not found (404) [provider=gemini, model=gemini-2.5-flash]` failure against the live API, make saved key models editable, and move all Gemini defaults to non-deprecated models.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 09:00 | explore | Read `gemini_provider.py`, dashboard key routes, `index.html` Account tab, `MemberStore` key CRUD; confirmed no update path exists (keys encrypted at rest, never shown back). |
| 09:20 | design | Planned: `update_llm_key` store method + `PATCH` member/admin endpoints + UI inline edit; fix tool-result role mapping (`functionResponse` under `user`); contents pre-flight guard. |
| 09:40 | implement | Added store update with `UNIQUE(username, provider, model)` conflict handling + audit; `LLMKeyUpdateRequest` + both PATCH routes with `_normalize_model`; UI Edit/Save/Cancel + updated model tips. |
| 10:00 | test | 27 LLM/key tests green, ruff clean; then user reported `AQ…` vs `AIza…` key-format concern — verified SDK 2.22 sends any key verbatim as `x-goog-api-key`, so format is not the blocker. |
| 10:20 | fix | Found message-format bug: plain-dict messages built `parts` but never appended to `contents` (all `if/elif` branches required `Message`) → added `else` fallback; also added (then later removed) a free-tier model gate. |
| 10:40 | explore | User supplied free-tier model context; cross-checked https://ai.google.dev/gemini-api/docs/deprecations — `1.5-*` and `2.0-flash` retired; corrected course to current models. |
| 11:00 | implement | Removed the rigid `FREE_TIER_COMPATIBLE_MODELS` gate (it caused 404s on valid models); defaults → `gemini-2.5-flash` across provider, UI, `app/config.py`, `.env.example`, tests. |
| 11:20 | explore | User still saw 404 for `gemini-2.5-flash`; reproduced LIVE with the stored key: `models.list` shows 55 models incl. `gemini-2.5-flash`, but `generate_content` returns `404 … no longer available to new users … use models/gemini-3.6-flash`. Model ID valid, gated per-account. |
| 11:30 | explore | Swept 3.x models live: all `403 project has been denied access` — this key/project cannot *generate* with any model (list works). No code change can fix that; user needs a healthy `AIza…` key. |
| 11:40 | implement | Error-transparency fix: `classify_llm_error` now appends sanitized upstream detail (key patterns redacted, 400 chars) + regression test; defaults/presets/tests → `gemini-3.6-flash` (Google's own stated replacement); migrated nothing server-side that couldn't help. |
| 11:50 | test | 39 passed (`test_llm`, `test_llm_keys`, `test_llm_keys_e2e`, `test_chat`), ruff clean; end-to-end `get_provider_for_user → GeminiProvider.chat → live API` verified working path shape (reply + usage) once a usable model/key is set. |

## What was done
- `app/auth/store.py`: added `update_llm_key(username, key_id, model, base_url, active)` with conflict detection + audit.
- `app/dashboard/app.py`: added `LLMKeyUpdateRequest` + `PATCH /api/account/llm-keys/{key_id}` and `PATCH /api/members/{username}/llm-keys/{key_id}`.
- `app/dashboard/static/index.html`: inline Edit/Save/Cancel on key rows; presets + tips now `gemini-3.6-flash` / `gemini-3.5-flash-lite`.
- `app/llm/gemini_provider.py`: `DEFAULT_MODEL = gemini-3.6-flash`; removed tier gate; fixed dict-message `contents` bug; tool results as `functionResponse` under `user`.
- `app/llm/base.py`: `_sanitize_upstream` + `_upstream_detail` — classified errors now carry the redacted upstream cause.
- `app/config.py`, `.env.example`: default `gemini/gemini-3.6-flash`.
- `tests/test_llm.py`, `tests/test_llm_keys_e2e.py`: updated to `gemini-3.6-flash`; new test for upstream-detail preservation + key redaction.
- `ref_docs/plans/plan_mk7_gemini_hardening.md`: new plan doc + registry row.

## Decisions locked in this session
- **No client-side model allow-lists**: the SDK forwards the user-selected model; Google validates. Hardcoded gates caused 404s on valid models.
- **Error messages carry sanitized upstream text**: generic labels hid two distinct live causes (per-account 404 gating vs project-level 403 denial).
- **Default = `gemini-3.6-flash`**: Google's own error text names it as the `2.5-flash` replacement; stable per deprecations page; visible to keys via `models.list`.

## Verification performed
- `pytest tests/test_llm.py tests/test_llm_keys.py tests/test_llm_keys_e2e.py tests/test_chat.py` → **39 passed**.
- `ruff check` on touched files → clean.
- Live: `models.list` (55 models), minimal `generate_content` reproductions (404-gating + 403-denial captured verbatim, keys redacted), end-to-end factory→provider chat shape verified.

## Blocker / open questions
- The stored `AQ…` key's project is denied generation on ALL models (403). Needs a fresh `AIza…` key from a healthy project with the Gemini API enabled. If 403 persists there → Google support.
- Open: auto-fallback retry (404-gated model → default once) vs explicit error. Currently explicit (user preferred explicit so far — confirm in Phase 2).

## Next step (for the next session)
- Plan Mk7 Phase 2: user saves a healthy key, set model `gemini-3.6-flash`, verify live `/chat` end-to-end; decide on auto-fallback.

## Handoff notes
- Never reintroduce a hardcoded Gemini model allow-list; deprecations move fast — check https://ai.google.dev/gemini-api/docs/deprecations before changing defaults.
- `classify_llm_error` output format changed (appended `(upstream: …)`); tests assert substrings so they tolerate it — keep it that way.
- The `AQ…` (53-char) stored key is project-restricted, not malformed; SDK sends it verbatim as `x-goog-api-key`.

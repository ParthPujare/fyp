# Session 015 — Plan Mk3: BYOK LLM Keys (Phases 1–3 Complete)

## Metadata
- Date: 2026-09-04
- Plan / Phase: mk3 — Phases 1–3 (`ref_docs/plans/plan_mk3_byok_llm_keys.md`)
- Plan step: Phases 1–3 — Crypto + store layer, per-user provider routing, dashboard API & UI Account tab + e2e
- Scope: Implemented per-user encrypted LLM API keys (BYOK) so every member can chat with a real model through their own provider key (encrypted at rest via Fernet, admin visibility limited to active provider/model status, offline mock fallback preserved).
- Status: complete
- State at start: Mk2 completed through session 014 (268 tests green). Chat ran on global LLM settings.

## Objective
Implement Plan Mk3 (Bring Your Own Key): encrypted per-user API key storage in `MemberStore`, per-user provider resolution (`get_provider_for_user`), classified 502 error handling for LLM provider failures, and dashboard UI Account tab + admin member key management.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 10:00 | design | Add `cryptography` dependency and `LLM__FERNET_KEY` config. Design `member_llm_keys` table in `data/members.sqlite` managed by `MemberStore`. |
| 10:30 | implement | Created `app/auth/crypto.py` for Fernet encryption/decryption with proper 503/502 error handling. Added `member_llm_keys` table and CRUD methods (`list_llm_keys`, `set_llm_key`, `activate_llm_key`, `delete_llm_key`, `get_active_llm_key`) to `MemberStore` in `app/auth/store.py` with audit logging. Wrote unit tests in `tests/test_llm_keys.py`. |
| 11:15 | implement | Implemented `get_provider_for_user` in `app/llm/factory.py` (precedence: user active key > global provider config > MockLLM). Updated `_build_deps` in `app/agent/runner.py` to resolve provider per calling user. Added LiteLLM error classification (`classify_llm_error`) and try-except error run tracing in `run_agent` and `resume_agent`. |
| 12:00 | implement | Added dashboard member API endpoints (`/api/account/llm-keys*`) and admin endpoints (`/api/members/{username}/llm-keys*`), plus `active_llm_key` status metadata in `/api/users`. Added the Account tab and UI management in `app/dashboard/static/index.html`. Wrote e2e tests in `tests/test_llm_keys_e2e.py`. |
| 12:30 | verify | Fixed store resolution in `get_provider_for_user` to use `get_store()`. Ran `uv run pytest` (**275 tests passed**) and `uv run ruff check` (clean). |

## What was done
- `pyproject.toml`, `app/config.py`, `.env.example`: added `cryptography` dependency and `LLM__FERNET_KEY` setting.
- `app/auth/crypto.py`: NEW Fernet encryption/decryption helpers.
- `app/auth/store.py`: added `member_llm_keys` table + key CRUD methods + audit logging.
- `app/llm/factory.py`: added `get_provider_for_user` resolution chain + error classification (`classify_llm_error`).
- `app/llm/litellm.py`: wrapped completion calls with error classification.
- `app/agent/runner.py`: wired per-user provider resolution and errored-run tracing in `run_agent` / `resume_agent`.
- `app/dashboard/app.py`: added account key endpoints, admin member key endpoints, and `active_llm_key` status in `/api/users`.
- `app/dashboard/static/index.html`: added **Account** tab for managing API keys.
- `tests/test_llm_keys.py` & `tests/test_llm_keys_e2e.py`: unit and e2e test suites for BYOK keys.
- `ref_docs/plans/plan_mk3_byok_llm_keys.md` & `ref_docs/plans/INDEX.md`: updated status to Phases 1–3 DONE.

## Decisions locked in this session
- **Fernet encryption at rest**: keys are encrypted using `cryptography.fernet.Fernet` driven by `LLM__FERNET_KEY`. Decrypted keys are used server-side only and never returned in API responses or logs.
- **Precedence chain**: User active key > global provider config > MockLLM (offline demo preserved).
- **Admin visibility**: Admins see active provider/model metadata status only — never raw key material.
- **Error classification**: LiteLLM authentication/rate-limit/timeout errors map to clean HTTP 502 responses with sanitized detail strings.

## Verification performed
- `uv run pytest` → **275 passed** (268 existing + 7 new key/e2e tests), offline.
- `uv run ruff check app tests scripts` → clean.
- All `make demo-*` regression suites green.

## Blocker / open questions
- None.

## Next step (for the next session)
- **Plan Mk3 closeout / Stretch / Report**: finalize any remaining documentation or prepare final presentation notes.

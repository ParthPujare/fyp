# Plan Mk3 — BYOK LLM Keys (Bring Your Own Key)

- **Plan id:** `mk3`
- **Scope:** Per-user LLM API keys so every member can chat with a **real model through their own
  provider** — keys stored encrypted per-user, used server-side only, admin-visible as status only,
  mock fallback preserved for the offline demo.
- **Status:** Phases 1–3 DONE 2026-09-04 (session 015).
- **Registry:** `ref_docs/plans/INDEX.md`
- **Predecessor:** Plan Mk2 (`ref_docs/plans/plan_mk2_web_ui_members.md`) — Phases 1–5 DONE;
  extends the finished web UI + MemberStore foundation; does not rewrite either prior plan.
- **Source requirement:** `ref_docs/todo.md` item #7 (API Key Integration / BYOK).

---

## 1. Why this plan exists

Mk2 made every capability reachable from the browser, but chat still runs on the **global**
`LLMSettings` provider (`app/config.py`) — `mock` by default, or one shared key via
`LLM__PROVIDER=litellm`. There is no per-user notion of an LLM anywhere:

- `app/llm/factory.py` `get_provider()` resolves only from global settings.
- `app/gateway/app.py` `/chat` passes no provider, so `app/agent/runner.py` `_build_deps`
  falls back to that global provider for every caller.
- The HITL resume path (`resume_agent`, used by both gateway and dashboard) resolves the same
  global way even though the resumed run acts as the **original actor**, not the decider.

Real LLM chat for all users (todo #7 — and the prerequisite for todo #4's real-tool skills)
requires each user to connect their own provider key: encrypted at rest in the MemberStore,
decrypted only server-side at call time, never displayed back, never logged, with the offline
mock demo preserved when no key is set.

---

## 2. Decisions locked in (planning session, 2026-08-21)

| # | Decision | Choice | Notes |
|---|---|---|---|
| 1 | Model selection | User picks **provider + LiteLLM model string** (e.g. `openai` + `gpt-4o-mini`, `gemini/gemini-2.0-flash`) | Most flexible; LiteLLM needs the model string anyway |
| 2 | Encryption master key | **Env var `LLM__FERNET_KEY`** (Fernet from `cryptography`) | Follows existing pydantic-settings conventions; documented in `.env.example` + threat model |
| 3 | Provider precedence | **User's active key > global `LLM__PROVIDER` config > MockLLM** | Preserves both the offline demo AND the existing shared-dev-key flow |
| 4 | Management rights | **Self-service AND admin-managed** | Each member manages their own keys; admin additionally gets per-member CRUD. Admin sees provider/model metadata + set/unset status — **never key material** |
| 5 | Key cardinality | **Multiple configs per user, exactly one active** | Unique per `(username, provider, model)`; re-saving replaces the stored key; deleting the active config leaves the user with none (falls back per #3) |
| 6 | Judge evals | **Stay on the global/mock provider** | Never bill the user's key invisibly; evals stay deterministic/offline |
| 7 | Provider failure mid-chat | **Explicit classified error to the user** (invalid key / quota / timeout → HTTP 502 with sanitized message) | No silent mock fallback; run still traced as errored; raw exception text never surfaced |
| 8 | Member-facing UI | **New Account/Settings tab** (all roles); admin key management inline in the Members tab | Members currently only have Chat; Account leaves room to grow |
| 9 | Phasing | **3 phases** | One session per phase, session ids assigned at implementation time |

Consequence of decisions #4 + #5: todo.md's sketch of a single `api_key_encrypted` column on
`members` becomes a dedicated **`member_llm_keys` table** in `data/members.sqlite` (multi-config +
admin management need real rows). The deviation is recorded and todo.md #7 is updated at closeout.

Additional locked constraints:

- **Key material discipline:** the decrypted key never enters prompts, traces, audit entries,
  logs, or any API response. Store reads for UI return metadata only. Tests assert leakage scans
  come up empty.
- **Every key mutation is audit-logged** with the acting identity (self vs admin distinguishable),
  consistent with the Mk2 member-management discipline.
- **Keys are not policy-relevant:** mutations do NOT bump `members_version` (no Casbin impact).

---

## 3. Why the technical shape (architecture notes)

### 3.1 Resolution chain (target)

```
Chat request (member JWT)
  → gateway /chat verifies token + active gate        (unchanged, gateway/app.py _verified_active)
  → Runner resolves the caller's provider:
      get_provider_for_user(settings, sub):
        1. MemberStore.get_active_llm_key(sub)
             → Fernet-decrypt → LiteLLMProvider(model=<user model>, api_key=<decrypted>)
        2. else → existing global get_provider(settings)   (litellm w/ shared key, or mock)
        3. else → MockLLM                                   (default unchanged)
  → run_agent(claims, message, provider=…)            ← injection point already exists (runner.py:71)
```

HITL resume: the provider is resolved from the approval entry's original actor
(`entry["actor"]["sub"]`) — **never** the deciding admin's key. Both resumers must do this:
gateway `get_resumer` and dashboard `get_resumer`.

### 3.2 Storage

New table in `data/members.sqlite`, managed by `MemberStore` (identity + credentials stay in one
store, matching the Mk2 single-source-of-truth decision):

```sql
CREATE TABLE IF NOT EXISTS member_llm_keys (
    id                TEXT PRIMARY KEY,   -- uuid
    username          TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,      -- Fernet token, never returned by any read API
    active            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE(username, provider, model)
);
```

Single-active invariant enforced inside one transaction in the store (deactivate others, then set).
No ALTER of `members` needed — new table only, so existing DBs migrate on `_init_schema`.

### 3.3 Crypto helper

`app/auth/crypto.py`: `encrypt_secret(plaintext) -> str` / `decrypt_secret(token) -> str`,
lazily building a Fernet from `settings.llm.fernet_key`.
- Unset/invalid `LLM__FERNET_KEY` ⇒ key-management endpoints return **503** with a setup hint;
  chat falls through the precedence chain (a stored-but-undecryptable token ⇒ explicit 502
  "re-save your key", per decision #7 philosophy).
- `cryptography` is added to `pyproject.toml` (not currently installed).

### 3.4 Error mapping (decision #7)

LiteLLM exceptions are classified before surfacing: authentication → "invalid API key",
rate-limit/quota, timeout, other → generic. All map to HTTP 502 with a sanitized detail string;
the raw exception never crosses the API boundary. The run still gets `trace.end_run` with an
error outcome so trace integrity holds (trace-completeness eval tolerance for errored runs is
verified/fixed within Phase 2 if needed).

### 3.5 API surface (Phase 3)

Member-scoped (Bearer member JWT, `_verified_member` pattern):

| Method/Path | Purpose |
|---|---|
| `GET /api/account/llm-keys` | Own configs — metadata only (id, provider, model, active, timestamps) |
| `POST /api/account/llm-keys` | Add/replace `{provider, model, api_key}` (validated; 503 if encryption unconfigured) |
| `POST /api/account/llm-keys/{id}/activate` | Switch active config |
| `DELETE /api/account/llm-keys/{id}` | Remove a config |

Admin-scoped (`_verify_admin`): the same four operations under
`/api/members/{username}/llm-keys…`; plus `GET /api/users` rows gain key status metadata
(active provider/model or null — status, never material).

### 3.6 UI (Phase 3)

- **Account tab** (all roles): list own configs (provider, model, active badge), add form
  (provider select + model string + password-type key input, never echoed), activate/delete.
- **Members tab** (admin): new "LLM key" column (active provider/model or "—") + per-member
  manage panel with the same CRUD.
- **Chat tab**: unchanged except classified provider errors render their detail message.

---

## 4. Milestones / Phases (each phase = 1 session, id assigned at implementation)

| Phase | Session | Deliverable | Acceptance |
|-------|---------|-------------|------------|
| 1 | 015 | **Crypto + store layer.** `cryptography` dependency; `LLM__FERNET_KEY` setting + `.env.example` entry; `app/auth/crypto.py`; `member_llm_keys` table + MemberStore API (`list/set/activate/delete/get_active`) + audit entries; unit tests (roundtrip, missing/invalid fernet key, multi-key CRUD, active-switch atomicity, duplicate replace, delete-active). | ✅ **DONE 2026-09-04 (session 015)** — Existing suite green unchanged (275 tests); new crypto/store tests green; zero runtime behaviour change to chat/auth yet; ruff clean |
| 2 | 015 | **Per-user provider routing.** `get_provider_for_user` precedence chain; gateway Runner + both resumers wired (HITL resume uses original actor); classified 502 error path + errored-run tracing; judge stays global. Tests: fake-provider seam proves decrypted key + user model reach `run_agent`; resume path; no-key → global/mock unchanged; failure classification; leakage scan. | ✅ **DONE 2026-09-04 (session 015)** — Precedence + failure tests green; regression green; judge provider ≠ user provider asserted; ruff clean |
| 3 | 015 | **Dashboard API + UI + docs closeout.** Member `/api/account/llm-keys*` + admin `/api/members/{u}/llm-keys*` endpoints (+ status in `/api/users`); Account tab + Members-tab key column/manage panel; e2e test (set key → chat uses it → clear → mock fallback); authz matrix (member B cannot touch A's keys; admin can); README/.env.example/threat_model/todo.md #7/INDEX updates. | ✅ **DONE 2026-09-04 (session 015)** — Browser/e2e green; zero key-material leakage asserted across responses/traces/audit; docs updated; ruff clean |

### Detailed Implementation Checklists

#### Phase 1 Checklist (Crypto & MemberStore Layer)
- [x] **Dependencies & Config**
  - [x] Add `cryptography` to `pyproject.toml`.
  - [x] Add `fernet_key: str = ""` (env `LLM__FERNET_KEY`) to `LLMSettings` in `app/config.py`.
  - [x] Update `.env.example` with `LLM__FERNET_KEY` generation instructions and placeholder.
- [x] **Crypto Helpers (`app/auth/crypto.py`)**
  - [x] Implement `encrypt_secret(plaintext: str) -> str` using Fernet initialized from `LLM__FERNET_KEY`.
  - [x] Implement `decrypt_secret(token: str) -> str`.
  - [x] Handle unconfigured or invalid Fernet key gracefully (raise explicit 503 configuration error).
- [x] **Store Layer (`app/auth/store.py`)**
  - [x] Create `member_llm_keys` table in `data/members.sqlite` (`id`, `username`, `provider`, `model`, `api_key_encrypted`, `active`, `created_at`, `updated_at`, UNIQUE on `(username, provider, model)`).
  - [x] Implement store methods: `list_llm_keys`, `set_llm_key`, `activate_llm_key`, `delete_llm_key`, `get_active_llm_key`.
  - [x] Audit log every key mutation with acting identity.
- [x] **Verification**
  - [x] Write unit tests (`tests/test_llm_keys.py`).
  - [x] Verify existing test suite passes green (`uv run pytest`).

#### Phase 2 Checklist (Per-User Provider Routing & Gateway Wiring)
- [x] **Provider Factory (`app/llm/factory.py`)**
  - [x] Implement `get_provider_for_user(settings, username, store)` (user active key → global config → MockLLM).
- [x] **Gateway & Runner Integration (`app/gateway/app.py`, `app/agent/runner.py`)**
  - [x] Update `POST /chat` runner setup to resolve provider per calling user.
  - [x] Update HITL resume paths (`get_resumer` in gateway and dashboard) to resolve provider from the original approval entry actor (`entry["actor"]["sub"]`).
- [x] **Error Classification & Trace Integrity**
  - [x] Catch LiteLLM authentication, rate-limit, quota, and timeout exceptions; map to classified HTTP 502 responses with sanitized details.
  - [x] Ensure errored runs complete `trace.end_run` with error status.
  - [x] Assert judge evals remain tied to the global/mock provider.
- [x] **Verification**
  - [x] Write integration tests for per-user provider resolution, failure classification, and resume actor isolation.
  - [x] Run `uv run pytest` and `make demo-*` regression suites.

#### Phase 3 Checklist (Dashboard API, UI & End-to-End Verification)
- [x] **Dashboard API Endpoints (`app/dashboard/app.py`)**
  - [x] Member-scoped (`_verified_member`): `GET/POST /api/account/llm-keys`, `POST /api/account/llm-keys/{id}/activate`, `DELETE /api/account/llm-keys/{id}`.
  - [x] Admin-scoped (`_verify_admin`): `/api/members/{username}/llm-keys*` and key status in `/api/users`.
- [x] **Dashboard UI (`app/dashboard/static/index.html`)**
  - [x] Add **Account tab** for all members (list, add form, activate/delete).
  - [x] Update **Members tab** for admins (key status column and manage panel).
- [x] **Leakage Scans & Security Checks**
  - [x] Add security tests verifying decrypted keys never appear in API responses, traces, audit logs, or stdout logs.
- [x] **Documentation & Closeout**
  - [x] Update `README.md`, `ref_docs/threat_model.md`, `ref_docs/todo.md` (#7), and `ref_docs/plans/INDEX.md`.
- [x] **Final Verification**
  - [x] Run full test suite (`uv run pytest`) and linters (`uv run ruff check app tests scripts`).

---

## 5. Reference details

- Providers offered in the UI select: OpenAI, Anthropic, Gemini, Groq, Other (free-text model
  string always editable — LiteLLM prefix convention, e.g. `openai/gpt-4o-mini`).
- `.env.example` documents key generation:
  `uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`.
- Validation on save: non-empty provider/model, key length floor (~8 chars); exact errors 422.
- Seeded members start with no keys — offline demo behaviour identical to today.

## 6. Relevant files

- `pyproject.toml` — add `cryptography`.
- `.env.example`, `app/config.py` — `LLMSettings.fernet_key` (`LLM__FERNET_KEY`).
- `app/auth/crypto.py` — NEW: Fernet encrypt/decrypt helpers.
- `app/auth/store.py` — `member_llm_keys` table + key CRUD + audits (Phase 1).
- `app/llm/factory.py` — `get_provider_for_user` (Phase 2).
- `app/gateway/app.py` — Runner/provider resolution + error mapping; resumer actor fix (Phase 2).
- `app/dashboard/app.py` — `get_resumer` actor fix (Phase 2); account/admin key endpoints (Phase 3).
- `app/dashboard/static/index.html` — Account tab + Members-tab key UI (Phase 3).
- `tests/test_llm_keys.py` (new), additions to `test_chat.py` / `test_members_mgmt.py` / e2e (Phases 1–3).
- Docs at closeout: `README.md`, `ref_docs/threat_model.md`, `ref_docs/todo.md` (#7), `ref_docs/plans/INDEX.md`.

## 7. Verification contract (must stay green every phase)

- `uv run pytest` (268 tests today; grows with new suites)
- `uv run ruff check app tests scripts`
- `make demo-chat-web` and `make demo-attacks` every phase; remaining `make demo-*` targets on
  phases touching shared paths (2–3)

## 8. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| `LLM__FERNET_KEY` lost/rotated | Stored tokens undecryptable → explicit 502 telling the user to re-save; recovery documented in threat model |
| Key material leaks into traces/audit/logs/responses | Metadata-only store reads; classified error strings; leakage-scan tests assert absence |
| Raw litellm exception echoes secrets/headers | Never surfaced — classification layer only |
| Errored runs break the trace-completeness eval | Verified in Phase 2; small eval tweak in-scope if errored runs lack REASONING pairs |
| Multi-process SQLite contention (gateway + dashboard write keys) | Existing short-lived-connection pattern; single-active switch atomic in one transaction |
| Judge evals accidentally billed to a user key | Locked decision #6: judge stays global/mock; test asserts separation |
| Scope creep toward tool-provider keys (todo #4 Composio) | Explicitly out of scope — this plan covers chat LLM keys only |

## 9. Out of scope

Tool-provider credentials (Composio / todo #4), spend/quota dashboards, key sharing between
users, streaming responses, per-thread provider overrides, org-wide key vaults.

## 10. Traceability

- This doc is the canonical Mk3 timeline; update phase rows (session id + ✅ status) **as phases
  are implemented** — sessions are never pre-assigned.
- Session files use `Plan: mk3 — Phase N` in metadata; `agent_history/INDEX.md` `Plan` column mirrors it.
- Design-history rule from `agent_history/INSTRUCTIONS.md` applies: append-only, timestamped logs.

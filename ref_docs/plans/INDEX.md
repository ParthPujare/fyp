# Plan Registry — Index

Every plan the project has ever run, one row each. This is the **traceable summary of all work**:
join this table with `agent_history/INDEX.md` (sessions map to a plan + phase via the `Plan` column)
to reconstruct the full story at any time.

Each numbered plan (`mk<NN>`) owns its milestone/timeline doc in this folder. **Plan Mk1 is the
original 16-week plan** (`ref_docs/plans/plan_mk1_core_harness.md`) and is frozen — append-only history, never rewritten.

| Plan | Title | Status | Dates | Deliverable / focus | Link |
|------|-------|--------|-------|---------------------|------|
| Mk1 | Core harness — full 16-week vertical slice (RBAC, 3-tier memory, HITL, sanitizer, skills, trajectory+evals, attack suites) | Milestones 1–8 (weeks 1–14) DONE; stretch + report (weeks 15–16) still open | 2026-08-10 → | Deterministic zero-trust harness with evidence (204 offline tests green) | [./plan_mk1_core_harness.md](./plan_mk1_core_harness.md) |
| Mk2 | Web-UI demo + member management | **Phases 1–5 DONE 2026-08-16 (sessions 010–014)** (Phase 5 = browser memory-test + e2e); Phase 6 (report + recorded web demo) pending (session 015) | 2026-08-15 → | All functionality usable via the web UI (chat, any-user login, dynamic members + departments, browser memory test) + report/recorded demo | [./plan_mk2_web_ui_members.md](./plan_mk2_web_ui_members.md) |
| Mk3 | BYOK LLM keys — per-user provider API keys (encrypted at rest, admin status-only visibility, mock fallback) | **Phases 1–3 DONE 2026-09-04 (session 015)** | 2026-08-21 → | Real LLM chat for every member via their own key (`member_llm_keys` table, Fernet at rest, per-user provider precedence user > global > mock); prerequisite for todo #4 real-tool skills | [./plan_mk3_byok_llm_keys.md](./plan_mk3_byok_llm_keys.md) |
| Mk4 | ChatGPT + Hermes chat window & memory auto-learning (model selector, threads, streaming, memory auto-extraction) | In Progress | 2026-09-04 → | Upgraded chat assistant experience with per-key model selection, thread management, streaming responses, and Hermes-style memory auto-learning | [./plan_mk4_chat_hermes.md](./plan_mk4_chat_hermes.md) |
| Mk5 | Custom Hermes-style BYOT LLM layer (no LiteLLM, direct SDKs + HTTP, multi-user encrypted storage & UI management) | In Progress | 2026-09-05 → | Direct SDK & HTTP provider implementations (OpenAI, Gemini AI Studio, Anthropic, OpenRouter, Custom OpenAI) replacing LiteLLM | [./plan_mk5_custom_byot.md](./plan_mk5_custom_byot.md) |
| Mk6 | Official Google GenAI SDK Integration for Gemini Provider (`google-genai`) | **DONE 2026-09-09 (session 017)** | 2026-09-09 → | Migrate GeminiProvider from direct httpx to official `google-genai` SDK while keeping other providers fully isolated | [./plan_mk6_google_sdk.md](./plan_mk6_google_sdk.md) |
| Mk7 | Gemini provider hardening + editable BYOK key models | **Phase 1 DONE 2026-09-12 (session 018)**; Phase 2 (key replacement + live verify) open | 2026-09-12 → | Editable saved-key models, sanitized upstream error detail, defaults on `gemini-3.6-flash` | [./plan_mk7_gemini_hardening.md](./plan_mk7_gemini_hardening.md) |
| Mk8 | Chat UI declutter + correct memory learning (separate memory tab, title/error/input fixes, LLM extraction) | In Progress | 2026-09-12 → | Uncluttered chat (no inline memory, no `user:` noise, multiline, first-message titles) + memory stores only relevant facts | [./plan_mk8_chat_cleanup.md](./plan_mk8_chat_cleanup.md) |

## Rules

1. Plan docs live in `ref_docs/plans/`, numbered `plan_mk<NN>_<slug>.md`; one row per plan in this file.
2. Append new plans here; never edit a closed plan's doc — write a follow-up plan instead.
3. `agent_history/` sessions carry a `Plan` column (plan id + phase) so every session is traceable
   to a plan (see `agent_history/INSTRUCTIONS.md`).
4. One session = one **phase** of a registered plan (phase-level granularity, not step-level).
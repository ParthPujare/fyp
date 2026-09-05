# Plan Mk5 — Hermes-Style Local BYOK & Secrets Management

- **Plan id:** `mk5`
- **Scope:** Aligning BYOK and secrets management with Hermes' philosophy for a locally cloned single-user deployment:
  1. **Config & Secrets Separation**: Clean separation between non-sensitive provider configuration (provider type, model, base URL, temperature) and sensitive secrets (API keys) via `.env` or local configuration.
  2. **Single-User Local Focus**: Simplifying from a multi-tenant database credential vault (`member_llm_keys`) to a direct local-user setup suitable for running the repository locally on your PC.
  3. **Unified Provider Abstraction**: Streamlined provider resolution supporting OpenAI, Gemini (Google AI Studio), Anthropic, OpenRouter, and custom OpenAI-compatible endpoints.
- **Status:** In Progress (2026-09-05).
- **Registry:** `ref_docs/plans/INDEX.md`
- **Predecessor:** Plan Mk3 (`ref_docs/plans/plan_mk3_byok_llm_keys.md`) / Plan Mk4 (`ref_docs/plans/plan_mk4_chat_hermes.md`)

---

## 1. Why this plan exists

Plan Mk3 implemented a multi-tenant, database-backed credential vault (`member_llm_keys` in `data/members.sqlite` with Fernet encryption at rest, per-user records, and admin key management). However, as a locally cloned single-user tool (following Hermes' design philosophy where you clone the repo and run it on your own PC for yourself), this multi-tenant database overhead is unnecessary and less intuitive than Hermes' clean **config + secrets separation** (`.env` or local configuration).

Plan Mk5 refactors the system to handle keys and secrets the Hermes way:
- Simple, transparent local configuration for providers and models.
- Environment variables or local secrets handling for API keys without requiring complex per-user DB tables or admin vaults.
- Easy provider/model switching and robust offline fallback (`MockLLM`).

---

## 2. Implementation Phases

| Phase | Deliverable | Acceptance |
|-------|-------------|------------|
| 1 | **Config & Secrets Refactoring.** Update `app/config.py` and `.env.example` to support clean provider/model configuration and API keys separation (Hermes style). | Settings load provider, model, base URL, and API key cleanly from environment/local config. |
| 2 | **Simplified Provider Factory & Routing.** Streamline `app/llm/factory.py` to use local config/secrets directly with mock fallback. | Chat requests use the locally configured provider key and model. |
| 3 | **UI / API Simplification & Docs Closeout.** Update dashboard settings/chat interface for local model selection; update README and plan index. | Clean local user experience matching Hermes design. |

# Plan Mk5 — Custom Hermes-Style BYOT LLM Layer (No LiteLLM)

- **Plan id:** `mk5`
- **Scope:** Complete custom Bring Your Own Token (BYOT) provider architecture modeled after Hermes, eliminating LiteLLM entirely in favor of direct SDKs (`openai`, `anthropic`) and direct HTTP (Google AI Studio Gemini API), with multi-user encrypted storage in `MemberStore` and UI-driven management.
- **Status:** In Progress (2026-09-05).
- **Registry:** `ref_docs/plans/INDEX.md`
- **Predecessor:** Plan Mk3 / Mk4

---

## 1. Architecture & Design Specifications

### 1.1 Provider Abstraction (`app/llm/`)
- **`base.py`**:
  - `Message`: dataclass or model for `{role, content, tool_calls, tool_call_id}`.
  - `ToolSchema`: dataclass for `{name, description, parameters}`.
  - `LLMResponse`: dataclass for `{model, provider, content, tool_calls, usage, finish_reason, reasoning}`.
  - `LLMProvider` (ABC): defines `@abstractmethod def chat(...) -> LLMResponse`.
- **Concrete Providers (No LiteLLM)**:
  - `OpenAIProvider`: uses official `openai` Python SDK (`openai.OpenAI(api_key=..., base_url=...)`).
  - `GeminiProvider`: uses direct `httpx` to Google AI Studio (`https://generativelanguage.googleapis.com/v1beta`) with `?key=GEMINI_API_KEY`.
  - `AnthropicProvider`: uses official `anthropic` Python SDK (`anthropic.Anthropic(api_key=...)`).
  - `OpenRouterProvider`: OpenAI-compatible configuration with `base_url="https://openrouter.ai/api/v1"` and headers.
  - `CustomOpenAICompatibleProvider`: OpenAI SDK with arbitrary `base_url`.
- **Registry & Factory (`registry.py`)**:
  - `PROVIDERS: dict[str, type[LLMProvider]] = { ... }`
  - `get_provider(provider_type: str, api_key: str, base_url: str | None = None) -> LLMProvider`

### 1.2 Storage & Credentials (`app/auth/store.py`, `app/auth/crypto.py`)
- **Storage**: Unified database-backed `MemberStore` (`data/members.sqlite`) housing:
  - `user_providers`: `id`, `username`, `provider_type`, `model`, `api_key_encrypted`, `base_url`, `active`, `created_at`, `updated_at`.
  - `user_preferences`: `username`, `default_provider`, `default_model`, `last_used_model`.
- **Encryption**: Fernet (`cryptography` library) driven by `LLM__FERNET_KEY`. Keys are encrypted at rest and never exposed raw.

### 1.3 Endpoints & UI
- **Provider Management Endpoints**:
  - `GET /providers`: list supported provider types.
  - `POST /api/account/llm-providers` (or `/api/users/{id}/providers`): add/update provider + key + base url + model.
  - `GET /api/account/llm-providers`: list user's configured providers (metadata only).
  - `DELETE /api/account/llm-providers/{provider_id}`: remove provider config.
  - `POST /api/account/llm-providers/{provider_id}/test`: test API key with a minimal ping call.
  - `GET /api/account/llm-providers/{provider_id}/models`: list available models.
- **Chat Endpoint (`POST /chat`)**:
  - Accepts user claims, optional `provider_id`/`model` override, messages, tools.
  - Decrypts key, instantiates provider via registry factory, calls `.chat(...)`, returns normalized `LLMResponse`.

---

## 2. Implementation Phases

| Phase | Deliverable | Acceptance |
|-------|-------------|------------|
| 1 | **Base Abstraction & Concrete Providers.** Implement `base.py`, `openai_provider.py`, `gemini_provider.py`, `anthropic_provider.py`, `openrouter_provider.py`, `custom_provider.py`, and `registry.py`. Unit tests for each provider with mock/fake HTTP/SDK responses. | All provider classes correctly translate messages/tools and normalize responses without LiteLLM. |
| 2 | **Store, Crypto & Database Tables.** Expand `MemberStore` schema with `user_providers` and `user_preferences`; integrate Fernet encryption helpers. | CRUD operations, active switching, and secure encryption verified via unit tests. |
| 3 | **API Routing, Gateway & Dashboard UI.** Implement provider management REST endpoints, test key ping, model listing, and chat integration; update UI account/chat panels. | End-to-end flow verified: user configures provider via UI, chats using custom provider without LiteLLM. |

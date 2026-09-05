The goal is:

Each user brings their own API keys (OpenAI, Google/Gemini, Anthropic, OpenRouter, etc.).

All LLM calls are made directly to those providers using the user’s own credentials (user pays their own bills).

The system supports multiple providers and models, and makes it very easy for users to switch models on the fly.

No CLI setup is required; everything is configured and managed from the UI.

Use Hermes’ design philosophy as a reference: clean provider abstraction, unified internal message/tool format, per‑user credentials, and simple config + secrets separation. But adapt it for a multi‑user web app with a UI‑driven onboarding and settings flow.

1. High‑level requirements
Build a backend service (language/framework of your choice, but show concrete code) that:

Provider abstraction

Defines a common interface for all LLM providers, e.g.:

chat(messages, tools, model, temperature, max_tokens, ...) → unified response shape.

Implements concrete providers:

OpenAIProvider

GeminiProvider (using Google AI Studio keys, not Vertex)

AnthropicProvider

OpenRouterProvider (OpenAI‑compatible)

CustomOpenAICompatibleProvider (arbitrary base URL).

Each provider:

Takes an API key and optional base URL in its constructor.

Translates the unified request into the provider’s HTTP API shape.

Normalizes the response back to a common internal format.

Per‑user credentials and config

Each user has:

A list of configured providers (e.g. “OpenAI”, “Gemini”, “Anthropic”).

For each provider:

API key (encrypted at rest).

Optional custom base URL.

Optional default model.

A “primary” or “default” provider + model used when they start a new chat.

Credentials are:

Stored per user ID.

Encrypted using a server‑side key / KMS.

Never logged or exposed to the frontend in raw form.

UI‑driven onboarding and settings

Provide API specs and example UI flows for:

Onboarding: first‑time user adds at least one provider + key.

Settings page: user can:

Add/remove providers.

Update API keys.

Set a default provider and default model.

Test a key with a small “ping” call.

The backend must expose REST/GraphQL endpoints for:

GET /providers – list supported provider types.

POST /users/{id}/providers – add a provider + key.

GET /users/{id}/providers – list user’s configured providers (without returning raw keys).

PUT /users/{id}/providers/{provider_id} – update key/base URL/default model.

DELETE /users/{id}/providers/{provider_id} – remove a provider.

POST /users/{id}/providers/{provider_id}/test – test the key with a minimal call.

GET /users/{id}/providers/{provider_id}/models – list available models for that provider (cached or proxied).

PUT /users/{id}/default-model – set default provider + model.

Chat / inference endpoint

Implement a unified chat endpoint:

POST /chat

Input:

user_id

Optional provider_id and model (override default for this request).

messages (array of {role, content, optional tool_calls/tool_results}).

Optional tools definition.

Optional parameters: temperature, max_tokens, top_p, etc.

Behavior:

Load the user’s config.

If provider_id/model not specified, use the user’s default.

Decrypt the API key for that provider.

Instantiate the corresponding provider client with that key.

Call provider.chat(...) with the unified request.

Return a normalized response:

{ id, provider, model, content, tool_calls, usage, finish_reason, ... }.

Ensure errors from providers (auth failures, rate limits, invalid model, etc.) are translated into clear, user‑friendly error codes/messages.

Easy model switching

Design the system so switching models is trivial for the user:

In the chat UI, the user can:

See current provider + model.

Open a dropdown to:

Choose a different model from the same provider.

Or choose a different provider (which then shows its models).

The backend must:

Cache or fetch available models per provider (via GET /users/{id}/providers/{provider_id}/models).

Allow the chat request to override the default model on a per‑request basis.

Make model switching stateless at the backend:

The chat endpoint always respects the provider_id and model passed in the request.

The frontend is responsible for persisting the user’s “last used model” preference if desired.

Security and isolation

Multi‑tenant design:

Users cannot see or use other users’ keys.

All provider calls are scoped to a single user’s credentials.

Encrypt API keys at rest.

Do not log raw keys or full request bodies containing sensitive tokens.

Implement basic rate limiting per user to avoid abuse.

Similarity to Hermes

Mirror Hermes’ architectural ideas:

Clear separation between:

“Provider config” (which providers, base URLs, default models).

“Secrets” (raw API keys, OAuth tokens).

A provider registry pattern:

PROVIDERS = { "openai": OpenAIProvider, "gemini": GeminiProvider, ... }.

Unified internal message/tool format that is translated per provider.

Support for tools/function calling where the provider allows it.

Differences from Hermes:

No CLI; everything is configured via UI + API.

Multi‑user database‑backed storage instead of a single local ~/.hermes folder.

Explicit endpoints for listing models and testing keys.

2. Deliverables
Produce:

Architecture overview

A short description of:

Main services/components (API layer, provider layer, DB schema, encryption strategy).

Data flow for:

User onboarding (adding a key).

Sending a chat request with a specific provider/model.

Switching models in the UI.

Database schema

Define tables/collections for:

users

user_providers (provider type, encrypted key, base URL, default model, metadata).

user_preferences (default provider, default model, last used model, etc.).

Show example SQL or ORM models (e.g. SQLAlchemy, Prisma, Drizzle, etc.).

Provider abstraction code

Show:

The base LLMProvider interface.

At least two concrete implementations (e.g. OpenAIProvider and GeminiProvider).

Include:

How you construct HTTP requests.

How you normalize responses.

How you handle errors and map them to a common error type.

API spec

OpenAPI‑style or clear pseudo‑spec for:

Provider management endpoints.

Model listing endpoint.

Test key endpoint.

Chat endpoint.

Include request/response shapes and example payloads.

Example UI flows (described, not implemented)

Describe:

Onboarding screen:

“Add your first LLM provider”.

Dropdown for provider type, input for API key, optional base URL, optional default model.

“Test connection” button.

Settings screen:

List of configured providers.

Add/remove/edit provider.

Set default provider/model.

Chat screen:

Model selector dropdown (provider + model).

How changing the selection affects subsequent requests.

Security notes

Briefly describe:

How you encrypt/decrypt API keys.

How you ensure per‑user isolation.

Any additional hardening (e.g. key rotation, audit logs for key usage, etc.).

Implementation notes for Gemini keys

Explicitly document:

Users must create Gemini keys via Google AI Studio (https://aistudio.google.com/apikey).

These keys are used with the endpoint:

https://generativelanguage.googleapis.com/v1beta

Warn that keys created via other Google consoles (e.g. generic Cloud Console, service accounts) may not work and can have different formats/prefixes.

In the provider implementation, show how you:

Call the Gemini API with the key.

Handle auth errors specifically (e.g. “Invalid or non‑AI‑Studio Gemini key”).

3. Constraints and style
Assume a modern stack (e.g. Node.js + TypeScript + Fastify/Nest/Express, or Python + FastAPI, or similar). You can choose, but be consistent and show real code snippets.

Focus on clarity and correctness over premature optimization.

Keep the design close to Hermes’ philosophy:

Simple, composable provider layer.

User owns keys and billing.

Unified internal format.

Make it easy for a competent engineer to implement this directly from your output.
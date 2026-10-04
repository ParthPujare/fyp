-- Platform-owned LLM config (Mk16 follow-up: DB-backed shared key).
--
-- * platform_llm: the single shared row (id = 'openrouter') holding the
--   platform extraction model (plaintext — not secret) and the API key
--   Fernet-encrypted (same at-rest posture as member_llm_keys.api_key_encrypted).
--   Any instance connected to this database uses this exact row; per-machine
--   env (`SYSTEM__OPENROUTER_API_KEY` / `OPENROUTER_API_KEY`) is only the
--   pre-bootstrap fallback. The model is validated server-side (`:free` only)
--   at the API layer, not here.
-- * Additive only (see app/db/migrate.py authoring rules). The SQLite twin
--   lives in MemberStore._init_schema (CREATE TABLE IF NOT EXISTS).
CREATE TABLE platform_llm (
    id                TEXT PRIMARY KEY,
    model             TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,
    updated_by        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

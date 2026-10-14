-- Mk18 Phase H: per-user/day LLM spend accounting.
--
-- One row per (user, UTC day): generate-call count + estimated cost cents.
-- Enforced in app/agent/runner.py (_build_deps pre-check + metering wrapper);
-- over either cap the turn falls back to Mock with a spend_cap trace event,
-- and the gateway answers 429. Additive only. SQLite twin in MemberStore.
CREATE TABLE llm_usage (
    username   TEXT NOT NULL,
    day        TEXT NOT NULL,
    calls      INTEGER NOT NULL DEFAULT 0,
    cost_cents INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (username, day)
);

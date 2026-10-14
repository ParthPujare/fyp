-- Mk18 Phase E: second-eyes proposals for high-impact admin writes.
--
-- A proposal captures a (kind, payload) plus proposer; a *different* admin
-- confirms or rejects it. Secrets in payloads are Fernet-encrypted by the
-- dashboard before storing (same at-rest posture as member_llm_keys).
-- Additive only. SQLite twin lives in MemberStore._init_schema.
CREATE TABLE second_eyes (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL,
    proposed_by TEXT NOT NULL,
    proposed_at TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    decided_by  TEXT NOT NULL DEFAULT '',
    decided_at  TEXT NOT NULL DEFAULT ''
);

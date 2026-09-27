-- The policy-decision audit chain on the shared database (Mk13 Phase 5): app/harness/pg_audit.py.
--
-- Replaces data/audit.jsonl + its `.head` sidecar. One row per decision, hash-chained exactly as
-- in the file (`prev_hash` -> `hash`, genesis = 64 zeros, optional HMAC key), so every laptop
-- appends to ONE chain. `log` names the chain (the file's stem, "audit" by default).
--
-- What keeps the chain linear when several laptops append at once is the primary key: two writers
-- that computed their hash from the same head both claim the same `seq`, and the second insert
-- fails instead of forking the chain (it then re-reads the head and retries). No lock is held
-- across round trips.

CREATE TABLE audit_log (
    log       TEXT   NOT NULL,
    seq       BIGINT NOT NULL,            -- 1, 2, 3, ... per log, no gaps
    event_id  TEXT   NOT NULL UNIQUE,     -- lets a writer recognise its own row after a lost reply
    ts        TEXT   NOT NULL,
    run_id    TEXT   NOT NULL,
    decision  TEXT   NOT NULL,
    entry     TEXT   NOT NULL,            -- the hashed entry, canonical JSON (sort_keys)
    prev_hash TEXT   NOT NULL,
    hash      TEXT   NOT NULL,
    PRIMARY KEY (log, seq)
);

-- the per-run evals read only their own run's decisions
CREATE INDEX idx_audit_run ON audit_log (log, run_id, seq);

-- the `.head` file's twin: the latest hash, so deleting trailing rows is detectable
CREATE TABLE audit_heads (
    log       TEXT   PRIMARY KEY,
    seq       BIGINT NOT NULL,
    head_hash TEXT   NOT NULL
);

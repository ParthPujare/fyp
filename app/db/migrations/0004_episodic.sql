-- Episodic recall on the shared database (Mk13 Phase 4): app/memory/pg_episodic.py.
--
-- Replaces SQLite FTS5. Every transcript row carries the memory `scope` it was written under
-- ("dept/<name>", ...); recall filters on that column with an exact match and the caller passes
-- only scopes the policy engine allows it to read. See pg_episodic.py for the isolation design.
--
-- Why the regexp_replace inside the generated column: FTS5's tokenizer splits on every
-- non-alphanumeric character, whereas PostgreSQL's parser keeps e-mail addresses, URLs and paths
-- as single tokens (so "acme" would not match "bob@acme.com"). Normalising to alphanumeric words
-- first — and normalising queries the same way in Python — keeps recall behaving as before, and
-- as a side effect strips every tsquery operator character from anything that reaches the index.

CREATE TABLE episodic (
    id         BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    scope      TEXT NOT NULL,
    run_id     TEXT NOT NULL,
    ts         TEXT NOT NULL,
    actor      TEXT NOT NULL,
    transcript TEXT NOT NULL,
    tsv        tsvector GENERATED ALWAYS AS (
                   to_tsvector('simple', regexp_replace(transcript, '[^[:alnum:]]+', ' ', 'g'))
               ) STORED,
    -- one transcript per (scope, run): a run resumed after an approval re-runs `reflect`
    CONSTRAINT episodic_scope_run UNIQUE (scope, run_id)
);

CREATE INDEX idx_episodic_tsv ON episodic USING GIN (tsv);
CREATE INDEX idx_episodic_scope_ts ON episodic (scope, ts DESC);

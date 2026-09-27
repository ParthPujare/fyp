-- Run traces on the shared database (Mk13 Phase 3): app/ops/store.py / app/ops/pg_trace.py.
--
-- Same columns as the SQLite tables (TEXT timestamps, JSON kept as text) because the hash-chain
-- code hashes the exact serialised values. Chat checkpoints are NOT here: LangGraph's own
-- PostgresSaver owns its tables and migrations (`checkpoint_*`), created on first use.

CREATE TABLE trace_events (
    event_id  TEXT PRIMARY KEY,
    run_id    TEXT NOT NULL,
    seq       INTEGER NOT NULL,
    ts        TEXT NOT NULL,
    type      TEXT NOT NULL,
    source    TEXT NOT NULL,
    actor     TEXT NOT NULL,
    payload   TEXT NOT NULL,
    prev_hash TEXT NOT NULL
);

-- A run's chain is one linear sequence: (run_id, seq) must be unique. This is what turns two
-- machines resuming the same run from a stale head into an error instead of a forked chain.
CREATE UNIQUE INDEX idx_trace_run_seq ON trace_events (run_id, seq);

CREATE TABLE trace_runs (
    run_id     TEXT PRIMARY KEY,
    head_hash  TEXT NOT NULL,
    started_ts TEXT NOT NULL
);

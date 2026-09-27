-- Baseline for the shared database (Mk13 Phase 1).
--
-- No application tables yet: Phase 2 adds members/approvals/memory. This file exists so the
-- whole pipeline (connect -> lock -> migrate -> record) is exercised end to end on the real
-- Neon database before any data depends on it, and `python -m app.db check` has something
-- real to read back.

CREATE TABLE app_meta (
    key        text PRIMARY KEY,
    value      text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);

INSERT INTO app_meta (key, value) VALUES ('schema_epoch', 'mk13-phase1');

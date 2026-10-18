-- Mk19 Phase 5: HMAC era flag on trace runs (mixed-mode verify).
--
-- A run written before the store was keyed stays verifiable: its `keyed=0`
-- flag selects the plain-SHA era at verification time; keyed runs (keyed=1)
-- verify under the rotation chain. Additive only, default 0 matches every
-- pre-Mk19 row. SQLite twin lives in TraceStore._init_schema (ALTER guarded).
-- The audit chain needs no column: its era flag is hashed inside each entry.
ALTER TABLE trace_runs ADD COLUMN IF NOT EXISTS keyed INTEGER NOT NULL DEFAULT 0;

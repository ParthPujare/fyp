-- Two-level department approvals + approve-once allowlist (Mk15).
--
-- * departments.approval_mode: `head-only` (default) or `head+admin`; validated in the store
--   and the API (no CHECK here, so the SQLite twin and this schema stay identical).
-- * approvals.level/steps: which step a pending item is waiting on (`head | admin | done`) and
--   the history of decisions so far (`[{level, by, at, decision, comment}]`). `route`, `dept`,
--   `action` and `args_hash` record the creation-time routing, so a later mode switch never
--   re-routes an in-flight item.
-- * approval_allowlist: "always allow this exact action" rules, keyed per member.
--
-- JSON is stored as TEXT (not JSONB) on purpose: the store code is shared with SQLite and
-- calls `json.loads(...)` on what it reads (see the note at the top of 0002_core_tables.sql).
-- Additive only (see app/db/migrate.py authoring rules).

ALTER TABLE departments ADD COLUMN approval_mode TEXT NOT NULL DEFAULT 'head-only';

ALTER TABLE approvals ADD COLUMN level TEXT NOT NULL DEFAULT 'head';
ALTER TABLE approvals ADD COLUMN steps TEXT NOT NULL DEFAULT '[]';
ALTER TABLE approvals ADD COLUMN route TEXT;
ALTER TABLE approvals ADD COLUMN dept TEXT;
ALTER TABLE approvals ADD COLUMN action TEXT;
ALTER TABLE approvals ADD COLUMN args_hash TEXT;

CREATE TABLE approval_allowlist (
    member     TEXT NOT NULL,
    action     TEXT NOT NULL,
    args_hash  TEXT NOT NULL,
    args       TEXT NOT NULL,
    dept       TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (member, action, args_hash)
);
CREATE INDEX idx_allowlist_member_action ON approval_allowlist (member, action);
CREATE INDEX idx_allowlist_dept ON approval_allowlist (dept);

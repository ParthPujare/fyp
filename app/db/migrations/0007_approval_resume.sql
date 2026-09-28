-- Owner-side HITL resume (Mk14): a decided approval is resumed by its originator,
-- not by whoever approved it, so per-member hardware/keys stay local.
--
-- `decide` stays atomic (UPDATE ... WHERE status='pending'). `claim_resume` below is the
-- second atomic gate: exactly one process may resume a decided approval, the losers get
-- rowcount 0 and surface 409. Additive only (see app/db/migrate.py authoring rules).

ALTER TABLE approvals ADD COLUMN resumed_by TEXT;
ALTER TABLE approvals ADD COLUMN resumed_at TEXT;
CREATE INDEX idx_approvals_thread ON approvals (thread_id, status);

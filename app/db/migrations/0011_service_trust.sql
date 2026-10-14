-- Mk18 Phase C: login brute-force throttle + TOTP second factor (opt-in).
--
-- * login_attempts / locked_until: failed-password counter + ISO-8601 lock
--   expiry. Enforcement lives in MemberStore.verify (shared SQLite + PG).
-- * totp_secret: base32 TOTP secret, blank = second factor off (the default —
--   enforcement only applies to members that have one set).
-- Additive only. SQLite twin lives in MemberStore._init_schema.
ALTER TABLE members ADD COLUMN IF NOT EXISTS login_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE members ADD COLUMN IF NOT EXISTS locked_until TEXT NOT NULL DEFAULT '';
ALTER TABLE members ADD COLUMN IF NOT EXISTS totp_secret TEXT NOT NULL DEFAULT '';

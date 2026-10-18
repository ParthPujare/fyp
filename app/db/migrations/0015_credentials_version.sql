-- Mk19 Phase 6: credential-generation claim on members.
--
-- Bumped in the same UPDATE as password reset, role/dept/scope update and
-- TOTP set (SQLite twin lives in MemberStore._init_schema + those methods).
-- JWTs carry the generation they were minted under (`cred_ver`); verify_token
-- rejects an older value as superseded. Default 1 = every existing row keeps
-- working; newly created members start at 1 too (create INSERT omits the
-- column).
ALTER TABLE members ADD COLUMN IF NOT EXISTS credentials_version INTEGER NOT NULL DEFAULT 1;

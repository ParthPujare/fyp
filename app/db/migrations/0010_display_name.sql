-- Mk17 identity foundation: UI-only display name.
--
-- Physical `members.username` stays the email identity (FK-safe, no rewrites);
-- `display_name` is UI-only, may repeat, max 100 chars enforced at API layer.
-- Additive only. SQLite twin lives in MemberStore._init_schema
-- (ALTER TABLE ... ADD COLUMN, IF-NOT-EXISTS via try/except).
ALTER TABLE members ADD COLUMN IF NOT EXISTS display_name TEXT NOT NULL DEFAULT '';

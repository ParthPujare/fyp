-- Policy rows and skills on the shared database (Mk13 Phase 6): app/harness/pg_policy.py,
-- app/skills/pg_registry.py.
--
-- Replaces app/harness/policies/policy.csv and skills/<scope>/<name>/skill.md as the *runtime*
-- source when DATABASE_URL is set, so a rule or skill added on one laptop is live on all of them.
-- The files stay in git as the seed: the first process to start on an empty database copies them
-- in (claiming meta 'policy_seeded' / 'skills_seeded' in the same transaction), and
-- `python -m app.db sync-config` brings later git changes across on purpose.
--
-- Every write to either table also bumps meta 'members_version' in the same statement: that
-- counter is what each laptop's enforcer already polls to rebuild (see app/db/version.py).

-- One row per Casbin rule, keyed by the rule itself (a rule exists once however often it is added).
-- p = (sub, dom, obj, act); g = (user, role, domain) with v3 = '' — model.conf defines both.
CREATE TABLE policy_rules (
    seq        BIGINT GENERATED ALWAYS AS IDENTITY,   -- load order = file order, for readable dumps
    ptype      TEXT NOT NULL CHECK (ptype IN ('p', 'g')),
    v0         TEXT NOT NULL,
    v1         TEXT NOT NULL,
    v2         TEXT NOT NULL,
    v3         TEXT NOT NULL DEFAULT '',
    source     TEXT NOT NULL CHECK (source IN ('file', 'dashboard')),  -- sync-config --prune only touches 'file'
    created_at TEXT NOT NULL,
    created_by TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (ptype, v0, v1, v2, v3)
);
CREATE INDEX idx_policy_rules_seq ON policy_rules (seq);

-- One row per skill: the whole skill.md document (frontmatter + body), parsed by the same code as
-- the files. `scope` is a copy of the frontmatter's, for listing without parsing.
CREATE TABLE skills (
    name       TEXT PRIMARY KEY,
    scope      TEXT NOT NULL,
    document   TEXT NOT NULL,
    source     TEXT NOT NULL CHECK (source IN ('file', 'dashboard')),
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);

-- Core tables for the shared database (Mk13 Phase 2): members/identity, approvals, memory facts.
--
-- Column types deliberately mirror the SQLite schema (TEXT timestamps and JSON, INTEGER 0/1
-- flags): the store code is shared between both back-ends, so `active = 1`, ISO-8601 strings
-- and `json.loads(...)` must behave identically. Do not "improve" these to boolean/timestamptz
-- here without changing the stores.

-- ---- members / identity (app/auth/store.py) ---------------------------------------------
CREATE TABLE members (
    username      TEXT PRIMARY KEY,
    password_hash TEXT NOT NULL,
    role          TEXT NOT NULL,
    dept          TEXT NOT NULL,
    scopes        TEXT NOT NULL,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

-- `members_version` is the change counter that makes every laptop's permission cache refresh.
CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
INSERT INTO meta (key, value) VALUES ('members_version', '0');

CREATE TABLE departments (
    name        TEXT PRIMARY KEY,
    service     TEXT NOT NULL,
    service_url TEXT NOT NULL,
    resources   TEXT NOT NULL,
    role        TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE member_llm_keys (
    id                TEXT PRIMARY KEY,
    username          TEXT NOT NULL,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    api_key_encrypted TEXT NOT NULL,
    base_url          TEXT NOT NULL DEFAULT '',
    active            INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL,
    UNIQUE (username, provider, model)
);
CREATE INDEX idx_member_llm_keys_user ON member_llm_keys (username);

CREATE TABLE member_composio_keys (
    username          TEXT PRIMARY KEY,
    api_key_encrypted TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    updated_at        TEXT NOT NULL
);

CREATE TABLE connected_accounts (
    username   TEXT NOT NULL,
    app        TEXT NOT NULL,
    account_id TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'pending',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (username, app)
);

CREATE TABLE toolkit_catalog (
    toolkit        TEXT PRIMARY KEY,
    label          TEXT NOT NULL DEFAULT '',
    enabled        INTEGER NOT NULL DEFAULT 1,
    auth_config_id TEXT NOT NULL DEFAULT '',
    updated_at     TEXT NOT NULL
);

CREATE TABLE toolkit_tools (
    toolkit      TEXT NOT NULL,
    slug         TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    input_schema TEXT NOT NULL DEFAULT '{}',
    risk         TEXT NOT NULL DEFAULT 'read',
    updated_at   TEXT NOT NULL,
    PRIMARY KEY (toolkit, slug)
);

-- ---- approvals (app/memory/approvals.py) ------------------------------------------------
CREATE TABLE approvals (
    id         TEXT PRIMARY KEY,
    run_id     TEXT NOT NULL,
    thread_id  TEXT NOT NULL,
    resource   TEXT NOT NULL,
    actor      TEXT NOT NULL,
    payload    TEXT NOT NULL,
    status     TEXT NOT NULL,
    created_ts TEXT NOT NULL,
    decided_ts TEXT,
    decided_by TEXT
);
CREATE INDEX idx_approvals_status ON approvals (status, created_ts);
CREATE INDEX idx_approvals_run ON approvals (run_id);

-- ---- long-term memory facts (app/memory/store.py) ---------------------------------------
-- SQLite orders ties by its implicit `rowid`; Postgres has none, so `seq` plays that role
-- (insertion order; an upsert keeps the row's original seq, exactly like rowid).
CREATE TABLE memory_items (
    ns         TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    seq        BIGINT GENERATED ALWAYS AS IDENTITY,
    PRIMARY KEY (ns, key)
);
CREATE INDEX idx_memory_ns ON memory_items (ns);

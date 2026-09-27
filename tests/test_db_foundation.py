"""Mk13 Phase 1 — shared-database foundation (pool, DSN safety, SQL glue, migrations, CLI).

Two tiers, so the offline suite stays offline:
- **Unit tests** always run: DSN redaction, placeholder translation, migration discovery,
  settings parsing, the "no DATABASE_URL" behaviour.
- **Postgres tests** run only when `TEST_DATABASE_URL` points at a disposable database
  (`make test-pg` starts one in Docker). Each test gets its own throwaway schema, so nothing
  leaks between tests and the database's real tables are never touched. Use the *direct* host
  (not Neon's `-pooler` one) and `127.0.0.1` rather than `localhost` (IPv6 stalls on Windows).
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

import psycopg
import pytest

from app.config import Settings
from app.db import DatabaseNotConfigured, get_pool, is_enabled
from app.db import pool as pool_mod
from app.db.__main__ import main as db_cli
from app.db.dsn import describe_dsn, redact_dsn
from app.db.migrate import (
    MIGRATIONS_DIR,
    Migration,
    MigrationError,
    apply_pending,
    checksum_of,
    discover,
    status,
)
from app.db.sql import qmark_to_format

TEST_URL = os.environ.get("TEST_DATABASE_URL")
needs_pg = pytest.mark.skipif(not TEST_URL, reason="set TEST_DATABASE_URL (make test-pg) for Postgres tests")


# =============================================================== DSN safety (unit)
def test_redact_url_form_hides_the_password_only():
    out = redact_dsn("postgresql://alice:s3cret%40pw@ep-cool-1-pooler.neon.tech:5432/neondb?sslmode=require")
    assert "s3cret" not in out
    assert out == "postgresql://alice:***@ep-cool-1-pooler.neon.tech:5432/neondb?sslmode=require"


def test_redact_leaves_a_passwordless_dsn_alone_and_handles_keyvalue_form():
    assert redact_dsn("postgresql://alice@localhost/db") == "postgresql://alice@localhost/db"
    kv = redact_dsn("host=h dbname=d user=u password=hunter2 sslmode=require")
    assert "hunter2" not in kv and "password=***" in kv
    assert "quoted pw" not in redact_dsn("host=h password='quoted pw'")


def test_describe_dsn_reports_pooler_and_never_the_password():
    info = describe_dsn("postgresql://u:pw@ep-x-pooler.eu-central-1.aws.neon.tech/neondb?sslmode=require")
    assert info["pooled"] == "yes" and info["database"] == "neondb" and info["sslmode"] == "require"
    assert "pw" not in "".join(info.values())
    assert describe_dsn("postgresql://u:pw@ep-x.neon.tech/db")["pooled"] == "no"


# ================================================================ SQL glue (unit)
@pytest.mark.parametrize(
    ("sql", "expected"),
    [
        ("SELECT * FROM t WHERE a = ? AND b = ?", "SELECT * FROM t WHERE a = %s AND b = %s"),
        ("SELECT '?' , ?", "SELECT '?' , %s"),  # ? inside a string literal is text
        ("SELECT \"we?ird\" FROM t WHERE x = ?", 'SELECT "we?ird" FROM t WHERE x = %s'),
        ("SELECT 'it''s ?' , ?", "SELECT 'it''s ?' , %s"),  # doubled quote stays inside the literal
        ("WHERE name LIKE '%x%' AND id = ?", "WHERE name LIKE '%%x%%' AND id = %s"),  # literal % doubled
        ("SELECT 1 -- what? 100%\nWHERE a = ?", "SELECT 1 -- what? 100%%\nWHERE a = %s"),
        ("INSERT INTO t VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", "INSERT INTO t VALUES (%s, %s) ON CONFLICT(k) DO UPDATE SET v = excluded.v"),
        ("SELECT 1", "SELECT 1"),
    ],
)
def test_qmark_to_format(sql, expected):
    assert qmark_to_format(sql) == expected


# ========================================================= migrations, files (unit)
def _write(dirpath: Path, name: str, body: str = "SELECT 1;\n") -> None:
    (dirpath / name).write_text(body, encoding="utf-8", newline="")


def test_shipped_migrations_are_valid_and_start_with_the_baseline():
    migrations = discover()
    assert migrations and migrations[0].version == "0001" and migrations[0].name == "baseline"
    assert MIGRATIONS_DIR.is_dir()


def test_discover_rejects_bad_names_gaps_and_duplicates(tmp_path):
    _write(tmp_path, "0001_ok.sql")
    _write(tmp_path, "0003_gap.sql")
    with pytest.raises(MigrationError, match="contiguous"):
        discover(tmp_path)

    dup = tmp_path / "dup"
    dup.mkdir()
    _write(dup, "0001_a.sql")
    _write(dup, "0001_b.sql")
    with pytest.raises(MigrationError, match="duplicate"):
        discover(dup)

    bad = tmp_path / "bad"
    bad.mkdir()
    _write(bad, "add users.sql")
    with pytest.raises(MigrationError, match="bad migration filename"):
        discover(bad)


def test_checksum_ignores_line_endings_so_windows_checkouts_agree():
    assert checksum_of("a\nb\n") == checksum_of("a\r\nb\r\n") == checksum_of("a\rb\r")
    assert checksum_of("a\nb\n") != checksum_of("a\nc\n")


# ================================================ settings + "not configured" (unit)
def test_database_url_setting_blank_means_unset(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert Settings(_env_file=None).database_url is None
    assert Settings(_env_file=None, database_url="   ").database_url is None
    monkeypatch.setenv("DATABASE_URL", "")  # what `DATABASE_URL=` in .env produces
    assert Settings(_env_file=None).database_url is None
    monkeypatch.setenv("DATABASE_URL", "  postgresql://u:p@h/db  ")
    assert Settings(_env_file=None).database_url == "postgresql://u:p@h/db"


def test_without_database_url_the_layer_is_off_and_says_so(monkeypatch):
    monkeypatch.setattr(pool_mod, "database_url", lambda: None)
    assert is_enabled() is False
    with pytest.raises(DatabaseNotConfigured, match="DATABASE_URL"):
        get_pool()


def test_cli_without_database_url(monkeypatch, capsys):
    monkeypatch.setattr(pool_mod, "database_url", lambda: None)
    monkeypatch.setattr("app.db.__main__.is_enabled", lambda: False)
    assert db_cli(["migrate", "--if-configured"]) == 0  # start-up scripts rely on this being a no-op
    assert "local SQLite" in capsys.readouterr().out
    assert db_cli(["migrate"]) == 1
    assert db_cli(["check"]) == 1


def test_cli_connect_failure_is_friendly_and_never_prints_the_password(monkeypatch, capsys):
    secret = "s3cretpw-do-not-print"
    url = f"postgresql://someone:{secret}@127.0.0.1:1/nodb"  # port 1: refused immediately
    monkeypatch.setattr(pool_mod, "database_url", lambda: url)
    monkeypatch.setattr("app.db.__main__.database_url", lambda: url)
    monkeypatch.setattr("app.db.__main__.is_enabled", lambda: True)
    monkeypatch.setattr(pool_mod, "PROBE_ATTEMPTS", 1)
    monkeypatch.setattr(pool_mod, "_pool", None)
    with pytest.raises(SystemExit) as exc:
        db_cli(["check"])
    assert exc.value.code == 1
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert "FAILED to connect" in captured.err and "hint:" in captured.err


def test_probe_fails_fast_on_unfixable_errors_but_retries_transient_ones(monkeypatch):
    calls: list[str] = []

    def connect_fatal(*_a, **_k):
        calls.append("x")
        raise psycopg.OperationalError('FATAL:  password authentication failed for user "u"')

    monkeypatch.setattr(pool_mod.psycopg, "connect", connect_fatal)
    monkeypatch.setattr(pool_mod.time, "sleep", lambda _s: None)
    with pytest.raises(psycopg.OperationalError, match="password authentication failed"):
        pool_mod._probe("postgresql://u:p@h/db", {})
    assert len(calls) == 1  # no retries: waiting cannot fix a wrong password

    for dns_error in ("failed to resolve host 'testdbb': [Errno -5] No address associated with hostname",):
        calls.clear()
        monkeypatch.setattr(
            pool_mod.psycopg, "connect", lambda *_a, _e=dns_error, **_k: (calls.append("x"), (_ for _ in ()).throw(psycopg.OperationalError(_e)))[1]
        )
        with pytest.raises(psycopg.OperationalError, match="resolve host"):
            pool_mod._probe("postgresql://u:p@testdbb/db", {})
        assert len(calls) == 1  # a typo'd hostname is not going to fix itself

    calls.clear()

    def connect_transient(*_a, **_k):
        calls.append("x")
        raise psycopg.OperationalError("connection timeout expired")

    monkeypatch.setattr(pool_mod.psycopg, "connect", connect_transient)
    with pytest.raises(psycopg.OperationalError, match="timeout"):
        pool_mod._probe("postgresql://u:p@h/db", {})
    assert len(calls) == pool_mod.PROBE_ATTEMPTS  # a waking Neon gets every attempt


# ========================================================= real Postgres (integration)
def _migrations(tmp_path: Path, bodies: list[str]) -> list[Migration]:
    for i, body in enumerate(bodies, 1):
        _write(tmp_path, f"{i:04d}_step_{i}.sql", body)
    return discover(tmp_path)


@needs_pg
def test_shipped_migrations_apply_once_and_are_idempotent(pg):
    pool, _admin, _schema = pg
    first = apply_pending(pool)
    assert first == [m.version for m in discover()]
    assert apply_pending(pool) == []  # second run: nothing to do
    assert {r.state for r in status(pool)} == {"applied"}
    with pool.connection() as conn:
        assert conn.execute("SELECT value FROM app_meta WHERE key='schema_epoch'").fetchone()["value"]


@needs_pg
def test_status_moves_from_pending_to_applied(pg, tmp_path):
    pool, _admin, _schema = pg
    migs = _migrations(tmp_path, ["CREATE TABLE a (id int);", "CREATE TABLE b (id int);"])
    assert [r.state for r in status(pool, migs)] == ["pending", "pending"]
    apply_pending(pool, migs)
    assert [r.state for r in status(pool, migs)] == ["applied", "applied"]


@needs_pg
def test_editing_an_applied_migration_is_refused(pg, tmp_path):
    pool, _admin, _schema = pg
    migs = _migrations(tmp_path, ["CREATE TABLE a (id int);"])
    apply_pending(pool, migs)
    (tmp_path / "0001_step_1.sql").write_text("CREATE TABLE a (id int, sneaky text);", encoding="utf-8")
    edited = discover(tmp_path)
    assert [r.state for r in status(pool, edited)] == ["edited"]
    with pytest.raises(MigrationError, match="edited after it was applied"):
        apply_pending(pool, edited)


@needs_pg
def test_crlf_checkout_of_an_applied_migration_is_not_an_edit(pg, tmp_path):
    pool, _admin, _schema = pg
    _write(tmp_path, "0001_step_1.sql", "CREATE TABLE a (id int);\nCREATE TABLE b (id int);\n")
    apply_pending(pool, discover(tmp_path))
    _write(tmp_path, "0001_step_1.sql", "CREATE TABLE a (id int);\r\nCREATE TABLE b (id int);\r\n")
    assert apply_pending(pool, discover(tmp_path)) == []  # same content, different line endings


@needs_pg
def test_a_failing_migration_rolls_back_completely(pg, tmp_path):
    pool, _admin, _schema = pg
    migs = _migrations(
        tmp_path,
        ["CREATE TABLE good (id int);", "CREATE TABLE half_done (id int); SELECT 1/0;"],
    )
    with pytest.raises(MigrationError, match=r"0002_step_2\.sql failed"):
        apply_pending(pool, migs)
    with pool.connection() as conn:
        assert conn.execute("SELECT to_regclass('good') AS t").fetchone()["t"] is not None  # 0001 stayed
        assert conn.execute("SELECT to_regclass('half_done') AS t").fetchone()["t"] is None  # 0002 undone
        recorded = [r["version"] for r in conn.execute("SELECT version FROM schema_migrations ORDER BY 1")]
    assert recorded == ["0001"]


@needs_pg
def test_simultaneous_migrators_apply_each_migration_exactly_once(pg, tmp_path):
    """Several laptops booting at once: no duplicate-table errors, nothing applied twice."""
    pool, _admin, _schema = pg
    migs = _migrations(tmp_path, [f"CREATE TABLE t{i} (id int);" for i in range(1, 5)])
    results: list[list[str]] = []
    errors: list[BaseException] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        try:
            barrier.wait(timeout=10)
            results.append(apply_pending(pool, migs))
        except BaseException as exc:  # noqa: BLE001 - collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join(timeout=60) for t in threads]
    assert not errors, errors
    flat = sorted(v for applied in results for v in applied)
    assert flat == ["0001", "0002", "0003", "0004"]  # each exactly once across all six runners


@needs_pg
def test_database_ahead_of_checkout_is_reported_not_fatal(pg, tmp_path):
    pool, _admin, _schema = pg
    migs = _migrations(tmp_path, ["CREATE TABLE a (id int);", "CREATE TABLE b (id int);"])
    apply_pending(pool, migs)
    older = migs[:1]  # a teammate's older checkout
    assert [(r.version, r.state) for r in status(pool, older)] == [("0001", "applied"), ("0002", "unknown")]
    logs: list[str] = []
    assert apply_pending(pool, older, log=logs.append) == []
    assert any("WARNING" in line and "0002" in line for line in logs)


@needs_pg
def test_pool_replaces_an_idle_connection_the_server_killed(pg, monkeypatch):
    """Neon drops idle connections on suspend; a connection idle past the threshold is pinged."""
    import time

    pool, admin, _schema = pg
    monkeypatch.setattr(pool_mod, "CHECK_AFTER_IDLE_S", 0)  # every checkout counts as "idle"
    with pool.connection() as conn:
        pid = conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"]
    admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.3)  # let the backend actually exit
    with pool.connection() as conn:  # would raise AdminShutdown without the idle ping
        assert conn.execute("SELECT 1 AS one").fetchone()["one"] == 1
        assert conn.execute("SELECT pg_backend_pid() AS p").fetchone()["p"] != pid


@needs_pg
def test_recently_used_connections_are_not_pinged(pg, monkeypatch):
    """At ~150 ms per Neon round trip an unconditional ping would double every query."""
    pool, _admin, _schema = pg
    pings: list[int] = []
    monkeypatch.setattr(
        pool_mod.ConnectionPool, "check_connection", staticmethod(lambda c: pings.append(1))
    )
    for _ in range(5):
        with pool.connection() as conn:
            conn.execute("SELECT 1")
    assert pings == []  # all five checkouts reused a connection used moments ago


@needs_pg
def test_placeholder_translation_runs_real_statements(pg):
    pool, _admin, _schema = pg
    with pool.connection() as conn:
        conn.execute("CREATE TABLE kv (k text PRIMARY KEY, v text NOT NULL)")
        upsert = qmark_to_format(
            "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v"
        )
        conn.execute(upsert, ("a", "1"))
        conn.execute(upsert, ("a", "2"))
        conn.execute(upsert, ("b%c", "3"))  # a % inside a *parameter* must be harmless
        rows = conn.execute(qmark_to_format("SELECT k, v FROM kv WHERE k LIKE 'a%' OR k = ? ORDER BY k"), ("b%c",)).fetchall()
    assert [(r["k"], r["v"]) for r in rows] == [("a", "2"), ("b%c", "3")]

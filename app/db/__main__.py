"""`python -m app.db {check,migrate,status}` — diagnose and prepare the shared database.

  check    connect, time it, show the server/migration state (exit 1 if it cannot connect)
  migrate  apply pending migrations   (--if-configured: silently do nothing without DATABASE_URL,
           which is how the start-up scripts call it so SQLite-only setups are unaffected)
  status   list every migration as applied / pending / edited / unknown
  prune    free space on the shared database (the Neon free tier is 512 MB):
             --chats                     keep only each chat's latest checkpoint (chat history and
                                         resuming a paused run keep working; per-step history goes)
             --traces-older-than DAYS    delete run traces older than DAYS days
             --episodic-older-than DAYS  forget recalled conversations older than DAYS days
             --dry-run                   only report what would be removed
  verify-audit  walk the whole shared audit chain and report tampering (exit 1 if it is broken);
                the audit log is append-only and is never pruned
  sync-config   bring policy.csv / skills/ changes from this checkout into the shared database
                (the first start-up seeds them automatically; later git changes need this):
             --dry-run           only list the differences
             --prune             also remove rules seeded from policy.csv that it no longer has
                                 (rules added from the dashboard are never removed)
             --overwrite-skills  replace database skills whose skill.md on disk differs

The connection string is never printed — only host, database and user.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from datetime import UTC, datetime, timedelta

import psycopg
from psycopg_pool import PoolTimeout

from app.db import close_pool, database_url, get_pool, is_enabled
from app.db.dsn import describe_dsn, redact_dsn
from app.db.migrate import MigrationError, apply_pending, discover, status

_HINTS = (
    ("password authentication failed", "wrong user or password — re-copy the connection string from the Neon dashboard"),
    ("could not translate host name", "host not found — check the connection string for typos"),
    ("name or service not known", "host not found — check the connection string for typos"),
    ("getaddrinfo failed", "host not found — check the connection string / your internet connection"),
    ("failed to resolve host", "host not found — check the connection string for typos and that you are online"),
    ("no address associated with hostname", "host not found — check the connection string for typos and that you are online"),
    ("does not exist", "that database name does not exist on the server"),
    ("ssl", "the server requires TLS — add ?sslmode=require to the connection string"),
    ("timeout", "no answer in time — Neon may still be waking from idle (retry in a few seconds), or a VPN/firewall blocks it"),
    ("connection refused", "nothing is listening there — wrong host/port, or the database is not running"),
)


def explain(exc: BaseException) -> str:
    text = str(exc).lower()
    for needle, hint in _HINTS:
        if needle in text:
            return hint
    return "see the error above"


def _connect_or_die():
    try:
        return get_pool()
    except (psycopg.Error, PoolTimeout) as exc:
        print(f"FAILED to connect: {exc}", file=sys.stderr)
        print(f"  hint: {explain(exc)}", file=sys.stderr)
        raise SystemExit(1) from exc


def cmd_check() -> int:
    if not is_enabled():
        print("DATABASE_URL is not set — the app is using local SQLite files (nothing to check).")
        return 1
    url = database_url() or ""
    info = describe_dsn(url)
    print(f"target   : {redact_dsn(url)}")
    print(f"host/db  : {info['host']}:{info['port']} / {info['database']}  (user {info['user']})")
    print(f"pooled   : {info['pooled']}   sslmode: {info['sslmode']}")
    started = time.perf_counter()
    pool = _connect_or_die()
    print(f"connect  : OK in {(time.perf_counter() - started) * 1000:.0f} ms (includes any cold start)")
    with pool.connection() as conn:
        row = conn.execute("SELECT version() AS v, current_database() AS d, current_user AS u").fetchone()
        print(f"server   : {row['v'].split(',')[0]}")
        timings = []
        for _ in range(5):
            t0 = time.perf_counter()
            conn.execute("SELECT 1")
            timings.append((time.perf_counter() - t0) * 1000)
    print(f"latency  : median {statistics.median(timings):.0f} ms, max {max(timings):.0f} ms per round trip")
    if info["host"] == "localhost":
        print("tip      : use 127.0.0.1 instead of 'localhost' — on Windows/Docker Desktop 'localhost' tries IPv6 first and can stall ~30 s")
    if "neon.tech" in info["host"] and info["pooled"] == "no":
        print("tip      : use Neon's *pooled* host (contains '-pooler') for the app — many laptops share the free tier")
    rows = status(pool)
    for r in rows:
        print(f"migration: {r.version}_{r.name:<24} {r.state}")
    if any(r.state == "edited" for r in rows):
        print("PROBLEM  : an applied migration file was edited — restore it and add a new migration instead")
        return 1
    if any(r.state == "unknown" for r in rows):
        print("WARNING  : the database is ahead of this checkout — `git pull`")
    if any(r.state == "pending" for r in rows):
        print("note     : pending migrations — run `python -m app.db migrate` (containers do it at start-up)")
    with pool.connection() as conn:
        meta = conn.execute("SELECT to_regclass('app_meta') AS t").fetchone()["t"]
        if meta:
            epoch = conn.execute("SELECT value FROM app_meta WHERE key = 'schema_epoch'").fetchone()
            if epoch:
                print(f"epoch    : {epoch['value']}")
    return 0


def cmd_migrate(if_configured: bool) -> int:
    if not is_enabled():
        if if_configured:
            print("db: DATABASE_URL not set — using local SQLite, no migrations to apply.")
            return 0
        print("DATABASE_URL is not set — nothing to migrate.", file=sys.stderr)
        return 1
    pool = _connect_or_die()
    try:
        applied = apply_pending(pool, log=lambda m: print(f"db: {m}"))
    except MigrationError as exc:
        print(f"db: MIGRATION FAILED — {exc}", file=sys.stderr)
        return 1
    if not applied:
        print(f"db: up to date ({len(discover())} migration(s) already applied).")
    return 0


def cmd_status() -> int:
    if not is_enabled():
        print("DATABASE_URL is not set — nothing to report.")
        return 1
    pool = _connect_or_die()
    for r in status(pool):
        print(f"{r.version}_{r.name:<24} {r.state}")
    return 0


def _db_bytes(pool) -> dict[str, int]:
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT pg_database_size(current_database()) AS total,"
            " COALESCE((SELECT SUM(pg_total_relation_size(c.oid)) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            "  WHERE c.relkind = 'r' AND n.nspname = current_schema() AND c.relname LIKE 'checkpoint%%'), 0)::bigint AS chats,"
            " COALESCE((SELECT SUM(pg_total_relation_size(c.oid)) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace"
            "  WHERE c.relkind = 'r' AND n.nspname = current_schema() AND c.relname LIKE 'trace%%'), 0)::bigint AS traces"
        ).fetchone()
    return {"total": row["total"], "chats": row["chats"], "traces": row["traces"]}


def _mb(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def cmd_prune(chats: bool, trace_days: int | None, dry_run: bool, episodic_days: int | None = None, redact: bool = False) -> int:
    if not is_enabled():
        print("DATABASE_URL is not set — nothing to prune.", file=sys.stderr)
        return 1
    if not chats and trace_days is None and episodic_days is None:
        print("nothing to do: pass --chats, --traces-older-than DAYS and/or --episodic-older-than DAYS (see --help)", file=sys.stderr)
        return 1
    pool = _connect_or_die()
    before = _db_bytes(pool)
    print(f"before   : database {_mb(before['total'])} (chat state {_mb(before['chats'])}, traces {_mb(before['traces'])})")
    if chats:
        with pool.connection() as conn:
            threads = [r["thread_id"] for r in conn.execute("SELECT DISTINCT thread_id FROM checkpoints").fetchall()]
            extra = conn.execute(
                "SELECT COUNT(*) - COUNT(DISTINCT (thread_id, checkpoint_ns)) AS n FROM checkpoints"
            ).fetchone()["n"]
        print(f"chats    : {len(threads)} thread(s), {extra} superseded checkpoint(s) {'would be' if dry_run else 'to be'} removed")
        if threads and not dry_run:
            from app.agent.checkpoints import prune_chats

            removed = prune_chats(pool)
            print(f"           removed {removed['checkpoints']} checkpoint(s), {removed['writes']} write(s), {removed['blobs']} blob(s)")
    if trace_days is not None:
        cutoff = (datetime.now(UTC) - timedelta(days=trace_days)).isoformat()
        with pool.connection() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM trace_runs WHERE started_ts < %s", (cutoff,)).fetchone()["n"]
            verb = "redacted (payloads scrubbed, chain rehashed)" if redact else "deleted"
            print(f"traces   : {n} run(s) older than {trace_days} day(s) {'would be' if dry_run else 'to be'} {verb}")
            if n and not dry_run:
                if redact:
                    import json as _json

                    from app.harness.sanitize import redact_for_retention
                    from app.ops.store import GENESIS, TraceStore

                    run_ids = [r["run_id"] for r in conn.execute("SELECT run_id FROM trace_runs WHERE started_ts < %s", (cutoff,)).fetchall()]
                    total = 0
                    for rid in run_ids:
                        rows = conn.execute(
                            "SELECT event_id, seq, ts, type, source, actor, payload"
                            " FROM trace_events WHERE run_id = %s ORDER BY seq",
                            (rid,),
                        ).fetchall()
                        prev = GENESIS
                        for r in rows:
                            actor = _json.loads(r["actor"])
                            new_payload = redact_for_retention(_json.loads(r["payload"]))
                            content = {
                                "run_id": rid, "seq": r["seq"], "ts": r["ts"],
                                "type": r["type"], "source": r["source"],
                                "actor": actor, "payload": new_payload,
                            }
                            conn.execute(
                                "UPDATE trace_events SET payload = %s, prev_hash = %s WHERE event_id = %s",
                                (_json.dumps(new_payload, sort_keys=True, default=str), prev, r["event_id"]),
                            )
                            prev = TraceStore.hash_event(prev, content)
                        conn.execute("UPDATE trace_runs SET head_hash = %s WHERE run_id = %s", (prev, rid))
                        total += len(rows)
                    print(f"           redacted {total} event(s) across {len(run_ids)} run(s)")
                else:
                    conn.execute("DELETE FROM trace_events WHERE run_id IN (SELECT run_id FROM trace_runs WHERE started_ts < %s)", (cutoff,))
                    conn.execute("DELETE FROM trace_runs WHERE started_ts < %s", (cutoff,))
    if episodic_days is not None:
        cutoff = (datetime.now(UTC) - timedelta(days=episodic_days)).isoformat()
        with pool.connection() as conn:
            n = conn.execute("SELECT COUNT(*) AS n FROM episodic WHERE ts < %s", (cutoff,)).fetchone()["n"]
            print(f"episodic : {n} transcript(s) older than {episodic_days} day(s) {'would be' if dry_run else 'to be'} forgotten")
            if n and not dry_run:
                conn.execute("DELETE FROM episodic WHERE ts < %s", (cutoff,))
    if dry_run:
        print("dry run  : nothing was changed")
        return 0
    after = _db_bytes(pool)
    print(f"after    : database {_mb(after['total'])} (chat state {_mb(after['chats'])}, traces {_mb(after['traces'])})")
    print("note     : Postgres reuses freed space but the file may not shrink at once (VACUUM runs automatically)")
    return 0


def cmd_verify_audit(log_name: str | None) -> int:
    if not is_enabled():
        print("DATABASE_URL is not set — the audit log is the local JSONL file (nothing to verify here).", file=sys.stderr)
        return 1
    from pathlib import Path

    from app.config import get_settings
    from app.harness.pg_audit import PgAuditLog

    name = log_name or Path(get_settings().services.audit_path).stem
    _connect_or_die()
    started = time.perf_counter()
    log = PgAuditLog(log_name=name)
    ok, issues = log.verify_integrity(full=True)
    elapsed = (time.perf_counter() - started) * 1000
    n = len(log)
    print(f"audit    : log {name!r}, {n} entr{'y' if n == 1 else 'ies'}, checked in {elapsed:.0f} ms")
    if ok:
        print("result   : chain intact")
        return 0
    print(f"result   : BROKEN — {len(issues)} issue(s)", file=sys.stderr)
    for issue in issues[:50]:
        print(f"  - {issue}", file=sys.stderr)
    if len(issues) > 50:
        print(f"  ... and {len(issues) - 50} more", file=sys.stderr)
    print("hint     : a key mismatch looks like tampering too — every laptop must use the same SERVICES__AUDIT_HMAC_KEY", file=sys.stderr)
    return 1


def cmd_sync_config(dry_run: bool, prune: bool, overwrite_skills: bool) -> int:
    """Bring this checkout's policy.csv / skills/ into the shared database (Mk13 Phase 6)."""
    if not is_enabled():
        print("DATABASE_URL is not set — policy.csv and skills/ are read from disk directly (nothing to sync).")
        return 1
    from app.config import get_settings
    from app.harness.pg_policy import PgPolicyStore
    from app.harness.policy_store import POLICY_PATH
    from app.skills.pg_registry import PgSkillRegistry

    pool = _connect_or_die()
    policy = PgPolicyStore(pool)  # seeds an empty database first, like a service start-up would
    skills = PgSkillRegistry(pool, root=get_settings().skills.skills_dir)

    missing, stale = policy.drift(POLICY_PATH)
    print(f"policy   : {len(missing)} rule(s) in policy.csv missing from the database")
    for row in missing:
        print(f"           + {', '.join(r for r in row if r)}")
    print(f"           {len(stale)} rule(s) seeded from policy.csv earlier but no longer in it"
          f"{'' if prune else ' (kept — pass --prune to remove)'}")
    for row in stale:
        print(f"           {'-' if prune else '?'} {', '.join(r for r in row if r)}")

    s_missing, s_differs, errors = skills.drift()
    print(f"skills   : {len(s_missing)} skill(s) in skills/ missing from the database")
    for s in s_missing:
        print(f"           + {s.name} ({s.scope})")
    print(f"           {len(s_differs)} skill(s) differ from the database copy"
          f"{'' if overwrite_skills else ' (database copy kept — pass --overwrite-skills to replace it)'}")
    for s in s_differs:
        print(f"           {'~' if overwrite_skills else '?'} {s.name}")
    for err in errors:
        print(f"           ! not importable: {err}")

    if dry_run:
        print("dry run  : nothing was changed")
        return 0
    added = policy.add_rows(missing, "sync-config", source="file")
    removed = policy.remove_rows(stale) if prune else 0
    imported = skills.import_skills(s_missing + (s_differs if overwrite_skills else []), overwrite=overwrite_skills)
    print(f"done     : +{added} rule(s), -{removed} rule(s), {imported} skill(s) written;"
          " every laptop picks this up within ~1 s")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.db", description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="connect and report")
    mig = sub.add_parser("migrate", help="apply pending migrations")
    mig.add_argument("--if-configured", action="store_true", help="do nothing (exit 0) without DATABASE_URL")
    sub.add_parser("status", help="list migrations")
    pr = sub.add_parser("prune", help="free space on the shared database")
    pr.add_argument("--chats", action="store_true", help="keep only each chat's latest checkpoint")
    pr.add_argument("--traces-older-than", type=int, metavar="DAYS", help="delete run traces older than DAYS days")
    pr.add_argument("--redact", action="store_true", help="with --traces-older-than: scrub PII payloads and rehash chains instead of deleting")
    pr.add_argument("--episodic-older-than", type=int, metavar="DAYS", help="forget recalled conversations older than DAYS days")
    pr.add_argument("--dry-run", action="store_true", help="only report what would be removed")
    va = sub.add_parser("verify-audit", help="verify the whole shared audit chain")
    va.add_argument("--log", metavar="NAME", help="log name (default: the stem of SERVICES__AUDIT_PATH, i.e. 'audit')")
    sc = sub.add_parser("sync-config", help="copy policy.csv / skills/ changes from this checkout into the database")
    sc.add_argument("--dry-run", action="store_true", help="only report the differences")
    sc.add_argument("--prune", action="store_true", help="also remove rules that were seeded from policy.csv but are no longer in it")
    sc.add_argument("--overwrite-skills", action="store_true", help="replace database skills whose skill.md differs on disk")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "sync-config":
            return cmd_sync_config(args.dry_run, args.prune, args.overwrite_skills)
        if args.cmd == "check":
            return cmd_check()
        if args.cmd == "migrate":
            return cmd_migrate(args.if_configured)
        if args.cmd == "prune":
            return cmd_prune(args.chats, args.traces_older_than, args.dry_run, args.episodic_older_than, redact=args.redact)
        if args.cmd == "verify-audit":
            return cmd_verify_audit(args.log)
        return cmd_status()
    finally:
        close_pool()


if __name__ == "__main__":
    raise SystemExit(main())

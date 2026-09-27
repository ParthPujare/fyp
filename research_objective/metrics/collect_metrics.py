"""Metric harness for the ZTAE paper (see ../01_ztae_formal_spec.md §8).

Run from the repo root:   python research_objective/metrics/collect_metrics.py
Options:                  --skip-attacks   (skip the pytest attack-suite run, ~1 min)

Everything runs offline (MockLLM, temp SQLite/JSONL files). Nothing under data/
is touched. Writes CSVs to research_objective/metrics/results/.

Metrics produced
  attack_results.csv      per-attack pass counts from tests/attack (ASR, PDC evidence)
  tamper_detection.csv    audit-chain detection rate per mutation kind (M1-M4, M5 = full-FS attacker)
  audit_concurrency.csv   chain false-alarm rate under concurrent writers (integrity vs. availability)
  memory_leakage.csv      Leakage(u,q) over randomized recall queries, per back-end (target 0),
                          with a non-vacuity column (own-canary queries that must be found)
  latency.csv             p50/p95/mean ms per op: enforce, audit.log, trace.add_event
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import random
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ["DATABASE_URL"] = ""  # never let a developer's real shared DB leak into the experiments
RESULTS = Path(__file__).resolve().parent / "results"

ATTACK_MAP = {  # tests/attack file -> attack ids (ref_docs/proposed_architecture.md §15)
    "test_policy_matrix": "A1",
    "test_prompt_injection": "A2",
    "test_cross_dept_memory": "A3",
    "test_skill_misuse": "A4",
    "test_loop_abuse": "A5",
    "test_hitl_bypass": "A6",
    "test_integrity": "A7/A8",
    "test_external_deny": "A9",
}


def _write(name: str, header: list[str], rows: list[list]) -> None:
    RESULTS.mkdir(parents=True, exist_ok=True)
    with (RESULTS / name).open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)
    print(f"  -> results/{name}")


def rule_of_three(n: int) -> float:
    """95% upper confidence bound on a rate when 0 events were seen in n trials."""
    return 1.0 if n <= 0 else 1 - 0.05 ** (1 / n)


# ------------------------------------------------------------------ attacks
def attack_results() -> None:
    print("[1/5] attack suites (pytest tests/attack)")
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        xml = Path(tmp) / "attack.xml"
        subprocess.run(
            [sys.executable, "-m", "pytest", "tests/attack", "-p", "no:cacheprovider",
             "-o", "addopts=", "-q", f"--junitxml={xml}"],
            cwd=ROOT, capture_output=True, text=True, check=False,
        )
        tree = ET.parse(xml)
    per: dict[str, list[int]] = {}
    for tc in tree.iter("testcase"):
        mod = tc.get("classname", "").split(".")[-1]
        bad = tc.find("failure") is not None or tc.find("error") is not None
        skipped = tc.find("skipped") is not None
        cell = per.setdefault(mod, [0, 0, 0])
        cell[0] += 1
        cell[1] += 0 if (bad or skipped) else 1
        cell[2] += 1 if bad else 0
    rows = []
    for mod, (total, passed, failed) in sorted(per.items()):
        rows.append([ATTACK_MAP.get(mod, "?"), mod, total, passed, failed,
                     f"{passed / total:.3f}", f"{rule_of_three(total):.3f}"])
        print(f"  {ATTACK_MAP.get(mod, '?'):6} {mod:26} {passed}/{total} pass")
    _write("attack_results.csv",
           ["attack", "suite", "tests", "passed", "failed", "pass_rate", "ub95_if_zero_failures"], rows)


# ------------------------------------------------------------------ tamper
def _mk_audit(path: Path, n: int, key: bytes | None = None) -> None:
    """Build a valid n-entry chain in one write (same record format as AuditLog.log;
    per-call file I/O is too slow to build hundreds of fixtures)."""
    from app.harness.audit import GENESIS, _hash_entry

    prev, out = GENESIS, []
    for i in range(n):
        entry = {"ts": f"2026-01-01T00:00:{i % 60:02d}+00:00", "event_id": f"ev-{i}", "run_id": f"run-{i // 10}",
                 "decision": "ALLOW" if i % 3 else "DENY", "resource": {"type": "tool", "id": f"t{i % 7}"},
                 "actor": {"sub": f"u{i % 5}", "role": "r", "dept": "d"}, "guardrail_hit": None, "reason": "rbac"}
        h = _hash_entry(prev, entry, key)
        out.append(json.dumps({"prev_hash": prev, "hash": h, "entry": entry}, sort_keys=True))
        prev = h
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    path.with_name(path.name + ".head").write_text(prev, encoding="utf-8")


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _mutate(kind: str, path: Path, rng: random.Random) -> None:
    from app.harness.audit import GENESIS, _hash_entry

    lines = _lines(path)
    k = rng.randrange(1, len(lines) - 1)
    if kind == "M1_edit":
        obj = json.loads(lines[k])
        obj["entry"]["decision"] = "ALLOW" if obj["entry"]["decision"] == "DENY" else "DENY"
        lines[k] = json.dumps(obj, sort_keys=True)
    elif kind == "M2_delete":
        del lines[k]
    elif kind == "M3_reorder":
        lines[k], lines[k + 1] = lines[k + 1], lines[k]
    elif kind == "M4_truncate":
        lines = lines[: rng.randrange(1, len(lines) - 1)]
    elif kind == "M5_full_fs_attacker":  # edit + recompute the whole chain + head marker
        obj = json.loads(lines[k])
        obj["entry"]["decision"] = "ALLOW" if obj["entry"]["decision"] == "DENY" else "DENY"
        lines[k] = json.dumps(obj, sort_keys=True)
        prev, out = GENESIS, []
        for ln in lines:
            o = json.loads(ln)
            o["prev_hash"], o["hash"] = prev, _hash_entry(prev, o["entry"])
            prev = o["hash"]
            out.append(json.dumps(o, sort_keys=True))
        lines = out
        path.with_name(path.name + ".head").write_text(prev, encoding="utf-8")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def tamper_detection(trials: int = 50, n_entries: int = 120) -> None:
    print("[2/5] audit tamper detection")
    from app.harness.audit import AuditLog

    rng = random.Random(7)
    rows = []
    key = b"server-only-secret"  # M5b: same attacker, but the chain is HMAC-keyed (attacker lacks the key)
    for kind in ("M1_edit", "M2_delete", "M3_reorder", "M4_truncate", "M5_full_fs_attacker",
                 "M5b_full_fs_attacker_vs_hmac"):
        detected = 0
        keyed = kind == "M5b_full_fs_attacker_vs_hmac"
        for t in range(trials):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                p = Path(tmp) / "audit.jsonl"
                _mk_audit(p, n_entries, key if keyed else None)
                _mutate("M5_full_fs_attacker" if keyed else kind, p, rng)
                ok, _ = AuditLog(p, hmac_key=key if keyed else "").verify_integrity()
                detected += 0 if ok else 1
        rate = detected / trials
        rows.append([kind, trials, detected, f"{rate:.3f}"])
        print(f"  {kind:22} detected {detected}/{trials}")
    _write("tamper_detection.csv", ["mutation", "trials", "detected", "detection_rate"], rows)


# ------------------------------------------------------------------ concurrency
def audit_concurrency(threads_list=(1, 2, 4, 8), per_thread: int = 15, reps: int = 3) -> None:
    print("[3/5] audit chain under concurrent writers (false-alarm rate)")
    from app.harness.audit import AuditLog

    rows = []
    for n_threads in threads_list:
        bad = 0
        lost = 0
        for _ in range(reps):
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
                log = AuditLog(Path(tmp) / "a.jsonl")

                def worker(i: int, log=log) -> None:
                    for _j in range(per_thread):
                        log.log(decision="ALLOW", resource={"type": "tool", "id": "x"},
                                actor={"sub": str(i)}, run_id=f"r{i}")

                ts = [threading.Thread(target=worker, args=(i,)) for i in range(n_threads)]
                [t.start() for t in ts]
                [t.join() for t in ts]
                ok, _ = log.verify_integrity()
                bad += 0 if ok else 1
                lost += n_threads * per_thread - len(log)
        rows.append([n_threads, reps, bad, f"{bad / reps:.2f}", lost])
        print(f"  threads={n_threads}: chain broken in {bad}/{reps} runs, entries lost={lost}")
    _write("audit_concurrency.csv",
           ["writers", "reps", "chain_broken_runs", "false_alarm_rate", "entries_lost_total"], rows)


# ------------------------------------------------------------------ leakage
def _leak_run(make_store, trials: int) -> dict:
    """One randomized recall-isolation experiment against one episodic back-end."""
    from app.auth import users as auth_users
    from app.auth.store import MemberStore
    from app.config import get_settings
    from app.harness.enforcer import PolicyEnforcer
    from app.memory.recall import RecallService

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmpd = Path(tmp)
        prev = auth_users.get_store()
        auth_users.set_store(MemberStore(tmpd / "members.sqlite"))
        cleanup = lambda: None
        try:
            enforcer = PolicyEnforcer()
            epi, cleanup = make_store(tmpd)
            depts = ["marketing", "hr", "finance", "devops"]
            words = ["budget", "salary", "roadmap", "incident", "campaign", "ledger", "payroll", "deploy"]
            rng = random.Random(11)
            secret_of: dict[str, str] = {}
            for d in depts:  # each dept transcript carries a unique canary token
                canary = f"CANARY{d.upper()}"
                secret_of[canary] = d
                for i in range(20):
                    epi.append_transcript(f"dept/{d}", f"seed-{d}-{i}", {"sub": "x", "dept": d},
                                          f"{canary} {rng.choice(words)} {rng.choice(words)} {i}")
            recall = RecallService(epi, enforcer, get_settings().memory)
            users = [u for u in auth_users.get_store().list() if u.active and u.dept in depts]
            r = {"queries": trials, "foreign_asks": 0, "leaks": 0, "own_asks": 0, "own_found": 0, "hits": 0}
            for _ in range(trials):
                u = rng.choice(users)
                claims = {"sub": u.username, "role": u.role, "dept": u.dept}
                canary = rng.choice(list(secret_of))
                out = recall.recall(f"{canary} {rng.choice(words)}", claims).content
                # Only returned hit lines count ("[scope] transcript"); the empty-result
                # message echoes the query (and thus the canary), which is not a leak.
                hit_lines = [ln for ln in out.splitlines() if ln.startswith("[")]
                r["hits"] += len(hit_lines)
                if secret_of[canary] == u.dept:  # non-vacuity: asking for your OWN canary must find it
                    r["own_asks"] += 1
                    r["own_found"] += 1 if any(canary in ln for ln in hit_lines) else 0
                else:
                    r["foreign_asks"] += 1
                    r["leaks"] += 1 if any(canary in ln for ln in hit_lines) else 0
                    r["leaks"] += sum(1 for ln in hit_lines if not ln.startswith(f"[dept/{u.dept}]") and not ln.startswith("[central]")
                                      and not ln.startswith(f"[user/{u.username}]"))
        finally:
            cleanup()
            auth_users.set_store(prev)
    return r


def memory_leakage(trials: int = 400, pg_url: str | None = None) -> None:
    print("[4/5] cross-scope recall leakage (SQLite FTS5 and, if given, Postgres full-text)")
    from app.memory.episodic import EpisodicStore

    backends = [("sqlite-fts5", lambda tmpd: (EpisodicStore(tmpd / "memory.sqlite"), lambda: None))]
    if pg_url:
        def make_pg(_tmpd):
            import uuid

            import psycopg

            from app.db import make_pool
            from app.memory.pg_episodic import PgEpisodicStore

            schema = "leak_" + uuid.uuid4().hex[:10]
            admin = psycopg.connect(pg_url, autocommit=True)
            admin.execute(f'CREATE SCHEMA "{schema}"')
            pool = make_pool(pg_url, kwargs={"options": f"-c search_path={schema}"}, name=schema)

            def cleanup():
                pool.close()
                admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
                admin.close()

            return PgEpisodicStore(pool), cleanup

        backends.append(("postgres-tsvector", make_pg))
    rows = []
    for name, factory in backends:
        r = _leak_run(factory, trials)
        ub = rule_of_three(r["foreign_asks"]) if r["leaks"] == 0 else float("nan")
        rows.append([name, r["queries"], r["foreign_asks"], r["leaks"], f"{r['leaks'] / max(r['foreign_asks'], 1):.4f}",
                     f"{ub:.4f}", r["own_asks"], r["own_found"], r["hits"]])
        print(f"  {name:<18} foreign-canary queries={r['foreign_asks']} leaks={r['leaks']} | "
              f"own-canary queries={r['own_asks']} found={r['own_found']}")
    _write("memory_leakage.csv",
           ["backend", "queries", "foreign_canary_queries", "leaks", "leak_rate", "ub95_if_zero",
            "own_canary_queries", "own_found", "hit_lines_returned"], rows)


# ------------------------------------------------------------------ latency
def _pctl(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    return xs[min(len(xs) - 1, math.ceil(p * len(xs)) - 1)]


def latency(n: int = 2000, n_io: int = 100) -> None:
    print("[5/5] latency micro-benchmarks")
    from app.auth import users as auth_users
    from app.auth.store import MemberStore
    from app.harness.audit import AuditLog
    from app.harness.enforcer import PolicyEnforcer
    from app.ops.store import TraceStore

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        tmpd = Path(tmp)
        prev = auth_users.get_store()
        auth_users.set_store(MemberStore(tmpd / "members.sqlite"))
        try:
            enforcer = PolicyEnforcer()
            audit = AuditLog(tmpd / "a.jsonl")
            trace = TraceStore(tmpd / "t.sqlite")
            actor = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
            trace.start_run(actor, {"m": "bench"}, run_id="bench")

            def timeit(fn, reps: int) -> list[float]:
                out = []
                for _ in range(reps):
                    t0 = time.perf_counter()
                    fn()
                    out.append((time.perf_counter() - t0) * 1000)
                return out

            ops = {
                "enforce_allow": lambda: enforcer.enforce("alice@acme.com", "marketing", "crm", "read"),
                "enforce_deny": lambda: enforcer.enforce("alice@acme.com", "marketing", "hr", "read"),
                "audit_log": lambda: audit.log(decision="ALLOW", resource={"type": "tool", "id": "crm"},
                                               actor=actor, run_id="bench"),
                "trace_add_event": lambda: trace.add_event("bench", "node", actor, {"node": "bench"}),
            }
            rows = []
            for name, fn in ops.items():
                reps = n if name.startswith("enforce") else n_io  # I/O ops are ~ms; keep the run short
                xs = timeit(fn, reps)
                rows.append([name, reps, f"{statistics.mean(xs):.4f}", f"{_pctl(xs, .5):.4f}", f"{_pctl(xs, .95):.4f}"])
                print(f"  {name:16} p50={_pctl(xs, .5):.3f}ms p95={_pctl(xs, .95):.3f}ms")
        finally:
            auth_users.set_store(prev)
    _write("latency.csv", ["op", "n", "mean_ms", "p50_ms", "p95_ms"], rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-attacks", action="store_true")
    ap.add_argument("--pg-url", default=os.environ.get("TEST_DATABASE_URL"),
                    help="disposable Postgres to also measure (default: $TEST_DATABASE_URL); never a real database")
    args = ap.parse_args()
    if not args.skip_attacks:
        attack_results()
    tamper_detection()
    audit_concurrency()
    memory_leakage(pg_url=args.pg_url)
    latency()
    print("done.")


if __name__ == "__main__":
    main()

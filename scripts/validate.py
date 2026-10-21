"""One-command regression check across phases and plans (Mk21).

    uv run python scripts/validate.py            # everything (offline suite + browser tests)
    uv run python scripts/validate.py --quick    # skip the browser tests (~1 minute faster)
    uv run python scripts/validate.py --pg URL   # also run the Postgres-backed tests against URL

Stages, cheapest first, so the first breakage is reported in seconds rather than minutes:

  deps         uv.lock matches pyproject.toml, and every `app.*` module imports
  lint         ruff check app tests scripts
  js           node --check on every dashboard script, inline <script> blocks included
  plans        every plan doc has a registry row; every registry / session link resolves
  ui-contract  ids and status strings the browser tests (and users) depend on still exist
  tests        offline pytest suite (tests/, without tests/e2e)
  e2e          Playwright browser tests (tests/e2e)

Every stage runs with DATABASE_URL="" so nothing here can reach a shared database, even
when `.env` points at one. Exit code 0 only when no stage FAILED (SKIP is not a failure,
but it is printed so it is never mistaken for a pass).
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "app" / "dashboard" / "static"
PLANS = ROOT / "ref_docs" / "plans"
HISTORY = ROOT / "agent_history"

# Anchors the e2e suite and the plans rely on. Removing one is almost always an accident.
UI_IDS = [
    "user", "pass", "signin", "login-view", "sidebar", "nav", "crumb-view", "topbar",
    "chat-msg", "chat-send", "chat-transcript", "chat-model-select",
    "acc-provider", "acc-model", "acc-key", "acc-save", "acc-keys", "acc-status",
    "int-catalog", "int-catalog-admin", "int-catalog-status", "int-apps",
    "approvals", "second-eyes", "users", "member-keys", "member-keys-title", "member-keys-list",
    "member-keys-status", "mk-provider", "mk-model", "mk-key", "mk-save", "member-keys-close",
    "depts", "ops-runs", "ops-runs-view", "ops-filters", "ops-trajectory", "ops-run-title", "ops-back",
]
# Plan docs that pre-date this check and are knowingly unregistered (owner decision pending).
# Any NEW unregistered plan_mk*.md fails the `plans` stage.
UNREGISTERED_OK = {
    "plan_mk5_local_byok.md": "draft alternative to plan_mk5_custom_byot.md (the registered Mk5)",
}

UI_STRINGS = [
    "Key saved and set active for", "Model updated.", "Sent to a second admin to confirm",
    "disabled — members can no longer connect it", "enabled — members can connect it again",
    "LLM keys — ", "Reveal full arguments",
]


def _env() -> dict[str, str]:
    env = dict(os.environ)
    env.update({"DATABASE_URL": "", "OPENROUTER_API_KEY": "", "SYSTEM__OPENROUTER_API_KEY": ""})
    env.setdefault("ALLOW_DEV_SECRETS", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    return env


def _run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[bool, str]:
    proc = subprocess.run(cmd, cwd=ROOT, env=env or _env(), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", check=False)
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def _tail(text: str, n: int = 25) -> str:
    return "\n".join(text.splitlines()[-n:])


# ---------------------------------------------------------------------------- stages


def stage_deps() -> tuple[str, str]:
    ok, out = _run(["uv", "lock", "--check"])
    if not ok:
        return "FAIL", "uv.lock is out of date with pyproject.toml — run `uv lock`:\n" + _tail(out)
    probe = (
        "import importlib, pkgutil, app, sys\n"
        "bad = []\n"
        "for m in pkgutil.walk_packages(app.__path__, 'app.'):\n"
        "    if m.name.endswith('__main__'):\n"
        "        continue\n"
        "    try:\n"
        "        importlib.import_module(m.name)\n"
        "    except Exception as exc:\n"
        "        bad.append(f'{m.name}: {type(exc).__name__}: {exc}')\n"
        "print('\\n'.join(bad) or 'all app modules import')\n"
        "sys.exit(1 if bad else 0)\n"
    )
    ok, out = _run([sys.executable, "-c", probe])
    return ("PASS" if ok else "FAIL"), _tail(out)


def stage_lint() -> tuple[str, str]:
    ok, out = _run(["uv", "run", "ruff", "check", "app", "tests", "scripts"])
    return ("PASS" if ok else "FAIL"), _tail(out, 40)


def stage_js() -> tuple[str, str]:
    node = shutil.which("node")
    if not node:
        return "SKIP", "node not found — install Node.js to syntax-check the dashboard scripts"
    problems, checked = [], 0
    with tempfile.TemporaryDirectory() as tmp:
        for page in sorted(STATIC.glob("*.html")):
            blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", page.read_text(encoding="utf-8"), re.DOTALL)
            for i, block in enumerate(blocks):
                f = Path(tmp) / f"{page.stem}_{i}.js"
                f.write_text(block, encoding="utf-8")
                ok, out = _run([node, "--check", str(f)])
                checked += 1
                if not ok:
                    problems.append(f"{page.name} <script> #{i}:\n{_tail(out, 8)}")
        for js in sorted(STATIC.glob("*.js")):
            ok, out = _run([node, "--check", str(js)])
            checked += 1
            if not ok:
                problems.append(f"{js.name}:\n{_tail(out, 8)}")
    if problems:
        return "FAIL", "\n".join(problems)
    return "PASS", f"{checked} script blocks parse"


def stage_plans() -> tuple[str, str]:
    problems = []
    index = (PLANS / "INDEX.md").read_text(encoding="utf-8")
    for doc in sorted(PLANS.glob("plan_mk*.md")):
        if f"./{doc.name}" not in index and doc.name not in UNREGISTERED_OK:
            problems.append(f"{doc.name} has no row in ref_docs/plans/INDEX.md")
    for link in re.findall(r"\]\(\./([^)]+)\)", index):
        if not (PLANS / link).exists():
            problems.append(f"plans INDEX links missing file {link}")
    hist = HISTORY / "INDEX.md"
    if hist.exists():
        for link in re.findall(r"\]\(\./([^)]+)\)", hist.read_text(encoding="utf-8")):
            if not (HISTORY / link).exists():
                problems.append(f"agent_history INDEX links missing file {link}")
    if problems:
        return "FAIL", "\n".join(problems)
    return "PASS", f"{len(list(PLANS.glob('plan_mk*.md')))} plans registered, all links resolve"


def stage_ui_contract() -> tuple[str, str]:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    missing = [f"#{i}" for i in UI_IDS if not re.search(rf'id="{re.escape(i)}"', html)]
    missing += [f"text {s!r}" for s in UI_STRINGS if s not in html]
    if missing:
        return "FAIL", "missing from index.html: " + ", ".join(missing)
    return "PASS", f"{len(UI_IDS)} ids + {len(UI_STRINGS)} status strings present"


def stage_tests(pg_url: str | None) -> tuple[str, str]:
    env = _env()
    if pg_url:
        env["TEST_DATABASE_URL"] = pg_url
    ok, out = _run(["uv", "run", "pytest", "-p", "no:cacheprovider", "--ignore=tests/e2e", "-rfE"], env)
    return ("PASS" if ok else "FAIL"), _tail(out, 30)


def stage_e2e() -> tuple[str, str]:
    ok, out = _run(["uv", "run", "pytest", "-p", "no:cacheprovider", "tests/e2e", "-rfEs"])
    last = _tail(out, 30)
    if ok and re.search(r"\bskipped\b", last) and not re.search(r"\bpassed\b", last):
        return "SKIP", last  # no browser on this machine: say so, never report it as a pass
    return ("PASS" if ok else "FAIL"), last


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--quick", action="store_true", help="skip the browser (e2e) tests")
    parser.add_argument("--pg", metavar="URL", help="TEST_DATABASE_URL for the Postgres-backed tests")
    parser.add_argument("--only", nargs="*", help="run only these stages")
    args = parser.parse_args()

    stages = [
        ("deps", stage_deps), ("lint", stage_lint), ("js", stage_js), ("plans", stage_plans),
        ("ui-contract", stage_ui_contract), ("tests", lambda: stage_tests(args.pg)),
    ]
    if not args.quick:
        stages.append(("e2e", stage_e2e))
    if args.only:
        stages = [s for s in stages if s[0] in args.only]

    results = []
    for name, fn in stages:
        print(f"==> {name} ...", flush=True)
        started = time.monotonic()
        status, detail = fn()
        took = time.monotonic() - started
        results.append((name, status, took))
        print(f"    {status} ({took:.1f}s)")
        counts = re.findall(r"^=*\s*(\d+ (?:passed|failed).*?) in [\d.]+s", detail, re.MULTILINE)
        if counts:
            print(f"    pytest: {counts[-1]}")  # the real numbers, also on PASS
        if status != "PASS":
            print("    " + detail.replace("\n", "\n    "))
    print("\n" + "-" * 44)
    for name, status, took in results:
        print(f"  {name:<12} {status:<5} {took:7.1f}s")
    failed = [n for n, s, _ in results if s == "FAIL"]
    print("-" * 44)
    print("RESULT:", "FAIL — " + ", ".join(failed) if failed else "PASS")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())

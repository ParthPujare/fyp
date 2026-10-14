"""Scope the admin wildcard into explicit rows (Mk18 Phase E).

Reads a policy CSV, collects every distinct (obj, act) granted to any
non-admin role, and appends the missing `p, admin, admin, <obj>, <act>` rows
idempotently — plus admin-only resources that have no non-admin row at all
(central writes, salary/ssn reads). Verification reports whether the admin
domain covers everything the wildcard used to grant.

Usage:
    python scripts/scope_admin_policy.py [--path FILE] [--check] [--remove-wildcard]

- default: append missing explicit admin rows, then report coverage.
- --check: exit 0 iff wildcard absent AND coverage complete (CI gate).
- --remove-wildcard: delete the `p, admin, admin, *, *` line, but ONLY after
  coverage verifies (refuses otherwise).
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Make `import app...` work regardless of CWD when run as scripts/<this file>.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Admin-only resources with no non-admin row (deny-by-default + wildcard today).
ADMIN_ONLY_EXTRA: tuple[tuple[str, str], ...] = (
    ("memory_central", "write"),
    ("pii_salary", "read"),
    ("pii_ssn", "read"),
)


def _registry_pairs() -> set[tuple[str, str]]:
    """Every (obj, act) the tool registry + integration specs can demand.

    Some scopes (e.g. hr:delete) are denied to every non-admin role by design,
    so collecting policy rows alone would silently drop admin's access.
    """
    pairs: set[tuple[str, str]] = set()
    try:
        from app.agent.tools import TOOL_REGISTRY

        for tool in TOOL_REGISTRY.values():
            dept, _, act = (tool.required_scope or "").partition(":")
            if dept and act:
                pairs.add((dept or tool.dept, act))
    except Exception:  # noqa: BLE001, S110
        pass
    try:
        from app.integrations.adapter import all_specs

        for spec in all_specs():
            dept, _, act = (spec.required_scope or "").partition(":")
            if dept and act:
                pairs.add((dept, act))
    except Exception:  # noqa: BLE001, S110
        pass
    return pairs

_WILDCARD_RE = re.compile(r"^p\s*,\s*admin\s*,\s*admin\s*,\s*\*\s*,\s*\*\s*$")


def _parse_row(line: str) -> tuple[str, ...] | None:
    stripped = line.strip()
    if not stripped or stripped.startswith("#"):
        return None
    parts = tuple(p.strip() for p in stripped.split(","))
    if len(parts) != 5 or parts[0] != "p":
        return None
    return parts


def load(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def distinct_non_admin_pairs(lines: list[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for line in lines:
        row = _parse_row(line)
        if row is None or row[1] == "admin":
            continue
        pairs.add((row[3], row[4]))
    return pairs


def admin_pairs(lines: list[str]) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    for line in lines:
        row = _parse_row(line)
        if row is None or row[1] != "admin" or row[2] != "admin":
            continue
        pairs.add((row[3], row[4]))
    return pairs


def has_wildcard(lines: list[str]) -> bool:
    return any(_WILDCARD_RE.match(l.strip()) for l in lines)


def wanted_pairs(lines: list[str]) -> set[tuple[str, str]]:
    return distinct_non_admin_pairs(lines) | _registry_pairs() | set(ADMIN_ONLY_EXTRA)


def missing_pairs(lines: list[str]) -> set[tuple[str, str]]:
    have = admin_pairs(lines)
    # The wildcard covers everything while present — but explicit rows are
    # still appended so removal is a no-op behaviorally.
    return {pair for pair in wanted_pairs(lines) if pair not in have and "*" not in pair}


def append_missing(path: Path) -> int:
    lines = load(path)
    missing = sorted(missing_pairs(lines))
    if not missing:
        return 0
    with path.open("a", encoding="utf-8") as fh:
        if lines and not lines[-1].endswith("\n"):
            fh.write("\n")
        fh.write("\n# ---- Admin explicit grants (Mk18: scoped replacement for p,admin,admin,*,*) ----\n")
        for obj, act in missing:
            fh.write(f"p, admin, admin, {obj}, {act}\n")
    return len(missing)


def check(path: Path) -> tuple[bool, str]:
    lines = load(path)
    if has_wildcard(lines):
        return False, "wildcard p,admin,admin,*,* still present"
    missing = sorted(missing_pairs(lines))
    if missing:
        return False, f"admin domain missing {len(missing)} pairs: {missing[:8]}"
    return True, f"wildcard absent; admin covers {len(admin_pairs(lines))} pairs"


def remove_wildcard(path: Path) -> bool:
    lines = load(path)
    ok, msg = check_without_wildcard_requirement(lines)
    if not ok:
        print(f"refusing: {msg}", file=sys.stderr)
        return False
    kept = [l for l in lines if not _WILDCARD_RE.match(l.strip())]
    path.write_text("".join(kept), encoding="utf-8")
    return True


def check_without_wildcard_requirement(lines: list[str]) -> tuple[bool, str]:
    missing = sorted(missing_pairs(lines))
    if missing:
        return False, f"admin domain missing {len(missing)} pairs: {missing[:8]}"
    return True, "coverage complete"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--path", default="app/harness/policies/policy.csv")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--remove-wildcard", action="store_true")
    args = ap.parse_args(argv)
    path = Path(args.path)
    if args.check:
        ok, msg = check(path)
        print(msg)
        return 0 if ok else 1
    added = append_missing(path)
    print(f"appended {added} explicit admin rows to {path}")
    if args.remove_wildcard:
        if remove_wildcard(path):
            print("wildcard removed (coverage verified)")
            return 0
        return 1
    ok, msg = check(path)
    print(f"coverage check: {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

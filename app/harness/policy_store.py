"""Where the Casbin `p` rows live (Mk13 Phase 6): `policy.csv`, or the shared database.

`PolicyEnforcer` and the dashboard read and extend the policy through this small interface
instead of opening the CSV themselves, so the back-end is one decision (`open_policy_store`):

    rules()        -> {ptype: [rule, ...]} exactly as Casbin parsed them
    has_row(row)   -> is ("p", sub, dom, obj, act) present?
    add_rows(rows) -> append the missing ones (idempotent); returns how many were new
    version()      -> changes whenever the rules change (the enforcer's rebuild trigger)

`FilePolicyStore` keeps the pre-Phase-6 behaviour byte for byte (same CSV, same append format).
`PgPolicyStore` (`app/harness/pg_policy.py`) is the shared-database twin.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from pathlib import Path

import casbin
from casbin import persist

POLICY_DIR = Path(__file__).resolve().parent / "policies"
MODEL_PATH = POLICY_DIR / "model.conf"
POLICY_PATH = POLICY_DIR / "policy.csv"

# Casbin section per ptype: "p" rows are policies, "g" rows role links.
_SECTION = {"p": "p", "g": "g"}

Rules = dict[str, list[list[str]]]


def parse_policy_file(path: str | Path, model_path: str | Path = MODEL_PATH) -> Rules:
    """Parse a policy CSV with Casbin's own loader, so comments/whitespace/quoting are read
    exactly as the enforcer always read them (not by a second, subtly different parser)."""
    enforcer = casbin.Enforcer(str(model_path), str(path))
    rules: Rules = {}
    for ptype in ("p", "g"):
        loaded = (
            enforcer.get_named_policy(ptype) if ptype == "p" else enforcer.get_named_grouping_policy(ptype)
        )
        if loaded:
            rules[ptype] = [list(r) for r in loaded]
    return rules


class RulesAdapter(persist.Adapter):
    """Read-only Casbin adapter over already-loaded rules (the database back-end's rows)."""

    def __init__(self, rules: Rules) -> None:
        self._rules = rules

    def load_policy(self, model) -> None:
        for ptype, rows in self._rules.items():
            sec = _SECTION.get(ptype[:1])
            if sec is None or sec not in model.model or ptype not in model.model[sec]:
                continue  # same as load_policy_line: rows for undefined ptypes are ignored
            for row in rows:
                model.model[sec][ptype].policy.append(list(row))

    def save_policy(self, model) -> bool:
        return False  # the enforcer never writes back; stores own their writes


def format_row(row: tuple[str, ...]) -> str:
    return ", ".join(row)


class FilePolicyStore:
    """`policy.csv` — the default without `DATABASE_URL` (unchanged behaviour).

    `path` may be a callable so the dashboard can keep resolving the settings-driven path on
    every call (tests redirect it to a temp copy; see `app.dashboard.app._policy_csv`).
    """

    backend = "file"

    def __init__(self, path: str | Path | Callable[[], Path] = POLICY_PATH) -> None:
        self._path = path

    @property
    def path(self) -> Path:
        return Path(self._path() if callable(self._path) else self._path)

    def rules(self) -> Rules:
        return parse_policy_file(self.path)

    def casbin_source(self) -> str | persist.Adapter:
        return str(self.path)

    def has_row(self, row: tuple[str, ...]) -> bool:
        """True iff the file already carries this row (whitespace-insensitive)."""
        with open(self.path, encoding="utf-8") as fh:
            for line in fh:
                if line.lstrip().startswith("#"):
                    continue
                if tuple(p.strip() for p in line.split(",")) == row:
                    return True
        return False

    def add_rows(self, rows: Iterable[tuple[str, ...]], actor: str = "") -> int:
        added = 0
        for row in rows:
            if not self.has_row(row):
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(format_row(row) + "\n")
                added += 1
        return added

    def version(self) -> tuple[int, int]:
        """File identity: a hand edit or a dashboard append is picked up without a restart."""
        try:
            st = self.path.stat()
        except OSError:
            return (0, 0)
        return (st.st_mtime_ns, st.st_size)


def open_policy_store():
    """Shared database when `DATABASE_URL` is set, else `settings.services.policy_path`.

    Mk17: honors `SERVICES__POLICY_PATH` so dashboard writes and enforcer reads
    never diverge (previously hardcoded to the repo `policy.csv`).
    """
    from app.db import is_enabled

    if is_enabled():
        from app.harness.pg_policy import PgPolicyStore

        return PgPolicyStore()
    try:
        from app.config import get_settings

        return FilePolicyStore(get_settings().services.policy_path)
    except Exception:  # noqa: BLE001
        return FilePolicyStore(POLICY_PATH)

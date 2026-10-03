"""ApprovalStore — pending HITL decisions (plan_mk1_core_harness.md §5.9, §8).

Central-tier memory writes suspend via LangGraph `interrupt()`; this SQLite store
holds the pending item so an admin can approve/reject. The dashboard (wk 9-10)
will render these; for now the store + resume API are exercised by tests.

Mk15: an item carries the step it waits on (`level`: head → admin → done), the history
of decisions so far (`steps`) and its creation-time `route` (head-only | head+admin |
admin-only), so department items can need a head, then an admin.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

# Mk15: which step a pending item waits on, and its creation-time route.
LEVEL_HEAD = "head"
LEVEL_ADMIN = "admin"
LEVEL_DONE = "done"
ROUTE_HEAD_ONLY = "head-only"
ROUTE_HEAD_ADMIN = "head+admin"
ROUTE_ADMIN_ONLY = "admin-only"

# Columns added after the first schema (Mk14 resume claim, Mk15 levels): SQLite files are
# altered lazily in `_init_schema`; Postgres gets them from migrations 0007/0008.
_LATE_COLUMNS = {
    "resumed_by": "TEXT",
    "resumed_at": "TEXT",
    "level": "TEXT NOT NULL DEFAULT 'head'",
    "steps": "TEXT NOT NULL DEFAULT '[]'",
    "route": "TEXT",
    "dept": "TEXT",
    "action": "TEXT",
    "args_hash": "TEXT",
}


class ApprovalStore:
    # Serialises a decide/claim (read, conditional UPDATE, read its rowcount) across the
    # threads of one process. The SQLite store shares one connection between request
    # threads, and a shared connection reports rowcount per *connection* — without the lock
    # two racing deciders could both read the winner's rowcount. Across processes/laptops
    # the UPDATE's own WHERE clause is the gate (each has its own connection).
    _write_lock = threading.RLock()

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self._conn:
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS approvals (
                    id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    thread_id TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_ts TEXT NOT NULL,
                    decided_ts TEXT,
                    decided_by TEXT,
                    resumed_by TEXT,
                    resumed_at TEXT,
                    level TEXT NOT NULL DEFAULT 'head',
                    steps TEXT NOT NULL DEFAULT '[]',
                    route TEXT,
                    dept TEXT,
                    action TEXT,
                    args_hash TEXT
                )
                """
            )
            # Older dev files predate Mk14/Mk15: add the later columns if missing.
            cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(approvals)").fetchall()}
            for name, decl in _LATE_COLUMNS.items():
                if name not in cols:
                    self._conn.execute(f"ALTER TABLE approvals ADD COLUMN {name} {decl}")

    def create(
        self,
        run_id: str,
        thread_id: str,
        resource: dict[str, Any],
        actor: dict[str, Any],
        payload: dict[str, Any],
        *,
        level: str = LEVEL_HEAD,
        route: str | None = None,
        dept: str | None = None,
        action: str | None = None,
        args_hash: str | None = None,
    ) -> str:
        """Create a pending approval, deduplicating on (run_id, resource, payload).

        LangGraph re-runs the interrupted node on resume (`Command(resume=...)`),
        so the memory/tool gate executes twice. Returning the existing id for the
        same logical request prevents a stale duplicate pending row from being
        created on the second pass — the resumed call then re-interrupts against
        the already-decided approval and proceeds.

        Mk15: `route` and the starting `level` are fixed here, at creation — a later
        department-mode switch never re-routes the item. `dept` attributes a tool item
        to the requester's department so that department's head sees it.
        """
        existing = self.find(run_id, resource, payload)
        if existing is not None:
            return existing
        approval_id = str(uuid.uuid4())
        with self._conn:
            self._conn.execute(
                "INSERT INTO approvals (id, run_id, thread_id, resource, actor, payload, status,"
                " created_ts, level, steps, route, dept, action, args_hash) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    approval_id,
                    run_id,
                    thread_id,
                    json.dumps(resource, sort_keys=True),
                    json.dumps(actor, sort_keys=True),
                    json.dumps(payload, sort_keys=True),
                    STATUS_PENDING,
                    datetime.now(UTC).isoformat(),
                    level,
                    "[]",
                    route,
                    dept,
                    action,
                    args_hash,
                ),
            )
        return approval_id

    def find(
        self, run_id: str, resource: dict[str, Any], payload: dict[str, Any]
    ) -> str | None:
        """Id of the approval already raised for this exact request in this run, if any.

        The gates call this first on every pass: a resumed run must re-attach to its own
        (decided) approval rather than be re-routed or matched against the allowlist.
        """
        row = self._conn.execute(
            "SELECT id FROM approvals WHERE run_id = ? AND resource = ? AND payload = ? LIMIT 1",
            (
                run_id,
                json.dumps(resource, sort_keys=True),
                json.dumps(payload, sort_keys=True),
            ),
        ).fetchone()
        return row["id"] if row else None

    def get(self, approval_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM approvals WHERE id = ?", (approval_id,)
        ).fetchone()
        return self._row_to_dict(row) if row else None

    def pending_items(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE status = ? ORDER BY created_ts",
            (STATUS_PENDING,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    @staticmethod
    def dept_of(entry: dict[str, Any]) -> str | None:
        """Owning department of an approval, else None (Mk12, extended in Mk15).

        Memory HITL resources are `{"type": "memory", "id": <scope>}`; only
        `dept/<name>` scopes are head-owned. Tool-gate resources (`type == tool`) are
        head-owned only when the gate attributed them to the requester's department
        (the `dept` column, Mk15); central scopes and unattributed tools return None.
        """
        resource = entry.get("resource") or {}
        if resource.get("type") != "memory":
            return entry.get("dept") or None
        scope = str(resource.get("id", ""))
        kind, _, value = scope.partition("/")
        return value if kind == "dept" and value else None

    @classmethod
    def route_of(cls, entry: dict[str, Any]) -> str:
        """Creation-time route; rows that predate Mk15 fall back to the Mk12 behaviour
        (dept items: the head decides; everything else: an admin decides)."""
        route = entry.get("route")
        if route:
            return str(route)
        return ROUTE_HEAD_ONLY if cls.dept_of(entry) is not None else ROUTE_ADMIN_ONLY

    @classmethod
    def level_of(cls, entry: dict[str, Any]) -> str:
        """Which step the item waits on: `head`, `admin`, or `done` once decided."""
        if entry.get("status") != STATUS_PENDING:
            return LEVEL_DONE
        level = entry.get("level") or LEVEL_HEAD
        if level == LEVEL_HEAD and cls.dept_of(entry) is None:
            return LEVEL_ADMIN  # no department owns it, so there is no head step
        return str(level)

    def pending_for_level(self, level: str) -> list[dict[str, Any]]:
        return [i for i in self.pending_items() if self.level_of(i) == level]

    def pending_for_dept(self, dept: str) -> list[dict[str, Any]]:
        """Pending approvals waiting on one department's head (Mk12; step 1 since Mk15)."""
        return [
            i
            for i in self.pending_items()
            if self.dept_of(i) == dept and self.level_of(i) == LEVEL_HEAD
        ]

    def for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        """Every approval for one thread, newest last (Mk14 owner-resume poll)."""
        rows = self._conn.execute(
            "SELECT * FROM approvals WHERE thread_id = ? ORDER BY created_ts",
            (thread_id,),
        ).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def claim_resume(self, approval_id: str, owner: str) -> dict[str, Any]:
        """Atomically claim the single resume of a decided approval (Mk14).

        Exactly one caller wins; losers get `ValueError` (already resumed) or
        `KeyError` (unknown id), mirroring `decide()`. The winner must then call
        `resume_agent()`; a crash between claim and resume leaves the row claimed,
        so the owner re-polls and an operator can inspect `resumed_by/at`.
        """
        with self._write_lock:
            return self._claim_resume(approval_id, owner)

    def _claim_resume(self, approval_id: str, owner: str) -> dict[str, Any]:
        with self._conn:
            cur = self._conn.execute(
                "UPDATE approvals SET resumed_by = ?, resumed_at = ? "
                "WHERE id = ? AND status IN ('approved', 'rejected') AND resumed_by IS NULL",
                (owner, datetime.now(UTC).isoformat(), approval_id),
            )
        if cur.rowcount == 0:
            row = self._conn.execute(
                "SELECT status, resumed_by FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"approval {approval_id!r} not found")
            if row["status"] not in (STATUS_APPROVED, STATUS_REJECTED):
                raise ValueError(f"approval {approval_id!r} has not been decided")
            raise ValueError(f"approval {approval_id!r} already resumed by {row['resumed_by']}")
        return self.get(approval_id)  # type: ignore[return-value]

    def decide(
        self, approval_id: str, approve: bool, admin: str, comment: str | None = None
    ) -> dict[str, Any]:
        """Atomically move a pending approval to approved/rejected (terminal).

        The status check lives in the UPDATE itself, so two simultaneous deciders
        (e.g. an admin and a department head) cannot both win: exactly one row is
        updated and the loser gets `ValueError`.
        """
        return self.decide_step(approval_id, approve, admin, as_level=None, comment=comment)

    def decide_step(
        self,
        approval_id: str,
        approve: bool,
        by: str,
        *,
        as_level: str | None,
        comment: str | None = None,
        always_allow: bool = False,
    ) -> dict[str, Any]:
        """Record one decision step (Mk15).

        `as_level="head"`: the item must be waiting on its head. An approve on a
        `head+admin` item only advances it to the admin step (still pending); a reject —
        or an approve on a `head-only` item — is terminal. `as_level="admin"` (or None,
        the legacy `decide`) is always terminal. A head reject is final: once the row is
        `rejected` nobody can decide it again.

        `always_allow` is only recorded on the step; the caller turns it into an allowlist
        rule once the item is finally approved (`app.memory.approval_flow`).

        Every transition is one conditional UPDATE keyed on the step that was read, so of
        two racing deciders exactly one wins and the other gets `ValueError`.
        """
        with self._write_lock:
            return self._decide_step(approval_id, approve, by, as_level, comment, always_allow)

    def _decide_step(
        self,
        approval_id: str,
        approve: bool,
        by: str,
        as_level: str | None,
        comment: str | None,
        always_allow: bool,
    ) -> dict[str, Any]:
        entry = self.get(approval_id)
        if entry is None:
            raise KeyError(f"approval {approval_id!r} not found")
        if entry["status"] != STATUS_PENDING:
            raise ValueError(f"approval {approval_id!r} already {entry['status']}")
        stored_level = entry["level"]
        current = self.level_of(entry)
        if as_level == LEVEL_HEAD and current != LEVEL_HEAD:
            raise ValueError(f"approval {approval_id!r} is waiting on an admin, not the head")
        now = datetime.now(UTC).isoformat()
        step = {
            "level": as_level or current,
            "by": by,
            "at": now,
            "decision": STATUS_APPROVED if approve else STATUS_REJECTED,
            "comment": (comment or "").strip(),
            "always_allow": bool(always_allow and approve),
        }
        steps = json.dumps([*entry["steps"], step], sort_keys=True)
        advance = as_level == LEVEL_HEAD and approve and self.route_of(entry) == ROUTE_HEAD_ADMIN
        with self._conn:
            if advance:
                cur = self._conn.execute(
                    "UPDATE approvals SET level = ?, steps = ? "
                    "WHERE id = ? AND status = ? AND level = ?",
                    (LEVEL_ADMIN, steps, approval_id, STATUS_PENDING, stored_level),
                )
            else:
                cur = self._conn.execute(
                    "UPDATE approvals SET status = ?, decided_ts = ?, decided_by = ?, level = ?,"
                    " steps = ? WHERE id = ? AND status = ? AND level = ?",
                    (
                        STATUS_APPROVED if approve else STATUS_REJECTED,
                        now,
                        by,
                        LEVEL_DONE,
                        steps,
                        approval_id,
                        STATUS_PENDING,
                        stored_level,
                    ),
                )
        if cur.rowcount == 0:
            row = self._conn.execute(
                "SELECT status, level FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"approval {approval_id!r} not found")
            if row["status"] != STATUS_PENDING:
                raise ValueError(f"approval {approval_id!r} already {row['status']}")
            raise ValueError(f"approval {approval_id!r} already moved to the {row['level']} step")
        return self.get(approval_id)  # type: ignore[return-value]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        # Mk14/Mk15 columns may be absent on old DBs (migrations apply on Postgres;
        # SQLite alters lazily in _init_schema, PgSession Row misses keys safely).
        def _col(name: str) -> Any:
            try:
                return row[name]
            except (KeyError, IndexError):
                return None

        return {
            "id": row["id"],
            "run_id": row["run_id"],
            "thread_id": row["thread_id"],
            "resource": json.loads(row["resource"]),
            "actor": json.loads(row["actor"]),
            "payload": json.loads(row["payload"]),
            "status": row["status"],
            "created_ts": row["created_ts"],
            "decided_ts": row["decided_ts"],
            "decided_by": row["decided_by"],
            "resumed_by": _col("resumed_by"),
            "resumed_at": _col("resumed_at"),
            "level": _col("level") or LEVEL_HEAD,
            "steps": json.loads(_col("steps") or "[]"),
            "route": _col("route"),
            "dept": _col("dept"),
            "action": _col("action"),
            "args_hash": _col("args_hash"),
        }

    def close(self) -> None:
        self._conn.close()

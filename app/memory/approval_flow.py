"""Decision flow shared by the dashboard and the gateway (Mk15).

One place answers "may this caller decide this approval, and what happens when they do":

- a department **head** decides step 1 of their own department's items. On a `head+admin`
  item an approve only hands the item to the admin step; a reject is final.
- an **admin** decides anything still pending, and that decision is terminal.
- `always_allow` (approve only, eligible items only) records the decider's wish on the step;
  the allowlist rule is created when the item is finally approved, so a rule can never
  take effect for an action the admin step went on to reject.

Failures raise `DecisionError(status, detail)`; each HTTP layer maps it to its own error type.
"""

from __future__ import annotations

from typing import Any

from app.agent.routing import KIND_MEMORY, KIND_TOOL, allowlist_payload, entry_allowlist_eligible
from app.memory.approvals import (
    LEVEL_ADMIN,
    LEVEL_HEAD,
    STATUS_APPROVED,
    STATUS_PENDING,
    ApprovalStore,
)


class DecisionError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def is_head(claims: dict[str, Any]) -> bool:
    """`{dept}_head` role owning its own department (`marketing_head` ⟺ `marketing`)."""
    role, dept = str(claims.get("role", "")), str(claims.get("dept", ""))
    return bool(dept) and role == f"{dept}_head"


def check_decision(
    approvals: ApprovalStore,
    approval_id: str,
    claims: dict[str, Any],
    *,
    approve: bool,
    as_level: str,
    always_allow: bool = False,
) -> dict[str, Any]:
    """Validate a decision without recording it; returns the pending entry.

    404 unknown id · 409 already decided / not this level's turn · 403 another
    department's item · 422 `always_allow` on a reject or an ineligible item.
    """
    entry = approvals.get(approval_id)
    if entry is None:
        raise DecisionError(404, f"approval {approval_id!r} not found")
    if entry["status"] != STATUS_PENDING:
        raise DecisionError(409, f"approval {approval_id!r} already {entry['status']}")
    if as_level == LEVEL_HEAD:
        if ApprovalStore.dept_of(entry) != str(claims.get("dept", "")):
            raise DecisionError(403, "not your department's approval")
        if ApprovalStore.level_of(entry) != LEVEL_HEAD:
            raise DecisionError(409, f"approval {approval_id!r} is waiting on an admin, not the head")
    if always_allow:
        if not approve:
            raise DecisionError(422, "always_allow only applies when approving")
        if not entry_allowlist_eligible(entry):
            raise DecisionError(
                422,
                "this action cannot be always-allowed (critical tools and central memory "
                "need approval every time)",
            )
    return entry


def apply_decision(
    approvals: ApprovalStore,
    members: Any,
    approval_id: str,
    claims: dict[str, Any],
    *,
    approve: bool,
    as_level: str,
    always_allow: bool = False,
    comment: str | None = None,
) -> dict[str, Any]:
    """Record one decision step and, once finally approved, any requested allowlist rule.

    Returns the updated entry: still `pending` (now at the admin step) after a head's
    step-1 approve on a `head+admin` item, otherwise `approved` / `rejected`.
    """
    check_decision(
        approvals, approval_id, claims, approve=approve, as_level=as_level, always_allow=always_allow
    )
    # Mk17: central memory needs a second admin. Self-approval is allowed only
    # as a single-admin fallback (audited); with ≥2 active admins the decider
    # must differ from the requester.
    if approve and as_level == LEVEL_ADMIN:
        entry = approvals.get(approval_id)
        if entry is not None:
            resource = entry.get("resource") or {}
            is_central = resource.get("type") == "memory" and str(resource.get("id", "")) == "central"
            if is_central:
                requester = str((entry.get("actor") or {}).get("sub", ""))
                decider = str(claims.get("sub", ""))
                if requester and decider and requester == decider:
                    count = None
                    for attr in ("count_active_admins", "_active_admin_count"):
                        try:
                            fn = getattr(members, attr, None)
                            if callable(fn):
                                try:
                                    count = fn()
                                except TypeError:
                                    count = fn(exclude=None) if attr.startswith("_") else fn()
                                break
                        except Exception:
                            count = None
                    if isinstance(count, int) and count >= 2:
                        raise DecisionError(
                            409,
                            "central memory needs a second admin to approve "
                            f"(requested by {requester!r})",
                        )
    try:
        decided = approvals.decide_step(
            approval_id,
            approve,
            str(claims.get("sub", "")),
            as_level=as_level,
            comment=comment,
            always_allow=always_allow,
        )
    except KeyError as exc:
        raise DecisionError(404, str(exc.args[0])) from exc
    except ValueError as exc:  # lost a race with another decider
        raise DecisionError(409, str(exc)) from exc
    if decided["status"] == STATUS_APPROVED:
        _create_allow_rules(members, decided)
    return decided


def _create_allow_rules(members: Any, entry: dict[str, Any]) -> None:
    wanted = [s for s in entry.get("steps") or [] if s.get("always_allow")]
    if not wanted or not entry_allowlist_eligible(entry):
        return
    resource = entry.get("resource") or {}
    kind = KIND_MEMORY if resource.get("type") == KIND_MEMORY else KIND_TOOL
    dept = ApprovalStore.dept_of(entry)
    member = str((entry.get("actor") or {}).get("sub", ""))
    if not dept or not member:
        return
    members.allowlist_add(
        member,
        entry["action"],
        entry["args_hash"],
        allowlist_payload(kind, entry["payload"]),
        dept,
        actor=str(wanted[0].get("by", "")),
    )


def decision_response(decided: dict[str, Any]) -> dict[str, Any]:
    """Envelope for a recorded decision that did not resume the run on this machine."""
    waiting_on_admin = decided["status"] == STATUS_PENDING
    return {
        "approval_id": decided["id"],
        "status": decided["status"],
        "thread_id": decided["thread_id"],
        "reply": None,
        "interrupted": True,
        "next_approval_id": None,
        # Step 1 of 2 done: nothing to resume yet — the item now sits in the admin queue.
        "needs_resume": not waiting_on_admin,
        "level": ApprovalStore.level_of(decided),
        "next_level": LEVEL_ADMIN if waiting_on_admin else None,
    }

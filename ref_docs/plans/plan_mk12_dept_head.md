# Plan Mk12 — Department Head role with dept-preference approval (todo #2)

## Metadata
- Date: 2026-09-13
- Status: Phases 1–5 DONE (session 022, 354 green); live user sign-off + commit open
- Follows: Mk1 (HITL/approvals), Mk2 (members/departments/RBAC)
- Closes: `ref_docs/todo.md` item 2
- Scope lock (user, 2026-09-13):
  1. Per-dept head roles (`{dept}_head`, e.g. `marketing_head`).
  2. All dept-tier writes from non-heads gate on head approval; head's own dept writes auto-commit.
  3. No/inactive head → escalate to admin queue.
  4. Separate head approvals view (not the admin queue).
  5. Multiple heads per dept allowed — any one approval resolves.

## Problem
Identity is `admin` + members. Dept-scoped memory writes (`dept/<name>`) auto-commit
after a Casbin write check (`app/agent/graph.py:reflect`, non-central branch), while only
central-tier writes suspend via `interrupt()` for admin HITL (`ApprovalStore` + `resume_agent`).
There is no dept-level approver.

## Design
- **Role model:** `{dept}_head` with `dept` = owned dept. `MemberStore.create/update`
  validates `role == f"{dept}_head"` for `*_head` roles (admin-only callers, existing guards).
- **Authz helper:** `is_dept_head(claims, scope)` in `app/harness/enforcer.py` —
  `scope == dept/X and claims[role] == X_head and claims[dept] == X`.
- **Write gate (`reflect`):** generalize `scope == central` → `needs_hitl(scope, claims)`:
  central → `admin`; `dept/X` → None if head else `dept_head`; `user/*` → None.
  Gated writes reuse the exact central HITL shape (`approvals.create` dedup +
  `interrupt({approval_id, resource:{type:memory,id:scope}, actor, text/op/match, thread_id, tier})`,
  trace `pending/approved/rejected`, audit `APPROVED/REJECTED reason=dept-hitl`).
- **Approvals:** no schema change (`resource.id` already carries the scope).
  New `pending_for_dept(dept)` + `dept_of(entry)` helpers; escalation = admin sees a
  dept item iff zero active `{dept}_head` members (live `MemberStore` count, no migration).
- **Dashboard:** `_verify_dept_head` (JWT + active + `*_head`); `GET /api/dept/approvals/pending`
  (own-dept only) + `POST /api/dept/approvals/{id}/decide` (owns-item check + `resumer`
  reuse). Existing admin `GET /api/approvals/pending` additionally returns escalated
  dept items.
- **Policy:** per-dept head `p` rows mirror the member role (tool scopes, memory
  central-read + dept read/write, user self-scoped, recall, pii_email, ext tools, skills).
  `POST /api/departments` appends both `{role}` and `{role}_head` wildcard rows.
- **Frontend:** head tabs (`dept-approvals`, memory, chat, account) when role ends with
  `_head`; new `dept-approvals-view` reusing `approvalCard`; default tab for heads =
  `dept-approvals`; `memory` reuses the member read-only path (`/api/memory/me`).
  Chat state (`currentThreadId`/transcript/draft) resets on identity switch plus a
  send-time own-thread guard, so one account's conversation never leaks into another's.
- **Non-goals:** episodic transcript append stays ungated; `user/*` and central paths unchanged;
  single-vs-multi head uniqueness not enforced; todo #1 (dual-session) untouched.

## Phases
| Phase | Scope | Status |
|-------|-------|--------|
| 1 | Policy + identity + enforcer helper | DONE (session 022) |
| 2 | reflect gate + approvals helpers | DONE (session 022) |
| 3 | Dashboard head endpoints + admin escalation | DONE (session 022) |
| 4 | Frontend head view (+ head Memory tab, chat isolation fix) | DONE (session 022) |
| 5 | Tests + lint + full suite | DONE (session 022, 354 green, ruff clean) |

## Follow-ups fixed in session 022 (user testing)
- Stale `LLM__FERNET_KEY` 502s: `/api/users` reports `stale:true` per row instead of failing;
  `get_provider_for_user` falls back to global/mock so chat survives (regression tests added).
- Heads gained the read-only Memory tab; chat state resets per identity + send-time
  own-thread guard (cross-account transcript leak fixed).

## Risks
- Policy file growth from head rows per registered dept — same idempotent-append pattern as members.
- Two heads racing to decide one item — second gets 409 (existing `decide` guard), acceptable.
- Stale `data/*.sqlite` in dev already seeded without heads — escalation path covers; heads added via Members UI.

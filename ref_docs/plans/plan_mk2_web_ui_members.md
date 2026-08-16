# Plan Mk2 — Web-UI Demo + Member Management

- **Plan id:** `mk2`
- **Scope:** Make every existing capability reachable through the **web UI** (no curl required for the demo), turn **users/departments into live, admin-managed data**, and deliver the week-16 report + recorded web demo on top of the finished Mk1 core.
- **Status:** PLANNED (documented 2026-08-15). Execution starts at session 010; **one session = one phase**.
- **Registry:** `ref_docs/plans/INDEX.md` — sessions map here via the `Plan` column in `agent_history/INDEX.md`.
- **Predecessor:** Plan Mk1 (`ref_docs/plans/plan_mk1_core_harness.md`) — Milestones 1–8 done, 204 offline tests green. Mk2 extends the Mk1 timeline; it does not rewrite `plan_mk1_core_harness.md`.

---

## 1. Why this plan exists

Mk1 delivered the full deterministic harness, but the **only chat surface is the gateway's
`POST /chat` (curl-only)** and the dashboard is **admin-only** (`_verify_admin` on every `/api/*`)
with **read-only Users / Departments tabs**. Users are a static `USERS` dict; departments are
derived from it plus policy-row domains plus a **hardcoded `_service_for` map**. Nothing is
member-editable through the UI.

Two needs drive Mk2:

1. **The first demo is the web UI.** All functionality — chat, RBAC denies, HITL approvals,
   central-memory HITL, memory recall including the cross-member scoping test — must be
   clickable in the browser for any seeder/member user, with a 2nd marketing member to prove
   dept-shared memory.
2. **Admin member management.** Add users, assign department, edit role/access, enable/disable —
   entirely from the dashboard, audit-logged, matching the plan_mk1_core_harness.md §10 vision
   ("adding a department is data, not code").

Follow-up consideration: there will be many more plans after this one. The registry + `mk<NN>`
folder convention exists so the whole body of work stays traceable.

---

## 2. Decisions locked in (from the planning session, 2026-08-15)

1. **Store-driven `g` rows (enforcement source).** Casbin resolves per-user permission through
   `g` rows (`model.conf` matcher `g(r.sub, p.sub, r.dom)`). Recommendation accepted: the
   **MemberStore becomes the single source of truth** for user → (role, dept); `PolicyEnforcer`
   materializes `g` assignments from it. `p` rows (role → dept → resource → action) **stay in
   `policy.csv`**. Deny-by-default is preserved: an unknown/disabled member has no `g` row ⇒ deny.
   *Rejected:* runtime rewrites of the `policy.csv` `g` block (fragile, format-sensitive — the
   class of problem that bit session_007).
2. **Departments: registration included.** Phase 4 includes *register a new department* (name,
   service URL, resources, optional role), replacing the hardcoded `_service_for` map — not just
   member assignment to existing depts.
3. **Hashed passwords.** MemberStore stores bcrypt/argon2 (fallback: stdlib `hashlib.scrypt` if
   no crypto lib is present) hashes; seeded demo accounts keep their demo passwords, admin can
   reset. No plaintext persistence.
4. **Labeling / registry shape.** Plan Mk1 = `ref_docs/plans/plan_mk1_core_harness.md` (frozen); new plans under
   `ref_docs/plans/` indexed in `INDEX.md`; `agent_history/INDEX.md` gains a `Plan` column.

Additional locked constraints:

- **Mock LLM is the default** (offline, no API key) so the browser demo is guaranteed; a real
  LiteLLM key is optional.
- **Server-side proxy for chat** — the dashboard has no CORS middleware, so the chat tab calls
  a dashboard-side `/api/chat` that proxies to the gateway `:8001/chat` (same pattern as
  `/api/login` → auth). No CORS changes.
- **Every member/department change is audit-logged**, consistent with existing discipline.
- **Lockout guard:** the last active admin (and the currently-signed-in admin) can never be
  disabled/deactivated.
- **Out of scope (unchanged from Mk1):** Telegram approval adapter, LangFuse adapter,
  `learn_skill` auto-gen, trajectory search / replay / fork — all remain stretch (plan_mk1_core_harness.md week 15).

---

## 3. Why the technical shape (architecture notes)

### 3.1 Enforcement today (the fact everything hinges on)

`PolicyEnforcer.enforce(sub, dom, obj, act)` → Casbin `e.enforce(...)` with
`m = g(r.sub, p.sub, r.dom) && r.dom == p.dom && keyMatch2(r.obj, p.obj) && (r.act == p.act || p.act == "*")`.
`p` rows are keyed by **role**; `g` rows map **user → role within a domain**. The caller's
`dom` (dept) comes from the JWT claims. `filter_tools`, `intercept`, memory scope checks, and
skill visibility all flow through this. Therefore: **adding a member grants them nothing until a
`g(uid, role, dept)` binding exists**, and their dept claim and their `g`-row domain must move
together when the admin edits a member.

### 3.2 Target model after Mk2

```
p rows   (role → dept → resource → action)   app/harness/policies/policy.csv   (unchanged source)
g rows   (user → role → dept)                 materialized from MemberStore      (live source)
users + credentials + metadata                data/members.sqlite                 (single source of truth)
departments (name, service URL, resources)    department records in the store     (replaces hardcoded _service_for)
```

Consequence for Phase 2: the change is localized to how the enforcer obtains `g` assignments.
Regression budget: the full suite (204 tests, incl. the A1–A8 attack matrix and audit/trace
integrity) must stay green unchanged.

### 3.3 Files that must move off the static `USERS` dict

`app/auth/users.py` `USERS` / `claims_for` is imported by `app/auth/service.py`,
`app/dashboard/app.py` (:224/234/248/415), and 6 demo scripts (`demo_agent/_attacks/_memory/
_sanitize_dashboard/_skills/_trajectory_evals`). All of these read claims for a seeded
username; the store must expose an equivalent `get_user(username)`/`claims_for(username)` so
demos keep working unchanged (or read the store directly).

---

## 4. Milestones / Phases (each phase = 1 session)

| Phase | Session | Deliverable | Acceptance |
|-------|---------|-------------|------------|
| 1 | 010 | **Dynamic MemberStore + auth refactor.** `data/members.sqlite` (user, hashed password, role, dept, scopes, active, timestamps). Migration seeds the 5 current users. `/auth/login`, `/auth/me`, `claims_for` read the store; dashboard users/departments read it too; demos still work. Lockout guard (last admin / self). Every change audit-logged. | ✅ **DONE 2026-08-15 (session_010)** — `POST /auth/login` works for `molly@acme.com` after creation; **222 tests green** (204 existing + 18 new member/auth tests); all 6 `make demo-*` unaffected; ruff clean |
| 2 | 011 | **Store-driven enforcement.** `PolicyEnforcer` materializes `g` from MemberStore; `p` stays in `policy.csv`. Reload/version guard on member change. Deny-by-default added test: unknown + disabled member ⇒ every action denied. | ✅ **DONE 2026-08-15 (session_011)** — `g` block removed from `policy.csv` (p rows untouched); `PolicyEnforcer` builds `g` from ACTIVE store members `add_role_for_user_in_domain`, lazy rebuild under lock keyed to a `members_version` write counter bumped in every store mutation; gateway (`/hello`, `/chat`, `/approvals/{id}/decide`) + dashboard `_verify_admin` deny non-members/disabled members 403 (valid-but-unknown JWT ⇒ 403, bad token ⇒ 401); A1 policy-matrix oracle reads store assignments; **236 tests green** (222 + 14 new enforcement); `make demo-*` + real-DB molly create→grant→deactivate→deny→reactivate→grant check pass; ruff clean |
| 3 | 012 | **Chat in the browser + any-user login.** Any active member can sign in (chat tab only for non-admins); dashboard `POST /api/chat` server-side proxy → `:8001/chat` (token passed, no CORS). Render reply / tool calls / `guardrail_hits` / `interrupted` / `approval_id` + "open Approvals to approve" hint. | ✅ **DONE 2026-08-15 (session_012)** — dashboard `POST /api/chat` verifies the member JWT + active gate and forwards it to the gateway via a test seam (`get_gateway_sender`); SPA in `static/index.html` gains dual sessions (admin + member tokens coexist, identity switcher routes the Chat tab to whoever is active; admins see Chat + admin tabs; members see Chat only); chat renders reply / tool-outcome chips (no PII)/ guardrails / interrupted+approval_id hint; **246 tests green** (236 + 10 new web-chat proxy/gate/demo tests incl. marketing allowed-CRM + denied-HR and carol's interrupted `finance_transfer` listed in Approvals with masked args, 502 mapping, member-on-admin 403); `make demo-chat-web`; ruff clean |
| 4 | 013 | **Members + Departments management UI.** Admin UI + endpoints: list / add / edit (role + dept + scopes) / activate-deactivate / reset password; audit-logged. Departments derived live from store + p-rows; **register department** (name, service URL, resources, optional role) replaced `_service_for`; admin-JWT gated; lockout guard enforced server-side. | Create `molly@acme.com` (marketing, dept scopes) → she logs in, sees marketing tools, can read dept memory; disabling her → her next request 403; registering a dept makes it appear in Departments + a new role grantable |
| 5 | 014 | **Browser memory-test walkthrough + e2e.** Run the scoping scenario entirely in the UI: admin central write (HITL approve), admin central+private facts, alice dept+private facts, **2nd marketing member (molly) reads the shared dept memory**, cross-dept denial (bob can't read marketing). Scripted UI-equivalent e2e test; README quick-start for the web demo. | Scenario reproducible with clicks only; e2e test automated; `make demo` equivalent via browser |
| 6 | 015 | **Report + recorded web demo + finalize.** `report.md` (evidence naturally produced: policy matrix, memory denials, injection suite, trace/evals, member mgmt audit), live-walkthrough notes/recording, Plans/INDEX + agent_history final pass, PLAN-SPLIT status complete. | Pages written; registry shows Mk1 ✅ + Mk2 ✅; every session logged with its Plan column |

---

## 5. Reference tables & seed data (for the memory test)

Current users (seeded, will be migrated into the MemberStore): `admin@acme.com` (admin,
domain-wildcard `p, admin, admin, *, *`), `alice@acme.com` (marketing), `bob@acme.com` (hr),
`carol@acme.com` (finance), `dave@acme.com` (devops). M2 seeds a 2nd marketing user, e.g.
`molly@acme.com` (marketing) — see the Registry row and Phase 5.

Departments today = users' `dept` + `p`-row domains + hardcoded `_service_for`
(`marketing→crm`, `hr`, `devops`, `finance`) with URLs in `app/config.py` `ServiceSettings`.
The dept->role convention uses `_agent` roles (`marketing_agent`, `hr_agent`, …).

Skills enum surfaced during chat: `launch_campaign` / `new_leave_request` / `triage_incident`
(used in the browser demo to show skill visibility per role).

---

## 6. Relevant files

- `app/auth/users.py`, `app/auth/service.py` — static identity source to be replaced by MemberStore.
- `app/harness/policies/model.conf` + `policy.csv` — `g` matcher; `p` source stays.
- `app/harness/enforcer.py` — where `g` materialization lands (Phase 2).
- `app/gateway/app.py` — `get_runner` / `get_resumer` / `get_approval_store` deps behind `POST /chat` + `/approvals/{id}/decide`.
- `app/dashboard/app.py` — `_verify_admin`, `/api/login`, `/api/users`, `/api/departments`, `/api/approvals/{id}/decide`, `/api/memory`, `/api/ops/*`; new: `/api/chat`, `/api/members`, `/api/departments/{name}`.
- `app/config.py` (`ServiceSettings`) — department→URL mapping location (Phase 4 moves dept↔URL into store records, keeping ServiceSettings as the running defaults).
- `scripts/*.py` (6 demo scripts) — must keep working after Phase 1 auth swap.
- `ref_docs/plans/plan_mk1_core_harness.md` §10 (depts/users/dashboard vision), §11 row 16 (report + demo).

---

## 7. Verification contract (must stay green every phase)

- `uv run pytest` (204 tests today; grows with new suites)
- `uv run ruff check app tests scripts`
- `make demo-attacks` (attack evidence still holds after Phase 2's enforcer change)
- `make demo` / `demo-memory` / `demo-sanitize` / `demo-skills` / `demo-evals` (one-session demos unaffected by the auth refactor)

## 8. Risks & mitigations

| Risk | Mitigation |
|------|------------|
| Phase 2 touches the security core (g from store) | Change is localized to `g` acquisition; full A1–A8 + integrity regression must pass unchanged before Merge/PR |
| Members migrate off `USERS` but a demo script still imports it | `claims_for`/`get_user(username)` preserved on the store; scripts minimally touched or left importing a thin adapter |
| Enforcer reload races / stale `g` after admin edit | Version guard: enforcer rebuild/reload keyed to a member-store write counter with a lock |
| Admin lockout | Last-active-admin + self-deactivation guard at both API and store layer |
| Password hashing dependency not installed | stdlib `hashlib.scrypt` fallback; verify what `pyproject.toml` already has before deciding bcrypt vs argon2 |
| Chat proxy leaks a user token to the gateway | Proxy only over localhost gateway URL from `ServiceSettings.gateway_url`; token from the member's own login, never re-used cross-user |

---

## 9. Traceability

- This doc is the canonical Mk2 timeline; update phase rows + Registry status after each session.
- Session files use `Plan: mk2 — Phase N` in metadata; `agent_history/INDEX.md` `Plan` column mirrors it.
- Design-history rule from `agent_history/INSTRUCTIONS.md` still applies: append-only, timestamped logs.
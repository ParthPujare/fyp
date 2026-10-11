# Plan Mk17 — Core Logic / Security Hardening (test.md findings, no UI)

## Metadata
- Date: 2026-10-11
- Status: In Progress (Phase 0 DONE, implementation starting)
- Follows: Mk15 (approvals), Mk16 (system memory), test run `ref_docs/test_run_2026-10-10/results.md` (206/212 + Docker/Composio/2-PC follow-ups)
- Source issues: Claude pre-commit report (Images 1-5: F1-F4 fixed, Major #1, Minor #2-10, UX #11-15) + `results.md` Findings #1-13
- Scope: logical / security loopholes only. UI (`_mask_payload` recipient/subject, chat-reopen, 380px overflow, approve-all, signed-in-elsewhere hint) explicitly deferred.
- Test policy: no Mock for final verification — user fires up tool-capable LM Studio; automated `fake_llm` harness + `pytest` for regression; throwaway PG/SQLite only, never shared Neon / `fyp-app-1` during fixes.

## User decisions (2026-10-11, locked)
1. **Platform extractor (Q1):** `user/*` auto-writes (speaker = `claims.sub`). `dept/*` may be *suggested* from any speaker's `our team...` talk but writes only after dept-head approval (head-only / head+admin / admin-only-escalated per dept mode). `central` only meaningful from `admin` chat (`company policy...`), writes only after a **second admin** approves; single-admin fallback = self-approve. Admin `I prefer...` stays `user/admin@...`. Classifier must be role-aware.
2. **Policy path (Q2):** Honor `SERVICES__POLICY_PATH` — fix `open_policy_store()` to respect `settings.services.policy_path`.
3. **Identity (Q3):** Current `username` is strictly email. Refactor: `email` = identity (login + JWT `sub` + `scope_user` + Composio + org), new `display_name`/`username` field with no format constraints, UI-only, may repeat.
4. **Memory caps (Q4):** Reject empty/whitespace `422`. New `MemorySettings.max_fact_chars = 2000` (`MEMORY__MAX_FACT_CHARS`), code-config now, UI later. Tier caps unchanged (8192 central / 16384 dept / 4096 user).
5. **Live model (Q5):** Tool-capable LM Studio, user provides `model id + base_url` at test time.
6. **Follow-ups:** dept member-talk = `suggest, head approves` (not ignore); central approver = second admin; login identity = email only.
7. **More follow-ups:** per-fact default `2000`; single-admin central = fallback self-approve; existing mismatches = `grandfather + flag` (warn, don't fail migration); `admin` role must be `dept admin`; order = least repetitive edits (identity foundation first so each file touched once).

## Facts (verified read-only 2026-10-11)
- Identity: `members(username TEXT PRIMARY KEY)` (`store.py:309-318`), logical FKs `member_llm_keys(username)` (`:365-376`), `member_composio_keys(username PK)` (`:402-408`), `connected_accounts(username,app PK)` (`:412-420`), `approval_allowlist(member,...)` (`:351-360`); PG twins `0002_core_tables.sql:9-65`, `0008:24-33`. `sub=username` (`store.py:207-214`, `jwt.py:23`, `service.py:53-59`), `scope_user(sub)` (`scopes.py:23-24`), thread default `user:<sub>` (`gateway/app.py:89`, `checkpoints.py:112`), Composio `user_id=sub` (`integrations/service.py:16-17`), provider lookup by `sub` (`llm/factory.py:54`). Full touch list in subagent report 2026-10-11.
- Role validation: `_validate_head_role` only (`store.py:1403-1406`), called in `create:1408-1417` / `update:1448-1459`. Request models free-form (`dashboard/app.py:732-737,851-854`). Canonical `head_role_for` (`enforcer.py:47-49`), `is_dept_head` (`:153-165`), `_verify_dept_head` (`dashboard/app.py:134-154`). Seeds: 4 agents + admin; policy.csv: 4 agents + 4 heads + admin.
- Login: `service.py:48-60` `get→verify→401`; PG `VERSION_TTL 1.0 / MEMBER_TTL 300 / READ_TTL 30` (`pg_store.py:55-57`), `list/get` memoised (`:138-151`), `verify` direct (`:192-197`). Stale `get()=None` → 401 before `verify`.
- Policy: `POLICY_PATH` hardcoded (`policy_store.py:23-25,78`), `open_policy_store()` ignores settings (`:119-127`); enforcer no-arg (`enforcer.py:58-71`, `gateway/app.py:72`, `dashboard/app.py:100`); dashboard `_policy_csv()` fresh per-call (`:1162-1171`) vs enforcer repo file → divergence when overridden. PG seed/drift default `POLICY_PATH` (`pg_policy.py:90,166,171,199`).
- Tool gate: `_is_gated = critical OR (write AND gate_write_tools)` (`graph.py:424-427`); `_execute_one` order `intercept(RBAC) → _critical_gate → execute_external` (`:560-624`); `execute_external` connected-check only after resume (`integrations/service.py:101-126`); `connect_required` only set in gateway (`gateway/app.py:205-213`). Critical unconnected suspends with `connect_required=[]` (APPR-10).
- Skills: `_ensure` requires `central|dept/*` (`model.py:81-92`), no `..`/charset check; `registry.save` `is_relative_to` raises bare `ValueError→500` (`registry.py:58-72`); handlers map parse→422 but `save ValueError` uncaught (`dashboard/app.py:1225-1274`).
- Memory write: `MemoryFactRequest/Item{text:str}` no min/max (`dashboard/app.py:398-403`); `POST/PUT` no empty/size check (`:1557-1585`); `MemoryEditor.add` no empty/cap, `replace` no budget (`edit.py:55-107`); `PUT` bypasses editor entirely; settings only tier caps (`config.py:51-60`).
- Resume/delete: `resume_agent KeyError→404 / ValueError→409` (`runner.py:324-364`); `_resume_owned` 404/403/409 + `RESUME_ELSEWHERE` (`dashboard/app.py:179-185,617-683`); gateway same (`gateway/app.py:356-397`); `DELETE threads` auto-rejects + `delete_thread` (`dashboard/app.py:488-517`).
- Cache: PG `_memo` `READ_TTL 30s` for keys/composio/accounts (`pg_store.py:159-189`); own-write `_invalidate` immediate, other-laptop stale ≤30s; system `_cached_provider lru_cache` (`system.py:126-134`), cleared only on config PUT/DELETE.
- Extractor: `classify` marker-based (`extract.py:42-47`); `system_extract_facts` keeps `scope==own` only (`llm_extract.py:93-115`); `_relevant_facts` early-returns system facts (`graph.py:258-281`); `reflect` gates `memory_allowed` → `_resolve_gate` (`user→none, central→admin-only, dept→mode`) → `interrupt` (`graph.py:699-865`).
- Tests: hermetic (`conftest.py:16-22` blanks `DATABASE_URL`, OpenRouter keys); `tmp_policy` redirect pattern (`test_members_mgmt.py:47-65`); 204+ offline tests.

## Phases (least-rework order — each file touched once)
| Phase | Goal | Key files | Acceptance |
|-------|------|-----------|------------|
| 0 | Safety: hermetic suite green baseline | `tests/conftest.py`, throwaway PG | `pytest -x -q` green before each phase; no shared-Neon writes |
| 1 | Identity foundation: `email` PK + `display_name`, strict email login, `admin-admin` + full role↔dept map, grandfather-flag | `store.py`, `pg_store.py`, `users.py`, `service.py`, `jwt.py`, `dashboard/app.py` member handlers, `enforcer.py g`, `scopes.py`, migrations `0002/0008` + new `0010_email_identity.sql` + SQLite twin | seeds login by email; `not-an-email,com→422`; display repeats OK; `marketing_agent/finance→422`; `admin/marketing→422`; offenders listed not failed |
| 2 | Policy path honor | `policy_store.py:open_policy_store`, `pg_policy.py` defaults, `enforcer.py`, `_policy_csv/_policy_store` | custom temp path writes visible to enforcer; `test_members_mgmt tmp_policy` passes unmodified |
| 3 | Tool gate + skills + memory validation | `graph.py:_execute_one`, `integrations/service.py`, `skills/model.py`, `registry.py`, `dashboard/app.py` skills + memory handlers, `memory/edit.py`, `config.py:MemorySettings.max_fact_chars` | APPR-10 `denied+connect_required`, 0 approvals; `..→422`; empty `422`; `>2000→422`; PUT enforces budget |
| 4 | Freshness: login direct, resume 404/409 split, key-cache invalidate | `service.py:login`, `pg_store.py`, `runner.py`, `dashboard/app.py` resume/delete, `gateway/app.py` | create+immediate login 8/8 on PG; delete→resume `409 chat deleted`; re-save key ≤5s live |
| 5 | Role-aware extractor + routing | `llm_extract.py`, `extract.py:classify`, `graph.py:_relevant_facts+reflect`, `approval_flow/routing` | user auto; dept suggests→head/admin; central→2nd admin (single fallback); second-admin enforced; traces distinct |
| 6 | Real-model verification (user LM Studio) | harness `part1..5b` + `pytest` | `AUTH-11b/APPR-10/SKL-10b/CHAT-08/MEM-02-03` green on real model; `DOCK/SET-08/hosted` stay deferred |

## Explicit non-goals
- UI masking (`to/subject/body ***`), chat-reopen, 380px layout, approve-all, signed-in-elsewhere.
- `DOCK-02/05/06`, `SET-08`, hosted `BYOK-04/05/16/17`, `OPS-05` judge-live, `INT-08` new-toolkit, `REC-05` unreachable, 3-PC latency feel.

## Risks
- Phase 1 migration touches PK + ~20 files; needs SQLite + PG twins + compat alias to avoid breaking audit/traces.
- Second-admin rule stalls single-admin deploys → fallback self-approve (user-approved) must be audited as `self-approved-single-admin`.
- Honoring custom policy path + PG seed interplay: DB-on stays PG truth; file drift stays log-only.

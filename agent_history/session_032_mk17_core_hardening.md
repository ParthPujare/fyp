# Session 032 — Mk17 core hardening: plan + Phases 1–5 + Neon 0010

## Metadata
- Date: 2026-10-11
- Plan / Phase: mk17 — Phases 0–5 (`plan_mk17_core_hardening.md`; Phase 6 real-model verification open)
- Plan step: plan doc → identity → policy path → gates/validation → freshness → role-aware extractor
- Scope: close test.md logical/security loopholes (no UI); bring Neon to 0010
- Status: complete offline (506 passed); uncommitted working tree; container image stale until rebuild
- State at start: HEAD `59cd1ab` ("testing report": Ops-tab `createElement` fix + 371-line `results.md`, 206/212); Claude pre-commit report (F1–F4 fixed, Major #1, Minor #2–10, UX #11–15) in Images 1–5

## Objective
Per user: fix-first (not finish-tests-first), document the multistep plan in `ref_docs/plans/`, implement core logical/security fixes with least-rework ordering, migrate Neon, provide a manual test guide. No Mock in final verification — user brings tool-capable LM Studio.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| T+0 | explore | Pulled commit is only F1 + report; F2–F4 claimed fixed earlier. `test.md` has 255 IDs; 10 nominally unrun (DOCK-03 actually covered via `SET-07/DOCK-03`), 8 distinct fails. Remaining tests are destructive/hosted-key/manual-only → fix-first is correct |
| T+1 | design | Remaining tests low-value/high-risk; open issues would invalidate re-runs. Proposed P0 enforcement → P1 validation → P2 UX-last; user confirmed fix-first |
| T+2 | design | User decisions reshape the work: email=identity + display_name, honor policy path, 2000-char fact cap (config), role-aware extractor (user auto / dept→head / central→2nd admin), least-rework order = identity foundation first so each file is touched once |
| T+3 | implement | `username` PK rename is data-loss risky; kept physical column, added `display_name`, enforced email format + strict role↔dept on top. Grandfather+flag (not fail) for the 2 live bad rows found later |
| T+4 | implement | `open_policy_store()` ignoring settings is a revoke-invisible hole; fixed + PG seed/drift defaults. Tool gate before connect check kills approver-spam. Skill `..` must 422 at parse, not 500 at save |
| T+5 | implement | Login 401 is a memo-ordering bug (cached `get` before direct `verify`); resume-after-delete needs checkpoint check (409, not 404/replay); key cache 30s→5s for auth material. Central self-approve needs the members store for the admin count |
| T+6 | test | 2 `test_system_memory` + 1 attack test encoded Mk16 user-only behavior; updated to Mk17 contract. Full suite 506 passed |
| T+7 | migrate | First new-code import with `DATABASE_URL` set auto-applied 0010 via `ensure_migrated` — verified rather than re-applied; backfilled 13 display_names; drift clean |

## What was done
- `ref_docs/plans/plan_mk17_core_hardening.md` (new) + `INDEX.md` Mk17 row
- `app/auth/store.py`: email regex/validators, `display_name` col (SQLite twin), `MemberRecord.display_name` + `email` alias, `_validate_email/_validate_role_dept` (strict admin-admin, `head role` message compat), `create/update` display_name, `list_role_dept_mismatches`, `count_active_admins`
- `app/db/migrations/0010_display_name.sql` (new, additive) + PG `PgMemberStore.get_direct`
- `app/auth/service.py`: login verify-first + direct claims read; `app/auth/pg_store.py`: `KEY_TTL_S=5.0` for keys/connections
- `app/dashboard/app.py`: member/dept create `email/display_name`, update 422 mapping, skills `save ValueError→422`, memory POST 422/413 + PUT validate+budget
- `app/harness/policy_store.py` + `pg_policy.py`: honor `SERVICES__POLICY_PATH`
- `app/agent/graph.py`: connect-check before `_critical_gate`, system path keeps all scopes + role-aware prompt, `MemoryValidationError→skipped`
- `app/agent/runner.py`: resume raises `ValueError(chat was deleted)` on missing checkpoint → 409
- `app/memory/llm_extract.py`: role-aware `build_messages`, `system_extract_facts` keeps dept/central; `edit.py`: `MemoryValidationError`, 2000-char cap (`config.py:MemorySettings.max_fact_chars`), `replace` budgets; `approval_flow.py`: central second-admin rule (single-admin fallback)
- `app/skills/model.py`: name/segment charset + `..` rejection at parse
- Tests: `test_system_memory.py` (2 updated to Mk17), `attack/test_skill_misuse.py` (parse-time rejection accepted)
- Neon: `0010` applied, `check` 0001..0010, audit 198 intact, 13 display_names backfilled, `sync-config --dry-run` clean
- Manual test guide delivered (curl steps + expected codes per change)

## Decisions locked in this session
- Least-rework order: identity foundation first, email refactor last would have double-touched ~20 files.
- Physical `username` column kept (FK-safe); logical identity is email; `display_name` UI-only, non-unique, ≤100 chars.
- Central memory: second admin required when ≥2 active; single-admin fallback self-approve (audited).
- Dept member-talk still suggests (head approves), not ignored. Per-fact cap 2000 via `MEMORY__MAX_FACT_CHARS`; tiers unchanged. UI fixes deferred.
- Existing mismatches grandfathered + flagged (`bobby@acme,com`, `timmy@acme.in`), migration never fails on them.

## Verification performed
- `pytest tests/ (minus PG phases)`: **506 passed, 17 skipped**; targeted suites (members/auth/enforcement/dept-head/policy/memory/skills/agent/approvals/resume/system-memory) green
- Live sanity (throwaway SQLite + Neon reads): email/role/admin rejects, empty→`empty`, 5KB→`too_long`, traversal→`SkillValidationError`, self-approve→409 with 2 admins / approved by 2nd / approved solo
- Neon: `status`/`check` 0001..0010, `verify-audit` intact, column present, backfill 13 rows, drift 0

## Blocker / open questions
- Phase 6 real-model verification open: needs user's tool-capable LM Studio `model id + base_url` (no Mock per user).
- Working tree uncommitted (16 modified + 0010 + plan doc); `fyp-app-1` container still pre-Mk17 image until `docker compose up -d --build` on every PC.
- Deferred: `DOCK-02/05/06`, `SET-08`, hosted `BYOK-04/05/16/17`, `OPS-05` live, `INT-08` new-toolkit, `REC-05`, UI masking/reopen/layout.

## Next step (for the next session)
- Phase 6: run real-model regression (`LOCAL-tool`, `MEM-02/03`, `APPR-10`) on throwaway DB with user's LM Studio; then commit + push + rebuild images.

## Handoff notes
- `ensure_migrated()` auto-applies on first new-code boot against Neon — the 0010 apply happened that way; future migrations need no manual `migrate` if a new-code process boots first.
- `bobby@acme,com` (comma, not dot) can never receive mail and `timmy` sits in the wrong dept with `scopes:['*']` — admin should fix both via Members tab.
- `bob@acme.com` is now `devops_agent/devops` on live (was `hr` seed) — valid pairing, left alone.

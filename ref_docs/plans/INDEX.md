# Plan Registry — Index

Every plan the project has ever run, one row each. This is the **traceable summary of all work**:
join this table with `agent_history/INDEX.md` (sessions map to a plan + phase via the `Plan` column)
to reconstruct the full story at any time.

Each numbered plan (`mk<NN>`) owns its milestone/timeline doc in this folder. **Plan Mk1 is the
original 16-week plan** (`ref_docs/plans/plan_mk1_core_harness.md`) and is frozen — append-only history, never rewritten.

| Plan | Title | Status | Dates | Deliverable / focus | Link |
|------|-------|--------|-------|---------------------|------|
| Mk1 | Core harness — full 16-week vertical slice (RBAC, 3-tier memory, HITL, sanitizer, skills, trajectory+evals, attack suites) | Milestones 1–8 (weeks 1–14) DONE; stretch + report (weeks 15–16) still open | 2026-08-10 → | Deterministic zero-trust harness with evidence (204 offline tests green) | [./plan_mk1_core_harness.md](./plan_mk1_core_harness.md) |
| Mk2 | Web-UI demo + member management | **Phases 1–3 DONE 2026-08-15 (sessions 010–012)**, **Phase 4 DONE 2026-08-16 (session_013)**; Phases 5–6 pending (one session = one phase, sessions 014–015) | 2026-08-15 → | All functionality usable via the web UI (chat, any-user login, dynamic members + departments, browser memory test) + report/recorded demo | [./plan_mk2_web_ui_members.md](./plan_mk2_web_ui_members.md) |

## Rules

1. Plan docs live in `ref_docs/plans/`, numbered `plan_mk<NN>_<slug>.md`; one row per plan in this file.
2. Append new plans here; never edit a closed plan's doc — write a follow-up plan instead.
3. `agent_history/` sessions carry a `Plan` column (plan id + phase) so every session is traceable
   to a plan (see `agent_history/INSTRUCTIONS.md`).
4. One session = one **phase** of a registered plan (phase-level granularity, not step-level).
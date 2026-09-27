# Session 026 — Mk13 Phase 4: episodic recall on the shared Postgres (Neon)

## Metadata
- Date: 2026-09-27
- Plan / Phase: mk13 — Phase 4 (`plan_mk13_docker_cloud_db.md`)
- Plan step: replace SQLite FTS5 recall with Postgres full-text search on the shared DB
- Scope: recall must stay scope-isolated (Theorem 2 / I2 in `research_objective/01_ztae_formal_spec.md`)
- Status: complete — verified on local Postgres and real Neon
- State at start: Phases 0-3 done; episodic transcripts were still per-machine SQLite

## Objective
"Remember what
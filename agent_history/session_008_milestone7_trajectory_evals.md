# Session 008 — Trajectory Recorder + Ops Trajectory Viewer + Evals Harness

## Metadata
- Date: 2026-08-15
- Plan step: Week 12 — trajectory viewer + evals harness (deterministic + judge); record everything the model saw (`PROMPT`/`REASONING`/`CONTEXT_INJECTION` events + per-event `source`), by-source trajectory viewer (admin-only, PII-sanitized), trace-completeness eval. Deliverable "Ops tab shows run trajectories filterable by source; eval scores per run" (ref_docs/plan.md §11 row 7)
- Scope: New `app/evals/` (deterministic checks + LLM-as-judge, `evaluate_run` → EVAL events), trace schema extension (3 trace-event types + `source` field + migration), trajectory recording in the graph (PROMPT/REASONING/CONTEXT_INJECTION, sanitized on copy), dashboard Ops tab (runs list, per-run trajectory with source filter chips, evals panel), runner eval hook, offline demo + tests.
- Status: complete
- State at start: Milestone 6 done (git HEAD `2e836fb` "milestone 6: folder skills + …"), 142 offline tests green, ruff clean. All M7 scope decisions pre-approved: build BOTH deterministic and judge evals; judge eval gated (runs only when a judge provider is injected or `settings.llm.provider == "litellm"`); eval fires only on final run completion (incl. resumed), never on interrupted partial runs; trajectory persists sanitized copies only; demo + tests use scripted MockLLM for judge verdicts.

## Objective
Deliverable: **ops tab shows run trajectories filterable by source; eval scores per run.** Sub-objectives: (1) record `PROMPT`/`REASONING`/`CONTEXT_INJECTION` events + per-event `source`, PII-sanitized; (2) `list_runs` + trajectory viewer filtered by source (admin-JWT); (3) auto-eval after every completed run — deterministic always, judge gated; results stored as traced EVAL events; (4) offline demo + tests; (5) docs (README, plan.md, proposed_architecture.md) + session summary.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 20:05 | explore | Read agent_history per session rules. M7 = plan.md row 7 (week 12). Made a plan-map (build trace events → store → LLM reasoning channel → graph recording → evals → runner hook → dashboard ops tab → demo → tests → docs). |
| 20:15 | design | Trace event universe stays `TraceEventType`; add `PROMPT`, `REASONING`, `CONTEXT_INJECTION`. Per-event `source` field with `DEFAULT_SOURCE` map (user/llm/memory/tool/guardrail/eval/system) + explicit override wins. |
| 20:30 | design | PII discipline: trajectory mutates the prompt/reasoning/context on **copy** via `sanitize_text` (dead-theory: mutating the real message list breaks what the model sees). Sanitizer must be scope-aware — reuse `sanitize_text`. |
| 20:45 | implement | schema.py: event types + `source` field + maps. store.py: `source` column in DDL + `ALTER TABLE … ADD COLUMN` migration guard (legacy trace.sqlite opens + migrates — verified via a temp legacy file), `get_run_tree(run_id, source=None)` filter, `list_runs()`, fixed an `f"""` prefix regression in `verify_integrity`. |
| 21:05 | implement | llm: `LLMResponse.reasoning`; mock passes reasoning from script/sequence; litellm reads `reasoning_content`/`reasoning`. graph: `_sanitize_trajectory`/`_sanitize_messages` helpers; trace `CONTEXT_INJECTION` in load_memory (kind=snapshot, scopes, chars), `PROMPT` (step, tool_schema_count, sanitized messages) + `REASONING` (step, sanitized text) in agent_node; `_trace_call` adds `result_summary` + `sanitized` to TOOL_CALL payload. |
| 21:25 | implement | evals package: `base.py` (EvalCheck/EvalResult with score), `deterministic.py` (5 checks: trace_integrity, trace_completeness [run_start/node/run_end present, prompt→reasoning pairs, contiguous seq], rbac_contract [no denied resource executed; allowed must have ALLOW/APPROVED audit; a skill aggregate is NOT a separate RBAC decision — justified by SKILL_USE gate or nested deny], audit_pairs, guardrails [no tool after terminal guardrail]), `judge.py` (3 dims 0–5, threshold 3, strict-JSON parse fail-on-parse → score 0, deterministic summarize_run/build_judge_prompt, broad-catch so a judge failure never sinks the run), `__init__.py` (evaluate_run writes EVAL events for det + optional judge; eval_enabled_for_judge gate). |
| 21:40 | implement | runner: `_fire_evals`; run_agent/resume_agent new kwargs `evals_enabled=True`, `judge_provider=None`; eval after `end_run` (incl. resumed); interrupted runs skip. Resume-outcome detection must not depend on last event (EVAL lands after run_end) — keyed off run_end payload. |
| 22:00 | implement | dashboard: admin-JWT ops endpoints `GET /api/ops/runs`, `GET /api/ops/runs/{run_id}?source=…`, `GET /api/ops/runs/{run_id}/evals` (404 on unknown run; 401/403 via `_verify_admin`). static: Ops nav button, runs table, run view w/ source filter chips, expandable event payload `pre` blocks, evals panel (PASS/FAIL badges, per-check score/detail). |
| 22:20 | implement | scripts/demo_trajectory_evals.py + `make demo-evals`: alice (crm search w/ reasoning + role-granted email), carol (skill w/ nested deny), scripted judge verdicts; prints source-filtered trajectory + eval scores. |
| 22:35 | test | test_evals (13) + test_trace additions (+5) + test_dashboard ops tests (+5) + test_agent trace/EVAL ordering + trajectory PII (SSN masked, email visible). |
| 22:50 | test | **`test_ops_runs_lists` failures.** First: `set(runs) == {run_a, run_b}` + event_count mismatch — I hard-coded 6 but `_seed_run` actually writes start, NODE, PROMPT, REASONING, CONTEXT_INJECTION, TOOL_CALL, end_run, EVAL = 8 (fixed expectation). Second: run_b showed `completed` not `resumed` — `list_runs` read `tree[-1]`, but EVAL now lands AFTER run_end. Fixed `list_runs` to scan for the run_end event (breaks when found) and read its payload → resumed detected. |
| 23:05 | lint | ruff F401 (unused json), I001 (runner imports), RUF022 (unsorted `__all__` in evals), BLE001 (blind `except Exception` in judge). Auto-fixed; judge broad-catch kept with `# noqa: BLE001` + justification comment (a judge failure must not sink the run). |
| 23:15 | verify | **161 passed** (142 baseline + 19 new), ruff clean, demo green (alice 2 events + carol nested deny + judge PASS 0.867), trace integrity OK. |
| 23:30 | docs | README (M7 status/status line, app/ tree, demo-evals target, trajectory+evals section, tests list, next milestones), plan.md (tree node ✅, tests 161, rows for week 11 skills AND week 12 trajectory), proposed_architecture.md status paragraph. |

## What was done
- `app/ops/schema.py`: `PROMPT`/`REASONING`/`CONTEXT_INJECTION` added to `TraceEventType`; `source: str` on `TraceEvent`; `DEFAULT_SOURCE` map + `EVENT_SOURCES`.
- `app/ops/store.py`: `source` column in DDL + `ALTER TABLE … ADD COLUMN` migration guard (legacy DB opens intact); `add_event(source=…)` derives default from type, explicit wins; `get_run_tree(run_id, source=None)` filter; new `list_runs()` (run_id/stated_ts/actor/event_count/outcome, outcome from the **run_end** event's payload — not the last row — unwraps `{"output": {…resumed…}}`); fixed `verify_integrity` `f"""` prefix regression.
- `app/llm/{base,mock,litellm}.py`: `LLMResponse.reasoning: str | None` end-to-end (script/sequence + provider `reasoning_content`).
- `app/agent/graph.py`: `_sanitize_trajectory`/`_sanitize_messages` (mask on copy via `sanitize_text`); `CONTEXT_INJECTION` in `load_memory` (kind=snapshot, scopes, chars); `PROMPT` (step, tool_schema_count, sanitized messages) + `REASONING` (step, sanitized text) in `agent_node`; `_trace_call` adds `result_summary` + `sanitized` to TOOL_CALL.
- `app/agent/runner.py`: `_fire_evals` helper; `evals_enabled=True`/`judge_provider=None` kwargs on `run_agent`/`resume_agent`; eval fires only after `end_run` (incl. resumed), never on interrupted runs.
- `app/evals/` (new): `base.py`, `deterministic.py` (5 checks incl. skill-aggregate reasoning), `judge.py` (JudgeEvaluator, strict parse, summarize_run un-wraps doubled `{"output":{"output":…}}`), `__init__.py` (`evaluate_run` writes EVAL events; `eval_enabled_for_judge`).
- `app/dashboard/app.py` + `static/index.html`: Ops tab — runs table → trajectory view with source filter chips → expandable payloads + evals panel; `/api/ops/runs`, `/api/ops/runs/{id}?source=…`, `/api/ops/runs/{id}/evals`.
- `scripts/demo_trajectory_evals.py` + `Makefile` `demo-evals`: 2-scenario offline demo w/ scripted judge.
- Tests: `tests/test_trace.py` +5 (source default/override, filter, list_runs, legacy migration), new `tests/test_evals.py` (13: det pass/fail, audit contradiction, EVAL tracing + judge 0.867, parse/invalid scores, runner EM_coop, trajectory PII, no-eval-on-interrupt via finance_transfer), `tests/test_dashboard.py` +5 ops, `tests/test_agent.py` updated for EVAL-after-run_end ordering.
- Docs: README, `ref_docs/plan.md`, `ref_docs/proposed_architecture.md` status sites updated.

## Decisions locked in this session
- **Judge suite ships; gated**: `eval_enabled_for_judge` runs the judge only when a judge provider is injected (scripted MockLLM in tests/demo) or the app runs on a real LiteLLM backend — CI stays deterministic.
- **Eval only on final completion**: interrupted (pre-HITL-decision) runs never get EVAL; resumed runs get one (payload carries `resumed: true`).
- **Skill double-trace**: a skill is traced as `SKILL_USE` + an aggregate `TOOL_CALL` (M6 legacy). `rbac_contract` treats the skill aggregate as an implementation artifact, not a separate RBAC decision — justified via the SKILL_USE gate / nested deny rows.
- **Trajectory PII**: sanitized on copy, never mutating the message list given to the model. Email survives trajectory (all roles own `pii_email`); SSN masked (`pii_ssn` un-granted) — asserted in tests + demo.
- **Resume/outcome detection in `list_runs` must scan for `run_end`**, never assume it is the last row (EVAL events now follow it).
- **Judge robustness**: broad-catch in the judge provider call with an explicit `# noqa: BLE001` — a judge failure scores 0 but must never sink the run's eval.

## Verification performed
- `uv run pytest` → **161 passed** (142 baseline + 19 new, all offline); `uv run ruff check app tests scripts` → clean.
- `uv run python scripts/demo_trajectory_evals.py` → alice: CRM search with REASONING, every event source-typed, source filter returns exactly the by-source subsets; carol: skill deny + nested deny audited; judge PASS score=0.867 (4/5 quality, 5/5 injection_resistance, 4/5 skill_faithfulness); EVAL events traced after run_end; trace integrity OK.
- Legacy-DB migration independently verified: temp trace.sqlite created by the pre-source DDL opens, migrates, and keeps older events readable.

## Blocker / open questions
- None. (Pre-existing: `InsecureKeyLengthWarning` in tests — harmless mock key.)

## Next step (for the next session)
- Execute **Weeks 13–14 (Milestone 8)**: attack suites — policy-matrix, prompt-injection, cross-dept memory, skill misuse, loop-abuse, HITL-bypass, trace/audit integrity + hardening. Every attack doubles as an eval vector/demo. Deliverable per plan.md §11 row 8: "Attack demo passes". (Stretch when budget allows: trajectory search / replay / fork from plan.md §10.)

## Handoff notes
- 161 tests must stay green; M7 was purely additive (no delete/write-path changes — the session_007 repo-wipe class can't recur from this milestone).
- `list_runs`/`summarize_run` both unwrap the doubled `end_run` payload nesting `{"output": {"output": …}}`; `start_run` wraps input as `{"input": {…}}` — keep both un-wraps in any future consumer.
- MockLLM spec keys now include `reasoning` (script or sequence). Judge verdicts in tests/demo are scripted via MockLLM — deterministic.
- `get_run_tree(run_id, source=None)` is the single filter point for the trajectory viewer; the dashboard refreshes ops runs from `/api/ops/runs` and event payloads from `/api/ops/runs/{id}?source=…` (multiple `source` query params supported).
- Eval event payload shape: `{"suite", "passed", "score", "checks": [{name, passed, detail, score}]}`; EVAL events append AFTER run_end (test_agent was updated to match).
- git: `master`, HEAD `2e836fb` (milestone 6); M7 changes are uncommitted in the working tree — commit as "milestone 7: trajectory recorder + ops viewer + evals harness".
- Run env unchanged: `uv run pytest`, `uv run ruff check app tests scripts`, `make demo-evals`; services :8000–:8006.
# Session 009 — Milestone 8: Attack Suites + Hardening

## Metadata
- Date: 2026-08-15
- Plan step: Weeks 13–14, attack suites + hardening (plan.md §11 row 8); deliverable "Attack demo passes"
- Scope: Implement A1–A8 attack suites in `tests/attack/`, harden audit + skill registry, write the attack demo, update docs
- Status: complete
- State at start: Milestones 1–7 built (161 offline tests green). Audit log was plain JSONL (no integrity). Skill registry `save()` had no root-escape guard. `tests/attack/` dir existed with empty `__init__.py`.

## Objective
From plan.md row 8 + previous session: attack suites (policy matrix, prompt-injection, cross-dept memory, skill misuse, loop-abuse, HITL-bypass, trace/audit integrity) + hash-chain audit hardening; "Attack demo passes".

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 10:00 | load | Re-read INSTRUCTIONS, INDEX, latest session; reviewed my own working notes + the implemented `tests/attack/**` files, runner/tools/graph/memory code. Key fact: `hr.delete` is *deliberately absent* from policy.csv (deny-by-default even inside HR) — so "same-role allowed tool" must use a *different* tool (e.g. devops deploy). |
| 10:20 | verify | `tests/attack/test_cross_dept_memory.py` green. |
| 10:40 | fix | Cross-dept write assertion used set-membership of full sentences instead of substring — fixed with `any("weekly standup" in t for t in texts)`; suite green. |
| 11:00 | design | Decided A4 needs a **registry hardening too**, not just tests: `SkillRegistry.save` path-escape guard (the session_007 delete-bug class, applied to writes). |
| 11:30 | test | `test_skill_misuse.py` written (5 tests: cross-dept skill invisible+denied, central skill nested devops blocked for non-devops, malicious skill with forbidden nested tool denied, save() rejects `..` escape, save() accepts normal path). All green on first run. |
| 13:00 | test | `test_loop_abuse.py` (5 tests: deny-storm bounded+short-circuited, tool-budget, max-steps, run-timeout, nothing past terminal guardrail). Fixed `_settings` helper (Settings has no `scheduler` — use `model_copy(update=...)`); reframed "no tool after terminal guardrail" (guardrail ends current batch, stops future steps → budget=2 ⇒ exactly 2 tools). |
| 14:00 | test | `test_hitl_bypass.py` (5 tests). Fixed resume needing a **shared** `InMemorySaver` (fresh instance per call → `KeyError: claims` on resume). Discovered gateway 403/409/404/401 already covered in `test_chat.py`, so kept the suite focused on business-level proofs + JWT role-forge. |
| 15:00 | test | `test_integrity.py` (9 tests: chain intact/deterministic, unicode chain, edit/delete/reorder/truncate detection, legacy-line backward-compat incl. legacy-after-hashed tamper, trace tamper + EVAL trace_integrity, EVAL audit_integrity on tampered audit). Fixed a stray `mkdir` on the file path; stocked `runs[-1]` for the tampered run_id. |
| 15:30 | fix | Full-suite run broke: `tests/attack/conftest.py` shadowed the root `tests/conftest.py` for `from conftest import make_runner` in root tests. Renamed attack helpers `conftest.py` → `helpers.py` and rewrote imports to absolute `tests.attack.helpers`; **185 → 204 tests green**. |
| 16:00 | lint | ruff: cleaned unused imports, import order, RUF059 underscore aliases, SIM401 in `audit.py`; **`ruff check app tests scripts` clean**. |
| 16:30 | demo | `scripts/demo_attacks.py` — 6 scenarios (A1 matrix, A2 injection, A4 skill nested deny, A5 deny-storm, A6 HITL suspends-until-approved, A7/A8 audit tamper detected). First run failed on the A1 "dave can" claim (`hr.delete` is deny-by-default for *everyone*) → switched to devops deploy as the same-role allow. `make demo-attacks` green. |
| 17:00 | docs | Updated README (new "Attack suites + hardening (Milestone 8)" section + tests bullet + Next milestones now only week 15–16), plan.md §11 row 8 → DONE session_009, proposed_architecture.md (status banner → M1–8, §15 matrix + Status/evidence column), summary.md (today-status). |

## What was done
- `app/harness/audit.py`: hash-chained JSONL audit log — per-line `prev_hash`/`hash` envelope (genesis `0×64`), head hash mirrored to a `.head` sidecar, `_last_hash()` bootstraps off the sidecar, `verify_integrity()` detects edit / reorder / delete / truncate / legacy-line-after-hashed. `entries()` still returns clean entry dicts (consumers unaffected).
- `app/evals/deterministic.py` (already had `_audit_integrity` from session_008; untouched) — attack tests now *prove* it fires on tamper.
- `app/skills/registry.py`: `save()` resolves the target path and raises `ValueError` if it escapes the registry root.
- `tests/attack/helpers.py` (renamed from `conftest.py`): `claims_for`, `ALICE/BOB/CAROL/DAVE/ADMIN`, `decisions_for`, `outcome_names`, `outcome_of`.
- `tests/attack/test_policy_matrix.py` (A1), `test_prompt_injection.py` (A2/B2/B3), `test_cross_dept_memory.py` (A3), `test_skill_misuse.py` (A4), `test_loop_abuse.py` (A5), `test_hitl_bypass.py` (A6), `test_integrity.py` (A7/A8) — 43 attack tests across 7 files.
- `scripts/demo_attacks.py` + `Makefile` `demo-attacks` target.
- Docs: `README.md`, `ref_docs/plan.md`, `ref_docs/proposed_architecture.md`, `ref_docs/summary.md` (+ `ref_docs/threat_model.md` created earlier in this session's planning).

## Decisions locked in this session
- Attack tests script the model to **fully comply** with each attack (worst case) — enforcement must block structurally; this is the "% prompt-independent" proof.
- `tests/attack/` has **no `conftest.py`** (it shadows the root one) — helpers live in `helpers.py`, imported absolutely as `tests.attack.helpers`.
- `hr.delete` policy row is deliberately absent → HR/delete is deny-by-default for every role; "same-role allow" demos use another operation (devops deploy).
- Guardrail semantics: a terminal guardrail ends the current tool batch and stops *future* steps (not the in-flight batch) — tests assert bounds accordingly (budget=2 ⇒ exactly 2 tools).
- Resume/HITL tests must share a single `InMemorySaver` across `run_agent` and `resume_agent`.

## Verification performed
- `uv run pytest tests/ -q` → **204 passed** (was 161).
- `uv run ruff check app tests scripts` → clean.
- `uv run python scripts/demo_attacks.py` (and `make demo-attacks`) → all 6 scenarios pass end-to-end.

## Blocker / open questions
- None. (Stretch items remain: weeks 15–16.)

## Next step (for the next session)
- Week 15/16 stretch + final report/recorded demo (plan.md §11 rows 15–16), e.g. `learn_skill` auto-gen + HITL, then report writing — or begin the recorded demo script (login as roles → tool use → deny → HITL → memory recall + skill → trace + eval review).

## Handoff notes
- The A6 HITL demo resumes with a scripted sequence that produces an extra post-resume interrupt (allowed, benign) — the demo's assertions pass regardless; a "cleaner" demo could script the follow-up agent step to answer instead.
- If the repo ever re-runs the full suite and hits import errors, check for a stray `tests/attack/conftest.py` from a future session (it must remain `helpers.py`).
- `tests/attack/test_hitl_bypass.py` shares gateway `dependency_overrides`; always clear them in `finally`.
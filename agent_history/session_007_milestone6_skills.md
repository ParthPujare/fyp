# Session 007 — Folder/Markdown Skills, Skill RBAC, Skills Editor + Memory Browser

## Metadata
- Date: 2026-08-15
- Plan step: Week 11 — folder/markdown skills + skill RBAC + skills editor + memory browser; deliverable "edit a skill → behavior changes on next run" (ref_docs/plan.md §11 row 6)
- Scope: New `app/skills/` (folder registry + model + executor), per-skill Casbin rows (skill_<name>, use), skill-exposed tools in the agent tool list ("skill-as-tool"), dashboard skills editor CRUD + memory browser (view/add/edit/delete), offline multi-scenario demo. Also: root-caused + fixed a severe `delete()` bug that wiped the whole repo, recovered all project files from an opencode snapshot, and re-initialized git.
- Status: complete
- State at start: Milestone 5 done (git HEAD `cnt`; repo later re-initialized, see Handoff): PII sanitizer, critical-tool HITL, dashboard v1 (:8006), hardened `/approvals/{id}/decide`; 127 offline tests green. All four M6 scope decisions pre-approved: skill-as-tool; Casbin rows per skill; memory browser = view + add/edit/delete (audit-logged); skills editor = full CRUD incl. create-from-UI.

## Objective
Deliverable: **edit a skill → behavior changes on next run.** Sub-objectives: (1) folder/markdown skills registry (`app/skills/`), (2) per-skill Casbin RBAC (p, <role>, <dom>, skill_<name>, use), (3) skills exposed as agent tools with nested `tool_calls` executed deterministically through the same intercept/HITL/sanitize/guardrails path, (4) dashboard skills editor (CRUD) + memory browser driven via `/api/skills`, `/api/memory`, (5) offline demo + tests.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 14:05 | explore | Continued M6 from session_006 handoff. Verified M5 wiring: `run_agent` accepts every component as kwargs; dashboard/gateway dependency overrides need `lambda:`. |
| 14:15 | design | Locked skill-as-tool: visible skills appended to tool schemas; harness executes nested `tool_calls[]` through the SAME `intercept_tool`/HITL/sanitize/guardrails path as direct tools — no second door policy. |
| 14:25 | design | Casbin rows per skill: `p, marketing_agent, marketing, skill_launch_campaign, use`; underscore names because `keyMatch2` treats `:` as a route-param delimiter (session_004/005 gotcha). |
| 14:40 | implement | `app/skills/model.py` + `registry.py` + `executor.py`: YAML front-matter parse (name/trigger/scope/owner_role/version/args_schema/steps/tool_calls), `{{var}}` template resolution against invoke args, `load_all/schema_for/get`, `SkillExecutor` that iterates referenced `tool_calls`, executes each via intercept→HTTP→sanitize, aggregates outcomes (any deny → denied; step error → error). |
| 15:00 | implement | Wired into `app/agent` `_build_deps`: `visible_skills = [s for s in skills.load_all() if enforcer.skill_allowed(s.name, claims)]`, each appended via `schema_for`; `_execute_one` checks TOOL_REGISTRY then `deps.skills.get(name)`; `_trace_skill` emits SKILL_USE events (payload skills/steps/outcome/reason; outcomes "denied"|"timeout"|"error"|"allowed") + audit rows resource `{type:"skill", id}`. |
| 15:15 | implement | Dashboard: `/api/skills` (list w/ `policy_row` flag) + `/api/skills/{name}` CRUD + `/api/memory/browse`/`/api/memory` + `/api/memory/{scope}/{id}` add/edit/delete — all admin-JWT-gated; skeleton replaced. Canonical URI set for the app (skills_path/static_path/data/health_port). |
| 15:30 | implement | Seed skills under `skills/{central/dept}`: `triage_incident` (central, all 4 roles), `new_leave_request` (hr), `launch_campaign` (marketing, 2 nested crm steps). policy.csv: 3 skill rows. |
| 15:40 | test | New `tests/test_skills.py` (11): parse/validation, skill RBAC matrix, schema exposure per role, nested call goes through intercept (carol denied on nested devops_create_deployment), audit rows, hot-reload on new settings. |
| 15:50 | test | `tests/test_dashboard.py` skills-editor + memory-browser tests; needed a sandbox skills fixture because CRUD writes to the real `skills/` dir from the auto-cleaned settings would mutate repo seeds. |
| 16:00 | 🚨 | **Repeated catastrophic file loss.** After every dashboard DELETE test (~10–15 s later) the whole workspace tree — `.git/objects`, `.venv`, even `/tmp` copies — vanished. Absolute paths in snapshots "corrupted" to the real project path on restore; `cp -a` restores stayed stable (92 files, 90 s); every re-run of the suite re-triggered the wipe. |
| 16:15 | explore | Hypothesized an external "wiper" process; disproved by timing + the fact that only suite runs afterward vanished. Recovered each time from `~/.local/share/opencode/snapshot/9a6d8cae148e5468bcc23286a343a18df03d5783/8260ba2007001df52d9ef428b82163dddbdb4e63`. |
| 16:30 | fix | **ROOT CAUSE FOUND — this session's own bug.** `parse_skill_document` leaves `skill.path = ""` → `registry.delete()` computes `Path("").parent` = `.` (cwd) → `rglob("*")` recursively unlinks the ENTIRE cwd (repo incl. .git + venv), then `rmdir('.')` → OSError. The dashboard DELETE test (`test_skills_editor_roundtrip`) was the wiper. |
| 16:40 | fix | Two-layer fix: `app/skills/model.py` `parse_skill` back-fills `path` via `dataclasses.replace`; `app/skills/registry.py::delete()` guards `skill_dir resolve == root` or not `is_relative_to(root)` → refuse. Removed now-unused `field` import (F401). |
| 16:55 | recover | `git --git-dir="<snapshot store>" archive a53b9927467edeed9eacb18357f0e7fec4b6e5a0 | tar -x` restored the full project (91 files incl. app/, tests/, skills/, agent_history, ref_docs, Makefile, README, pyproject, uv.lock). `~/.fyp_recovery/src` kept as pristine canonical copy; `run.sh` one-shot build+test. |
| 17:05 | fix | Tests against the restored project: fixture now writes BOTH sandbox skills (`editor_skill` no policy row, `launch_campaign` name-matches real policy row) → assert `policy_row` False/True; DELETE route: `/api/memory/dept/marketing/does-not-exist` (no trailing slash — greedy `{scope:path}` swallowed it → 405 not 404). Lint: removed unused `make_runner` import (test_skills.py), I001 blank line, RUF059 `dash`→`_`. |
| 17:15 | verify | **142 passed** (was 127), ruff clean — this time run IN the real project dir with a fresh venv; dir stayed stable (bug fixed). |
| 17:20 | ops | Original git history destroyed by the bug (objects/refs unlinked). Fresh `git init -b master`; restored-but-uncommitted tree has no commits yet. User confirmed: finish M6 deliverables AND commit the restored state. |
| 17:40 | implement | `scripts/demo_skills.py` (4 scenarios, offline MockLLM + in-process services): (1) per-role schema visibility, (2) alice invokes launch_campaign → nested crm calls execute (steps=2, allowed), (3) carol's triage_incident ALLOWed but nested devops DENIED → skill denied + audit [ALLOW,DENY]; admin allowed, (4) hot reload — swap step-2 YAML block in skill.md → audited nested tool changes on next run. Fixed: missing `asyncio` import, non-existent `crm_get_campaign` (wrong swap target), RUF015 `[.][0]`→`next(...)`. Demo passes + trace integrity OK. |

## What was done
- `app/skills/model.py`: `parse_skill` — **path back-fill via `dataclasses.replace`** (the wipe fix); `field` import removed.
- `app/skills/registry.py`: `delete()` — **resolve + `is_relative_to(root)` guard**, refuses to delete anything not under the registry root; prunes empty parents only after the guard passes.
- `app/skills/executor.py`: deterministic nested `tool_calls` execution through intercept/HITL/sanitize/guardrails; outcome aggregation (denied / error / timeout / allowed).
- `app/agent/graph.py` + `_build_deps`: `visible_skills` per-role, skill schemas appended to tool list, `_execute_one` dispatch to `SkillExecutor`, `_trace_skill` SKILL_USE events, skill audit rows.
- `app/harness/policies/policy.csv`: `skill_launch_campaign` (marketing_agent), `skill_new_leave_request` (hr_agent), `skill_triage_incident` (all 4 roles).
- `skills/{central/triage_incident, dept/hr/new_leave_request, dept/marketing/launch_campaign}/skill.md`: 3 seed skills.
- `app/dashboard/app.py`: /api/skills list (w/ `policy_row`) + `/name` CRUD, /api/memory browse + add/edit/delete; `static/index.html` skeleton replaced; canonical URI.
- `tests/test_skills.py` (11 new), `tests/test_dashboard.py` (fixed fixture + editor/memory assertions), lint fixes in `test_skills.py`.
- `scripts/demo_skills.py` (new): 4-scenario M6 deliverable demo, fully offline.
- `README.md`, `Makefile`: Milestone 6 status + `demo-skills` target.
- `~/.fyp_recovery/`: canonical fixed copy + `run.sh` (gated out of commit; outside repo).

## Decisions locked in this session
- **Skill-as-tool**: visible skills are real tools; nested calls go through the same RBAC/HITL/sanitize pipeline as direct tools — a skill can never bypass policy.
- **Per-skill Casbin rows** `skill_<name>, use` (underscore names — colon delimiter gotcha); non-listed skills invisible to the schema AND cannot be invoked.
- **Hot reload = read on every run**: registry scans `settings.skills.skills_dir` fresh per `_build_deps`, so editing skill.md on disk changes the next run's nested behavior (no caching by name in the demo path).
- **Safety over (mis)feature**: `SkillRegistry.delete` refuses path traversal by construction; registry roots are resolved + containment-checked. This class of repo-wipe can't recur.

## Verification performed
- `uv run pytest` → **142 passed** (was 127), fully offline; `uv run ruff check app tests scripts` → clean.
- `uv run python scripts/demo_skills.py` → scenario 1: alice/bob/carol see only role-visible skills; scenario 2: launch_campaign runs 2 nested crm steps, allowed; scenario 3: carol SKILL_USE denied (nested devops block) w/ audit [ALLOW, DENY]; admin allowed; scenario 4: swapping the step-2 YAML block changes audited nested tools (`crm_create_campaign` → `crm_search_customer`) on the next run; trace integrity OK.
- Demonstrated root cause: identical wipe reproduced in `/tmp` + recovery copies until the model/registry fix landed; after fix, full `uv sync` + `pytest` + `ruff` ran in-place with zero loss.

## Blocker / open questions
- Original git history (Milestones 1–5 commits) unrecoverable locally — objects were unlinked by the delete bug. Candidate remotes in shell history: `https://github.com/ParthPujare/RAG.git`, `https://github.com/ParthPujare/research_agent.git` (prior notes mention origin/main `646e1a9`, unverified). User to confirm the real remote URL; recovery is a `git clone` + cherry-pick/squash onto the fresh `master`.
- `InsecureKeyLengthWarning` in tests is pre-existing (12-byte HMAC test key); harmless.

## Next step (for the next session)
- Execute **Week 12 (Milestone 7)**: trajectory viewer + evals harness — record every input the model saw (`PROMPT`/`REASONING`/`CONTEXT_INJECTION` events + `source`), by-source trajectory viewer (ops tab, admin-only, PII-sanitized), trace-completeness eval with per-run scores. Deliverable: "ops tab shows run trajectories filterable by source".

## Handoff notes
- **The repo was fully lost and rebuilt twice this session. Recovery source (do not delete):** `~/.local/share/opencode/snapshot/9a6d8cae148e5468bcc23286a343a18df03d5783/8260ba2007001df52d9ef428b82163dddbdb4e63` (git store), latest full tree `a53b9927467edeed9eacb18357f0e7fec4b6e5a0`. Restore: `git --git-dir="$S" archive "$t" | tar -x -C <dest>`. `~/.fyp_recovery/src` = pristine fixed source; `~/.fyp_recovery/run.sh` = one-shot build+test.
- **Git state:** fresh repo (`git init -b master`), zero commits, whole tree untracked. Restored + finished state should be committed BEFORE further work.
- **The wipe bug must never regress:** never call `Path(...).parent` on an empty path in deletion code; `delete()` must refuse anything outside the resolved registry root (see `app/skills/registry.py` + `app/skills/model.py`).
- `_build_deps` builds `visible_skills` from `enforcer.skill_allowed(name, claims)`; skills appear as `name`-tools with `schema_for`; a skill invoked without matching policy row → treated as unknown tool (denied at schema/intercept).
- Dashboard test fixture uses a temp skills dir with BOTH a no-policy sandbox skill (`editor_skill`) and a name-colliding-with-real-policy skill (`launch_campaign`) to exercise `policy_row` True/False.
- Demo/test routes are offline (httpx.MockTransport + `serve_request`); dashboard demo uses in-process executor shared as `executor=` kwarg.
- Guardrail/service defaults unchanged from M5: max_steps=10, tool_budget=8, call_timeout=30s, run_timeout=120s; services :8000–:8006.
# Session 001 — Docs Restructure + Plan Refinement + Proposed Architecture

## Metadata
- Date: 2026-08-10
- Plan step: Setup / Foundation (pre-step — repo contains only planning docs)
- Scope: Refine the project plan, produce a detailed architecture document, and create the session-history system.
- Status: complete
- State at start: Git repo `ParthPujare/fyp` with 3 commits and 3 markdown files at root (`plan.md` 2026-08-01, `problem.md` 2026-08-01, `summary.md` 2026-08-02). No code.

## Objective
Turn the existing single-file plan into a refined multi-step plan, compare it against the reference video (https://youtu.be/rvRyBhILrls — "You Can Learn AI Agent Harness In Real Code In 20 Min" by Sean's AI Stories), and produce a detailed architecture flow doc. Then set up a folder that records each session's thinking so future sessions can pick up where the last one left off.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 07:00 | explore | Read `plan.md`, `problem.md`, `summary.md`. Fetched the full transcript of the reference video (Waku agent harness) to extract its pillars: loop engineering / end-loop guardrails, three-type memory (semantic + episodic + procedural), evals (deterministic + judge), tracing (event trees, tokens, latency), dashboard as ops console, skills as editable markdown. |
| 07:05 | design | Mapped video pillars against the plan. Found 5 gaps: (1) no tracing of agent runs — only an audit log; (2) no loop guardrails despite problem.md calling out "uncontrolled multi-step actions"; (3) evals not a runtime component, no LLM-as-judge; (4) no episodic memory (searchable past sessions); (5) dashboard is admin-only, no ops/memory/skill/trace views. Also found a sequencing bug: memory nodes (wks 3–5) were planned before Casbin (wks 6–8), but memory RBAC depends on Casbin. |
| 07:10 | decision | Asked the user two scope questions. Answers: all four new pillars become CORE (tracing, evals, loop guardrails, episodic memory), and tracing is a custom local SQLite store (offline, deterministic, shares conventions with audit log) — NOT LangFuse/LangSmith. |
| 07:20 | implement | Moved `plan.md`, `summary.md`, `problem.md` into new `ref_docs/` using `git mv` to preserve history. |
| 07:30 | implement | Rewrote `ref_docs/plan.md`: added decisions #16–18 (loop control, tracing, evals), new sections §6 Loop Engineering & Guardrails, §7 Tracing, §8 Evals; added episodic tier to §5; folder/markdown skills in §9; two-face dashboard in §10; refined §11 into an 11-milestone / 16-week multi-step timeline (Casbin pulled earlier to wks 5–6 so memory can depend on it); expanded evidence + risks; kept unchanged sections verbatim. |
| 07:40 | implement | Wrote `ref_docs/proposed_architecture.md`: system context, runtime topology (ports 8000–8006), end-to-end request flow, harness pipeline, Casbin model.conf + policy.csv samples, tool lifecycle (filter → intercept → execute), memory read/write/recall flows, HITL gate, sanitization, loop guardrails, LangGraph graph definition, data models (trace + audit + skill + JWT), DB layout, security model, attack test matrix A1–A8, dashboard views. |
| 07:55 | verify/commit | `git add` + commit `c99627f` ("Restructure docs into ref_docs, refine plan, add proposed architecture"), pushed to `origin/main`. Working tree clean. |
| 08:00 | decision | User asked for a session-history system: a folder where each session writes one md summary, documented with an instructions file so all sessions write the same way. Created `agent_history/` with `INSTRUCTIONS.md` (rules + required structure), `INDEX.md` (session table), and this file. |

## What was done
- `ref_docs/plan.md`: refined into a multi-step, 11-milestone / 16-week plan; added 3 locked decisions; added §6 (loop guardrails), §7 (tracing), §8 (evals); extended §5 (episodic memory), §9 (folder/markdown skills), §10 (ops dashboard), §11 (timeline), §12 (evidence), §13 (risks).
- `ref_docs/proposed_architecture.md`: new — full architecture flow doc (16 sections).
- `ref_docs/summary.md`, `ref_docs/problem.md`: moved unchanged (git history preserved).
- `agent_history/INSTRUCTIONS.md`: new — format spec + rules for session summaries.
- `agent_history/INDEX.md`: new — session index table.
- `agent_history/session_001_docs_and_architecture.md`: this file.

## Decisions locked in this session
- **All four new pillars are CORE scope**: tracing, evals harness (deterministic + judge), loop guardrails, episodic memory.
- **Tracing = custom local SQLite trace store** (append-only, integrity-hashed), not LangFuse/LangSmith. LangFuse/LangSmith remain documented stretch adapters.
- **Casbin pulled earlier** in the timeline (wks 5–6) so memory nodes can depend on it from day one.
- **Skills stored as folder/markdown registry** (`skills/<scope>/<name>/skill.md`), human-editable + hot-reloadable.
- **Docs live in `ref_docs/`**; session history lives in `agent_history/`.

## Verification performed
- `git status` → clean after push.
- `git log --oneline` → `c99627f` HEAD on `main`, pushed to `origin/main` (https://github.com/ParthPujare/fyp.git).

## Blocker / open questions
- None. Project has no code yet — the next session starts the actual build.

## Next step (for the next session)
- Execute **Weeks 1–2 (Milestone 1)**: repo scaffold (`pyproject.toml`, `.env.example`, `README.md`), mock JWT login + verify, LiteLLM provider + `MockLLM`, 4 mock services (FastAPI + seeded SQLite), and define the trace event schema (as per `ref_docs/plan.md` §11 row 1 and `proposed_architecture.md` §12). Deliverable: services run; role-aware hello; tests green offline.

## Handoff notes
- The reference video (Waku agent harness) is the demo/narrative template for the final demo script (Weeks 15–16): memory recall → tool call → skill → deny → HITL → trace + eval review.
- `proposed_architecture.md` is the implementation blueprint — the next session should use §12 (data models), §2 (runtime topology / ports), and §6 (tool lifecycle) when scaffolding.
- The trace schema (defined in milestone 1) should be designed to share event conventions with the audit log (JSONL) — see `proposed_architecture.md` §12.

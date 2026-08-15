# Agent Session History — Instructions

This folder is the **collective memory of past sessions**. Every time a new session works on the project, it should (a) **read** this folder to understand what was thought before, and (b) at the end, **write** a session summary so the next session can pick up seamlessly.

## Rules (all sessions must follow)

1. **One session = one phase of a registered plan.** Plans live in `ref_docs/plans/` and are
   indexed in `ref_docs/plans/INDEX.md`; **Plan Mk1** is the original 16-week plan
   (`ref_docs/plans/plan_mk1_core_harness.md` §11). Every session names its plan + phase (e.g. `mk2 — Phase 1`,
   `Mk1 — milestone 2`). Do not bundle multiple phases into one session. New work that is not a
   phase of an existing plan first needs a plan doc (new `plan_mk<NN>_<slug>.md` + a Registry row).
2. **Read before you act.** At session start, read this file, then `ref_docs/plans/INDEX.md`,
   then `INDEX.md`, then the latest session summary. State what you learned in your first message.
3. **Write after you finish.** Before ending the session, create a new summary file, update
   `INDEX.md`, and update the plan phase status + `ref_docs/plans/INDEX.md`.
4. **Be specific about state.** "Worked on memory" is useless. "Implemented `reflect` node in `app/memory/reflect.py`, wired Casbin write-check, 3 tests passing" is useful.
5. **Preserve the timestamped log.** The "What was thought, when" section is the point of this folder — it lets future sessions reconstruct reasoning, not just results.
6. **Do not delete or rewrite old summaries.** Append-only. If a decision was wrong, add a correction note in the new session's summary instead of editing history.
7. **Keep it concise but complete.** Target ~40–120 lines. Summaries are for orientation, not a transcript dump.

## Naming convention

```
agent_history/session_<NNN>_<short_slug>.md
```

- `<NNN>` — zero-padded, incrementing session number (001, 002, …).
- `<short_slug>` — 2–5 lowercase words describing the focus (e.g. `casbin_rbac_core`).
- Example: `session_003_casbin_rbac_core.md`

## Required structure (fill every section)

```markdown
# Session <NNN> — <Short Title>

## Metadata
- Date: <YYYY-MM-DD>
- Plan / Phase: <plan id + phase, from ref_docs/plans/ (e.g. "mk2 — Phase 1", "Mk1 — milestone 2")>
- Plan step: <weeks + milestone name, from the plan doc's phase table>  <!-- for Mk1: weeks + §11 milestone -->
- Scope: <one-line: what this session is about>
- Status: <in-progress | complete | blocked>
- State at start: <how the project looked when this session began — files, git HEAD, what was done before>

## Objective
<What this session was supposed to deliver (from the plan / previous session's Next Step).>

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| <HH:MM> | <explore/design/implement/test/fix/commit> | <the actual reasoning at that moment> |

## What was done
- <file path>: <what changed and why>
- <file path>: <what changed and why>

## Decisions locked in this session
- <decision 1 — and why>
- <decision 2 — and why>

## Verification performed
- <command / test run and its result>

## Blocker / open questions
- <anything blocking, or a decision deferred to a later session>

## Next step (for the next session)
- <the single next thing to do, mapped to the plan>

## Handoff notes
- <anything the next session MUST know that isn't captured above>
```

## `INDEX.md`

Every session must append one row:

```markdown
| 001 | Mk1 | <slug> | <plan step> | <date> | <one-line summary> | <link> |
```

Columns: `Session | Plan | Focus | Plan step | Date | One-line summary | Link`. Keep the table sorted by session number (newest last).

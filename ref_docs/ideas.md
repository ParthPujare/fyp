# Ideas Log

A running log of external ideas worth borrowing for the harness. Each entry links back to the source and notes how it could map onto our project.

---

## Traces — DeepSeek Harness "Trajectory" View

**Source:** https://youtu.be/qg9EyGOZd9U (DeepSeek Harness demo). Also described in the official docs: "Everything the model sees is recorded in an append-only session log: system prompts, reasoning, tool calls and results, subagent scheduling, and every context injection."

**The idea:**

Instead of only storing structured audit decisions (our current trace store — allow/deny/approve rows), keep a full **append-only event stream of the entire run**, capturing *everything the model sees*, not just what it did:

- system prompts and context injections
- model reasoning / thinking
- tool calls and their results
- subagent scheduling (if any)
- every state snapshot along the way

Then expose a **Trajectory view** that lets you inspect the run *by source* (which part of the system contributed which tokens/events), and lets **resume, fork, search, and replay** all operate on that same event stream.

**Why it fits our project:**

- Our harness already traces every run to a local SQLite event tree (plan_mk1_core_harness.md decision #17, `app/ops/`). This turns that trace store into a first-class *replayable record* of what the agent saw and did — stronger evidence than isolated decision rows.
- The audit log stays the source of truth for *policy decisions* (allow/deny/approve); the trajectory log becomes the source of truth for *what actually happened* in a run.
- Forking a trajectory is a natural fit for our HITL flow: an admin could fork a suspended run to replay it under different policies before approving.
- Search + replay directly supports the plan's evals (decision #18) — deterministic contract checks and LLM-as-judge can replay a stored trajectory offline.

**Mapping onto our stack:**

- Keep the SHA-256 integrity chain from the existing trace store and extend the event schema to include the "model saw" events (prompts, context injections, reasoning, tool I/O), not just tool calls and tokens.
- Group events by source so the viewer can filter (e.g., "show me only what came from memory tiers").
- Add a simple read-only trace/trajectory viewer to the existing admin dashboard (plan_mk1_core_harness.md decision #10) instead of a separate UI.
- Stretch: a `fork`/`replay` action on a stored trajectory for debugging a blocked or failed run.
- LangFuse/LangSmith remain documented stretch adapters; the trajectory format should be close enough to their run-tree model to map later.

**Status (2026-08-15):** **adopted.** Core scope — record everything the model sees (`PROMPT` / `REASONING` / `CONTEXT_INJECTION` events + a per-event `source`) and a by-source trajectory viewer — is folded into `ref_docs/plans/plan_mk1_core_harness.md` §7 and the Wk-12 milestone (decision #17). Stretch scope — FTS5 search / replay / fork — is documented for Wk 15 in plan_mk1_core_harness.md §11. The data model in `ref_docs/proposed_architecture.md` §12 reflects the new event types + `source` field.

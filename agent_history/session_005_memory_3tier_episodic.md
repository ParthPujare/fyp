# Session 005 — 3-Tier + Episodic Memory

## Metadata
- Date: 2026-08-10
- Plan step: Weeks 7–8 — 3-tier + episodic memory: BaseStore namespaces, `load_memory`/`reflect`/`recall`, frozen snapshot, extract/dedup/budget/eviction, HITL for central writes, thread-backed chat (ref_docs/plan.md §11 row 4)
- Scope: Persistent 3-tier memory (central / dept / user) on a custom `SqliteBaseStore`, rule-based fact extraction + editor with per-tier budget, FTS5 episodic recall (scope-gated), frozen snapshot per session, LangGraph checkpointer for multi-turn `/chat`, and HITL (`interrupt()` + SQLite `ApprovalStore`) for central-tier writes.
- Status: complete
- State at start: Milestone 3 done (git HEAD `4899a17`): Casbin RBAC core (model.conf + policy.csv, `filter_tools`, `intercept`, JSONL audit), 16 HTTP tools, bounded LangGraph loop, trace store; 69 offline tests green. `casbin` installed; `langgraph-checkpoint-sqlite` not yet added; memory milestone + all 4 scope decisions from the prior session's prompt (threads, HITL now, dept-tier episodic, custom SqliteBaseStore).

## Objective
Deliverable (plan §11): multi-turn chat with RBAC-scoped recall incl. past-session search. Sub-objectives: (1) 3-tier persistent memory through the same Casbin chain, (2) `load_memory`/`reflect`/`recall` graph nodes, (3) frozen snapshot, (4) extract/dedup/budget/eviction, (5) thread-backed `/chat` via a checkpointer, (6) HITL for central writes (approved by decision: dashboard deferred to wk 9–10).

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 09:00 | explore | Read INDEX/INSTRUCTIONS/session_004 + plan §4/§5/§8/§11 + architecture §7/§8. Plan scope: 3-tier namespaces, load/reflect/recall, snapshot, extract/dedup/budget/eviction, HITL. |
| 09:10 | explore | Checked installed LangGraph for a SQLite `BaseStore` — only `InMemoryStore` ships (non-persistent); no official SQLite BaseStore. Confirmed the 4 scope decisions: thread-backed `/chat` now; interrupt + ApprovalStore now (dashboard later); episodic transcripts in dept tier; custom `SqliteBaseStore`. |
| 09:15 | explore | **keyMatch2 gotcha**: Casbin `keyMatch2` treats `:` as a route-param delimiter — obj `memory:central` matched `memory:dept`. Decision: memory obj names use underscores (`memory_central`, `memory_dept`, `memory_user`, `memory_recall`); human-readable scope strings (`central`, `dept/<name>`, `user/<uid>`) live in audit resources + episodic table. |
| 09:20 | explore | **BaseStore namespace validation** forbids "." in labels — user uid is an email → reversibly base64url-encode the uid into the namespace label (`uid_label`/`sub_from_label`), keep raw uid in the scope string. |
| 09:25 | implement | Deps: `uv add langgraph-checkpoint-sqlite==3.1.1` (brings `aiosqlite`, `sqlite-vec`); `SqliteSaver` import verified. |
| 09:30 | implement | `app/config.py`: `MemorySettings` (central_bytes=8192, dept_bytes=16384, user_bytes=4096, snapshot_max_chars=6000, recall_top_k=5, episodic_scope="dept") + `ServiceSettings.memory_path`/`checkpoint_path`; `.env.example` updated. |
| 09:35 | implement | `app/memory/` package: `scopes.py`, `store.py` (SqliteBaseStore over `langgraph.store.base.BaseStore` — get/put/search/delete/list_namespaces via batch/abatch, JSON values, WAL, namespace validation), `episodic.py` (FTS5 virtual table, bm25 ranking, scope-restricted search), `approvals.py` (pending/approved/rejected rows), `extract.py` (deterministic markers: `MEMORY_MARKER`, `CORRECTION_MARKER`, `CENTRAL_MARKER`, `DEPT_MARKER`; ops add/replace/remove), `edit.py` (MemoryEditor: dedup via normalized match, replace/remove by unique substring, per-tier byte budget with oldest-out eviction), `snapshot.py` (build_snapshot capped by whole lines), `recall.py` (RecallService: readable scopes from Casbin + FTS5 search → ToolResult). |
| 09:45 | implement | `app/harness/enforcer.py`: `memory_allowed(scope, act, claims)` (central read-all/write-admin, self-scoped dept/user, deny unknown) + `memory_recall_allowed`; `policy.csv` memory rows (central read for all roles; dept/user read+write per role; recall `use`). |
| 09:50 | implement | `app/agent/tools.py`: `ToolSpec.handler: str = "http"` + `recall` tool (handler="recall", required_scope="memory_recall:use"). |
| 09:55 | implement | `app/agent/graph.py`: graph = START → load_memory → agent ⇄ execute_tools → finalize → reflect → END. `load_memory` asks Casbin which scopes may be READ, distils frozen snapshot into a system message (traced MEMORY_LOAD per scope), resets per-turn control fields, and skips rebuild on later turns; `execute_tools` routes handler=="recall" to RecallService (intercept still gates); `reflect` appends dept-tier episodic transcript, extracts facts, enforces each write (DENY → audit+skip), suspends central writes via `interrupt()` (approval_id payload), applies editor for allowed dept/user writes, traces MEMORY_WRITE with decision. |
| 10:05 | implement | `app/agent/runner.py`: `run_agent(claims, message, thread_id=None)` with `_build_deps` (defaults via explicit `None` guards), SqliteSaver checkpointer default, `AgentResult` += thread_id/interrupted/approval_id; `resume_agent(approval_id)` reads ApprovalStore entry then `graph.invoke(Command(resume=approved))` on the same thread. |
| 10:10 | implement | `app/gateway/app.py`: `/chat` accepts `thread_id` (default `user:<sub>`); `/approvals/{id}/decide` (404/409 guards, admin) → decide + resume; shared cached `_get_components`; `get_approval_store` dep for testability. |
| 10:15 | fix | **run_id mismatch**: `TraceStore.start_run` minted its own uuid, so the RUN_START event landed under a different run_id than the node events → added optional `run_id` param to `start_run`. |
| 10:20 | test | `tests/test_memory.py` (25 tests): scopes/encoding roundtrip, SqliteBaseStore CRUD/search/filter/list_namespaces, editor (add/dedup/replace/remove/ambiguous/budget-evict), extraction classification + correction→replace, episodic FTS5 scope-gating, recall readable-scope guarantee, Casbin memory matrix, snapshot content/cap, snapshot injection (MEMORY_LOAD traced), recall tool through the graph, thread persistence + snapshot reuse (no reload on turn 2), dept/user writes self-scoped, central HITL approve→commit / reject→no-commit / non-admin deny (no interrupt) / resume requires decision. |
| 10:30 | fix | **Empty-snapshot reuse bug**: `state.get("memory_snapshot")` was falsy for an empty snapshot so turn 2 re-ran load_memory (3 MEMORY_LOAD events). Added explicit `memory_loaded` flag channel; frozen-snapshot reuse now exact. |
| 10:35 | fix | **Budget-eviction test math** (eviction fires on 2nd+3rd adds for a 30-byte cap, not the 3rd). |
| 10:40 | fix | **resume used a different checkpointer** in tests (default SqliteSaver vs run's InMemorySaver) → shared InMemorySaver between run and resume in `_hitl_setup`; gateway test needed `lambda: runner` overrides (FastAPI introspects plain-function overrides as dependencies → 422). |
| 10:45 | fix | Removed an invalid "cross-dept write" test — `classify()` can only produce central / own-dept / own-user scopes, so other-dept writes can't be attempted via extraction; cross-scope denial is covered by the enforcer matrix + non-admin central deny test. |
| 10:50 | verify | Full suite **95 passed** (69 → 95), ruff clean, `scripts/demo_memory.py` works end-to-end (dept write, central DENY, admin HITL approve/reject, recall, thread reuse), trace integrity OK. |
| 10:55 | docs | README (status M4, layout, memory + chat sections, demo-memory, test count 95, next milestones), ref_docs/plan.md (§4 memory ✅, §11 row ✅ session_005), ref_docs/proposed_architecture.md (status banner, §7 impl notes incl. underscore obj names + dept-tier episodic decision). |

## What was done
- `pyproject.toml`: + `langgraph-checkpoint-sqlite` (uv.lock updated).
- `app/memory/` (new): `scopes.py`, `store.py` (SqliteBaseStore), `episodic.py` (FTS5), `approvals.py`, `extract.py`, `edit.py` (MemoryEditor), `snapshot.py`, `recall.py`, `__init__.py`.
- `app/config.py`: + `MemorySettings` + `ServiceSettings.memory_path`/`checkpoint_path`; `.env.example`: + `MEMORY__*`, `SERVICES__MEMORY_PATH`, `SERVICES__CHECKPOINT_PATH`.
- `app/harness/enforcer.py`: + `memory_allowed`, `memory_recall_allowed`; `app/harness/policies/policy.csv`: memory `p` rows.
- `app/agent/tools.py`: `ToolSpec.handler` + `recall` tool (#17).
- `app/agent/graph.py`: rewritten with `load_memory`/`reflect` nodes + `memory_loaded` flag; `execute_tools` routes recall.
- `app/agent/runner.py`: thread-backed `run_agent` + `resume_agent` (Command(resume=...)).
- `app/ops/store.py`: `start_run(..., run_id=None)`.
- `app/gateway/app.py`: thread-backed `/chat` + `/approvals/{id}/decide` (+ `get_approval_store` dep).
- `tests/conftest.py`: `memory_store`/`episodic`/`approvals`/`recall_service` fixtures; `runner_factory` forwards memory injectables with tmp-backed defaults.
- `tests/test_memory.py` (new, 25 tests); `tests/test_policy.py`, `tests/test_tools.py`, `tests/test_chat.py` updated (recall tool visibility, route-map skip, FakeRunner thread_id, approval-endpoint test).
- `scripts/demo_memory.py` (new); `Makefile`: `demo-memory` target.
- Docs: README, `ref_docs/plan.md`, `ref_docs/proposed_architecture.md`.

## Decisions locked in this session
- **Thread-backed `/chat` now** (LangGraph checkpointer; default thread `user:<sub>`); snapshot frozen per thread via `memory_loaded` flag.
- **Custom `SqliteBaseStore`** (subclass of `langgraph.store.base.BaseStore`) — no official SQLite BaseStore exists; persistent via SQLite, namespaced 3 tiers.
- **Episodic transcripts in dept tier** (`dept/<name>`) per user decision; `recall` searches only the caller's readable scopes (Casbin-derived; no cross-scope code path).
- **HITL for central writes now** via `interrupt()` + SQLite `ApprovalStore`; only admin may write central (RBAC + interrupt); dashboard deferred to wk 9–10.
- **Memory Casbin obj names use underscores** (`memory_central` etc.) — `keyMatch2` treats `:` as a route-param delimiter.
- **Self-scoping in the enforcer**: dept scope must equal caller dept, user scope must equal caller uid; unknown subs denied by default.
- **MemoryEdit ops**: dedup by normalized match (replace-in-place), replace/remove by unique substring (ambiguous → `MemoryMatchError`), oldest-out eviction per tier budget returned as `EditOutcome.evicted`.
- **Audit** gains `APPROVED`/`REJECTED` decisions (schema already supported); every memory decision (ALLOW/DENY/APPROVED/REJECTED/skipped) is traced with MEMORY_WRITE.

## Verification performed
- `uv run pytest` → **95 passed** (was 69), fully offline; `uv run ruff check app tests scripts` → clean.
- `uv run python scripts/demo_memory.py` → dept/user facts committed (no interrupt), alice central DENY (no approval row), admin central suspend → approve commits / reject doesn't, recall returns scope-gated FTS5 hits, 2-turn thread shows 0 memory_load events on turn 2 (snapshot reused), trace integrity OK.
- Live smoke: `tests/test_chat.py::test_approval_decision_endpoint` drives `/chat` → suspend → `/approvals/{id}/decide` → resume → committed (200/409/404 paths).

## Blocker / open questions
- None. Notes for later: `build_snapshot` returns `""` when no facts — fine now because `memory_loaded` flag drives reuse, but a future empty-state system message could make it non-empty. Approval `decide` is not itself RBAC-gated yet (endpoint trusts `admin` field) — dashboard milestone (wk 9–10) should authenticate the decider.

## Next step (for the next session)
- Execute **Weeks 9–10 (Milestone 5)**: PII sanitizer + HITL dashboard v1 (approvals, users, depts) — render pending `ApprovalStore` items in a web panel, add PII masking on tool responses + recalled transcripts, and harden `/approvals/{id}/decide` with real admin auth.

## Handoff notes
- Memory injectables (`store`, `episodic`, `approvals`, `recall`, `checkpointer`) are all injectable into `run_agent`/`resume_agent` with explicit `None` guards; `runner_factory` in conftest defaults them to `tmp_path` SQLite (never touch repo `data/` in tests).
- `resume_agent` MUST receive the same checkpointer as the original `run_agent` (thread state lives there); gateway shares one set of components via `_get_components`.
- FastAPI dependency overrides that return plain functions are introspected as sub-dependencies — always override with `lambda: <callable-object-or-closure>`.
- `TraceStore.start_run` accepts an optional `run_id`; runner mints `run_id` first so the RUN_START event chains with node events.
- Trace schema already has `memory_load`/`memory_write` event types; audit `DECISIONS` already includes `APPROVED`/`REJECTED`.
- The `recall` tool is #17 in the registry; `filter_tools`/tests expect ALICE to see 4 crm tools + recall, BOB hr-read/create + recall.

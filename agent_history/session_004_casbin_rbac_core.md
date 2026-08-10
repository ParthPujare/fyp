# Session 004 — Casbin RBAC Core

## Metadata
- Date: 2026-08-10
- Plan step: Weeks 5–6 — Casbin core: model.conf + policy.csv (RBAC + domains), `filter_tools`, `intercept`, JSONL audit log (ref_docs/plan.md §11 row 3)
- Scope: Replace the `scope_allowed` JWT-scope stub with Casbin enforcement; per-role tool-schema filtering; runtime interception; append-only audit log; unauthorized tools physically blocked.
- Status: complete
- State at start: Milestone 2 done (git HEAD `65f3a05`): LangGraph bounded loop + 16-tool HTTP registry/executor + guardrails + trace, wired into `/chat`; 55 offline tests green. Scope-gate stub at `app/agent/enforce.py:scope_allowed` was the deliberate swap point; `casbin` not installed.

## Objective
Deliverable: unauthorized tool physically blocked; per-role schema filtering so only permitted tools enter the LLM context; audit log (allow/deny decisions, matching trace conventions). Replace the stub via `enforcer.enforce(sub, dom, obj, act)`, keeping the `(tool, claims) -> bool` call shape.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 08:00 | explore | Read INDEX/INSTRUCTIONS/session_003 + plan §4/§11 + proposed_architecture §5–§6, §12. Confirmed Milestone 3 = Casbin core; harness lives in `app/harness/`; `enforce.py` is the swap point. |
| 08:10 | decision | Asked 3 scope questions. User locked all recommendations: (1) standard RBAC-with-domains (`crm:read` → obj=`crm`, act=`read`), (2) enforce on user `sub` via `g` rows, (3) deny-by-default, no explicit deny rows. |
| 08:15 | implement | `uv add casbin` → 1.43.0 (pure Python; Python 3.14 fine). |
| 08:20 | implement | `app/harness/policies/model.conf` (RBAC-with-domains, `keyMatch2` on obj, `*` wildcard on act) + `policy.csv` (role-level `p` rows + user→role→dom `g` rows; `hr.delete` deliberately absent). |
| 08:25 | implement | `app/harness/enforcer.py`: `PolicyEnforcer` wraps `casbin.Enforcer`; `enforce(sub,dom,obj,act)`, `tool_allowed(tool,claims)` (parses `required_scope` "dept:action" → obj/act), injectable model/policy paths. Verified 11-check matrix by hand. |
| 08:30 | implement | `app/harness/filter.py` (`filter_tools`), `intercept.py` (`intercept_tool` → denied ToolResult | None, audits each decision), `audit.py` (`AuditLog`, append-only JSONL, schema per §12 incl. APPROVED/REJECTED for later). |
| 08:35 | implement | `app/config.py`: + `ServiceSettings.audit_path` (default `data/audit.jsonl`); `.env.example` + `SERVICES__AUDIT_PATH`. |
| 08:40 | implement | Wiring: `graph.py` `AgentDeps` += enforcer/audit; `execute_tools` uses `intercept_tool` (deny-retry short-circuit preserved); `runner.py` builds tool_schemas via `filter_tools` + accepts enforcer/audit; deleted `app/agent/enforce.py`. |
| 08:45 | implement | `MockLLM.last_tools` records the schemas handed to `generate` (observes filtering in tests). |
| 08:50 | test | `tests/conftest.py` += `enforcer`/`audit_log` fixtures, `make_runner` passes them through; updated `test_tools.py` scope-gate tests → enforcer; wrote `tests/test_policy.py` (matrix, deny-by-default, admin wildcard, filter visibility, intercept, audit determinism/schema, agent integration). |
| 08:55 | fix | **Audit falsiness bug**: `AuditLog.__len__` made empty instances falsy, so `audit = audit or AuditLog(...)` in `run_agent` silently re-created the default (`data/audit.jsonl`) and tests saw empty logs. Switched runner defaults to explicit `if x is None:` guards (also for trace/enforcer). |
| 09:00 | fix | Test expectation: BOB must NOT see `hr_delete_employee` (lacks `hr:delete`) — filter test asserted the wrong set. |
| 09:05 | verify | 69/69 tests green offline, ruff clean, `make demo` shows ALLOW/DENY audit + trace tree, live smoke (auth+gateway) boots and `/chat` responds; no stray `data/` writes from tests. |

## What was done
- `pyproject.toml`: + `casbin` (uv.lock updated).
- `app/harness/` (new): `policies/model.conf`, `policies/policy.csv`, `enforcer.py` (PolicyEnforcer), `filter.py` (filter_tools), `intercept.py` (intercept_tool), `audit.py` (AuditLog), `__init__.py`.
- `app/config.py`: + `ServiceSettings.audit_path`; `.env.example`: + `SERVICES__AUDIT_PATH`.
- `app/agent/graph.py`: `AgentDeps` += `enforcer`, `audit`; `execute_tools` now calls `intercept_tool` (enforce → audit ALLOW/DENY) and preserves the deny-retry short-circuit.
- `app/agent/runner.py`: `run_agent` accepts/creates `enforcer` + `audit`; tool_schemas built from `filter_tools(...)`; explicit `None` guards for injectables.
- `app/agent/enforce.py`: **deleted** (stub replaced by `PolicyEnforcer.tool_allowed`).
- `app/llm/mock.py`: + `last_tools` recording (observes schema filtering).
- `tests/conftest.py`: `enforcer`, `audit_log` fixtures; `make_runner` passes enforcer/audit; `test_tools.py`, `test_agent.py`, `test_chat.py` updated to the new injectables.
- `tests/test_policy.py` (new, 14 tests): policy matrix, deny-by-default, admin wildcard, unknown-user denial, `filter_tools` visibility (incl. schemas reaching the LLM via `last_tools`), `intercept` gate + audit, audit determinism + schema, agent integration (denied/allowed → audit entry).
- `scripts/demo_agent.py`: Casbin + audit scenario (5 sections; audit tail prints ALLOW/DENY per run).
- Docs: `README.md` (status M3, layout, RBAC section, test count 69, next milestones), `ref_docs/plan.md` (§4 harness ✅, §11 row), `ref_docs/proposed_architecture.md` (status banner, §5 model/policy → standard form).

## Decisions locked in this session
- **Standard RBAC-with-domains**: `enforce(sub, dom, obj, act)` where `sub`=user email, `dom`=JWT dept, `obj`=resource namespace, `act`=action; tool scope `crm:read` → obj=`crm`, act=`read`.
- **User-level subject**: `g` rows map user→role→dom in policy.csv (matches plan §3 chain); role change = edit policy, no code.
- **Deny-by-default, no explicit deny rows**: unmatched `(sub,dom,obj,act)` is denied; the policy-matrix test suite enumerates every denial.
- **Admin** = `p, admin, admin, *, *` (own `admin` domain + wildcard obj/act).
- **Audit at decision time**: ALLOW is logged before execution (records what policy decided, not whether execution succeeded); deny-retry repeats are not re-audited (one decision per tool per run).
- **Injectables with explicit `None` checks** in `run_agent` — never `or`-chaining on objects that define `__len__`.

## Verification performed
- `uv run pytest` → **69 passed** (was 55), fully offline.
- `uv run ruff check app tests` → All checks passed.
- `uv run python scripts/demo_agent.py` → alice ALLOW crm_search (audited), alice cross-dept hr_delete DENY, bob deny-storm bounded (8 calls, 7 deny_retry + budget), trace integrity OK, audit tail shows 3 decisions (1 ALLOW + 2 DENY).
- Live smoke (`uvicorn` auth :8000 + gateway :8001): login → `/chat` returns valid reply + run_id; boots clean with new imports; no stray `data/` from tests.

## Blocker / open questions
- None. Notes for later: `AuditLog.__len__` is convenient for tests but makes empty instances falsy — do not use `audit or default` patterns with it. `keyMatch2` on `obj` + `*` on `act` is the wildcard strategy; dom is the caller's own dept. The `scopes` JWT claim is now redundant for tools but stays (memory/pii milestones will key off `memory:`/`pii:` objects in the same policy file).

## Next step (for the next session)
- Execute **Weeks 7–8 (Milestone 4)**: 3-tier + episodic memory — BaseStore namespaces, `load_memory`/`reflect`/`recall` nodes, frozen snapshot, extract/dedup/budget/eviction, memory read/write through the same Casbin chain (`memory:<scope>` objects), and thread-backed `/chat` (checkpointer).

## Handoff notes
- Casbin swap point is `app/harness/enforcer.py:PolicyEnforcer` — `tool_allowed(tool, claims)` keeps the old stub's shape; graph uses `intercept_tool` (audits + returns blocked ToolResult or None).
- Memory milestone will add policy objects like `memory:central` / `memory:dept:<name>` / `memory:user:<uid>` with `read`/`write` acts — just add `p` rows, no enforcer changes.
- Audit log lives at `settings.services.audit_path` (`data/audit.jsonl`); entries are `run_id`-linked with the trace store. HITL (`APPROVED`/`REJECTED`) and sanitize slots are already in the audit schema / intercept pipeline for weeks 9–10.
- Test infra to reuse: `enforcer` + `audit_log` fixtures, `MockLLM.last_tools` for schema-visibility assertions, `tests/conftest.make_runner` now threads enforcer/audit.
- `filter_tools` currently runs in `run_agent` (pre-graph); the architecture also sketches it as a graph entry node — equivalent for now, revisit when memory nodes land (wk 7–8).

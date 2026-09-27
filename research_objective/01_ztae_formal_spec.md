# ZTAE — Zero-Trust Agent Execution: Formal Specification (Option A + B, C, E, F, G as sections)

> Companion to `00_RESEARCH_PLAN.md` (frozen, unedited). Written 2026-09-19 against commit `32c92c9`
> plus the audit in this session. Code references are `file:line` in this repo.
> Numbers in §8 come from `metrics/collect_metrics.py` (re-run to refresh; CSVs in `metrics/results/`).
> Related-work citations in §10 are from memory — verify every one before it goes in a paper.

## 0. The claim in one paragraph

An LLM agent that can call enterprise tools is a *confused deputy* whose "policy" lives in a prompt.
ZTAE moves the policy out of the model and into a small deterministic **reference monitor** wrapped around the
agent loop. We state five invariants (I1–I5), prove that they hold for **every** possible LLM behaviour
(not "with high probability"), give the exact assumptions under which the proofs hold, and measure the
security/latency cost. The model is treated as an adversarial oracle: it may emit any tool call, any argument,
any number of times.

Correction to the plan's wording: `P(execute forbidden t) = 0` should be stated as a **universally
quantified safety property** — *for all* LLM functions `𝓛`, no execution trace contains a forbidden call.
Probabilities only enter when measuring empirical ASR of real models (§8).

## 1. System model

| Symbol | Meaning | Code |
|---|---|---|
| `c = (sub, role, dept)` | claims from a verified JWT; the *only* identity input | `app/auth/jwt.py`, `gateway/app.py:_verified_active` |
| `𝒯` | tool registry: internal HTTP tools + external (Composio) tools | `agent/tools.py`, `integrations/adapter.py` |
| `ρ(t) ∈ {read, write, critical}` | tool risk class | `ToolSpec.risk_level` |
| `𝒮` | skills: named, ordered lists of tool calls | `skills/` |
| `P(sub, dom, obj, act) ∈ {Allow, Deny}` | Casbin RBAC-with-domains, deny-by-default | `harness/enforcer.py`, `policies/` |
| `Σ = {central, dept/d, user/u}` | memory scopes | `memory/scopes.py` |
| `𝓛` | the LLM — **untrusted**, arbitrary function from context to proposed calls | `llm/*` |
| `A` (audit), `Tr` (trace) | hash-chained JSONL / SQLite logs | `harness/audit.py`, `ops/store.py` |
| `Ap` | approval store (pending/approved/rejected) | `memory/approvals.py` |

**Trusted computing base (TCB):** JWT verification, `PolicyEnforcer`, `intercept_tool`, the LangGraph nodes in
`agent/graph.py`, `ApprovalStore`, `AuditLog`, `MemberStore`. **Untrusted:** `𝓛`, tool outputs, memory contents,
dept-service responses, the browser.

### 1.1 Threat model

In scope: prompt injection (direct A2, via tool output B2, via memory B3), cross-department/user data access (A3),
skill/confused-deputy misuse (A4), loop abuse / deny-storms (A5), HITL bypass (A6), log tampering (A7/A8),
external-tool abuse (A9). The adversary can fully control `𝓛`'s output and can post-hoc edit files **only where stated**.

Out of scope (state these in the paper; reviewers will ask): policy *misconfiguration* (we prove the monitor
enforces the policy, not that the policy is right), compromise of the TCB or host, side channels, colluding
admins, and semantic attacks that stay inside an allowed permission (e.g. an *allowed* `gmail_send` with
malicious body — see §9, gap G6).

## 2. Definitions

**Visibility.** `V(c) = { t ∈ 𝒯 : P(c.sub, c.dept, obj(t), act(t)) = Allow }` (`filter_tools`). Only `V(c)` schemas enter the prompt.

**Decision.** `D(t, c) = Allow` iff `P(c.sub, c.dept, obj(t), act(t)) = Allow` evaluated **at call time** (`intercept_tool`).
Every decision appends an audit record `⟨run, resource, actor, decision⟩`.

**Run.** A sequence of steps; step *i* is `𝓛` proposing `π_i ⊆ 𝒯×Args`, followed by the harness processing each proposal.
Bounds `S=10` steps, `B=8` tool calls, `T=120 s` (`GuardrailSettings`).

**Readable scopes.** `R(c) = {central} ∪ {dept/c.dept} ∪ {user/c.sub}` filtered by `P(·, memory_tier, read)`
(`enforcer.memory_allowed` — also *self-scoped*: a `dept/x` scope requires `x = c.dept`, a `user/x` scope requires `x = c.sub`).

**Approval automaton (Option C).** States `RUN → SUSPENDED(a) → {RESUMED-approve, RESUMED-reject} → RUN|TERMINAL`,
with `a` a fresh UUID bound to `(run_id, resource, payload)` (`ApprovalStore.create/_find`).

## 3. Invariants

| ID | Statement | Enforced by |
|---|---|---|
| **I1** No-execute-without-ALLOW | ∀ executed side-effecting call `t` in run `r`: `D(t,c)=Allow` at call time ∧ ∃ audit `ALLOW` for `(r,t)` written **before** execution | `graph.py:_execute_one` (327) → `intercept_tool` |
| **I2** No-cross-scope-read | Every fact/transcript reaching the LLM or a tool result originates from a scope in `R(c)`; PII fields not readable by `c` are masked | `load_memory`, `RecallService.recall`, `sanitize_*` |
| **I3** No-pre-approval-execution | A `critical` tool (or central/dept memory write) executes only after `Ap[a].status = approved` for **that** call, decided by an authorised decider, **at most once** | `_critical_gate` (248), `reflect` (496), `decide` endpoints |
| **I4** Tamper-evident log | Any edit/delete/reorder/truncate of a chain prefix is detected by `verify_integrity` *given an uncompromised head* | `AuditLog`, `TraceStore.verify_integrity` |
| **I5** Bounded execution | Every run terminates; a denied tool is terminal for the run (≤ 1 extra call per denied tool) | `guardrails.py`, `execute_tools`, `should_continue` |

## 4. Theorems and proof sketches

**Theorem 1 (Compositional deny-by-default — I1).**
*Assume* (A1) the TCB is correct; (A2) every side-effecting code path is reachable only through `_execute_one`;
(A3) `P` is deny-by-default and evaluated on verified claims. *Then* for every `𝓛`, every trace satisfies I1.

*Sketch.* (i) The only way `𝓛` influences the world is by returning `ToolCall(name,args)` to `agent_node`; these are
placed in `pending_calls` and consumed only by `execute_tools`, which dispatches each through `_execute_one` (A2).
(ii) `_execute_one` resolves `name` in the registry/adapters/skills; an unknown name returns an error result and
executes nothing. (iii) For a known tool it calls `intercept_tool`, which returns a denied `ToolResult` unless `P` allows
(deny-by-default, A3) and writes the audit record *before* returning `None` (permission to proceed). (iv) Only a `None`
return reaches `executor.execute` / `execute_external` / `recall`. (v) Visibility filtering is **not** needed for
safety — it shrinks the attack surface and saves tokens; the proof rests on (ii)–(iv). ∎

**Corollary 1.1 (non-transitive privilege through skills — Option G).** `SkillExecutor` is constructed with
`run_step=_execute_one`, so each nested call `tᵢ` of skill `S` re-enters the same chain: `Outcome(S)=Allow ⇔
D(use S)=Allow ∧ ∀i D(tᵢ)=Allow`. A skill visible to a role therefore cannot grant that role a tool it lacks (A4).

**Theorem 2 (Scope isolation — I2, Option B).** For every query `q` and claims `c`,
`Leakage(c,q) = |Recall(q,c) ∩ ¬R(c)| / |Recall(q,c)| = 0`. *Sketch:* `RecallService.readable_scopes` builds
the scope list from `memory_allowed` and passes only that list to the store; no code path searches another scope.
The snapshot path in `load_memory` reads only scopes for which `memory_allowed(scope,"read")` is true. PII masking
(`sanitize.py`) is applied on the tool-result and recall paths and to persisted trajectory text. ∎

*Defence in depth (Mk13 Phase 4, shared Postgres).* Once recall moved to a shared database the property could no
longer rest on one `WHERE` clause. Measured on the target database, **Row Level Security is not an available
backstop**: the application's role on Neon (`fyp_owner`) has `BYPASSRLS`, so a policy would appear to protect while
enforcing nothing — deliberately not used. The guarantee is instead the conjunction of independent layers
(`app/memory/pg_episodic.py`, `episodic.py`, `recall.py`): **L1** exact-match, parameterised scope predicate
(`scope = ANY(?)`; never `LIKE`, never case-folded); **L2** fail-closed check of every returned row
(`check_in_scope` raises `RecallIsolationError`, returning nothing rather than a partial result); **L3** an
independent policy re-check of each hit's scope in `RecallService` (a store returning foreign rows still cannot
reach the model), degrading the tool to "unavailable" on an L2 trip; **L4** the query text is normalised to
alphanumeric words and compiled with `phraseto_tsquery`, so no tsquery/FTS5 operator, quote or NUL byte can
reach the index (this also fixed a pre-existing crash: a NUL in a query truncated the SQLite FTS5 string).
The property then holds if *any one* of L1–L3 is correct. Evidence that the tests can detect a leak (mutation
testing): with L1 removed and L2/L3 disabled, 1 190 leaked results over 555 queries; with `=` weakened to a
prefix `LIKE`, `dept/hr` returned `dept/hr2`, `dept/hr/x`, …; with the query normalisation removed and
`to_tsquery` used, operator injection crashes the search — all three caught by `tests/test_pg_phase4.py`.
*Weakness to disclose:* masking is field-name + regex based (email, SSN) — it is not a general DLP, so I2's PII clause
is only as strong as `SENSITIVE_FIELDS` and the regexes.

**Theorem 3 (HITL safety — I3, Option C).** *Claim:* `EXEC(t)` is reachable from `SUSPENDED(a)` only via
`RESUMED-approve(a)`, and each `a` yields at most one `EXEC`. *Sketch:* `_critical_gate` calls LangGraph `interrupt()`
*before* returning `None`; execution proceeds only if the resume value is `True`, which `resume_agent` derives from the
stored approval status (never from request bodies). `ApprovalStore.decide` checks `status='pending'` and then updates
(second decide → `ValueError`) — a check-then-act, **not** an atomic compare-and-set (see G9). Decider identity comes from verified claims; admin-only for tool/central gates,
own-dept head or admin-on-escalation for `dept/*` writes. ∎
*Status:* the counter-examples G1–G3 and the decide race G9 (§9) were fixed on 2026-09-20 and are covered by regression tests (`tests/test_hitl_resume.py`). One residual gap remains: a **skill** whose *nested* step is critical still replays its earlier steps on resume (the whole skill runs inside one node run). State the theorem for direct tool calls and central/dept memory writes, or fix skill execution the same way (one step per node run).

**Theorem 4 (Tamper evidence — I4, Option E).** With `H₀=0⁶⁴`, `Hᵢ = SHA-256(Hᵢ₋₁ ‖ canon(eᵢ))` and head `H_n` stored
out of the attacker's reach, any modification of `e₁..e_n`, any reordering, deletion or truncation changes `H_n`
(collision resistance). *Limitation (measured, §8):* the head marker lives beside the log, so an attacker with
filesystem write access recomputes the chain **and** the head — detection rate 0/50 with the default SHA-256 chain. I4 therefore holds
only against attackers who cannot rewrite the head. *Implemented mitigation (G5):* setting `SERVICES__AUDIT_HMAC_KEY` makes the chain
HMAC-SHA-256; the same attacker, lacking the key, is detected 50/50. Residual: an attacker who also obtains the key or the gateway's
memory, and a key must be set before the first line is written (an existing plain chain will not verify under a key). Note this is a hash **chain**, not a Merkle tree; do not
call it Merkle in the paper.

**Lemma 5 (Termination — I5, Option F).** Each iteration of the `agent ⇄ execute_tools` cycle increments `steps_used`;
`should_continue` ends the run when `steps_used ≥ S`, `tool_calls_used ≥ B` or the monotonic clock exceeds `T`.
`_execute_one` short-circuits any name in `denied_tools` without re-evaluating policy, so a deny-storm costs one policy
evaluation per distinct tool. ∎ *Caveat:* `resume_agent` builds a fresh tracker, so `S`, `B` and `T` restart on every
resume — the bounds are per *invocation*, not per logical run, and time spent `SUSPENDED` is unbounded by design. State
the bound as `(k+1)·(S, B, T)` for a run resumed `k` times, or persist the counters in graph state.

## 5. The algorithm (as implemented — use this, not the pseudo-code in the plan)

```
AgentTurn(token, msg):                                  # gateway → run_agent → LangGraph
  c ← verify_jwt(token); require active_member(c.sub)   # 401/403
  tools ← V(c) ∪ visible_external(c) ∪ visible_skills(c)          # T1 filter (defence in depth)
  snap  ← Snapshot(R(c), ≤ 6000 chars)                            # T2 frozen per thread
  loop while ¬terminal:                                           # I5
    out ← 𝓛(sanitize(history), snap, tools)
    for (t,args) in out.tool_calls:
      if unknown(t):            result ← error;  continue
      if t ∈ denied_tools:      result ← DENY(short-circuit); continue
      if D(t,c) = Deny:         audit(DENY); denied_tools += t; result ← DENY; continue    # I1
      audit(ALLOW)
      if ρ(t)=critical:  a ← approvals.create(run,t,args); v ← interrupt(a)                # I3
                         if ¬v: audit(REJECTED); denied_tools += t; continue
                         audit(APPROVED)
      result ← sanitize( exec(t,args), c )                                                 # I2
      trace(TOOL_CALL …)
  reflect: append episodic transcript to dept scope; extract facts; for each fact f:
      if ¬P(c, tier(f), write): audit(DENY)
      elif tier(f)=central ∨ (tier(f)=dept ∧ ¬is_head(c)): interrupt(approval)             # I3
      else apply(f)
  return reply
```

## 6. Metrics (formal definitions)

| Metric | Definition | Target |
|---|---|---|
| PDC — policy decision correctness | `|{(u,o,a): enforcer = oracle}| / |matrix|`, oracle = allow rows of `policy.csv` ⊕ active members | 1.0 |
| ASR_k — attack success rate, class k | executed-forbidden / attempts, with a worst-case-compliant scripted model | 0 (report 95 % upper bound `1−0.05^{1/N}` ≈ `3/N`) |
| Leakage | Thm 2 formula over randomized (user, query) pairs | 0 |
| Audit-pair completeness | fraction of executed calls with a preceding ALLOW/APPROVED | 1.0 |
| Tamper-detection rate | detected / trials per mutation M1–M5 | 1.0 for M1–M4; M5 reported honestly |
| Audit false-alarm rate | chains reported broken with **no** attacker | 0 (currently 1.0 at ≥ 2 writers) |
| Overhead | p50/p95 ms per `enforce`, `audit.log`, `trace.add_event`; ms/turn vs no-gate | report |
| TaskSuccess | fraction of scripted benign tasks completed identically to the no-gate baseline | ≈ 1.0 |
| TrustScore (opt.) | `w₁(1−ASR)+w₂TaskSuccess+w₃(1−PIIleak)−λ·Overhead` | ablation only |

**Ablation design (Option H).** Four configurations on the same task+attack corpus: `none` (LLM → tools directly),
`filter-only`, `filter+intercept`, `full` (+HITL+sanitize+memory gates). Expected shape: ASR 100 % → ~100 % (filter alone
is bypassable by a compliant model) → 0 → 0, with utility flat and latency growing by the numbers in §8. The
`filter-only` row is the paper's key evidence that *enforcement, not visibility, is what matters*.

## 7. Evidence map (existing tests → claims)

| Attack | Suite (`tests/attack/`) | Tests | Supports |
|---|---|---|---|
| A1 policy matrix | `test_policy_matrix.py` | 9 | PDC, Thm 1 (A3 assumption) |
| A2/B2/B3 injection | `test_prompt_injection.py` | 6 | Thm 1 (model fully complies, still blocked) |
| A3 memory | `test_cross_dept_memory.py` | 4 | Thm 2 |
| A4 skills | `test_skill_misuse.py` | 5 | Cor 1.1 |
| A5 loops | `test_loop_abuse.py` | 5 | Lemma 5 |
| A6 HITL | `test_hitl_bypass.py` | 5 | Thm 3 (happy/negative paths only — see G1–G3) |
| A7/A8 integrity | `test_integrity.py` | 9 | Thm 4 (single writer) |
| A9 external | `test_external_deny.py` | 3 | Thm 1 for Composio tools |

*Caveat on PDC:* the A1 oracle is derived from the same `policy.csv` the enforcer loads, so it validates the
enforcer/materialisation code, not the policy itself. Say so, or add an independently written "intended policy" table.

## 8. Measured results (this machine: Windows 11, Python 3.12; re-run for your paper hardware)

| Metric | Result | Source CSV |
|---|---|---|
| Attack suites A1–A9 | 46 / 46 pass | `attack_results.csv` |
| Tamper detection M1 edit / M2 delete / M3 reorder / M4 truncate | 50/50 each (100 %) | `tamper_detection.csv` |
| Tamper detection M5 (attacker recomputes chain + head), plain SHA-256 chain | **0/50** | `tamper_detection.csv` |
| Same attacker vs HMAC-keyed chain (M5b) | 50/50 | `tamper_detection.csv` |
| Leakage, randomized recall (SQLite FTS5 **and** Postgres tsvector), 459 foreign-canary queries each | 0 leaks on both (95 % UB 0.65 %); non-vacuity: 131/141 own-canary queries found | `memory_leakage.csv` |
| Recall-isolation test suite (`tests/test_pg_phase4.py`) | 555-query randomized property × 5 callers, 45 adversarial query strings, 11 confusable scope names, fail-closed and end-to-end chats — pass on both back-ends; 3/3 injected leak bugs detected | — |
| Audit chain false alarm with concurrent writers (after fix G4) | 0/3 at 1, 2, 4 and 8 writers, no entries lost (was 3/3 broken at ≥ 2 writers) | `audit_concurrency.csv` |
| Latency p50 / p95 (after G4; machine load varies run to run) | `enforce` allow 0.6 / 0.7 ms; deny 1.3 / 1.6 ms; `audit.log` 17 / 31 ms (includes the new cross-process lock); `trace.add_event` 3.8 / 5.5 ms | `latency.csv` |

Reading these honestly: before the 2026-09-20 fixes the security properties held only in the single-writer regime (concurrent writers
corrupted the chain — 3/3 broken at ≥ 2 writers). The lock now serialises writers across threads and processes, at the cost of
serialising all audit appends. `audit.log` still dominates per-decision overhead
(a HITL-gated call writes 2 audit records, ALLOW then APPROVED ⇒ ≈ 80 ms at p50 on this disk; Windows file I/O amplifies
it, but the unbatched read-modify-write of the head file is the structural cause).

## 9. Known gaps found while preparing this spec

**Status 2026-09-20:** G1, G2, G3, G4, G5 (optional HMAC), G8 and G9 are fixed with regression tests in `tests/test_hitl_resume.py`.
Still open: **G6** (semantic attacks inside an allowed permission — out of ZTAE's scope by design), **G7** (discovered Composio tools are not
exposed; deliberately not "fixed" because exposing 20+ tools to a small local model degrades it — decide expose-vs-delete), and the
skill-replay residual noted under Thm 3. The table below records the original findings.

| # | Invariant | Defect | Evidence | Fix |
|---|---|---|---|---|
| G1 | I3 once-only | On resume LangGraph re-runs `execute_tools` from the top, so calls **before** the gated one in the same step run twice | repro: `finance_list_accounts` traced twice for one turn (a write tool would duplicate side-effects) | Process one call per node invocation (loop `execute_tools`→`execute_tools` while `pending_calls`), or persist per-call results in state and skip completed ones |
| G2 | I3 progress | `resume_agent` ignores a second `__interrupt__` (two critical calls in one step, or a later step): reports `interrupted=False`, reply `"no reply produced"`, calls `end_run` while an approval is still pending | repro: 2nd approval left pending, run marked ended | Handle `__interrupt__` in `resume_agent` exactly like `run_agent`; skip `end_run`/evals while suspended |
| G3 | I3 fidelity | Resume drops `key_id`, so the continuation may run on a different model/provider than the user picked | repro: factory saw `key_id=None` on resume | Persist `key_id` with the approval/thread and pass it through `resume_agent` |
| G4 | I1/I4 | `AuditLog.log` is an unlocked read-modify-write of `.head`; gateway (:8001) and dashboard (:8006) both write the same file | §8 concurrency row | Process-wide lock + cross-process file lock (or move audit to SQLite with one transaction per append); write head via temp-file + `os.replace` |
| G5 | I4 | Head marker shares the log's trust domain | M5 0/50 | HMAC the chain with a key held only by the gateway, or periodically anchor the head (signed timestamp / remote witness / append-only store) |
| G6 | I1 (scope of) | RBAC decides *whether* `gmail_send` may run, not *what* it sends. An injected instruction can make an allowed send carry attacker text or data | design limit | Out of scope for ZTAE as stated; mitigate with HITL on outbound content (already `critical`) and, as future work, information-flow labels (cf. CaMeL/FIDES) |
| G7 | I1 (coverage) | Composio *discovered* tools (`spec_from_discovered`) are never exposed: `runner._build_deps` intersects visible names with the 4 hard-coded seeds | code reading (`runner.py:98-107`) | Either expose discovered specs (they then go through the same `_execute_one`) or delete the dead discovery path so the paper does not claim it |
| G9 | I3 once-only | `ApprovalStore.decide` is SELECT-then-UPDATE without `WHERE status='pending'`; two simultaneous deciders (admin + head after escalation) can both pass the check. The HTTP handlers also let the resulting `ValueError` surface as a 500 instead of 409 | code reading (`approvals.py:132-146`, `dashboard/app.py:407-461`) | `UPDATE … WHERE id=? AND status='pending'` and check `rowcount`; map the failure to 409 |
| G8 | reproducibility | `MemoryEditor._enforce_budget` "oldest-out" relies on `updated_at` ordering; ties fall back to random UUID keys → flaky eviction (`test_editor_budget_evicts_oldest` failed once in 2 full runs) | 1 failure / 3 runs | `ORDER BY updated_at, rowid` in `SqliteBaseStore.items_in` |

## 10. Positioning and paper outline

*Novelty framing (say it like this):* not a new access-control model — RBAC + reference monitors are classical
(Anderson 1972; Saltzer & Schroeder 1975; Schneider's enforceable-policy result, 2000). The contribution is
(1) mapping the agent loop onto a reference monitor so that the LLM is a *fully untrusted proposer*; (2) the
composition of scoped memory, HITL automaton, sanitisation and tamper-evident logging into one invariant set with
proofs; (3) a measured attack/utility/latency evaluation including honest failure modes (§8–9).
Compare against: prompt-level guardrails; tool-permission systems for agents (e.g. Progent-style privilege control);
CaMeL-style design-level defences; AgentDojo as the benchmark for the utility side. *(Verify all citations.)*

**Venue.** IEEE TDSC or TIFS (access control + systems evidence). Use TKDE only if you lead with Option B.

**Outline.** 1 Intro & threat model · 2 System model & invariants I1–I5 · 3 Algorithm · 4 Proofs (Thm 1–4, Lemma 5, Cor 1.1)
· 5 Implementation · 6 Evaluation (attack suites, ablation, overhead, concurrency) · 7 Limitations (§1.1, G4–G6)
· 8 Related work · 9 Conclusion.

## 11. Order of work (recommended)

1. ~~Fix G1–G4~~ done 2026-09-20 (tests + re-measured metrics). Remaining: skill-step replay, G7 decision.
2. Add the ablation runner (`none` / `filter-only` / `filter+intercept` / `full`) and a small benign-task corpus for TaskSuccess.
3. G5: HMAC is implemented (optional). Decide whether the paper claims I4 against a file-only attacker (needs the key set) or also wants an external anchor.
4. Freeze the spec, then write §4 of the paper directly from this file.

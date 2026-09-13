# Research Objective — Novel Algo / Formalism Plan

> RULE: `research_objective/` is frozen. No edits except on explicit request.
> Purpose: derive one IEEE-level formal contribution (algorithm + math + metrics)
> from `fyp-agent-harness` (zero-trust gateway: RBAC + intercept, 3-tier memory,
> HITL, PII sanitize, skills, trajectory + evals, attacks A1–A8).

## 1. What counts as novelty for IEEE

Not "built a chatbot". Must be one of:
1. New enforceable model + safety invariant + proof sketch
2. New measurable tradeoff (security vs utility vs latency) with formula
3. New attack taxonomy + defense with ASR → 0 evidence (your A1–A8)

You already have all three raw materials. Need to formalize one.

## 2. Candidate formal contributions (all angles)

### Option A — ZTAE: Zero-Trust Agent Execution (RECOMMENDED, strongest)
Formalize your core pipeline as a reference monitor.

```
T_vis(r) = filter_tools(r, Policy)          # LLM never sees forbidden schemas
D(t,r)   = intercept(t,r) ∈ {ALLOW,DENY,SUSPEND}  # runtime re-check
Invariant: Execute(t) ⟺ ∃ audit e: e.decision=ALLOW ∧ e.run_id=run(t)
```

Paper theorem: *Compositional Deny-by-Default* —
even if LLM is fully adversarial/compliant with attacker (your A2/A3/A4 threat
model), `P(execute forbidden t) = 0` up to policy-correctness.
Proof sketch: (1) schema hiding + (2) runtime intercept + (3) audit pairing check.
Novel vs ReAct/LangChain: enforcement in code, not prompt.

Metrics to store here: Policy Decision Correctness (=1.0 on A1 matrix),
Attack Success Rate A2/A3/A4 (=0), audit-pair completeness (=1.0).
File: `01_ztae_model.md` (future).

### Option B — 3-Tier Scoped Memory with Budgeted Snapshot (strong, systems angle)
Formalize memory admission + recall as access-controlled IR.

```
Write(s,f): ALLOW iff can_write(role(s), tier(f))  # central→admin+HITL only
Snapshot(u) = Top-k( readable_scopes(u) ∩ store ) s.t. Σ tokens ≤ B
Recall(q,u) = FTS5(q) ∩ readable_scopes(u)         # then sanitize()
```

Novelty: cross-dept leakage = 0 by construction; bounded context (B) with
oldest-out eviction; episodic recall gated, not global vector search.
Formula to publish: `Leakage(u,q) = |Recall ∩ ¬readable| / |Recall| = 0`,
plus Precision@k / Recall@k vs no-gate baseline (ablation).

### Option C — HITL as Safety Automaton (good for S&P / trustworthy-AI venue)
Model critical tools + central writes as suspend-resume state machine:

```
S ∈ {RUN, SUSPENDED(id), RESUMED(decision), TERMINAL}
critical(t) → SUSPEND; execute ⟺ decision=APPROVED by admin ∧ fresh id ∧ once-only
```

Invariant: no pre-approval execution; reject-never-executes; no double-decide.
Publish transition diagram + A6 bypass-ASR=0 table. Small but rigorous.

### Option D — Scope-Aware PII Declassification Lattice
```
sanitize(x, role): salary/ssn visible iff role=admin else MASK; email always visible
Trajectory store: persist only sanitize(x, role)  # nothing leaked to disk/LLM
```

Novelty: field lattice + regex/text dual path + summary regeneration.
Metric: PII Leakage Rate (=0 for non-admin on tests), utility preserved
(structured task success unchanged). Good as § of A, weak alone.

### Option E — Hash-Chained Trajectory + Audit (integrity angle)
```
H_0 = 0^64; H_i = SHA256(H_{i-1} || canonical(e_i)); verify ⟺ recompute chain
EVAL_det = trace_integrity ∧ completeness ∧ rbac_contract ∧ audit_pairing
```

Publish tamper-detection rate (=1.0 on edit/delete/reorder/truncate, A7/A8)
+ overhead (ms/event, bytes/event). Good supporting theorem, weak alone.

### Option F — Bounded Execution / Anti-Loop Guardrails (theory angle)
```
Terminate ⟺ steps≤10 ∧ tool_calls≤8 ∧ t_run≤120s ∧ deny→terminal (no retry)
```

Prove termination + bounded blast radius (deny-storm cost ≤ 1 extra call).
Metric: max steps/calls/time on A5 loop-abuse suite. Nice lemma for A.

### Option G — Skill-as-Tool with Nested Re-Enforcement
```
Skill S = (schema_S, [t1..tn]); use(S) checked, then EACH ti re-intercepted
Outcome(S)=ALLOW ⟺ use(S)=ALLOW ∧ ∀i D(ti)=ALLOW
```

Novelty: privilege is not transitive through skill abstraction (blocks
confused-deputy). A4 evidence: allowed skill + denied nested tool.
Best as corollary of A.

### Option H — Empirical Security-Utility-Latency Tradeoff (evaluation paper)
Define composite score:
```
TrustScore = w1*(1-ASR) + w2*TaskSuccess + w3*(1-PIIleak) - λ*LatencyOverhead
```
Publish ablation: no-gate vs filter-only vs full (filter+intercept+HITL+sanitize).
IEEE likes this less than a model, but it makes any of A–C publishable.

## 3. Comparison

| Option | Novelty | Proof-ability | Evidence ready (tests/) | Paper fit |
|---|---|---|---|---|
| A ZTAE | ★★★ | ★★★ (invariant) | A1,A2,A3,A4,A6 + audit | TDSC / TIFS / Access control |
| B Memory | ★★☆ | ★★☆ (leak=0) | memory, recall, cross-dept | TKDE / IR |
| C HITL | ★★☆ | ★★★ (automaton) | A6 | Trustworthy AI |
| D PII | ★☆☆ | ★★☆ | sanitize | supporting § |
| E Chain | ★★☆ | ★★★ | A7/A8 | supporting § |
| F Bounds | ★☆☆ | ★★★ | A5 | lemma |
| G Skills | ★★☆ | ★★☆ | A4 | corollary of A |

## 4. Recommended path

**Main claim = A (ZTAE reference monitor), with B + E + A6 as theorems/sections.**
Single unified algorithm to publish:

```
function AgentTurn(u, msg):
  tools ← filter_tools(role(u))          # (T1) least-privilege visibility
  snap  ← Snapshot(u) bounded by B       # (T2) scoped memory
  loop ≤ max_steps, budget ≤ tool_budget:
    out ← LLM(tools, snap, sanitize(history, role))
    for t in out.tool_calls:
      d ← intercept(t, role(u))          # (T3) runtime gate
      audit(d); if d=DENY: terminate-run
      if d=SUSPEND: await admin_decide() # (T4) HITL automaton
      if d=ALLOW: res ← sanitize(exec(t), role)
      trace(PROMPT, REASONING, TOOL_IO, EVAL)
  return reply
```

Invariants: I1 no-execute-without-ALLOW, I2 no-cross-scope-read, I3 no-pre-approval-exec, I4 tamper-evident.

## 5. Tests & metrics to store here (future, on request only)

```
research_objective/
  00_RESEARCH_PLAN.md (this file)
  metrics/  # ASR, PDC, leakage, latency, TaskSuccess, TrustScore csvs
  tests/    # minimal repro scripts distilled from tests/attack/*
  figs/     # automaton diagram, pipeline diagram, tradeoff curves
```

Do NOT duplicate `tests/` now. When asked, distill: A1 matrix oracle,
A2/A3 injection ASR harness, A4 nested-skill case, A5 bound check,
A6 HITL-bypass check, A7/A8 tamper check, latency microbench.

## 6. Suggested next step (needs explicit ask)

Pick 1: (A) full ZTAE formal spec, (B) memory formalism, (H) metric harness.
Then freeze scope to one IEEE venue (TDSC/TIFS vs TKDE vs Trustworthy-AI).

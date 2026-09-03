# Algorithmic and Mathematical Foundations for Research Presentation: The Zero-Trust Enterprise Agent Harness

## 1. Introduction & Research Positioning

Deploying a single, multi-tenant corporate AI assistant across heterogeneous departments (Marketing, HR, DevOps, Finance, Admin) presents a fundamental security paradigm conflict: **probabilistic language models cannot serve as the authoritative guardians of deterministic access control policies.** 

Traditional agent frameworks rely on *soft guidance* (system prompt instructions such as *"do not let non-admin users delete records"*), which are non-deterministic, unenforceable, and trivially bypassed via prompt injection, role-play jailbreaks, and tool-output poisoning. Furthermore, agents typically operate on blanket credentials, un-gated multi-step loops, and unbounded shared memory stores.

This work introduces a **Zero-Trust Enterprise Agent Harness**, formalizing the interaction between an untrusted language model and enterprise resources as a **Guarded Deterministic Finite-State Transducer with Cryptographic Provenance**. This document outlines the rigorous mathematical formulations, algorithms, and security invariants designed for publication in top-tier security and AI systems conferences (e.g., IEEE S&P, ACM CCS, NeurIPS, USENIX Security).

---

## 2. Formal System Model & Definitions

### 2.1 Identity and Authorization Context
Let $\mathcal{U}$ be the set of authenticated users. Every incoming request is bound to a cryptographically verified JSON Web Token (JWT) yielding an authentication context vector:
$$\mathcal{C}_{\text{auth}} = (\text{sub}, \text{role}, \text{dom}, \text{scopes})$$
where:
- $\text{sub} \in \mathcal{U}$ is the unique user identifier (e.g., `alice@acme.com`).
- $\text{role} \in \mathcal{R}$ is the assigned role (e.g., `marketing_agent`, `admin`).
- $\text{dom} \in \mathcal{D}$ is the departmental domain context (e.g., `marketing`, `hr`, `finance`, `devops`, `admin`).
- $\text{scopes} \in \mathcal{S}$ is the set of granted functional permission scopes (e.g., `crm:read`, `crm:create`).

### 2.2 The Policy Oracle
Let $\Pi$ be a policy decision point implemented via Casbin RBAC with domains. The policy oracle is a deterministic binary function:
$$P: \mathcal{U} \times \mathcal{D} \times \mathcal{O} \times \mathcal{A} \to \{\text{Allow}, \text{Deny}\}$$
mapping a subject, domain, target object namespace ($\mathcal{O}$), and action ($\mathcal{A}$) to a strict boolean decision under a **deny-by-default** axiom:
$$\forall (u, d, o, a) \notin \text{Rules}, \quad P(u, d, o, a) = \text{Deny}$$

---

## 3. Core Algorithmic Contributions

### 3.1 Algorithm 1: The Guarded Execution Transducer (Separation of Proposal and Enforcement)

In standard agent architectures, the LLM directly invokes execution primitives. In our harness, the agent runtime is modeled as a state transition system where the LLM acts exclusively as an *untrusted proposal generator*, and the harness acts as a *deterministic enforcement transducer*.

* **State at step $t$:** $S_t = (\text{Context}_t, \text{History}_t, \mathcal{C}_{\text{auth}})$
* **Proposal generation:** $\mathcal{A}_t = \text{LLM}(S_t)$ where $\mathcal{A}_t = \{a_1, a_2, \dots, a_k\}$ is a set of proposed tool calls $a_i = (\text{tool}_i, \text{args}_i)$.

**Transition Function $\delta(S_t, a)$: Lease-to-Execute Protocol**
For each proposed action $a$ targeting resource object $\text{res}(a)$ and action $\text{act}(a)$:

$$\delta(S_t, a) = \begin{cases} 
\text{Sanitizer}(\text{Execute}(a), \mathcal{C}_{\text{auth}}) & \text{if } P(\text{sub}, \text{dom}, \text{res}(a), \text{act}(a)) = \text{Allow} \land \text{Risk}(a) \neq \text{Critical} \\[6pt]
\text{Suspend}(\text{ApprovalStore}, S_t, a) & \text{if } P(\text{sub}, \text{dom}, \text{res}(a), \text{act}(a)) = \text{Allow} \land \text{Risk}(a) = \text{Critical} \\[6pt]
\text{PermissionError} & \text{if } P(\text{sub}, \text{dom}, \text{res}(a), \text{act}(a)) = \text{Deny}
\end{cases}$$

#### Research Significance
This guarantees physical non-bypassability: no prompt injection, semantic jailbreak, or model hallucination can execute an unauthorized side-effect because the code path to service endpoints is physically unreachable without a valid authorization token and positive policy evaluation.

---

### 3.2 Algorithm 2: The Deny-Storm Short-Circuit (Loop-Abuse Prevention)

Prompt-injected or malfunctioning models frequently engage in "deny-storms" — repeatedly attempting to invoke unauthorized tools in a recursive loop, exhausting computational steps and token budgets.

* **Definition:** Let $\mathcal{D}_r = \{a_1, a_2, \dots\}$ be the cumulative set of explicitly denied tool calls within run $r$.
* **Short-Circuit Predicate $R$:** For any new proposed action $a \in \mathcal{A}_t$:

$$R(a, \mathcal{D}_r) = \begin{cases} 
\text{ShortCircuit}(\text{"Blocked earlier this run; route around"}), & \text{if } a \in \mathcal{D}_r \\[6pt]
\text{EvaluatePolicy}(a), & \text{otherwise}
\end{cases}$$

#### Research Significance
By maintaining an ephemeral run-scoped denial memory $\mathcal{D}_r$, the harness eliminates redundant policy evaluation cycles, prevents compute denial-of-service (DoS) attacks, and forces the LLM's reasoning engine to dynamically backtrack and explore alternative valid solution paths.

---

### 3.3 Algorithm 3: Scope-Hierarchical Memory Lattice & Isolation Algebra

Memory in enterprise assistants must be partitioned to prevent cross-departmental and cross-user data leakage. We model memory as a partially ordered set (poset) across three distinct security tiers: $\mathcal{M} = \{\text{Central}, \text{Dept}_d, \text{User}_u\}$.

* **Read-Path Lattice Projection:** For an actor with context $\mathcal{C}_{\text{auth}}$, the accessible memory subspace $M_{\text{accessible}}$ injected into context window $C_m$ is bounded by the meet/join of permissions:
  $$M_{\text{accessible}} = \bigcup_{s \in \mathcal{M}} \Big[ \mathbb{I}\left( P(\text{sub}, \text{dom}, \text{obj}(s), \text{read}) = \text{Allow} \right) \cdot \text{Store}(s) \Big]$$
  where $\text{obj}(\text{Central}) = \text{memory\_central}$, $\text{obj}(\text{Dept}_d) = \text{memory\_dept}$, and $\text{obj}(\text{User}_u) = \text{memory\_user}$.

* **Write-Path Gating:** Any extracted fact $f$ destined for tier $s$ requires:
  $$P(\text{sub}, \text{dom}, \text{obj}(s), \text{write}) = \text{Allow} \quad \land \quad (s \neq \text{Central} \lor \text{AdminApproval} == \text{True})$$

#### Research Significance
This mathematical formulation enforces strict least-privilege memory isolation. A Marketing user's context window can *never* ingest HR episodic transcripts or central company policies without explicit administrative privilege, neutralizing memory-poisoning attack vectors.

---

### 3.4 Algorithm 4: Cryptographic Hash-Chained Provenance (Merkle-Style Integrity)

To provide non-repudiation and withstand post-compromise log tampering by an attacker with file-system access, all audit decisions and trajectory events are cryptographically chained.

* **Event Hashing Function:** For event $E_i$ with payload $W_i$, timestamp $\tau_i$, and sequence index $i$:
  $$H_i = \text{SHA-256}\left( H_{i-1} \parallel W_i \parallel \tau_i \parallel \text{id}_i \right)$$
* **Integrity Verification:** A verification function $\text{Verify}(\mathcal{E})$ checks the sidecar head pointer $H_{\text{head}}$ against recomputed hashes:
  $$\forall k \in [1, n], \quad H_k \stackrel{?}{=} \text{SHA-256}(H_{k-1} \parallel W_k \parallel \dots)$$
  Any unauthorized modification, deletion, reordering, or truncation of historical event $E_k$ immediately breaks the verification chain ($H_k \neq H'_k$), exposing forensic tampering.

---

## 4. Summary Table for Research Presentation

| Aspect | Conventional Agentic Systems | Our Zero-Trust Harness Approach | Algorithmic / Math Basis |
|---|---|---|---|
| **Access Control** | Prompt instructions ("soft rules") | Deterministic Casbin RBAC engine | Binary Oracle $P(u, d, o, a)$ with Deny-by-Default |
| **Execution Path** | Direct LLM tool dispatch | Guarded Deterministic Transducer | Separation of Proposal ($\mathcal{A}_t$) and Enforcement ($\delta$) |
| **Loop Abuse** | Unbounded retries / token burn | Ephemeral denial cache | Short-Circuit Predicate $R(a, \mathcal{D}_r)$ |
| **Memory Access** | Blanket vector search (all documents) | 3-tier lattice projection | Poset Meet/Join filtering $M_{\text{accessible}}$ |
| **Audit & Provenance** | Plaintext JSON / SQLite rows | Append-only SHA-256 hash chains | Merkle-style cryptographic chaining $H_i$ |

---

## 5. Mapping to Automated Attack Verification Suites (`tests/attack/`)

The theoretical guarantees above are backed by 204 automated integration and attack tests:
1. **A1 (Policy Matrix):** Validates that non-admin cross-department calls yield hard `DENY` (`test_policy_matrix.py`).
2. **A2 (Prompt Injection):** Verifies that injected messages (`"delete all HR records"`) fail even when the model is scripted to fully comply (`test_prompt_injection.py`).
3. **A3 (Memory Poisoning):** Proves that poisoned memory/recall cannot cross departmental boundaries (`test_cross_dept_memory.py`).
4. **A4 (Skill Misuse):** Ensures nested tool calls inside skills are re-checked by `intercept` (`test_skill_misuse.py`).
5. **A5 (Deny-Storms):** Confirms guardrail termination and short-circuiting under aggressive tool retry loops (`test_loop_abuse.py`).
6. **A6 (HITL Bypass):** Enforces that critical actions (`risk=critical`) suspend execution and require admin JWT authorization (`test_hitl_bypass.py`).
7. **A7 & A8 (Tamper Evidence):** Verifies hash-chain integrity detection across audit and trace logs (`test_integrity.py`).

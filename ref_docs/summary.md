# Agent Harness RBAC — Project Summary

## The Idea

An enterprise deploys a **single central AI personal assistant** that serves everyone in the company — Marketing, HR, DevOps, Finance, and admins — from one place. As users interact with it, the assistant dynamically adapts to the role of the person signed in: a Marketing user sees and can do Marketing things; a DevOps user sees and can do DevOps things. One assistant, many roles, no separate agents per team.

At the heart of the project is a **zero-trust harness** — a security boundary between the AI model and the real-world systems it touches. Nothing (a tool, a piece of memory, a skill) is ever trusted to the model's own judgment. Every action is checked against a defined policy before it is allowed to happen, and every decision leaves an auditable record.

## Why It Matters

Today's AI assistants are secured the wrong way. Access control is usually written as instructions in the system prompt ("do not let non-admin users delete records") — which is non-deterministic, unenforceable, and trivially overridden by prompt injection. A user can simply talk the model into doing something it was told not to do. Agents also tend to run on blanket credentials, so there is no layer enforcing that a Marketing user cannot touch HR or DevOps systems, and multi-step actions can execute without anyone approving them. On top of that, an agent's memory — what it remembers across conversations — can leak knowledge across users and departments with no check on who is allowed to see or write it.

Security that depends on a model "remembering" to obey is not security. Access must be enforced in code and policy, never in a prompt.

## The Unique Contributions

1. **Deterministic enforcement over probabilistic judgment.** Access decisions are made by an explicit policy engine and enforced at runtime — a blocked action is physically denied with a permission error, regardless of what the model decides or how the user manipulates it. Same input always produces the same decision.

2. **A single assistant with role-bound power.** The assistant's capability is derived from the authenticated user's corporate role (from their identity claims), not from what the model happens to be willing to do. Dynamic, per-role behavior without deploying separate agents.

3. **Policy-controlled memory across three tiers.** Memory is a first-class resource with its own access rules: a central (company-wide) tier, per-department tiers, and a user-private tier. A Marketing user can never read HR's or another user's memory, and can never write to the central tier without admin approval. This memory scoping is one of the distinctive ideas of the project.

4. **Human-in-the-loop gates for high-risk actions.** High-impact operations (large financial actions, changes to company-wide memory, new skills) don't just run — they suspend and wait for an actual human administrator to approve or reject them.

5. **Sensitive data is permission-aware.** Even a permitted tool's response is masked before reaching the model, so the user only ever sees data their role is allowed to see.

6. **Evidence by design.** Every allow, deny, and approval decision is logged as structured audit evidence — ready to serve as the foundation for compliance and observability.

## What This Means in Practice

A Marketing user asks the assistant for CRM data: it works. The same user, or a prompt-injection attempt, tries to delete an HR record or trigger a DevOps change: the harness blocks it at runtime and logs the denial. A finance transfer is requested: it suspends until an admin approves it on a dashboard. The assistant remembers a user's preferences across sessions in their private tier, and the company's shared policies in the central tier — but only the people allowed to see and change those things can.

## Current Status (2026-08-10)

**Milestone 1 (weeks 1–2) is complete** — the foundation is running and tested offline:

- Repo scaffold at the repo root (uv, `pyproject.toml`, `.env.example`, `Makefile`, `scripts/run_dev.sh`).
- **Mock JWT auth** (`app/auth/`, :8000): 5 seeded roles (marketing/hr/finance/devops/admin), HS256 issue + verify, `POST /auth/login`, `GET /auth/me`.
- **Pluggable LLM** (`app/llm/`): provider protocol, deterministic `MockLLM` (default — offline CI), `LiteLLMProvider` (Gemini/OpenAI/Claude), factory.
- **Trace store** (`app/ops/`): append-only SQLite with SHA-256 per-run integrity chain; tamper/truncation detection.
- **4 dept mock services** (`app/services/`, :8002–8005): crm/hr/devops/finance, FastAPI + seeded SQLite (hr includes salary/ssn PII for the later sanitizer).
- **Gateway shell** (`app/gateway/`, :8001): role-aware `POST /hello` — the harness entry point that becomes the LangGraph gateway.
- **32 offline tests green** (`uv run pytest`), `ruff` clean.

**Next milestone (weeks 11–12):** skills registry + skills editor, then the **trajectory viewer + evals harness** — the trace store records *everything the model saw* (system prompt, context injections, reasoning, tool I/O) with a by-source, admin-only viewer and a trace-completeness eval (plan.md §7, §11). Full plan: `ref_docs/plan.md` §11; build log: `agent_history/`.

## Out of Scope

The project deliberately does not build an agent framework from scratch, does not train or fine-tune models (it uses off-the-shelf models), and does not implement enterprise identity infrastructure (identity is simplified). The focus is the harness — the enforcement layer — and proving, with deterministic tests, that it holds under attack.

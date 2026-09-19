# Todo / Backlog

Known improvements to implement when the time comes. Not yet scheduled — tracked here so they don't get lost.

Status review: 2026-09-13 (session 022) — items 2, 4, 7, 8 DONE; item 3 PARTIAL (LLM-first extraction live, keyword fallback retained); rest OPEN.

---

## 1. Restrict dual-session login to admins — OPEN

The header (where "Harness Demo" is written) currently always offers two session slots via the identity switcher pills (`admin` / `member`).

- If the **first login is an admin**, show the option to add a second **member** session (current behavior).
- If the **first login is NOT an admin** (a member signs in first), there should be **no option to add another session** at all.

Points of interest: `app/dashboard/static/index.html` (header `switcher`, `renderSwitcher`, `openLogin`) and the session logic backing it.

---

## 2. Add a "Department Head" role with dept-preference approval — DONE (Mk12, session 022)

Identity is currently just `admin` + department members. Add a **dept head** role:

- A dept head belongs to a department and must **approve dept-tier (`dept/<name>`) preference writes**, similar to how admin approves central-tier writes via HITL today.
- This means dept-scoped memory writes should funnel through an approvals queue / HITL gate owned by that department's head, not auto-commit immediately.

This interacts with the memory write path (`app/agent/graph.py` `reflect`, central-write HITL at line ~445) and the approvals model (`app/memory/approvals.py`), plus Casbin policy + member roles.

> Status 2026-09-13: implemented — `{dept}_head` roles, dept-HITL gate in `reflect`, `GET/POST /api/dept/approvals/*`, admin escalation, head UI view. See `ref_docs/plans/plan_mk12_dept_head.md`.

---

## 3. Auto-learn user facts with LLM as the base (Hermes-style) — PARTIAL (Mk4/Mk8 in progress)

The product vision: a **personal assistant for company users where sharing memory boosts its ability** — like Hermes.

- Use an **LLM as the base for learning facts about users** from normal usage/conversation, replacing/augmenting the current keyword-rule extraction in `app/memory/extract.py`.
- Extract durable user facts (preferences, role/context, recurring tasks, constraints) and propose them as memory writes through the existing tier + approval path (central/dept/personal).
- Users should **not** have to explicitly add preferences, although they keep the **ability to do so manually** if they want to.

> Status 2026-09-13: partial — LLM-first relevance extraction live (`app/memory/llm_extract.py` via `graph.py:_relevant_facts`, local providers pinned to heuristics); keyword rules in `app/memory/extract.py` retained as fallback, not yet replaced.

---

## 4. Make Skills useful — connect to real tools + bring your own LLM — DONE (Mk9 + Mk3/Mk5/Mk10/Mk11)

The Skills section is currently dry/useless as a demo surface. To get real value out of it:

- The harness should be able to **connect to external tools and use them** (something like **Composio**).
- Real tool use requires a real LLM, so **each user must connect their own provider** via an API key or equivalent in preference to a shared demo provider.

Skills therefore become the abstraction layer over connected tools rather than canned markdown scripts (`app/skills/`).

> Status 2026-09-13: done — hybrid Composio + direct tool layer (Mk9, live Gmail verified session 021), per-user BYOK keys (Mk3/Mk5/Mk10/Mk11); skills execute through the same enforced tool chain.

---

## 5. Auto-generate user-linked skill files (Hermes-style) — OPEN

Like Hermes, the assistant should be able to **write its own `skills.md` files based on the tasks it performs** — procedures the assistant figures out once and then reuses.

- Skills are then **linked to the individual user** who generated/owns them (per-user registry, not global).
- **Bonus:** add the ability to **share skills** (user-to-user or team-wide) as an add-on.

---

## 6. Ask clarifying questions instead of assuming — OPEN

For ambiguous tasks, ask clarifying questions rather than assuming intent — similar to opencode's behavior.

---

## 7. Connect external tools (direct + via Composio) for real tool-calling — DONE (Mk9, session 021 live Gmail)

Make tool-calling actually useful so this becomes a real harness, not just chat + memory:

- Support connecting to various tools **directly** plus through an aggregator like **Composio** (or equivalent platform).
- Connected tools become callable by the agent (LangGraph tool nodes), scoped per-user via their own credentials/keys.
- Skills (`app/skills/`) become the abstraction layer over connected tools rather than canned markdown scripts.

> Status 2026-09-13: done — see item 4 (Mk9).

---

## 8. Switch model for the active key via the chat interface — DONE (Mk10/Mk11: chat key/model picker)

- Allow the user to **switch the model used with their active API key** from within the chat UI (e.g. model picker/dropdown in the dashboard chat surface).
- Applies per-session/user without re-entering the key; gateway rebuilds the per-user provider (LiteLLM) for the selected model.
- Fall back to the default/mock provider behavior when no key is set (preserves offline demo).

> Status 2026-09-13: done — chat key/model picker (`chat-model-select`, per-key models) + Account pick-or-type model list with local-model Detect; stale-key fallback keeps mock behavior (session 022).

---

## 9. Self-edit codebase via the chat UI (lowest priority) — OPEN

- Allow fixes/updates to the codebase to be made **through the chat UI** (agent edits code, user reviews/applies).
- Lowest priority — treat as a stretch goal after tool connectivity (7) and model switching (8) are solid.

---

## 10. External DB provisioning with request-access flow — OPEN

If the agent doesn't have access to an external DB/source, it should request access on the user's behalf, then auto-connect once granted:

- e.g. new marketing employee wants to research old campaigns but lacks access → asks the agent → agent files an access request to the data owner/admin.
- On approval, the harness provisions the connection (credentials, scoped read access) and the agent can then query/answer from that DB.
- Needs: per-source access policy, pending-request state + notifications, audit trail, and least-privilege / revocable grants.

## 11. After creating an ID provide some sets of feature so that for each approval is not required. 
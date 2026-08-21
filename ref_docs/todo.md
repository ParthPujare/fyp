# Todo / Backlog

Known improvements to implement when the time comes. Not yet scheduled — tracked here so they don't get lost.

---

## 1. Restrict dual-session login to admins

The header (where "Harness Demo" is written) currently always offers two session slots via the identity switcher pills (`admin` / `member`).

- If the **first login is an admin**, show the option to add a second **member** session (current behavior).
- If the **first login is NOT an admin** (a member signs in first), there should be **no option to add another session** at all.

Points of interest: `app/dashboard/static/index.html` (header `switcher`, `renderSwitcher`, `openLogin`) and the session logic backing it.

---

## 2. Add a "Department Head" role with dept-preference approval

Identity is currently just `admin` + department members. Add a **dept head** role:

- A dept head belongs to a department and must **approve dept-tier (`dept/<name>`) preference writes**, similar to how admin approves central-tier writes via HITL today.
- This means dept-scoped memory writes should funnel through an approvals queue / HITL gate owned by that department's head, not auto-commit immediately.

This interacts with the memory write path (`app/agent/graph.py` `reflect`, central-write HITL at line ~445) and the approvals model (`app/memory/approvals.py`), plus Casbin policy + member roles.

---

## 3. Auto-learn preferences (Hermes-style) instead of explicit adds

The product vision: a **personal assistant for company users where sharing memory boosts its ability** — like Hermes.

- Preferences should be **auto-learned** from normal usage/conversation (the current keyword-rule extraction in `app/memory/extract.py` is a primitive first step; continue expanding toward genuine learned behavior).
- Users should **not** have to explicitly add preferences, although they keep the **ability to do so manually** if they want to.

---

## 4. Make Skills useful — connect to real tools + bring your own LLM

The Skills section is currently dry/useless as a demo surface. To get real value out of it:

- The harness should be able to **connect to external tools and use them** (something like **Composio**).
- Real tool use requires a real LLM, so **each user must connect their own provider** via an API key or equivalent in preference to a shared demo provider.

Skills therefore become the abstraction layer over connected tools rather than canned markdown scripts (`app/skills/`).

---

## 5. Auto-generate user-linked skill files (Hermes-style)

Like Hermes, the assistant should be able to **write its own `skills.md` files based on the tasks it performs** — procedures the assistant figures out once and then reuses.

- Skills are then **linked to the individual user** who generated/owns them (per-user registry, not global).
- **Bonus:** add the ability to **share skills** (user-to-user or team-wide) as an add-on.

---

## 6. Ask clarifying questions instead of assuming

For ambiguous tasks, ask clarifying questions rather than assuming intent — similar to opencode's behavior.

---

## 7. API Key Integration (BYOK — Bring Your Own Key)

Core functionality to enable real LLM chat for all users (like ChatGPT):

- Each user provides their own LLM API key (OpenAI, Anthropic, Gemini, etc.) via the dashboard
- Keys are stored encrypted per-user in the MemberStore (`data/members.sqlite`)
- Gateway uses the user's key for their chat requests (LiteLLM provider per-user)
- Fallback to mock provider if no key is set (preserves offline demo)
- Admin can view key status (set/unset) but not the key itself
- Keys never leave the server; only used server-side for LLM calls

Points of interest:
- `app/auth/store.py` — add `api_key_encrypted` column to members table
- `app/llm/factory.py` — per-user LLMProvider creation from stored key
- `app/gateway/app.py` — `/chat` uses caller's provider
- `app/dashboard/static/index.html` — key management UI in member settings
- `app/config.py` — encryption settings (Fernet from `cryptography`)
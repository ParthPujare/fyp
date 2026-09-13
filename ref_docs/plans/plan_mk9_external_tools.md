# Plan Mk9 — Real Tool-Calling Harness (Hybrid Composio + Direct, Gmail + GitHub MVP)

- **Plan id:** `mk9`
- **Scope:** Turn the chatbot into a real harness: connect external apps per-user (BYO Composio) and call them through the existing zero-trust pipeline (filter → intercept → HITL → sanitize → audit → trace). MVP apps: Gmail + GitHub. Skills become the abstraction over connected tools.
- **Status:** Phases 0–6 DONE 2026-09-13 (333 offline tests green; `make demo-external`).
- **Registry:** `ref_docs/plans/INDEX.md`
- **Predecessors:** Mk1 (harness), Mk2 (dashboard/members), Mk3/Mk5 (BYOK + direct providers), Mk4/Mk8 (chat)
- **Backlog sources:** `ref_docs/todo.md` #4 (skills over real tools + own LLM) and #7 (direct + Composio real tool-calling). Does **not** cover todo #5 (auto-generated per-user skills), #9 (self-edit via chat), #10 (external DB request-access flow) — noted as follow-ups.

---

## 0. Decisions locked (2026-09-13 Q&A)

| Question | Answer |
|---|---|
| Integration approach | **Hybrid** — Composio for OAuth apps (Gmail/Outlook/GitHub/Canva) + direct REST for custom/internal APIs |
| Launch scope | **Gmail + GitHub MVP** (one email + one dev tool, covers read+write) |
| Credentials | **Per-user BYO Composio**: each user connects their own Composio account/project key; apps are linked inside Composio. Harness stores only `entityId=sub` + connected-account mapping, never raw OAuth tokens |
| Enforcement | **Full harness** — Casbin filter+intercept, critical-write HITL, PII sanitize, audit+trace, same as internal tools |
| First usable slice | **Connect + chat-use** — Account-tab Connect buttons + chat can call tools with approval flow |

Implication: useful calling requires a real LLM (Mk3/Mk5 BYOK). MockLLM stays for offline CI via a `FakeComposioClient`.

---

## 1. Goals / non-goals

Goals:

1. Per-member `Connect Gmail / Connect GitHub` in dashboard, backed by their own Composio linkage.
2. Connected tools callable from `/chat` as normal LangGraph tool calls, gated by Casbin + HITL + sanitize + audit + trace.
3. `Skills/` can wrap connected tools (e.g. `send_campaign_email` → `gmail_send`, `triage_github_issue` → `github_create_issue`).
4. Fully offline test path (`uv run pytest`, `make demo-external`) with no network/key.

Non-goals (MVP): Outlook/Canva (phase 8), per-user auto-generated skills (todo #5), share-skills, DB request-access flow (todo #10), global shared mailbox, background sync/webhooks inbound.

---

## 2. Architecture

No harness fork. External tools enter as `ToolSpec` and reuse the existing chain.

```
Chat (/chat) → runner._build_deps() → filter_tools(internal + external visible)
  → graph agent_node (LLM w/ tool_schemas)
  → graph execute_tools._execute_one()
      → intercept_tool (Casbin) → critical HITL gate → executor → sanitize → trace+audit
```

New module:

```
app/integrations/
  __init__.py
  config.py    # ComposioSettings: api_key, base_url, timeout, entity_id_prefix
  vault.py     # Fernet helpers reusing app/auth/crypto.py; per-user BYO key encrypt/decrypt
  store.py     # members.sqlite extension: connected_accounts(username, app, account_id, status, updated_at)
  client.py    # ComposioClient wrapper (lazy `import composio`), FakeComposioClient for tests/demo
  adapter.py   # allow-listed Composio actions -> ToolSpec(handler="external")
  direct.py    # generic REST connector with per-user header injection (custom APIs)
  service.py   # orchestration: list_visible_tools(claims) / execute(claims, name, args)
```

Conventions to follow:

- `ToolSpec` shape in `app/agent/tools.py:22` (`name/dept/description/http_method/path_template/args_schema/required_scope/risk_level/handler/param_aliases`). External tools set `handler="external"`, `dept` = `gmail|github`, `http_method="EXTERNAL"`, `path_template=""`.
- `tool_schema()` in `app/agent/tools.py:276` reused unchanged for LLM context.
- `PolicyEnforcer.tool_allowed()` in `app/harness/enforcer.py:88` splits `required_scope` on `:` → `(obj, act)`, so `gmail:read` maps to Casbin `(obj=gmail, act=read)` with zero enforcer changes.
- `SkillExecutor` in `app/agent/graph.py:374` already routes nested calls through `_execute_one()` — external nested calls inherit intercept/HITL automatically.

---

## 3. Credential & connection model

- Global fallback: `COMPOSIO__API_KEY` in `.env` (project key, server-side only).
- Per-user override (preferred): Account tab field `Composio API key` → Fernet-encrypted at rest (`LLM__FERNET_KEY` pattern from Mk3), never returned by any API (metadata/status only).
- Resolution: per-user key > global key > unconfigured (external tools invisible, offline-safe).
- `entityId`: `claims["sub"]` verbatim (e.g. `alice@acme.com`). One Composio entity per member; connected accounts inside Composio per app.
- `connected_accounts` table caches `{username, app, account_id, status: pending|active|revoked, updated_at}`. Source of truth for "connected" remains Composio `connectedAccounts/list`; the table is a fast filter + audit aid.
- OAuth flow: `POST /api/integrations/connect {app}` → server creates `connected_accounts(pending)` + returns Composio `redirectUrl` → user links app in Composio → `GET /api/integrations/status` polls to `active` (webhook optional, phase 8). `POST /api/integrations/disconnect {app}` marks revoked + calls Composio delete.

---

## 4. MVP tool allow-list

| ToolSpec.name | Composio action (indicative, confirm in spike) | Scope | Risk | Notes |
|---|---|---|---|---|
| `gmail_fetch` | `GMAIL_FETCH_EMAILS` | `gmail:read` | `read` | args `{query, max_results}`; sanitize body |
| `gmail_send` | `GMAIL_SEND_EMAIL` | `gmail:send` | `critical` | args `{to, subject, body}`; HITL + PII mask `to/body` in Approvals list |
| `github_list_repos` | `GITHUB_LIST_REPOS` | `github:read` | `read` | args `{query?}` |
| `github_create_issue` | `GITHUB_CREATE_ISSUE` | `github:create` | `write` (HITL if repo is prod — phase 5 toggle) | args `{repo, title, body}` |

Cap MVP at these four. New apps/actions require allow-list edit + policy row + test (deny-by-default otherwise).

Args schemas are strict JSON-Schema (`type/object/properties/required`) so `intercept` arg validation and LLM function-calling both hold.

---

## 5. Policy matrix (Casbin)

Append to `app/harness/policies/policy.csv` (underscore objs; `:` is a `keyMatch2` delimiter — see existing `memory_*`/`pii_*` note):

```csv
# ---- External tools (Mk9) ----
p, marketing_agent, marketing, gmail, read
p, marketing_agent, marketing, gmail, send
p, devops_agent, devops, github, read
p, devops_agent, devops, github, create
p, marketing_agent, marketing, skill_send_campaign_email, use
p, devops_agent, devops, skill_triage_github_issue, use
```

Notes:

- Unknown/disabled member → no `g` row → deny everywhere (Mk2 Phase 2 invariant preserved).
- Cross-dept example stays denied: `alice(marketing) → github:create` has no row → `filter_tools` hides schema + `intercept_tool` DENYs + audit `DENY`.
- Admin wildcard `p, admin, admin, *, *` covers external automatically.
- Future Outlook/Canva follow the same one-row-per-(role,app,act) pattern; department registration (`POST /api/departments`) is unaffected.

---

## 6. Execution path changes (minimal diff)

1. `app/agent/tools.py` — add `handler="external"` support (no shape change); document `http_method="EXTERNAL"` convention.
2. `app/agent/graph.py:_execute_one()` (~line 322) — branch: `if tool.handler == "external": result = sanitize_tool_result(external_service.execute(...))`. Keep `_trace_call()` + `_outcome()` unchanged so `TOOL_CALL` events carry `sanitized` flag.
3. `app/agent/runner.py:_build_deps()` (~line 92) — `tool_schemas = [internal filtered] + [external visible+connected filtered]`; `SkillRegistry` visible list unchanged. `resume_agent()` re-resolves external deps from `entry["actor"]`.
4. `app/harness/sanitize.py` — extend field-name masking to `to/subject/body/title` + email/SSN regex on fetched bodies (same `MASK` contract).
5. `app/gateway/app.py` — no new route; `/chat` and `/approvals/{id}/decide` already suspend/resume critical tools via `interrupt()`.
6. `app/dashboard/app.py` — new member-gated routes (see §7); admin routes unchanged.

Guardrails (`max_steps/tool_budget/call_timeout/run_timeout`) apply to external calls identically; deny-retry short-circuit in `graph.py:338` covers external RBAC storms.

---

## 7. API + UI

Dashboard (member JWT; reuse BYOK Account-tab pattern):

- `POST /api/integrations/key {api_key}` — save per-user BYO Composio key (encrypted, metadata-only response).
- `DELETE /api/integrations/key` — remove override (falls back to global).
- `POST /api/integrations/connect {app: gmail|github}` — → `{redirectUrl, account_id}`.
- `GET /api/integrations/status` — → `[{app, status, updated_at}]` (no key material).
- `POST /api/integrations/disconnect {app}` — revoke.
- Existing `POST /api/chat`, `GET /api/memory/me`, Approvals list (mask `to/body/title` in pending view, full args only post-approve in trace detail).

UI (Account tab + Chat tab):

- Composio key input + `Gmail [Connect] [Disconnect]`, `GitHub [Connect] [Disconnect]` pills with active-dot.
- Chat renders external `tool_calls` outcomes (`allowed/denied/timeout`) already; add app icon prefix only.
- Unconnected user: tools invisible, chat replies normally (mock fallback preserved).

---

## 8. Skills bridge (todo #4)

New seed skills (same markdown/YAML front-matter, `app/skills/registry.py` unchanged):

- `skills/dept/marketing/send_campaign_email/skill.md` — `tool_calls: [gmail_send]`, `owner_role: marketing_agent`.
- `skills/central/triage_github_issue/skill.md` — `tool_calls: [github_list_repos, github_create_issue]`, visible per §5 rows; nested `github_create_issue` by marketing stays DENIED (A4 analogue).

Hot-reload invariant (edit file → next run changes) preserved.

---

## 9. Implementation phases

| Phase | Deliverable | Files touched | Acceptance |
|---|---|---|---|
| 0 | **Spike + pin SDK.** Confirm action IDs, entity scoping, `redirectUrl` flow; pin `composio` version | `pyproject.toml`, spike notes in session log | Action IDs + auth flow documented; version pinned or `httpx`-fallback chosen |
| 1 | **Config + vault + store.** `ComposioSettings`, `connected_accounts` migration, Fernet BYO key, `GET /status` | `app/integrations/config.py, vault.py, store.py`, `app/config.py`, `.env.example`, `tests/test_integrations_store.py` | Per-user key round-trip encrypted; status endpoint offline-safe; tests green |
| 2 | **Client + adapter + executor.** Lazy `composio` import, `FakeComposioClient`, 4-tool allow-list → `ToolSpec`, `ExternalExecutor` w/ timeout | `app/integrations/client.py, adapter.py, direct.py, service.py`, `tests/test_integrations_adapter.py` | `tool_schema()` valid; fake fetch/send/list/create deterministic |
| 3 | **Harness wiring.** `_execute_one` external branch, `_build_deps` merge, sanitize extension, policy rows | `app/agent/graph.py, runner.py`, `app/harness/policies/policy.csv`, `app/harness/sanitize.py`, `tests/test_integrations_policy.py` | `alice gmail_fetch` allowed; `alice github_create` DENY+audit; `gmail_send` suspends for HITL |
| 4 | **Dashboard connect + chat-use.** Key/connect/status/disconnect routes + Account-tab UI | `app/dashboard/app.py`, `app/dashboard/static/index.html`, `tests/test_integrations_api.py` | Click-only connect (fake redirect in tests); chat calls external tool; approvals mask PII |
| 5 | **Skills bridge.** 2 seed skills + policy rows + nested-deny test | `skills/.../skill.md`, `tests/test_integrations_skills.py` | Marketing skill sends via HITL; nested forbidden tool DENIED inside allowed skill |
| 6 | **Evals + hardening + demo.** RBAC-contract eval covers external objs; A9-style attack test; `make demo-external` | `app/evals/*`, `tests/attack/test_external_deny.py`, `scripts/demo_external.py`, `Makefile` | `uv run pytest` offline green; demo shows allowed fetch + cross-dept DENY + HITL send |
| 7 | Docs + rollout | `README.md`, `.env.example`, this plan | README connect flow + fallback documented |

Each phase = one session per repo rule (`agent_history/` one phase per session; INDEX.md `Plan` column = `mk9-p<N>`).

---

## 10. Test strategy (all offline)

- Unit: vault encrypt/decrypt, adapter schema validity, `FakeComposioClient` determinism.
- Policy: matrix oracle extension (role × `gmail|github` × act), unknown/disabled deny, cross-dept deny.
- Agent: scripted MockLLM → external call allowed/denied/HITL-suspend → approve/reject resume (mirror `tests/test_agent*.py` pattern with injected fake service).
- API: member JWT gates (401/403), connect polling, disconnect revocation, key metadata-only (no raw key leak).
- Attack (new `A9`): tool-output poisoning from fake Gmail body → sanitized + denied exfil; forged approval → 403; replayed `approval_id` → 409.
- Trace integrity: external `TOOL_CALL` events chain-verified (`audit_integrity`/`trace_integrity` evals unchanged).

---

## 11. Risks / mitigations

- Composio SDK drift → pin version; isolate all SDK calls in `client.py` so fallback to raw `httpx` is one-file change.
- OAuth token handling → never store tokens; store only `account_id` + status; all secrets server-side.
- PII in email bodies → sanitize before LLM + before trace persist (same `_sanitize_trajectory` path as prompts).
- Cost/latency → `call_timeout_seconds` + `tool_budget` already bound; add per-app `max_results` cap in adapter.
- Mk5 divergence (`plan_mk5_custom_byot.md` vs `plan_mk5_local_byok.md`) → Mk9 assumes per-member key vault (Mk3/Mk5-custom); if local-BYOK wins, per-user override becomes no-op and global key is sole path.

---

## 12. Follow-ups (out of scope)

- Outlook + Canva onboarding (same adapter + 2 policy rows each).
- Todo #5 per-user auto-generated `skills.md` + sharing.
- Todo #10 external-DB request-access flow (pending-request state + provisioning).
- Inbound webhooks (new email → notify) + scheduled sync; direct-connector mTLS/OAuth2-client-credentials.

# Browser validation run — 2026-10-10

Automated run of `ref_docs/test.md` in **headed Chrome** (Playwright), one browser profile per person (admin, head, member), plus raw-API calls for the negative rows.

## How it was run

- **Isolated copy of the app**, not the shared Neon database: copied code + policy + skills, local SQLite, auth on port 8010 (port 8000 was in use on this PC). Nothing was written to the team database or to the repo's `policy.csv` / `skills/`.
- **Scripted local model** (`harness/fake_llm.py`) registered as an LM Studio key, so chat messages like `TOOL finance_transfer {...}` make the model call exactly that tool through the real provider path. No real LLM was used.
- Second pass with `APPROVALS__GATE_WRITE_TOOLS=true` after a full restart of all services.
- Re-run: `bash harness/start.sh <repo>` then `python harness/part1.py <repo>` … `part5b.py` (needs `pip install playwright requests`).

## Result: 206 of 212 checks passed

## Findings (most serious first)

| # | Severity | Where | What happens | Status |
|---|---|---|---|---|
| 1 | Major | Ops tab | The run list did not render at all in the browser ("Cannot read properties of null"): each row was built from a `<tr>` string inside a `<div>`, which browsers discard. | **Fixed** in `app/dashboard/static/index.html` (`appendRunRow`), re-tested: 123 runs listed, trajectories open |
| 2 | Major (live only) | Memory extraction | When the shared "platform memory" model is configured, `_relevant_facts` keeps **only user-scope facts** from it. Dept and central facts are then never proposed from chat, so MEM-02/03 and every memory-based approval row cannot trigger. Not hit in this run (platform model unconfigured here) — read from `app/agent/graph.py`. | Open — decide if intended |
| 3 | Minor | APPR-10 | An unconnected critical external tool (`gmail_send`) suspends for approval first; head and admin both approve, and only then the run fails with "gmail is not connected". `connect_required` is empty on the suspended turn. | Open |
| 4 | Minor | AUTH-11 | Only `*_head` roles are checked against the department. `marketing_agent` + dept `finance` is accepted; that member then matches no policy row and is denied everything. | Open |
| 5 | Minor | CHAT-08 / APPR-21 / DEST-02 | After deleting a chat with a pending approval, the approval is auto-rejected and decide is 409 (correct), but the owner's resume returns **404 "claims"** instead of a clear 409 (local SQLite mode). | Open |
| 6 | Minor | SKL-10 | A skill scope with `..` is blocked (nothing written outside `skills/`) but the API answers **500** instead of 422. | Open |
| 7 | Minor | AUTH-10 | Usernames are not format-checked (`not-an-email,com` accepted). | Open |
| 8 | Minor | MEMB-08 | An empty memory fact and a 200 KB fact are both accepted by `POST /api/memory/...`. | Open |
| 9 | UX | CHAT-06 | After a page reload the chat list is there but the transcript is empty until a chat is clicked. | Open |
| 10 | UX | UX-11 | At ~380 px width the page overflows sideways (usable, but needs horizontal scroll). | Open |


## Rows in test.md that disagree with the code (behaviour is correct, wording is not)

- **AUTH-09**: an admin can never deactivate *themselves* (409 by design); a second admin can deactivate the first (200).
- **APPR-06**: `hr_delete_employee` by an HR member is denied by RBAC (no `hr:delete` in the policy), so it never reaches approval. RBAC-05 says the same.
- **RBAC-18**: a non-admin central write is denied outright, not suspended.
- **MBR-03 / RBAC-20**: after an admin moves a member to another department, the old permissions stop immediately; the new department's permissions need a fresh login (the token still carries the old department).
- **AUTH-18 note** ("edit form defaults role to admin"): not reproduced here — bob's form opened with `hr_agent` / `hr`. It may be a timing issue on the slower shared database; worth a retest there.


## Not covered by this run

- Anything needing 3 PCs or the shared Neon database: SET-01/02/03/08, CHAT-09/10/14, all of §17 (DB) and §18 (Docker).
- Real providers and Composio: BYOK-04/05/11/12/15/16/17, all of §4 (INT), OPS-05 (judge evals), SKL-06, RBAC-13 gmail/github part.
- Not automated: AUTH-15 (identity pills, being removed), MEM-12/13, REC-04/05/07, PII-03, OPS-08, DEST-03..06, UX-02/08.


## All checks

IDs with a letter suffix (e.g. `APPR-05b`) are extra checks added around that row.

| ID | Status | What was observed |
|---|---|---|
| SET-04 | Pass | member -> Chat, admin -> Approvals (head landing checked in HEAD-01) |
| SET-05 | Pass | extra test users created (molly via UI; heads/admin2 via API) |
| SET-06 | Pass | own key added (scripted local model stands in for a real provider); keyless members stay on Mock |
| SET-07 | Pass | {'status': 'ok', 'service': 'dashboard'} |
| AUTH-01 | Pass | alice lands on Chat tabs=['Chat', 'Account', 'Memory', 'Log out']; admin lands on Approvals tabs=['Chat', 'Account', 'Approvals', 'Members', 'Departments', 'Always allowed', 'Skills', 'Memory', 'Ops', 'Log out'] |
| AUTH-02 | Pass | alerts=['invalid credentials', 'invalid credentials', 'invalid credentials']; still on login view (first=[]) |
| AUTH-03 | Pass | tampered token -> 401 {"detail":"invalid token"} |
| AUTH-04 | Pass | gateway 401 {"detail":"token expired"}; dashboard 401 |
| AUTH-05 | Pass | UI signed out=True; old token -> {'dash /api/chat': 403, 'dash /api/memory/me': 403, 'gw /hello': 403, 'gw /chat': 403, 'gw /chat/threads': 403} |
| AUTH-06 | Pass | fresh login while deactivated -> 401 |
| AUTH-07 | Pass | after reactivate login -> 200 |
| AUTH-08 | Pass | self + last-admin deactivate -> 409 an admin cannot deactivate themselves |
| AUTH-09 | Pass | with 2 admins: admin2 deactivates admin1 -> 200 (allowed). An admin deactivating THEMSELVES is always 409 by design (self-lockout guard) — this is what the tester hit; the row's wording is ambiguous, behaviour is correct |
| AUTH-10 | Pass | short pw/duplicate/empty -> [422, 409, 422]. NOTE: username format is not validated ('not-an-email,com' -> 200) |
| AUTH-11 | Pass | head-role mismatch (marketing_head + hr) -> 422 (expected 422). BUT a non-head mismatch (marketing_agent + finance) is ACCEPTED -> 200: only *_head roles are validated against the dept (this is what the tester saw). Such a member matches no policy row, so every tool/memory action is denied. |
| AUTH-11b | **Fail** | GAP: member role is not validated against its department for non-head roles (POST/PUT /api/members) |
| AUTH-12 | Pass | head adding admin/hr_head/marketing_head/hr_agent -> [403, 403, 403, 403] |
| AUTH-13 | Pass | head patching other head/admin/self/cross-dept -> {'mhead2@acme.com': 403, 'admin@acme.com': 403, 'mhead@acme.com': 403, 'bob@acme.com': 403} |
| AUTH-14 | Pass | /auth/me with OLD token -> 200 {"sub": "bob@acme.com", "role": "hr_agent", "dept": "hr", "scopes": ["hr:read"]} |
| AUTH-16 | Pass | member -> [403, 403, 403, 403]; no token -> [401, 401, 401, 401] |
| AUTH-17 | Pass | head on admin-only endpoints -> [403, 403, 403, 403] |
| AUTH-18 | Pass | reset: old pw 401, new pw 200 |
| AUTH-18b | Pass | Edit form for bob prefilled role='hr_agent' dept='hr' (expected hr_agent / hr) |
| AUTH-19 | Pass | -> 422 |
| AUTH-20 | Pass | login hint: Mock login for any active member, e.g. alice@acme.com / alice123 (marketing) or  |
| CHAT-01 | Pass | brand-new user threads -> ['user:fresh@acme.com'] |
| CHAT-02 | Pass | reply='echo: [PCA-test] hello from molly'; sidebar titles=['[PCA-test] hello from molly', '[PCA] Our team prefers Figma for mockups', '[PCA-test] hello from molly', 'TOOL crm_search_customer {"query": "acme', 'TOOL crm_search_customer {"query": "acme', 'TOOL hr_list_leave_requests {}', 'TOOL hr_list_leave_requests {}'] |
| CHAT-03 | Pass | old thread history loaded and the follow-up continued in it |
| CHAT-04 | Pass | Shift+Enter kept a newline ('line one\nline two'); Enter sent the message |
| CHAT-05 | Pass | suspended turn renders: 'retros. ⏹ run suspended (approval 8d4517ab-524a-42f3-acc1-d0876ccefae7) — your department head must approve it. This chat updates by itself once it is decided. Resume now' |
| CHAT-06 | Pass | after reload 6 chats listed and history reopens. NOTE (UX): the transcript is EMPTY right after reload until a chat is clicked |
| CHAT-07 | Pass | threads 6 -> 5 (stays gone after refresh); Ops runs kept (12 -> 12) |
| CHAT-08 | **Fail** | pending approval auto-rejected on chat delete; decide/resume afterwards are 409 (the 304 the tester saw is just the browser cache for the static page) |
| CHAT-11 | Pass | bob reading / deleting / gateway-reading / polling molly's thread -> [403, 403, 403, 403] |
| CHAT-12 | Pass | same browser, molly -> bob: sidebar only shows bob's threads ['user:bob@acme.com']; bob's message did not land in molly's thread; bob now has 1 |
| CHAT-13 | Pass | '_' is not treated as a wildcard: a_b -> ['user:a_b@x.com:trap']; aXb -> ['user:aXb@x.com:trap'] |
| CHAT-15 | Pass | rapid double-send -> HTTP [200, 200]; thread has 2 messages, no crash |
| CHAT-16 | Pass | a turn killed mid-flight is lost by design; a suspended turn survives (above) |
| CHAT-17 | Pass | run suspended BEFORE the restart; after all services restarted it was decided (head, admin) and resumed; fact saved |
| CHAT-18 | Pass | run 1fd9b200 trajectory event types: ['context_injection', 'eval', 'memory_load', 'memory_write', 'node', 'prompt', 'reasoning', 'run_end', 'run_start', 'tool_call'] |
| BYOK-01 | Pass | key saved through the Account tab and shown Active |
| BYOK-02 | Pass | second key saved -> active ['second-model']; activate first -> active ['fake-tool-model'] (exactly one active) |
| BYOK-03 | Pass | per-message key pick and default both reach the provider (both answered by the configured endpoint, not Mock) |
| BYOK-06 | Pass | custom_openai without base_url -> 422 |
| BYOK-07 | Pass | unsupported provider -> 422 |
| BYOK-08 | Pass | empty model -> 422; empty key -> 422 |
| BYOK-09 | Pass | rename onto an existing (provider, model) -> 400 LLM key configuration for provider 'lmstudio' and model 'fake-tool-mod; still 2 rows |
| BYOK-10 | Pass | unknown key id DELETE/PATCH -> [404, 404] |
| BYOK-13 | Pass | Detect on a stopped server -> 502 in 3.3s (no hang) |
| BYOK-14 | Pass | Detect against a running local server -> 'Found 1 loaded model(s).' |
| BYOK-18 | Pass | member reading another member's keys -> 403; key material never returned |
| BYOK-19 | Pass | admin sees metadata only: [('lmstudio', 'fake-tool-model', True), ('lmstudio', 'second-model', False)] |
| BYOK-20 | Pass | provider unreachable -> 502 'LLM provider error: APIConnectionError - Connection error. [provider=lmstudio, model=m] (u' (no key in the message) |
| MEM-01 | Pass | user-tier fact saved with no approval; user scope = ['[PCA] Alice prefers Slack over Teams.'] |
| MEM-02 | Pass | head approved ('approved — waiting on requester to resume'); alice's chat auto-resumed=True; dept/marketing = ['[PCA] Our team policy requires weekly retros.', 'Campaigns ship on Tuesdays', '[PCA] Our team prefers Figma for mockups.', '[PCA] Our team prefers async design reviews.'] |
| MEM-03 | Pass | central write suspended even for an admin; after approve central = ['[PCA] Company policy requires annual vendor audits.', '[PCA] Company policy requires quarterly vendor audits.', 'Fiscal year starts in April'] |
| MEM-04 | Pass | admin added facts to central (UI) and user/admin (API) |
| MEM-05 | Pass | molly (2nd marketing member) scopes=['central', 'dept/marketing', 'user/molly@acme.com']; sees alice's dept fact and central |
| MEM-06 | Pass | bob scopes=['central', 'dept/hr', 'user/bob@acme.com']; recall of marketing phrase -> 'TOOL RESULT: No recalled conversations matching 'weekly retros' in your permitted scopes.' |
| MEM-07 | Pass | hr fact approved by hr head; dept/hr=['[PCA] Our team rota uses PagerDuty.']; invisible to marketing |
| MEM-08 | Pass | {'alice@acme.com': ['central', 'dept/marketing', 'user/alice@acme.com'], 'bob@acme.com': ['central', 'dept/hr', 'user/bob@acme.com'], 'mhead@acme.com': ['central', 'dept/marketing', 'user/mhead@acme.com'], 'admin@acme.com': ['central', 'dept/admin', 'user/admin@acme.com']} |
| MEM-09 | Pass | head's own dept fact committed with no suspension |
| MEM-10 | Pass | thread started before the approval keeps its frozen snapshot (no re-injection: True); a brand-new thread's snapshot contains the approved dept fact: True |
| MEM-11 | Pass | correction replaced the old fact (tea facts=0, coffee facts=1); user scope=['[PCA] Actually Alice prefers coffee in the morning instead.', '[PCA] Alice prefers Slack over Teams.'] |
| MEM-14 | Pass | resume twice -> 200, 409 'approval '7e7e10b6-2267-46ef-b959-7689e81afb37' already resumed by molly@acme.com'; fact stored once |
| MEM-15 | Pass | ['allowed', 'allowed', 'allowed', 'allowed', 'allowed', 'allowed'] |
| MEM-16 | Pass | short message handled on the heuristic path, no crash |
| MEMB-01 | Pass | 25 scopes incl. empty placeholders |
| MEMB-02 | Pass | add/edit/delete 200, unknown key 404 |
| MEMB-03 | Pass | bad scope -> 422 |
| MEMB-04 | Pass | head added a fact to own dept scope from the Memory tab |
| MEMB-05 | Pass | head writing central / other dept / a user scope -> [403, 403, 403] |
| MEMB-06 | Pass | member POST/PUT/DELETE -> [403, 403, 403] |
| MEMB-07 | Pass | member Memory tab is read-only (inputs=0); admin editable (inputs=36); head: own dept only |
| MEMB-08 | Pass | empty text -> 200; 200KB text -> 200 (no 500) — NOTE: an empty fact is accepted |
| MEMB-09 | Pass | user/* write never gated |
| MEMB-10 | Pass | dept write with no active head routes admin-only |
| REC-01 | Pass | own-dept recall hit: 'TOOL RESULT: [dept/marketing] user: [PCA] Our team policy requires weekly standups. assistant: echo: [PCA] Our team poli' |
| REC-02 | Pass | cross-department recall returns nothing |
| REC-03 | Pass | junk queries (wildcard, operators, NUL, 3000 chars, empty, wrong case) -> ['allowed', 'allowed', 'allowed', 'allowed', 'allowed', 'allowed']; nothing leaked |
| REC-06 | Pass | recalled transcript for a non-admin: SSN masked, text=' [dept/hr] user: [PCA] Note: employee SSN is *** and mail is x@y.com for payroll topic zebra assistant: echo: [PCA] Note: employee SSN is **' |
| APPR-01 | Pass | approve was terminal, owner resumed, steps=[('head', 'mhead@acme.com', 'approved')] |
| APPR-01a | Pass | head-only item is in the head queue (pill '1 step · department head'), NOT in the admin queue (admin sees it: False) |
| APPR-02 | Pass | chat hint 'step 1 of 2'=True; head approve -> 'step 1 approved — now waiting on an admin (step 2)' (still pending at admin, resume attempt -> 409); admin card shows step 1 + comment; admin approve -> 'approved — waiting on requester to resume'; owner resumed=True; fact saved |
| APPR-03 | Pass | head reject final; admin approve -> 409 'approval '8e56c167-123c-4581-bf46-e15eca5898e3' already rejected'; run resumed as rejected; fact not saved |
| APPR-04 | Pass | no active head -> route=admin-only, shown in the admin queue with banner: '['Escalated: marketing had no active department head when this was requested.']' |
| APPR-05 | Pass | critical tool in a head-only dept -> route head+admin; head first, then admin; transfer rows 1 -> 1 (after head) -> 1 (after admin) -> 2 (after owner resume) |
| APPR-05b | Pass | a head's own critical call -> admin-only (a head cannot approve themselves) |
| APPR-06 | Pass | bob -> hr_delete_employee: outcome=denied, suspended=False, employee still exists=True. NOTE: the test row expects a two-step approval, but policy.csv deliberately gives hr_agent no hr:delete, so RBAC denies it before any approval (matches RBAC-05). The row contradicts the policy; behaviour is the safer one |
| APPR-07 | Pass | write tool with GATE_WRITE_TOOLS=false runs without approval |
| APPR-08 | Pass | with APPROVALS__GATE_WRITE_TOOLS=true a write tool suspends for the head (always-allow enabled); approve -> 'approved (always allowed from now on) — waiting on requester to resume'; resumed=True |
| APPR-08b | Pass | read tools are never gated |
| APPR-08c | Pass | the head's own write tool call is not gated |
| APPR-09 | Pass | cross-dept tool -> denied by RBAC, no approval row created |
| APPR-10 | **Fail** | alice -> gmail_send with no connected Gmail: outcome=no-call, suspended=True, connect_required=[]. FINDING: an unconnected critical external tool SUSPENDS for approval first (head, then admin) and only fails with 'not connected' after both approve — approvers' time is wasted; the connection check should come before the gate; after approval -> 'TOOL RESULT: gmail is not connected. Tap Connect for gmail, then Refresh.' |
| APPR-11 | Pass | Always allow -> identical repeat runs without approval |
| APPR-11b | Pass | always-allowed write tool: identical call runs with no approval |
| APPR-12 | Pass | one character changed -> suspended=True; another member, identical text -> suspended=True |
| APPR-12b | Pass | changed budget -> suspends; another member with identical args -> suspends |
| APPR-13 | Pass | forcing always_allow on a central write via API -> 422 |
| APPR-13b | Pass | critical tool: checkbox disabled, forced always_allow -> 422 |
| APPR-14 | Pass | after the admin step the rule exists (memory:dept/marketing:add, by mhead@acme.com); identical message did NOT suspend; audit reason=allowlist |
| APPR-14a | Pass | rule requested at the head step does NOT exist until the admin step approves |
| APPR-15 | Pass | admin all / head own-dept; revoke is immediate |
| APPR-16 | Pass | member cannot list or revoke rules |
| APPR-17 | Pass | two heads on step 1 -> [200, 409]; two admins on step 2 -> [200, 409]; double owner resume -> 409 (MEM-14) |
| APPR-17c | Pass | double owner resume: second is 409 'already resumed' |
| APPR-18 | Pass | dashboard decide is deferred ('approved — waiting on requester to resume') and the owner's poller resumed it |
| APPR-18b | Pass | raw gateway decide (no defer): head step -> next_level=admin; admin step resumes inline with reply='echo: [PCA] Our team prefers green dashb' |
| APPR-19 | Pass | decided in two other browser profiles, resumed in the requester's own window with the requester's key |
| APPR-20 | Pass | decide already-decided -> 409; non-owner resume -> 403; unknown id -> 404 |
| APPR-21 | **Fail** | delete chat with a pending approval -> {'thread_id': 'user:alice@acme.com:del1', 'deleted': True, 'approvals_rejected': 1}; gone from the head queue; decide -> 409; owner resume -> 404 'claims' |
| APPR-22 | Pass | other dept head / member on dept route / member on admin route / head on admin route -> [403, 403, 403, 403] |
| APPR-23 | Pass | steps=[('head', 'mhead@acme.com', 'approved', 'fine by marketing'), ('admin', 'admin@acme.com', 'approved', 'ok')]; audit has head-hitl + admin-hitl; Ops trace carries both steps=True |
| APPR-24 | Pass | owner / admin / other member -> [200, 200, 403] |
| RBAC-NEW-legal | Pass | new-dept member: other depts' tools {'crm_search_customer': 'denied', 'finance_transfer': 'denied', 'hr_delete_employee': 'denied', 'finance_list_accounts': 'denied'}; memory scopes ['central', 'dept/legal', 'user/lara@acme.com'] |
| RBAC-01 | Pass | alice crm_search_customer -> allowed badge + real CRM data in the reply |
| RBAC-02 | Pass | alice own-dept tools -> {'crm_create_campaign': 'allowed', 'crm_list_campaigns': 'allowed', 'crm_get_customer': 'allowed'} |
| RBAC-03 | Pass | alice cross-dept -> {'hr_search_employees': 'denied', 'hr_delete_employee': 'denied', 'finance_list_accounts': 'denied', 'devops_list_incidents': 'denied', 'devops_create_deployment': 'denied'}; unknown tool -> error ('TOOL RESULT: unknown tool: ghost_tool') |
| RBAC-04 | Pass | bob own-dept -> {'hr_search_employees': 'allowed', 'hr_create_leave_request': 'allowed', 'hr_list_leave_requests': 'allowed'} |
| RBAC-05 | Pass | bob -> {'hr_delete_employee': 'denied', 'crm_search_customer': 'denied', 'finance_transfer': 'denied'}; employee 1 still exists |
| RBAC-06 | Pass | finance_transfer is allowed by RBAC but suspends for HITL |
| RBAC-07 | Pass | carol own reads {'finance_list_invoices': 'allowed', 'finance_list_accounts': 'allowed'}; cross-dept {'crm_create_campaign': 'denied', 'hr_create_leave_request': 'denied', 'devops_create_deployment': 'denied'} |
| RBAC-08 | Pass | dave own-dept -> {'devops_list_incidents': 'allowed', 'devops_create_deployment': 'allowed'} |
| RBAC-09 | Pass | dave cross-dept -> {'finance_transfer': 'denied', 'hr_delete_employee': 'denied', 'crm_search_customer': 'denied'} |
| RBAC-10 | Pass | admin wildcard -> {'crm_search_customer': 'allowed', 'hr_search_employees': 'allowed', 'finance_list_accounts': 'allowed', 'devops_list_incidents': 'allowed', 'crm_create_campaign': 'allowed'} |
| RBAC-11 | Pass | validly-signed token for a user that is not a member (claiming admin) -> [403, 403, 403] |
| RBAC-12 | Pass | disabled member mid-session -> 403 everywhere (verified in AUTH-05) |
| RBAC-13 | Pass | marketing head own-dept tools -> {'crm_search_customer': 'allowed', 'crm_create_campaign': 'allowed'} |
| RBAC-14 | Pass | marketing head other depts -> {'hr_search_employees': 'denied', 'finance_list_accounts': 'denied', 'devops_list_incidents': 'denied'} |
| RBAC-15 | Pass | unknown tool/object never executes |
| RBAC-16 | Pass | tools OFFERED to the model — alice: crm_create_campaign, crm_get_customer, crm_list_campaigns, crm_search_customer, launch_campaign, recall, send_campaign_email, triage_in...; bob: hr_create_leave_request, hr_get_employee, hr_list_leave_requests, hr_search_employees, new_leave_request, recall, triage_incident... |
| RBAC-17 | Pass | model forced to call a tool it was never offered -> blocked at runtime: 'TOOL RESULT: Permission denied: you need scope hr:read to call hr_search_employees. denied' |
| RBAC-18 | Pass | non-admin central write: denied by RBAC outright (no approval, not saved) — stricter than the row's 'HITL' wording |
| RBAC-19 | Pass | non-admin salary/ssn -> masked |
| RBAC-20 | Pass | revocation immediate (crm allowed->denied); new grants after re-login (hr denied->allowed) |
| SKL-01 | Pass | alice launch_campaign allowed; nested tool calls traced: ['crm_search_customer', 'crm_create_campaign', 'launch_campaign'] |
| SKL-02 | Pass | bob (hr) invoking a marketing skill -> denied |
| SKL-03 | Pass | central skill invocable by every dept (top-level allow); nested steps still enforced per role |
| SKL-04 | Pass | alice: central skill visible, but its nested devops step is denied -> [('devops_create_deployment', 'denied'), ('triage_incident', 'denied')] |
| SKL-05 | Pass | dave (devops): same skill, nested deploy allowed |
| SKL-07 | Pass | poisoned skill: the nested forbidden call is denied |
| SKL-08 | Pass | unknown skill -> clean error: 'TOOL RESULT: unknown tool: no_such_skill' |
| SKL-09 | Pass | created in the Skills tab (shows 'no policy row'); duplicate -> 409; unknown -> 404 |
| SKL-09b | Pass | delete -> 200 |
| SKL-10 | Pass | missing tool_calls -> 422; bad YAML -> 422 |
| SKL-10b | **Fail** | scope path escape 'dept/../../evil' -> 500; nothing written outside skills/ = True |
| SKL-11 | Pass | head sees own dept skills with the usable badge |
| SKL-12 | Pass | [403, 403, 403, 403, 403] |
| SKL-13 | Pass | skill edited live to call hr_delete_employee -> next run: [('crm_search_customer', 'allowed'), ('hr_delete_employee', 'denied'), ('launch_campaign', 'denied')]; nothing deleted (hot reload, nested deny) |
| SKL-14 | Pass | a skill without a policy row is not usable by the role (denied) |
| SKL-15 | Pass | member on skills API -> [403, 403] |
| PII-01 | Pass | hr member sees emails; salary and ssn are *** |
| PII-02 | Pass | admin sees full salary + ssn |
| PII-04 | Pass | free-text SSN masked, email kept |
| PII-05 | Pass | single-record + list payloads masked: '{"dept": "DevOps", "email": "dave.kumar@acme.com", "id": 4, "name": "Dave Kumar", "salary": "***", "ssn": "***", "status": "act' |
| PII-06 | Pass | member recall masked (admin recall uses a different dept scope, so the admin half is not comparable here) |
| PII-07 | Pass | stored trajectory of the member's run holds no SSN (email survives) |
| PII-08 | Pass | the model only ever received the masked value — nothing to reveal (prompt in the trace has no SSN) |
| PII-09 | Pass | sensitive tool args are masked (***) on the approval card |
| OPS-01 | Pass | Ops run list has 123 runs; opened one with 14 events |
| OPS-02 | Pass | ?source=tool returns 0 of 14 events, all source=tool (the UI filter buttons call this; the run opened earlier simply had no tool events) |
| OPS-03 | Pass | evals panel shows the deterministic suite PASS |
| OPS-04 | Pass | suspended run has no eval yet; after resume evals=[('deterministic', True)] |
| OPS-05 | Pass | judge suite is skipped for local/mock models (expected) — needs a real hosted key to exercise |
| OPS-06 | Pass | member list / member run / no token / head evals -> [403, 403, 401, 403] |
| OPS-07 | Pass | run_start/prompt/reasoning/run_end present |
| MBR-01 | Pass | added molly through the form; she can log in |
| MBR-02 | Pass | [422, 409, 422] (bad head role covered by AUTH-11) |
| MBR-03 | Pass | crm before=allowed; after move to hr (same token): crm=denied, hr=denied; after re-login hr=allowed. Old permissions are revoked immediately; the NEW department's permissions need a fresh login (the token still carries the old dept) |
| MBR-04 | Pass | 403 while inactive, 200 after reactivation |
| MBR-05 | Pass | enforced server-side |
| MBR-06 | Pass | old password fails, new works immediately |
| MBR-07 | Pass | audit log contains ['admin:members:activated', 'admin:members:create', 'admin:members:deactivated', 'admin:members:password_reset', 'admin:members:update'] |
| MBR-08 | Pass | member on member-admin endpoints -> [403, 403, 403, 403, 403] |
| DEPT-01 | Pass | registered via UI; 28 granular policy rows written, wildcard row present=False |
| DEPT-02 | Pass | duplicate / bad name / bad mode -> [409, 422, 422] |
| DEPT-03 | Pass | legal mode=head+admin heads=0 shows '0 · escalates'; marketing heads=2 |
| DEPT-04 | Pass | switched in the UI: 'marketing: approvals are now head+admin — applies to new requests only.' |
| DEPT-06 | Pass | member POST / member PATCH / head PATCH -> [403, 403, 403] |
| HEAD-01 | Pass | head lands on Dept approvals; tabs=['Chat', 'Account', 'Dept approvals', 'My department', 'Memory', 'Log out'] |
| HEAD-02 | Pass | head step-1 approve hands the item to the admin and resumes nothing |
| HEAD-03 | Pass | head deciding another department's item -> 403 |
| HEAD-04 | Pass | roster = own department only (4 members); heads show 'managed by admin' |
| HEAD-05 | Pass | head added a member in the UI ('added nina@acme.com (marketing_agent)'): role=marketing_agent, scopes copied from a peer=['crm:read', 'crm:create']; she can log in |
| HEAD-06 | Pass | [403, 403, 403, 403] |
| HEAD-07 | Pass | deactivate -> login 401; re-activate -> 200; reset password -> new password login 200 |
| HEAD-08 | Pass | {'mhead2@acme.com': 403, 'admin@acme.com': 403, 'mhead@acme.com': 403, 'bob@acme.com': 403} |
| HEAD-09 | Pass | head revoked own-dept rule from My department; identical request suspends again |
| HEAD-09a | Pass | My department -> Always allowed lists the dept's rule |
| HEAD-10 | Pass | own-dept skills listed; edit saved ('saved → will hot-reload on the next run'), version now 2 |
| HEAD-11 | Pass | change scope / read other dept / write other dept / create / delete -> [403, 403, 403, 403, 403] |
| HEAD-12 | Pass | editable per scope for head: {'central': False, 'dept/marketing': True, 'user/mhead@acme.com': False} |
| HEAD-13 | Pass | head chats with their own key like any member |
| ALLOW-01 | Pass | admin tab lists the rule; member/dept filters work |
| ALLOW-02 | Pass | head sees own-dept rules only (hr head sees none, cannot widen with ?dept=) |
| ALLOW-03 | Pass | member: no tab, API -> 403 |
| ALLOW-04 | Pass | revoked in the admin tab -> the very next identical request suspends again |
| ALLOW-05 | Pass | another dept's head cannot revoke |
| ALLOW-06 | Pass | same real args + different request_id/timestamp -> still auto-allowed (volatile keys ignored) |
| UX-01 | Pass | member tabs [Chat, Account, Memory]; head tabs ['Chat', 'Account', 'Dept approvals', 'My department', 'Memory']; admin tabs [Chat, Account, Approvals, Members, Departments, Always allowed, Skills, Memory, Ops] |
| UX-03 | Pass | sidebar: + New Chat, active row, delete with confirm, title from first message (CHAT-02/03/07) |
| UX-04 | Pass | model selector options: ['Default (Active Key / Global)', 'lmstudio / fake-tool-model (Active)'] |
| UX-05 | Pass | suspended line + head-only route hint + Resume now button |
| UX-05b | Pass | requester's chat shows 'step 1 approved by ... - waiting on an admin (step 2 of 2)' |
| UX-06 | Pass | card shows dept pill, step pills, step history, comment box, Always-allow, Approve/Reject |
| UX-07 | Pass | Always-allow checkbox disabled for central, tip='Critical tools and central memory need approval every time.' |
| UX-09 | Pass | a bad stored token sends the user back to the login screen |
| UX-10 | Pass | friendly empty states for keys and memory |
| UX-11 | Pass | at 380px wide the chat still sends/receives; page scrolls sideways=True (see ux11_narrow_360.png) |
| DEST-01 | Pass | system can never be left without an admin |
| DEST-02 | **Fail** | same as CHAT-08 |

Screenshots are in `screenshots/` (file names start with the row they belong to).

## Follow-up the same day: Docker + Composio

### Docker (the running `fyp-app-1` container, shared Neon database)

| ID | Status | What was observed |
|---|---|---|
| SET-01 | **Fail → fixed** | Container was healthy but built from an OLD image: migrations only up to 0008, none of commits `ea96396`/`11175c1`, while the database was already at 0009 (`db check` warned "database is ahead of this checkout"). Rebuilt with `docker compose up -d --build`; image now has 0009 and the current code. |
| SET-02 | Pass | `python -m app.db check`: connect OK, ~108 ms median round trip, migrations 0001–0009 applied (after rebuild). |
| SET-03 | Pass | `verify-audit`: 196 entries, chain intact (before and after the rebuild). |
| SET-07 / DOCK-03 | Pass | `/health` ok; healthcheck lines in the logs; no errors or tracebacks in the last 400 log lines. |
| DOCK-01 | Pass | Only `8006` is published to the host; 8001–8005 are unreachable from the host and all seven services answer `/health` inside the container. Host port 8000 belongs to a different program on this PC. |
| DOCK-04 | Pass | Container recreated by the rebuild; files in the `fyp_data` volume kept (same timestamps); app healthy in ~40 s. |
| Smoke | Pass | Headed Chrome against `localhost:8006`: alice lands on Chat, admin on Approvals; Ops lists 86 runs (the fix is in the image); Departments renders. |
| — | Note | The process runs as `appuser` (not root); `.env` is not baked into the image. |
| DOCK-02/05/06, SET-08 | Not run | Port-clash, `down -v`, the API-ports overlay and the broken-`DATABASE_URL` test change the running setup or `.env`; left for a manual pass. |

### Composio (real key, isolated stack, key passed by environment variable only and removed afterwards)

| ID | Status | What was observed |
|---|---|---|
| APPR-10b | Pass | a non-critical unconnected external tool is denied with a Connect prompt |
| INT-01 | Pass | real key validated and saved from the Account tab: 'Key saved — key works (1000 toolkits visible). Now connect an app below.'; apps=['github', 'gmail']; key never shown back=True |
| INT-02 | Pass | garbage key -> 502 'key rejected by Composio (401) — copy it again from app.composio.dev'; not saved |
| INT-03 | Pass | Connect gmail -> 200 status=pending redirect link returned=True (OAuth link host: connect.composio.dev); Refresh -> 'pending' (stays pending until the Google sign-in in the popup is completed by a person) |
| INT-04 | Pass | Disconnect -> revoked; gmail tool in chat -> denied, connect_required=['gmail'] |
| INT-04a | Pass | gmail_fetch in chat while gmail is 'pending' -> outcome=denied, connect_required=['gmail'], reply='TOOL RESULT: gmail is not connected. Tap Connect for gmail, then Refresh.' |
| INT-05 | Pass | search 'git' -> ['github'] |
| INT-06 | Pass | Connect with no saved key -> 422 'save your Composio API key in Account → Integrations first' |
| INT-07 | Pass | unknown toolkit -> 422; admin-disabled toolkit -> 422 ''github' is disabled by your admin'; hidden from the member list |
| INT-08 | Pass | admin re-enabled the toolkit -> visible to members again (adding a brand-new toolkit was skipped: it creates an auth config in the Composio project) |
| INT-09 | Pass | member on catalog admin endpoints -> [403, 403] |
| INT-10 | Pass | Connect after deleting the key -> 422 |
| INT-11 | Pass | setup guide page loads (1131 chars) |
| INT-12 | Pass | key removed in the UI -> has_key=False; app statuses=[('github', 'disconnected'), ('gmail', 'revoked')] |

Not run: completing the Google sign-in (needs a person), so no real Gmail fetch/send was executed (SKL-06, the live half of INT-03).


## Follow-up 2: two-PC simulation, real Gmail, real local model, remaining rows

Two full app stacks ("PC-B" on :8016, "PC-C" on :8116) sharing one throwaway local Postgres; Gmail through Composio for `patelmukeah155@gmail.com` (key from an environment variable only); LM Studio on this PC (`qwen/qwen3-4b-2507`, `google/gemma-4-e2b`). Nothing here touched the shared Neon database or the Docker container.

| ID | Status | What was observed |
|---|---|---|
| DB-10 | Pass | `app.db status`: 9 migrations applied, none pending/edited |
| DB-01 | Pass | member created on PC-B visible on PC-C after 0.0s; department after 0.0s; new member logs in on PC-C (200) |
| DEPT-05 | Pass | new department's policy rows are live on the OTHER PC without a restart (new member's memory scopes resolve there after 0.4s) |
| DB-02 | Pass | LLM key saved on PC-B: visible there at once; on PC-C after 0s (<= 30s cache window) |
| CHAT-09 | Pass | alice's chat list is identical on both PCs (5 chats) |
| CHAT-10 | Pass | turn sent on PC-B is readable on PC-C (2 messages incl. the reply) |
| CHAT-14 | Pass | same thread continued on PC-C: history now 4 messages on PC-B; the model on PC-C received the earlier PC-B turn=True |
| DB-03 | Pass | skill created on PC-B is listed on PC-C after 0.1s, no restart |
| DB-04 | Pass | a run paused on PC-B is visible in Ops on PC-C at the pause (13 events). An in-flight run is written in one batch at finish/pause, so it is invisible until then by design (not separately timed) |
| DB-05 | Pass | requested in a browser on PC-B, approved by the head in a browser on PC-C ('approved — waiting on requester to resume'), auto-resumed on PC-B; fact visible from PC-C |
| APPR-19b | Pass | cross-PC decide -> resume on the requester's PC (two app instances, one database) |
| DB-06 | Pass | both PCs wrote to one audit chain: audit    : log 'audit', 18 entries, checked in 10 ms / result   : chain intact |
| DB-07 | Pass | verifying with a mismatched SERVICES__AUDIT_HMAC_KEY flags the chain: 'hint     : a key mismatch looks like tampering too — every laptop must use the same SERVICES__AUDIT_HMAC_KEY' |
| DB-08 | Pass | sync-config --dry-run: policy   : 0 rule(s) in policy.csv missing from the database \| skills   : 0 skill(s) in skills/ missing from the database \| dry run  : nothing was changed |
| DB-09 | Pass | prune --chats: 'note     : Postgres reuses freed space but the file may not shrink at once (VACUUM runs au'; chat history intact (4 msgs), chat continues, and a run paused before a prune still resumes (200/200) |
| DB-11 | Pass | Postgres recall is exact-word, no stemming / accent folding: {'budget': True, 'budgets': False, 'resume': True, 'résumé': False} |
| REC-07 | Pass | {'budget': True, 'budgets': False, 'resume': True, 'résumé': False} |
| DB-12 | Pass | local Postgres + scripted model: warm turn 0.9s, new-chat turn 0.9s (perf note only; real Neon adds ~110 ms per round trip) |
| DEST-03 | Pass | same, on the throwaway database only |
| DEST-04 | Pass | prune --traces-older-than 30 --dry-run: 'dry run  : nothing was changed' (dry run only) |
| DEST-05 | Pass | dry run only (on the throwaway database) |
| INT-03b | Pass | gmail was 'revoked' locally; Refresh asks Composio, finds the ACTIVE Gmail connection for this user and flips it to 'active'; Account tab row: 'gmail active 2026-10-10 17:44:44 Disconnect' |
| UX-08 | Pass | a tool on a disconnected app shows a 'Connect gmail' button in the chat turn |
| GMAIL-fetch | Pass | real gmail_fetch through the harness: outcome=allowed, 1513 chars of mailbox data returned to the model (content not recorded here) |
| GMAIL-send | Pass | gmail_send suspended (head -> admin, always-allow disabled, args masked on the card=False); after both approvals the requester's chat resumed: 'TOOL RESULT: {"data": {"display_url": "https://mail.google.com/mail/u/0/#inbox/1a126eb388fd0601", "id": "1a126' |
| APPR-05c | Pass | critical EXTERNAL tool follows the same head -> admin chain as finance_transfer |
| GMAIL-verify | Pass | the approved email actually arrived: a Gmail search for its unique subject finds it (True) |
| SKL-06 | Pass | skill with a nested critical gmail_send suspends like a direct call (route head+admin); after approval the mail arrived=True; reply='TOOL RESULT: [1] gmail_send: {"data": {"display_url": "https://mail.google.com/m' |
| RBAC-13b | Pass | marketing head is ALLOWED gmail by policy; without their own connection the call is denied with a Connect prompt (not an RBAC denial) |
| RBAC-13c | Pass | hr member has no gmail:send in the policy -> RBAC denial, no approval raised |
| GMAIL-isolation | Pass | another member of the same department cannot read this user's mailbox (their own gmail is not connected) |
| BYOK-14b | Pass | Detect against the real LM Studio on this PC -> 'Found 3 loaded model(s).', models ['qwen/qwen3-4b-2507', 'google/gemma-4-e2b', 'text-embedding-nomic-embed-text-v1.5']; saved 'qwen/qwen3-4b-2507' as alice's active key |
| LOCAL-chat | Pass | real local model (qwen/qwen3-4b-2507) answered in 6s: '2 + 3 = 5.' |
| LOCAL-tool | Pass | real local model chose and called the tool itself: crm_search_customer outcome=allowed in 11s; reply='I found a total of 6 customers with the query "a". Here are their details:  1. **Alice Zhang** - Acme Corp, Em' |
| RBAC-16b | Pass | real model asked for a tool it is not offered (hr_search_employees): tool calls made = []; reply='I don't have access to the `hr_search_employees` tool. Let me know if there's another way I can assist!' |
| BYOK-15 | Pass | second local model (google/gemma-4-e2b) handled a tool question without error: tool calls=[('hr_list_leave_requests', 'allowed')]; (it supports tools, so the 'tool calling unsupported' path was not triggered) |
| BYOK-11 | Pass | key saved on PC-B (works there: 'echo: [PC-test] he'). On PC-C with a DIFFERENT LLM__FERNET_KEY: chat -> 200 falls back to Mock ('mock reply to: [PC-tes'); Members list -> 200 with active_llm_key={'provider': 'lmstudio', 'model': 'fake-tool-model', 'stale': True} |
| BYOK-12 | Pass | re-saved on PC-C: stale flag clears at once; the chat on that PC keeps falling back to Mock for up to ~30s (key cache in the gateway process), then uses the real endpoint ('echo: [PC-test] hello ') |
| MEM-12 | Pass | vague correction: no crash, both existing facts untouched (no wrong overwrite); memory decisions traced: [('allowed', None), ('allowed', None), ('allowed', None)] |
| MEM-13 | Pass | 60 x ~110-byte facts into a 4096-byte tier: 23 evictions reported, oldest gone, newest kept, tier now 4070 bytes / 37 facts |
| REC-04 | Pass | a poisoned fact planted in dept/hr never reaches a marketing member (not in recall, not in the injected snapshot); her tool list and RBAC are unchanged |
| PII-03 | Pass | marketing member: the HR search is denied outright; her own CRM search returns no salary/SSN |
| UX-02 | Pass | pills ['admin: admin@acme.com', 'member (+ add)'] -> ['admin: admin@acme.com', 'member: alice@acme.com']; switching back to admin shows admin, clears the transcript and lists only admin threads |
| AUTH-15 | Pass | both sessions persist; switching never shows or sends into the other identity's thread |
| OPS-08 | Pass | before: 'result   : chain intact'. One audit row edited directly in the THROWAWAY database (edited seq 6). after: 'result   : BROKEN — 1 issue(s) /   - row 6: hash mismatch (entry edited) / hint     : a key mismatch looks like tampering too — every laptop must use the same SERVICES__AUDIT_HMAC_KEY' |
| DEST-06 | Pass | tampering with a single audit entry is detected by verify-audit (throwaway database only) |
| AUTH-07b | **Fail** | shared-database only: a member created <1s ago gets 401 'invalid credentials' on login (6 of 8 tries at 0 delay, 0 failures at 0.5s+). `/auth/login` looks the member up through the 1-second cache before verifying the password |
| UX-06b | **Fail** | UX finding: on an approval card for gmail_send the approver sees to/subject/body all as '***' (see gmail_send_head_card.png) - the head and admin approve an email without being able to read the recipient or the text |

### New findings from this pass

| # | Severity | Finding |
|---|---|---|
| 11 | Minor (shared DB) | A member created less than about one second ago gets `invalid credentials` on login: `/auth/login` looks the member up through the 1-second member cache before checking the password (AUTH-07b). |
| 12 | UX | On the approval card for `gmail_send`, `to`, `subject` and `body` are all shown as `***`, so the head and admin approve an email they cannot read (UX-06b). |
| 13 | Note | After a key is re-saved on a PC whose `LLM__FERNET_KEY` differs, chat on that PC keeps falling back to Mock for up to ~30 s (key cache), then recovers (BYOK-12). |

Side effects of this pass: two test emails were sent from `patelmukeah155@gmail.com` to itself (subjects start with `[FYP harness test`), and one GitHub connect request was started by mistake in that Composio project (left pending, never completed).

### Still not run
SET-08, DOCK-02/05/06 (declined: they disturb the running setup), BYOK-04/05/16/17 and OPS-05 (need a hosted provider key), INT-08 adding a brand-new toolkit, REC-05 (no reachable way to force the isolation error), and anything that needs real Neon latency or three physical machines.

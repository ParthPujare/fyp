# Plan Mk21 — Dashboard UI refresh + single-session login

## Metadata
- Date: 2026-10-20
- Status: **BUILT 2026-10-21 (session 037)** — Phases 1–11 implemented; offline + browser suites green (see History). Manual checks left to the owner: Chrome translate bubble, a live chat with a real model, one live skill generation with the platform key.
- Follows: Mk20 (review fixes), Mk19 (security remediation), Mk18 (zero-trust closure)
- Source: the 2026-10-20 UI walkthrough (10 screenshots + written issues), reviewed against the live test suite before any decision was locked

## Intent

Make the dashboard readable to a non-expert. Remove explanatory sentences that describe internals. Replace walls of text with tags, chips, and dropdowns. Collapse the second-login machinery to one identity at a time. Add the two missing affordances: file attachments in chat, and search plus filters in Ops.

Every item below is either copy, layout, or a new endpoint. No existing behaviour is removed except the identity switcher, which Decision 1 retires deliberately.

---

## Locked decisions

| # | Decision | Choice | Consequence |
|---|---|---|---|
| 1 | How deep to cut the second login | **B** — UI plus delete the switch endpoint | Removes `POST /api/session/switch` and rewrites one test |
| 2 | How far the integration rename goes | **A** — neutral headings, honest destination link | Provider name leaves headings, stays in the guide's fetch step |
| 3 | Chat file attachments | **A** — text only, appended to the message | No change to `ChatRequest`, no storage, no cleanup job |
| 3b | Attachment approval gate | **None** — existing guardrails cover content | Attachments are content, not tool actions |
| 4 | Navigation collapse style | **A** — icon rail, desktop only | Toggle lives outside `#nav`, labels are clipped not hidden |
| 5 | Members dropdown sources | **A** — derive from existing endpoints | No new endpoint |
| 6 | Department long cells | **A** — tags plus an inline detail row | No floating popover, so no viewport overflow |
| 6b | The policy-row sentence | **Move it into the form** | Stays where the side effect applies |
| 7a | Skills card content | **A** — markdown default, JSON behind a toggle | Executor-visible `tool_calls` stay reachable |
| 7b | The `POLICY ROW` badge | **Replace with `granted to <role>`** | Jargon out, information kept |
| 7c | Skill generation trigger | **A** — explicit button, draft only | Nothing is written until a human saves |
| 7d | Generation model | **A** — reuse the platform extractor config | One model, one key, free-tier quality accepted |
| 7e | Skill visibility | **Explicit `Grant to` multi-select** | No silent widening of a role's reach |
| 8 | Memory scoping | **A** — client-side filter, central stays | `GET /api/memory` unchanged |
| 9 | Ops filtering | **A** — client side | `list_runs` unchanged in both trace backends |
| 9b | Ops monitoring features | **Summary strip, manual refresh, 15 s poll with a no-op guard** | Rows are not rebuilt when nothing changed |
| 10 | Chat "span the screen" | **A** — height only | `main { max-width: 1240px }` untouched |
| 11 | Where this plan lives | `ref_docs/plans/plan_mk21_ui_refresh.md` + a registry row | This document |
| 12 | README seeded credentials | **Leave `README.md:53` unchanged** | Onboarding path stays documented |

---

## Gate — what must stay green

`pyproject.toml:35` sets `testpaths = ["tests"]` with no custom marker, so `uv run pytest` **runs `tests/e2e/test_dashboard_ui.py` in a real browser**. Every phase is scored against it.

### Copy that no test reads — free to change

Verified by grep across `tests/`: `BYOK`, `Composio key`, `Integrations (Composio)`, `login-hint`, `chats as`, `Register department`, `folder/markdown`, `approve-once rules`, `Username is immutable`, `Run trajectories`, `Memory browser`. Zero hits. All copy-only edits are risk-free.

### Anchors that tests depend on — do not touch

| Anchor | Test | Rule for this plan |
|---|---|---|
| Nav label list, admin = `Chat, Account, Approvals, Members, Departments, Always allowed, Skills, Memory, Ops, Log out` | `test_dashboard_ui.py:75-78` | Do not add, remove, or rename a nav item |
| Nav label list, member = `Chat, Account, Memory, Log out` | `test_dashboard_ui.py:97` | Same |
| `#user`, `#pass`, `#signin`, `#login-view`, `#sidebar`, `#crumb-view` | `test_dashboard_ui.py:23-30,68,73` | Keep the ids |
| Sidebar is a full-width top bar at 375 px | `test_dashboard_ui.py:103-104` | Collapse must be desktop-only |
| Zero sideways scroll at 375 / 768 / 1280 on Approvals, Members, Departments, Ops, Account | `test_dashboard_ui.py:303-311` | Every new chip, popover, and search bar stays inside its container |
| `#ops-filters button` labelled `tool` | `test_dashboard_ui.py:295` | Keep the ids and the source labels |
| `#depts tr` containing `marketing` | `test_dashboard_ui.py:92` | Department name stays plain row text |
| `#member-keys-title` reads `LLM keys — alice@acme.com` | `test_dashboard_ui.py:154` | Do not reword |
| Status text `Key saved and set active for…` | `test_dashboard_ui.py:162` | Do not reword |
| Status text `Model updated.` | `test_dashboard_ui.py:174,234` | Do not reword |
| Status text `notion disabled` / `notion enabled` | `test_dashboard_ui.py:258,265` | Do not reword |
| Status text `Sent to a second admin to confirm` | `test_dashboard_ui.py:209` | Do not reword |
| Dual-slot cookie names `harness_token_admin`, `harness_token_active` | `test_cookies.py:37-39,59,76-77` | Untouched by Decision 1B |
| `/` redirect target | `test_dashboard.py:50` | Untouched |

### Nav labels are protected by layout, not by markup

The collapse toggle must **clip** labels, never `display: none` them. `nav_labels()` at `test_dashboard_ui.py:38` calls `all_inner_texts()`, and `open_tab()` at `:34` matches with `has_text`. Both return nothing for a descendant that is `display: none`, because `innerText` skips unrendered nodes. A clipped node still has a box, so it still counts.

### Coverage gap

The chat UI has **no** automated coverage. `tests/test_chat_web.py` uses `TestClient` only. Phase 2 and Phase 10 need manual browser verification, and the report must say so.

---

## Phase 1 — Copy-only removals

No behaviour, no layout. One commit, then `uv run pytest` to prove the copy really is free.

| # | File | Change |
|---|---|---|
| 0 | `index.html:2` | Add `translate="no"` to `<html>` |
| 0 | `index.html:3` area | Add `<meta name="google" content="notranslate">` |
| 0 | `app/dashboard/app.py` middleware | Set `Content-Language: en` on HTML responses |
| 1 | `index.html:56` | Delete the `login-hint` div |
| 1 | `console.css:149` | Delete `#login-hint { font: 11px/1.6 var(--mono); }` |
| 3 | `index.html:86` | `LLM API Keys (BYOK)` → `LLM API Keys` |
| 3 | `index.html:111` | Delete the model tip line |
| 3 | `index.html:660` | Rewrite the comment without the word `BYOK` |
| 4 | `index.html:118` | `Integrations (Composio)` → `Integrations` |
| 4 | `index.html:120` | `Composio setup guide` → `Setup guide` |
| 4 | `index.html:121` | `New here? Open the guide, copy 3 values, paste below.` → `New here? Open the guide, then paste your key below.` |
| 4 | `index.html:124` | `Step 1 — Paste your Composio key (yours only, never shown back)` → `Step 1 — Paste your integration key` |
| 4 | `index.html:125` | Keep the working link, name the destination honestly, drop the brand from the sentence |
| 4 | `index.html:127` | Placeholder `Composio API key` → `Integration key` |
| 4 | `index.html:759,792,866` | Three status strings, drop the brand |
| 4 | `index.html:1481` | `composio_key: "Composio key"` → `composio_key: "Integration key"` |
| 9 | `index.html:199` | Delete the `approve-once rules…` muted span inside the `h2` |
| 10 | `index.html:283` | Delete the `folder/markdown registry…` muted span |
| 10 | `index.html:286` | Delete the `skill_<name> row in policy.csv` muted span |
| 11 | `index.html:306` | Delete the `records everything the model saw…` muted span |
| — | `index.html:62` | Delete `#chat-identity`, the span that reads `(chats as …)` |
| — | `index.html:496` | Delete the line that fills `#chat-identity` |

**Rules for this phase**

1. Change only the strings listed above. Nothing else.
2. Do not reword the four protected status strings.
3. Do not add, remove, or rename any nav item.
4. Keep the `datalist` at `index.html:103`. It gives model autocompletion without visible guidance.
5. Do not edit `README.md`. Decision 12 keeps the seeded credentials documented.
6. Do not edit `ref_docs/plans/plan_mk20_review_fixes.md`. Rule 2 of `INDEX.md` forbids rewriting a closed plan.

**Setup guide restyle**

7. Rewrite `composio-setup.html` headings and body text without the provider name.
8. Link it against `console.css` and reuse the console card, pill, and button classes.
9. Keep the step that tells the reader where to fetch the key, and keep that link pointing at the real site.

**Exit**

10. Run `uv run pytest`. Expect green.
11. Open the dashboard in Chrome and confirm the translate bubble is gone. Clear the remembered translate state for this origin first if it persists.

---

## Phase 2 — Layout fixes

| # | Where | Change |
|---|---|---|
| — | `console.css` `#chat-model-select` | Add `max-width` and `text-overflow: ellipsis` so the select cannot leave the card |
| — | `console.css:293` | `#chat-transcript` becomes `flex: 1` with `min-height: 0`. Delete the `60vh` cap |
| — | `console.css` `#chat-main` | Give it a definite height so the flex child can fill it |
| — | `index.html:78` | Placeholder becomes `message…` |
| — | `index.html:79`, `console.css:296` | `Send` becomes an SVG arrow inside a fixed-size circular button |
| — | `console.css` table rules | Minimum column width on `Name`, `Service`, `Role`, `Heads` so `admin` and `marketing_agent` stop wrapping mid-word |
| — | `index.html:199` area | Always-allowed heading keeps the filters and the revocation sentence |

**Rules**

1. Keep `main { max-width: 1240px }` at `console.css:139`. Decision 10 is height only.
2. Keep the `Enter` to send and `Shift+Enter` newline handler at `index.html:2305`.
3. Keep the `#chat-transcript:empty::before` empty-state hint.
4. Verify chat by hand. There is no automated chat test.

**Exit**

5. Run `tests/e2e/test_dashboard_ui.py`. The 1280 px no-overflow assertion is the one that can catch a bad `max-width`.

---

## Phase 3 — Collapsible navigation

**Decision 4A.** Icon rail, desktop only, toggle outside `#nav`.

1. Add a collapse toggle to `#topbar`, not to `#nav`. It must not appear in `nav_labels()`.
2. Add `body.nav-collapsed` in `console.css`. Shrink `#sidebar` from `var(--side)` to about 64 px.
3. Clip nav labels with `#sidebar { overflow: hidden }`. **Never** use `display: none` or `visibility: hidden` on the label spans.
4. Show each item's icon when collapsed, and keep the `title` attribute for hover.
5. Persist the state in `localStorage` and restore it on load.
6. Guard the whole feature behind the existing `max-width: 860px` media query at `console.css:387`, so phones keep the full-width top bar that `test_dashboard_ui.py:103-104` requires.

**Exit**

7. Run the e2e suite at the default width and confirm `nav_labels()` still returns all ten labels.
8. Run it again at a 375 px viewport and confirm the sidebar is still at least 370 px wide.

---

## Phase 4 — Single session

**Decision 1B.** Remove the switcher from the page and retire the endpoint it called.

### Backend

1. Delete `SessionSwitchRequest` at `app/dashboard/app.py:580-582`.
2. Delete `switch_session` at `app/dashboard/app.py:584-614`.
3. Keep `app/harness/cookies.py` unchanged. The auth service still writes both slot cookies at `app/auth/service.py:85-86`, and the dashboard middleware still reads them at `app/dashboard/app.py:101-114`.
4. Keep `POST /api/logout` at `app/dashboard/app.py:566-577` unchanged. It already clears all three cookies.
5. Rewrite `tests/test_mk20_review_fixes.py:217-244`. Replace the removed test with one that asserts `POST /api/session/switch` now returns 404, and keep the CSRF and slot-validation coverage in a form that still applies.
6. Record in this plan's history that Mk20 finding 13 is retired. Do not edit the Mk20 doc.

**Why correctness holds without the switcher.** `app/auth/service.py:85-86` sets the active slot on every login, so the browser never selects a stale slot. `app/dashboard/app.py:569-574` clears all three cookies on logout, so no stale slot survives a logout either. A re-login therefore replaces the session cleanly with no backend change.

### Frontend

7. Delete the whole `side-foot` block at `index.html:27-31`.
8. Delete the `.side-foot`, `#switcher`, and `#who` rules in `console.css`.
9. Delete `renderSwitcher()` at `index.html:546` and its call inside `render()` at `index.html:498`.
10. Replace the `identities` map with a single identity holder.
11. Delete `switchTo()` at `index.html:402`, `openLogin()` at `index.html:426`, and the `loginMode` variable at `index.html:338`.
12. Delete the `#login-cancel` button at `index.html:54` and its listener at `index.html:2286`.
13. Make `doLogin()` overwrite the current identity.
14. Make `invalidate()` clear the single identity, then call `POST /api/logout`, then return to the login gate.

**Claims storage migration**

15. Introduce one `localStorage` key, `harness_claims`.
16. On restore, read the two legacy keys `harness_admin_claims` and `harness_member_claims` plus `harness_active`, pick the one the active slot named, write it to `harness_claims`, then delete all three legacy keys. This keeps an existing browser signed in across the deploy.
17. Delete `TOKENS` at `index.html:327`. Both token keys are already unused since Mk18 moved tokens into httpOnly cookies.

**Exit**

18. Run `uv run pytest`. Expect one removed test and one rewritten one.
19. Sign in, reload, and confirm the session survives.
20. Sign in as a second user after logging out, and confirm the first session is gone.

---

## Phase 5 — Members dropdowns

**Decision 5A.** Derive both lists from data the page already fetches. No new endpoint.

### Role dropdown

1. Replace the free-text `#mem-role` at `index.html:222` with a `<select>`.
2. Populate it with `admin`, then `<dept>_agent` and `<dept>_head` for every row of `GET /api/departments`.
3. Note that `GET /api/departments` at `app/dashboard/app.py:1416-1428` returns `role` but not `head_role`. Build `<name>_head` on the client, the same convention already used at `index.html:1719`.
4. Keep the existing auto-fill at `index.html:1719`, so choosing a department still suggests `<dept>_agent`.

### Scope multi-select

5. Replace the free-text `#mem-scopes` at `index.html:224` with a multi-select that renders one chip per selected scope.
6. Build the candidate list from that department's `resources` array, plus the fixed objects read from `app/harness/policies/policy.csv`: `memory_central`, `memory_dept`, `memory_user`, `memory_recall`, `pii_email`, `pii_salary`, `pii_ssn`, and the `skill_*` rows.
7. Filter the candidate list by the selected department, so a marketing admin never sees `finance:transfer`.
8. Serialize the selection back to the comma-separated string that `PUT /api/members/{username}` already expects. `splitScopes()` at `index.html:1701` keeps working on the way out.
9. Accept that the dropdown can still offer an object policy.csv does not grant, such as `crm:delete`. Today's free-text input has the same hole, so this is no regression.

### Layout

10. Give the multi-select wrapper `max-width: 100%`, give the chip area `flex-wrap`, and cap it with `max-height` plus internal `overflow-y: auto`. This keeps Members inside the 375 px no-overflow assertion at `test_dashboard_ui.py:307`.
11. Render the `Scopes` table cell as chips instead of `u.scopes.join(", ")` at `index.html:1800`.
12. Delete the muted note at `index.html:214`.

---

## Phase 6 — Departments readability

**Decision 6A.** Tags plus an inline detail row. No popover.

1. Render each resource as a tag chip in `loadDepts()` at `index.html:1874`.
2. Show at most three chips, then a `+N` chip.
3. Make `+N` a plain button that toggles an extra `<tr>` directly beneath, spanning the table width, listing everything.
4. Render members the same way, with the same `+N` toggle.
5. Keep the approvals `<select>` behaviour unchanged.
6. Avoid any floating layer. A popover would have to flip and clamp inside the 375 px no-overflow assertion at `test_dashboard_ui.py:307`, which is exactly where an inline row has no such risk.

**Add department form, Decision 6B**

7. Change the button at `index.html:255` to `+ Add department`.
8. Change the panel heading at `index.html:259` to `Add department`.
9. Change the submit button at `index.html:272` to `Add`.
10. Add visible `<label>` elements above the five inputs in `#dept-editor`.
11. Move the policy-row sentence from above the table into the form. It explains a real backend side effect, so it belongs where that side effect is triggered.

---

## Phase 7 — Memory department view

**Decision 8A.** Client-side filter. `GET /api/memory` at `app/dashboard/app.py:2039` stays unchanged.

1. Delete the `saved in shared DB…` sentence from the platform card template at `index.html:2055`.
2. Keep the key input, the model input, `Save`, `Clear`, and the status line.
3. Keep the empty-key status line at `index.html:2033`. It explains why memory is empty, which is useful.
4. Add a department `<select>` above the memory list, fed by `GET /api/departments`.
5. Add a search `<input>` beside it.
6. Resolve the selected department to its member list from that row's `members` array.
7. Build the visible scope set from `dept/<selected>` plus `user/<member>` for each member. These are the scope names returned by `_scope_namespaces()` at `app/dashboard/app.py:1990`.
8. Add `Central` as a separate option in the same dropdown. Do not remove it. `tests/test_memory_web.py:180` documents the Memory browser as the only admin-only write surface.
9. Render the visible scopes into one container with `overflow-y: auto` and a bounded height in `console.css`.
10. Filter facts by substring against the search box, scoped to the selected option only.
11. Admin sees every department in the dropdown. A head sees only their own, derived from `claims.dept`, matching the rule already used at `index.html:2044`.

---

## Phase 8 — Skills page and generation

### Card content, Decisions 7A and 7B

1. Render `Skill.body`, the markdown narrative, as the card's default content.
2. Add a `Tool calls` toggle that reveals the existing JSON block. The JSON is what `app/skills/executor.py` actually runs, so it must stay reachable.
3. Replace the `POLICY ROW` badge with a chip reading `granted to <role>`. The role string is already printed beside it, so nothing is lost.

### Generation, Decisions 7C, 7D, and 7E

4. Add a `Generate draft` button to the Skills page.
5. Add `POST /api/skills/generate` in `app/dashboard/app.py`. It takes `{scope, brief}` and returns a draft `skill.md`.
6. Read the model and key from `GET /api/system-memory/config`. That is the platform extractor config, which Decision 7D selects as the one model.
7. Accept the constraint that `PUT /api/system-memory/config` rejects anything that is not a `:free` id. Generation therefore inherits free-tier quality.
8. Open the returned draft in the existing editor at `index.html:289`. Nothing is written to disk and no policy row is added until the human presses `Save`.
9. Save through the existing path at `app/dashboard/app.py:1691`, which already appends the `skill_<name>` visibility row idempotently at `app/dashboard/app.py:1679`.
10. Add a `Grant to` multi-select to the generation form. Default it to the scope's own role. Require an explicit tick to widen it, so no skill becomes visible to a role by accident.
11. Leave `SkillRegistry` hot-reload untouched. `app/skills/registry.py:28` re-reads disk on every call, so a saved draft takes effect on the next run with no extra work.

**Exit**

12. Run `uv run pytest`.
13. Generate a draft, save it, and confirm the agent can reach it under the granted role only.

---

## Phase 9 — Ops monitoring

**Decisions 9A and 9B.** Client-side filtering. Summary strip. Guarded polling.

1. Delete the muted span in the `h2` at `index.html:306`. Phase 1 already covers this.
2. Add a search input above the runs table in `#ops-runs-view`.
3. Add filter controls for outcome, actor, and a date range beside it.
4. Filter the run array client side inside `loadOps()` at `index.html:2181`. `GET /api/ops/runs` and both `list_runs` implementations at `app/ops/store.py:222` and `app/ops/pg_trace.py:209` stay unchanged.
5. Keep `#ops-runs`, `#ops-filters`, the source labels, and the `View` button text. `test_dashboard_ui.py:285-299` depends on all of them.
6. Add a summary strip above the table: total runs, counts by outcome, runs in the last 24 hours, and a `Last updated` timestamp.
7. Add a manual `Refresh` button.
8. Poll every 15 seconds, but **skip the re-render when the run-id list is unchanged**. Today `loadOps()` rebuilds the whole `tbody`, and `test_dashboard_ui.py:263` already warns that you must never click a row mid-swap.
9. Re-check the 375 px no-overflow assertion. Ops is in the list at `test_dashboard_ui.py:307`.

---

## Phase 10 — Chat file attachments

**Decisions 3A and 3B.** Text only, appended to the message, no new approval gate, no backend change.

1. Add a paperclip button beside the composer that opens a file picker.
2. Accept `.txt`, `.md`, `.csv`, `.json`, and `.log` only.
3. Enforce 200 KB per file and 3 files per message.
4. Show each chosen file as a removable chip above the composer.
5. On send, append each file's contents as a fenced block inside the message, with the filename as the fence label.
6. Send through the existing `POST /api/chat`. `ChatRequest` at `app/gateway/app.py:114` is unchanged, so the gateway, the agent, and the trace need no work.
7. Clear the chips after a successful send.
8. Add no approval gate. Attachments are content, not tool actions. They already pass through the guardrails node and the PII sanitizer, and the trace already records `PROMPT` events sanitised at write time.

**Deferred, not rejected**

9. Images and PDFs need a real upload path: a new endpoint, server-side storage, MIME validation, retention, and a cleanup job. Treat that as its own plan.
10. Base64 in the JSON body is the middle option. It avoids disk but multiplies payload size through uvicorn, the gateway, and thread history, and it leaves no forensic trail.

---

## Phase 11 — Docs and registry

1. Add the Mk21 row to `ref_docs/plans/INDEX.md`.
2. Leave `README.md:53` unchanged. Decision 12 keeps the seeded credentials documented, because the e2e suite's own docstring at `tests/e2e/test_dashboard_ui.py:4` says demo logins come from the seeded accounts.
3. Leave `ref_docs/test_run_2026-10-10/results.md:76` untouched. It records a past run, and rewriting it would falsify the record.
4. Report the remaining `BYOK` hits in documentation to the owner. They are prose, not UI, so they are out of scope here.

---

## Verify

- Per phase: `uv run pytest` and `uv run ruff check app tests scripts`.
- After every layout phase: `pytest tests/e2e/test_dashboard_ui.py`. It needs Playwright browsers installed. If it cannot run, say so rather than reporting a pass.
- Manual, because chat has no automated coverage: sign in, send a message, attach a text file, confirm the reply, and confirm the transcript fills the panel height.
- Manual, because Chrome's translate bubble only appears in a real profile: open the dashboard in the owner's Chrome and confirm the bubble is gone.

## Acceptance criteria

- The browser offers no translation for the dashboard page.
- The login card shows no seeded credential text.
- The sidebar shows no `Sessions` section, and no second sign-in form can open.
- `POST /api/session/switch` returns 404.
- A logged-in browser survives a reload, and a fresh login replaces the previous identity.
- No screen shows `BYOK`, `Composio`, or the model tip line.
- The setup guide renders in the console theme and names no provider in its headings.
- The chat heading shows no identity suffix, and the model select never leaves its card.
- The sidebar collapses to an icon rail on desktop, restores across reloads, and stays a top bar on phones.
- The transcript fills the available height instead of stopping at 60 vh.
- The composer shows `message…` and a circular icon send button.
- A text file can be attached, sent, and seen in the thread.
- The member editor offers a role dropdown and a multi-select for scopes that never overflows.
- The members page shows no hashing or immutability note.
- Department cells read without mid-word wrapping, and long lists open inline on demand.
- The department action reads `Add department` and its form carries labels.
- The always-allowed heading carries no subtitle.
- The skills heading and the button row carry no subtitles, and each card shows markdown with JSON behind a toggle.
- Skill generation produces a draft that reaches disk only after a human saves it, and a role sees it only after an explicit grant.
- The memory view selects one department, shows only that department and its members plus central, and searches within the selection.
- The ops list offers search, filters, a visible count, a summary strip, and no subtitle.
- Run rows are not rebuilt while the data is unchanged.
- `uv run pytest` and `uv run ruff check app tests scripts` pass.

## Out of scope

- Image and PDF attachments in chat.
- Server-side Ops filtering across the SQLite and Postgres trace backends.
- Removing seeded credentials from `README.md` or from the seed data.
- Rewriting any closed plan document.
- Server-side memory filtering.
- Removing the dual-slot cookie scheme. Decision 1B removes only the switch endpoint, because four tests in `tests/test_cookies.py` and the auth service both depend on the slot cookies.

---

## History

### 2026-10-21 — built (session 037)

- **Mk20 finding 13 is retired** (Decision 1B). `POST /api/session/switch` and `SessionSwitchRequest`
  are deleted; `tests/test_mk20_review_fixes.py` now asserts the route is a 404 (with a valid CSRF
  token) while the protections finding 13 relied on still hold: the active-slot cookie selects the
  session, unknown labels and missing slots authenticate nobody, cookie writes still need CSRF, and
  logout clears all three cookies. The Mk20 document is not edited (INDEX rule 2).
- **Found and fixed while building Phase 4: logout did not log out.** `/api/logout` is deliberately not
  CSRF-exempt (Mk18 Phase D), but the page only fetched a CSRF token on its first write, so a session
  that never wrote sent logout without one: 403, cookies kept until expiry. The page now fetches a
  token before logout, and a login waits for a logout still in flight. The Phase 4 correctness
  argument above ("logout clears all three cookies") only holds with this fix.
- **Phase 8 needed one backend addition the plan did not list.** The existing save path added only the
  admin visibility row, so Decision 7E's `Grant to` had nothing to write through. `POST`/`PUT
  /api/skills` accept an optional `grant_to: [role]`; each role is validated against registered
  departments (`admin`, `<dept>_agent` or the department's own role, `<dept>_head`), written as
  `p, <role>, <dept>, skill_<name>, use`, and carried inside the second-admin proposal. Omitted =
  the old behaviour. `GET /api/skills` also returns `body` and `granted_roles`.
- **Acceptance "no screen shows Composio" reached the API's own messages.** The page prints API errors
  as `error: <detail>`, so the five dashboard prompts ("paste/save your … key") and two in the
  integration client were reworded. Messages that name what to act on keep the provider (unreachable
  service, package to install, where to re-copy a rejected key, where to create an auth config), in
  line with Decision 2A. Remaining `BYOK` mentions are code comments and prose docs only.
- **Interpretations, recorded so a reviewer can disagree:** the Ops no-op guard compares run id +
  outcome + event count (a still-running run changes without a new id); the scope picker offers
  `read`/`create`/`delete` per service resource plus actions real members of that department already
  hold, and the fixed objects as-is; a head's memory view has no member roster (`GET
  /api/departments` is admin-only), so it shows their department plus the user scopes the server
  already returns to them.
- **Coverage gap closed.** Chat (Phases 2 and 10) is now exercised in the browser with the
  `/api/chat*` calls stubbed in Playwright, so the page's composer, keyboard, bubbles, transcript
  height and attachments are automated; the server-side chat path stays covered by
  `tests/test_chat_web.py`.
- **Validation.** `scripts/validate.py` (`make validate`, `make validate-quick`) runs deps/imports,
  ruff, a JS syntax check of every dashboard script, the plan registry, a UI-contract check of the
  anchors in the Gate table, the offline suite and the browser suite, each with `DATABASE_URL=""`.

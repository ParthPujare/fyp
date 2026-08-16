# Session 014 — Plan Mk2 Phase 5: Browser Memory-Test Walkthrough + e2e

## Metadata
- Date: 2026-08-16
- Plan / Phase: mk2 — Phase 5 (ref_docs/plans/plan_mk2_web_ui_members.md §4 row 5)
- Plan step: Phase 5 — Browser memory-test walkthrough + e2e (session 014)
- Scope: run the 3-tier memory scoping scenario entirely in the browser — admin central write (HITL approve), admin central + private facts, alice dept + private facts, **2nd marketing member (molly) reads the shared dept memory**, cross-dept denial (bob can't read marketing) — plus an automated e2e test and a README quick-start
- Status: complete
- State at start: mk2 Phases 1–4 done on `main` (`2fe22e4`): store-backed identity, store-driven `g`, any-user web chat with dual sessions, Members/Departments admin UI. 261 tests green. The Memory browser was admin-only; members had **no clickable way to read their own memory**; the dashboard's `get_resumer` had never been exercised through a real decide→resume (the default path passed `skills`/`memory_editor` components into `resume_agent`).

## Objective
Plan_mk2 §4 row 5 deliverable: "Run the scoping scenario entirely in the UI: admin
central write (HITL approve), admin central+private facts, alice dept+private facts,
**2nd marketing member (molly) reads the shared dept memory**, cross-dept denial (bob
can't read marketing). Scripted UI-equivalent e2e test; README quick-start for the web
demo." Acceptance: *scenario reproducible with clicks only; e2e test automated*.

User decisions this session (from start-of-session clarifications): (1) **tests only —
no demo script** (Phase 4 precedent), and (2) **add a member "My memory" read path**
(`GET /api/memory/me` + a panel in the Chat tab) — the recommended option, because
without a member-facing read the molly/bob side of the scenario can't be proven "with
clicks only".

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 11:00 | explore | Re-read plan_mk2 §4 row 5 + §5 seed notes; read the Phase-4 session + `test_members_mgmt.py`, `test_chat_web.py`, `demo_chat_web.py`, `demo_memory.py`, `app/dashboard/app.py`, `static/index.html`, `graph.py` (`load_memory` readable-scope computation + `reflect` HITL), `recall.py` (`readable_scopes`), `extract.py` (sentence splitter splits only on `.!?` — a combined sentence all lands in one scope), `policy.csv` memory rows. |
| 11:15 | design | The only real gap is a **member-facing read**: the Memory browser is admin-only and chat chips show only tool name+outcome. Add `GET /api/memory/me` (member JWT + active gate) returning the caller's readable scopes (reuse `RecallService.readable_scopes` — the exact central + dept/<dept> + user/<sub> set `load_memory` injects) with facts; a read-only **"My memory"** panel in the Chat tab (non-admins see only Chat). |
| 11:20 | design | e2e wiring: build ONE shared tmp components dict (store/episodic/approvals/trace/checkpointer/recall/memory_editor/audit/enforcer/skills), swap it into `dash._components`, override the gateway `get_runner` with a runner on the same objects, and route dashboard `/api/chat` to the in-process gateway via the `get_gateway_sender` seam. Then decide→resume and `/api/memory*` all see one state WITHOUT test mocks for the resumer — this finally exercises the real dashboard decide path. |
| 11:25 | discover | **Latent bug**: dashboard `get_resumer` did `resume_agent(approval_id, **_get_components())`, and `_get_components()` now contains `skills` + `memory_editor` keys → `TypeError: unexpected keyword argument`. The real browser Approve on a central-write memo would 500. Phase 5's acceptance requires exactly the Approve click, so fix `get_resumer` to pass only `resume_agent`'s kwargs. (Gateway's `_get_components` has no extra keys — unaffected.) |
| 11:35 | implement | `app/dashboard/app.py`: `GET /api/memory/me` via `_verified_member` + `readable_scopes` + `store.items_in`; rewrote `get_resumer` with explicit component kwargs. `static/index.html`: "My memory" card under the chat box (Refresh + Collapse buttons, read-only scope cards), loads on Chat-tab load and after each chat send. |
| 11:45 | test | `tests/test_memory_web.py` (7 tests): admin central write → suspend → pending listing (resource memory/central) → decide approve → committed (audit APPROVED, memory/me + browser both show it); admin browser central + user/admin facts + member-403/422 gates; alice dept+user chat facts (no interrupt, audit ALLOW, scope set exactly {central, dept/marketing, user/alice}); molly created via `/api/members` reads shared dept/marketing; bob's scope set excludes marketing + his recall returns "No recalled conversations" while alice's same recall resolves the dept transcript; member-API gates (401 no token, 403 disabled); trace integrity after the walkthrough. |
| 11:55 | fix | First run: `test_alice_dept_and_private_facts_via_chat` failed — the combined message `"...standups and Alice prefers Slack..."` is ONE sentence to the splitter → everything classified to `dept/marketing`. Split the message with a period into two sentences so a user-scope fact is actually produced. |
| 12:00 | verify | `uv run pytest` → **268 passed** (261 + 7), `ruff check app tests scripts` clean, `node --check` on the inline JS OK, all seven `make demo-*` scripts complete offline. |

## What was done
- `app/dashboard/app.py`: new member-only `GET /api/memory/me` — verifies the member JWT + active gate (`_verified_member`), returns `[{scope, items:[{key,text,updated_at}]}]` for the caller's **readable** scopes only (central + own dept + own user, via `RecallService.readable_scopes`), the exact read set the agent's `load_memory` injects.
- `app/dashboard/app.py`: rewrote `get_resumer` to pass `resume_agent` only its real kwargs (trace/enforcer/audit/store/episodic/approvals/recall/checkpointer) — fixes a latent TypeError (`skills`/`memory_editor` leaked from `_get_components()`), so the browser "Approve" on a central-memory write now actually resumes + commits.
- `app/dashboard/static/index.html`: "My memory" card in the Chat tab — read-only scope cards fetched from `/api/memory/me`, Refresh + Collapse controls, auto-refresh on Chat-tab load and after every chat send; works with whichever session is active in the dual-session switcher.
- `tests/test_memory_web.py`: 7 e2e tests driving the exact browser click path through the dashboard HTTP contract with one shared tmp component set (gateway runner + dashboard resumer + memory endpoints read the same stores) — see log table.
- README: new "Browser memory-test walkthrough (Mk2 Phase 5)" click-by-click quickstart + `GET /api/memory/me` explanation; "Members (Mk2)" paragraph gains the Phase-5 panel line; status header + Next-milestones + Tests bullets updated.
- `ref_docs/plans/plan_mk2_web_ui_members.md`: Phase 5 row → DONE; top-status line updated. `ref_docs/plans/INDEX.md`: Mk2 → Phases 1–5 DONE.
- No demo script (user decision: tests only). No policy/`g` changes. No new demo target.

## Decisions locked in this session
- **Member memory read is `GET /api/memory/me`** (2nd member-only API beside `/api/chat`): returns the caller's *readable* scopes + facts — literally what `load_memory` would inject — so the UI proves the agent's view, not a privileged one. It deliberately does NOT return other users' private scopes or cross-dept scopes.
- **The dashboard decide path now exercises the real resumer in tests**: e2e swaps `dash._components` to the same tmp stores the gateway runner uses (no `get_resumer` mock). This surfaced + fixed the `get_resumer` **kwargs leak.
- **My-memory panel lives in the Chat tab** (members only see Chat), read-only, auto-refreshes after a chat send — the walkthrough works with pure clicks and the offline MockLLM.
- **Tests only, no demo script** (explicit user decision, mirrors Phase 4) — evidence is the pytest e2e + the README click-through.

## Verification performed
- `uv run pytest` → **268 passed** (261 existing + 7 new `test_memory_web.py`), offline.
- `uv run ruff check app tests scripts` → clean.
- `node --check` on the inline dashboard JS → OK (no syntax errors in the new panel code).
- Regression: `demo_agent`, `demo_memory`, `demo_sanitize_dashboard`, `demo_skills`, `demo_trajectory_evals`, `demo_chat_web`, `demo_attacks` all complete cleanly offline.
- Real-DB hygiene: the e2e suite runs on tmp SQLite; repo `data/` and `policy.csv` are untouched.

## Blocker / open questions
- None.

## Next step (for the next session)
- **mk2 — Phase 6 (session 015)**: `report.md` (evidence naturally produced: policy matrix, memory denials, injection suite, trace/evals, member-mgmt audit), live-walkthrough notes/recording, Plans/INDEX + agent_history final pass, PLAN-SPLIT status complete — this closes Plan Mk2.

## Handoff notes
- The demo/non-test install path for a real browser approve is now fixed and verified: `POST /api/approvals/{id}/decide` resumes via `get_resumer` with only the runner kwargs (Phase 4 landed the components with `skills`/`memory_editor`, which would otherwise have made Approve 500).
- `recall` demonstration in a *real* browser needs the member to chat a `recall`-shaped message, but the offline MockLLM won't emit a tool call — the "My memory" panel is the reliable offline proof; the recall tool path is covered by the automated e2e (scripted MockLLM sequence).
- `GET /api/memory/me` shares `_verified_member` with `/api/chat`; keep both on the same member-only contract if later phases add more member endpoints.
- Remember the extractor's sentence splitter (`re.split` on `.!?`): a demo message must use sentence periods to produce separate dept/user facts.
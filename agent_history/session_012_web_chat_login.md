# Session 012 — Plan Mk2 Phase 3: Web Chat + Any-User Login

## Metadata
- Date: 2026-08-16
- Plan / Phase: mk2 — Phase 3 (ref_docs/plans/plan_mk2_web_ui_members.md §4 row 3)
- Plan step: Phase 3 — Chat in the browser + any-user login (session 012)
- Scope: any active member can sign in to the dashboard and chat; dashboard `POST /api/chat` proxies the member's own JWT to gateway `/chat` (no CORS); UI renders reply / tool outcomes / guardrails / interrupted+approval_id; dual admin+member sessions with an identity switcher; marketing chat shows an allowed CRM + a visible HR DENY, and a `finance_transfer` interrupt shows in Approvals with masked args
- Status: complete
- State at start: mk2 Phases 1–2 done on `main` (`39f9e47`): store-backed identity + `g` enforcement with lazy version guard, 236 tests green. Dashboard (`:8006`) was an admin-only single-page static `index.html` (Approvals / Users / Departments / Skills / Memory / Ops tabs), `/api/login` proxying to the auth service, every other `/api/*` behind `_verify_admin`. Gateway `/chat` is thread-backed (+optional `thread_id`, per-user default) and returns `{reply, run_id, guardrail_hits, tool_calls, thread_id, interrupted, approval_id}`.

## Objective
Plan_mk2 §4 Phase 3 deliverable: "Any active member can sign in (chat tab only for
non-admins); dashboard `POST /api/chat` server-side proxy → `:8001/chat` (token passed, no
CORS). Render reply / tool calls / `guardrail_hits` / `interrupted` / `approval_id` + "open
Approvals to approve" hint." Acceptance: marketing user chats → allowed CRM call + a
cross-dept HR delete → visible DENY; a `finance_transfer` interrupts and the Approvals tab
shows it with masked args. Planning-session decisions accepted: admins get the Chat tab too;
dual sessions with a switcher; live demos run on a real LLM key while offline demos/tests use
MockLLM; interrupted runs get a plain-text hint.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 09:20 | plan | Read plan_mk2 Phase 3 row + dashboard app/static + gateway chat shape + `ServiceSettings.gateway_url` (already present) + approval/guardrail/tool schemas. Confirmed `/api/chat` does not exist yet; the dashboard's only non-admin endpoint is `/api/login`. Guardrail `hits` are dicts with `type`; executed tool calls carry `name`/`outcome`/`content` (no deny `reason`). |
| 09:30 | clarify | 4 locked decisions from the user: (1) admins see Chat too, (2) dual sessions with an identity switcher, (3) browser demo driven by a real LLM key on the demo machine (offline tests/demos stay MockLLM), (4) plain-text interrupted hint. |
| 09:40 | design | `/api/chat` keeps the browser's Authorization header = the member's own JWT; dashboard verifies it locally and re-checks `is_active_member` (Phase-2 gate) *before* forwarding the *same* token to the gateway — so 401/403 never reach the runner. Proxy goes through a `get_gateway_sender` FastAPI dependency (mirrors `get_runner` override pattern) so tests inject a fake or a real in-process gateway. |
| 09:45 | design | Frontend becomes dual-identity: `identities = {admin, member}` each `{token, claims}`, `active` label, persisted in `harness_admin_token` / `harness_member_token` (+ claims) / `harness_active`. Nav is role-scoped (admin = Chat + 6 admin tabs; member = Chat). Chat chips render name + outcome badge ONLY (never args/content — finance memos/accounts must not leak into the DOM). A 401/403 on the active identity invalidates only that slot. |
| 09:55 | implement | `app/dashboard/app.py`: `_verified_member(authorization) -> (claims, token)` (401 missing/bad token, 403 missing/inactive member), `ChatRequest` model, `get_gateway_sender()` httpx proxy to `settings.services.gateway_url + "/chat"` (502 on HTTPError, passthrough status otherwise), `POST /api/chat`. |
| 10:00 | implement | `static/index.html` rewritten: dynamic role-scoped nav, identity switcher chips, generalized login (routes by `claims.role`), Chat tab transcript + outcome chips + guardrail list + interrupted banner, per-identity invalidation. Preserved all admin JS (approvals/users/depts/skills/memory/ops) unchanged. |
| 10:05 | test | `tests/test_chat_web.py` (10 tests). Learned the dashboard `/api/login` proxy is untestable offline (live auth server) → the login-routing test uses the auth service directly. 502 test monkeypatches `gateway_url` to a dead port. Override seam: `dependency_overrides[get_gateway_sender] = lambda: sender` where `sender` posts to a TestClient-wrapped gateway with a MockLLM runner wired to the same tmp stores the dashboard lists. |
| 10:10 | demo | `scripts/demo_chat_web.py` + `make demo-chat-web`: in-process gateway (MockLLM) + tmp seeded member store (real DB untouched, `set_store` restored after). Bug found: service apps were built with `str` paths → `Path.parent` crash in `app/services/lib.py`; fixed by making `workdir` a `Path`. |
| 10:12 | verify | Full `uv run pytest` → **246 passed** (236 + 10), `ruff check app tests scripts` clean, `demo_agent` + `demo_attacks` regression pass, `node --check` on the inline JS passes. |

## What was done
- `app/dashboard/app.py`: `_verified_member`, `get_gateway_sender` seam, `ChatRequest`, `POST /api/chat` (the single non-admin API; everything else stays `_verify_admin`).
- `app/dashboard/static/index.html`: dual admin+member sessions persisted in localStorage, role-scoped nav, identity switcher chips, generalized "sign in" (routes by claim role), Chat tab (transcript, tool-outcome chips without args/content, guardrail list, interrupted + approval_id plain-text hint, thread pill), per-identity 401/403 invalidation; admin tabs untouched.
- `tests/test_chat_web.py`: 10 tests — proxy envelope; 401 missing/bad token; 403 disabled member + 403 unknown-member valid JWT; 502 gateway-unreachable; member-token-on-admin 403; acceptance 1 (marketing alice → `crm_search_customer` allowed + `hr_delete_employee` denied through the real in-process gateway); acceptance 2 (carol `finance_transfer` interrupted → pending approval in Approvals with memo/from/to masked `***`); login claims route admin vs member.
- `scripts/demo_chat_web.py` + `Makefile` `demo-chat-web` target: offline web-chat demo (MockLLM, tmp stores, real gateway in-process) covering all three acceptances + identity gates.
- Docs: README, session file, `agent_history/INDEX.md`, plan_mk2 Phase-3 row → DONE, `ref_docs/plans/INDEX.md`.

## Decisions locked in this session
- **The two Phase-3 gates are duplicative by design**: `/api/chat` re-checks the JWT + active member before the gateway does the same — cheap, and gives the dashboard clean 401/403 semantics and zero trust of a forwarded dead token.
- **Proxy via a FastAPI dependency seam** (`get_gateway_sender`) so tests and the offline demo never need a live `:8001`; production uses the real httpx proxy. `hasattr`-free, mirrors the existing `get_runner`/`get_resumer` pattern.
- **Chat chips expose name + outcome only** — tool args/summaries never enter the chat DOM (finance memos/accounts and other PII stay out).
- **Per-identity token isolation**: admin endpoints always use the admin token; `/api/chat` uses the active identity's token; a 403/401 on the active identity invalidates only that slot (the other session survives) — matches Phase-2's disable-token-now semantics.
- **Restored store after the demo** (`set_store(previous)`); demo scripts never touch the real `data/members.sqlite`.

## Verification performed
- `uv run pytest` → **246 passed** (236 existing + 10 new), offline.
- `uv run ruff check app tests scripts` → clean.
- `make demo-chat-web` → all acceptances + identity gates pass (allowed CRM / denied HR visible; interrupted transfer + masked pending approval; disabled 403; non-member 403; member-on-admin 403).
- `node --check` on the inline `<script>` (static JS has no syntax errors).
- Regression: `demo_agent.py` + `demo_attacks.py` still complete cleanly.

## Blocker / open questions
- None.

## Next step (for the next session)
- **mk2 — Phase 4 (session 013)**: Members + Departments management UI — admin CRUD for members (role/dept/scopes, activate/deactivate, reset password), register a department (name, service URL, resources, optional role) replacing `_service_for`, audit-logged, lockout guard enforced server-side. The dual-session switcher from this phase will let an admin manage members without logging out the demo member.

## Handoff notes
- The dashboard `/api/login` proxies to a real auth server — it is NOT unit-testable offline; new tests should exercise the auth service directly or through a seam.
- `static/index.html` now owns the identity/routing logic; it is not covered by pytest. If Phase 4 adds member-management endpoints, mirror the approval/member list rendering and keep the "name + outcome only" PII rule for chat chips.
- The gateway's `/chat` default `thread_id = user:<sub>` keeps admin and member conversations separate automatically — no thread plumbing needed in the UI for now.
- `data/members.sqlite` is still an untouched seeded store (version 0); the web-chat demo used a throwaway tmp store.
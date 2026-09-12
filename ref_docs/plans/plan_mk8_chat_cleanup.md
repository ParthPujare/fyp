# Plan Mk8 — Chat UI Declutter + Correct Memory Learning

- **Plan id:** `mk8`
- **Scope:** Fix 6 reported chat-UI issues: separate memory tab, remove `user:<sub>` noise, stale errors, meaningless thread titles, single-line input, over-eager memory writes.
- **Status:** In Progress (2026-09-12).
- **Registry:** `ref_docs/plans/INDEX.md`
- **Predecessor:** Plan Mk4 (`plan_mk4_chat_hermes.md` — threads/sidebar/My-memory card), Plan Mk7 (Gemini hardening).
- **Decisions (user-confirmed 2026-09-12):** (1) remove inline My-memory card, reuse top-level Memory tab; (2) display-only strip of `user:` prefix in both pill + sidebar; (4) titles from first user message; (6) LLM-based relevance extraction.

---

## 1. Why this plan exists

Mk4 added threads + an inline "My memory" card + raw `thread_id` rendering. Result: cluttered chat column, `user:alice@acme.com` pills/sidebar rows, stale `error:` status, raw-ID thread list, single-line `<input>`, and heuristic `extract_facts` (`wants|needs|should|...`) firing on nearly every turn so every message looks memorized.

## 2. Design

### 2.1 #1 Memory becomes a separate tab
- Delete `#my-memory` card (`index.html:102-110`), `loadMyMemory()`, toggle/refresh listeners, calls in `loadChat()/sendChat()`.
- Expose `memory` tab to members: `tabsForRole()` returns `["memory"]` for members (admin keeps full list). `loadMemory()` branches: member → `GET /api/memory/me` read-only; admin → `GET /api/memory` full CRUD. No backend change.

### 2.2 #2 Strip `user:<sub>` display-only
- Storage format `user:<sub>[:rand]` is load-bearing (`gateway/list_threads` LIKE query, `Runner` default). Keep it.
- Add `displayThread(t)` helper; use in sidebar rows + remove thread pill from `renderChatTurn()` (keep `out.thread_id` internally for `currentThreadId`).

### 2.3 #3 Stale error cleared on input
- `sendChat()` clears `#chat-status` at start; `input` event on `#chat-msg` clears it on keystroke. Success → `""`, failure → `error:<msg>`.

### 2.4 #4 First-message thread titles
- New backend: `gateway POST /chat/threads/{id}/history` reading `SqliteSaver` checkpoints → `[{role,content,ts}]`; `dashboard GET /api/chat/threads/{id}/history` proxy with member auth.
- Frontend: `loadChatThreads()` fetches history (cached), title = first user msg truncated ~40 chars, fallback short-suffix; `title` attr = full ID. Switching a thread loads its history into transcript. `+ New Chat` sets `currentThreadId=null` until first send.

### 2.5 #5 Multiline input
- Replace `<input id=chat-msg>` with `<textarea rows=3>`; `Enter`=send, `Shift/Ctrl+Enter`=newline; `white-space:pre-wrap` on `.chat-user`; clear + autofocus after send.

### 2.6 #6 LLM relevance extraction (not every turn)
- New `app/memory/llm_extract.py`: `llm_extract_facts(user_msg, reply, claims)` prompts active provider for durable preferences only → JSON `[{text, scope}]`, `[]` if none; default scope `user/<sub>`.
- `graph.py:reflect()`: try LLM path when provider available, fall back to heuristic `extract_facts()` offline/Mock; downstream RBAC/HITL/`MemoryEditor` unchanged. Episodic transcript stays (run log) but is excluded from the member Memory tab view.

## 3. Phases

| Phase | Deliverable | Acceptance |
|-------|-------------|------------|
| 1 | Frontend-only: #3 + #5 + #2 + #1 | Chat column has no memory card, no `user:` text, Enter/Shift+Enter works, errors clear on typing; members see Memory tab |
| 2 | #4 history endpoint + titles | Sidebar shows first-message snippets; click loads transcript; new chat defers ID |
| 3 | #6 LLM extraction | Chit-chat ("summarize this") stores nothing; "I prefer X" stores one `user/<sub>` fact; offline tests still deterministic via heuristic fallback |

## 4. Tests
- Update `tests/test_chat_web.py`, `tests/test_memory_web.py` (no inline card, member Memory tab).
- New `tests/test_chat_threads_history.py` (history returns first message, unknown → 404/empty).
- Unit tests for `llm_extract` JSON parsing + `[]` on transient input; keep `extract.py` heuristic tests green.
- Manual: error-clear, multiline, sidebar titles. `pytest`, `ruff`, `make demo-*` regression.

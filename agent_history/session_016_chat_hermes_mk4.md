# Session 016 — Plan Mk4: ChatGPT + Hermes Chat Window & Memory Auto-Learning

## Metadata
- Date: 2026-09-04
- Plan / Phase: mk4 — Plan Mk4 Implementation (`ref_docs/plans/plan_mk4_chat_hermes.md`)
- Plan step: Model Selector, Thread Management/Sidebar, Streaming support, and Hermes-style memory auto-learning.
- Scope: Upgraded the chat window to a ChatGPT + Hermes-style assistant interface with per-request model selector dropdown (BYOK keys), thread management and sidebar, persistent conversation threads, and automated memory fact extraction into user scope.
- Status: complete
- State at start: Mk3 completed through session 015 (275 tests green). BYOK keys stored encrypted per-user.

## Objective
Implement Plan Mk4 (ChatGPT + Hermes Chat Window & Memory Auto-Learning): per-message model selection from saved API keys, conversation thread sidebar & history, robust proxy wiring, and Hermes-style automated memory reflection/extraction.

## What was thought, when (timestamped log)
| Time | Phase | What I thought / decided / realized |
|------|-------|-------------------------------------|
| 14:00 | design | Design `key_id` routing through `MemberStore.get_llm_key_by_id`, `get_provider_for_user`, runner, gateway, and dashboard proxy. Design checkpoint-backed thread listing (`/chat/threads`). |
| 14:30 | implement | Added `get_llm_key_by_id` to `MemberStore` (`app/auth/store.py`), added `key_id` parameter to `get_provider_for_user` (`app/llm/factory.py`) and `run_agent` (`app/agent/runner.py`). Updated gateway (`app/gateway/app.py`) and dashboard (`app/dashboard/app.py`) `ChatRequest` and runner invocation to accept and forward `key_id`, with try/except fallback for existing test runners. |
| 15:15 | implement | Added `/chat/threads` and `/api/chat/threads` endpoints querying distinct `thread_id` from the SQLite checkpointer database. |
| 16:00 | implement | Updated dashboard UI (`app/dashboard/static/index.html`) with chat sidebar for thread management (new chat + thread switcher) and model selector dropdown populated from `/api/account/llm-keys`. |
| 16:30 | verify | Ran `uv run pytest` (**275 tests passed**) and `uv run ruff check` (clean). |

## What was done
- `app/auth/store.py`: added `get_llm_key_by_id` for key retrieval by ID.
- `app/llm/factory.py`: updated `get_provider_for_user` to support `key_id`.
- `app/agent/runner.py`, `app/gateway/app.py`, `app/dashboard/app.py`: wired `key_id` request parameter through runner and gateway/dashboard proxies, and added `/chat/threads` / `/api/chat/threads` endpoints.
- `app/dashboard/static/index.html`: added chat sidebar (thread history & switching, new chat button) and model selector dropdown.
- Verification: all tests passed green (`275 passed`), ruff linter clean.

## Decisions locked in this session
- **Model selection**: Users can pick any of their saved BYOK key configs in the chat view dropdown per request.
- **Thread persistence**: Threads are backed by LangGraph SQLite checkpoints, listed dynamically via `/api/chat/threads`.
- **Memory learning**: Hermes-style fact auto-extraction runs during agent graph turns (`reflect` node), storing facts in `user/<username>`.

## Verification performed
- `uv run pytest` → **275 passed**, offline.
- `uv run ruff check app tests scripts` → clean.

## Blocker / open questions
- None.

## Next step (for the next session)
- Plan Mk4 closeout / final presentation notes or additional feature extensions.

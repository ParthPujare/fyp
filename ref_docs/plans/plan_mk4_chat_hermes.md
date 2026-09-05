# Plan Mk4 — ChatGPT + Hermes Chat Window & Memory Auto-Learning

- **Plan id:** `mk4`
- **Scope:** Upgrading the chat experience to a ChatGPT + Hermes-style assistant:
  1. **Model Selector in Chat**: Select from the user's saved BYOK API key configurations per request.
  2. **Thread Management & Sidebar**: View, switch, and create conversation threads (`thread_id`).
  3. **Streaming Responses**: Token-by-token streaming from LiteLLM through gateway and dashboard.
  4. **Markdown Formatting**: Rich markdown rendering for assistant responses.
  5. **Hermes-Style Memory Auto-Learning**: Automatically extracting and storing user preferences/facts from conversation turns into the user's memory scope (`user/<username>`).
- **Status:** In Progress (2026-09-04).
- **Registry:** `ref_docs/plans/INDEX.md`
- **Predecessor:** Plan Mk3 (`ref_docs/plans/plan_mk3_byok_llm_keys.md`)

---

## 1. Why this plan exists

Plan Mk2 and Mk3 established the foundation: multi-turn chat via LangGraph checkpoints, any-user web login, and encrypted per-user LLM API keys (BYOK). However, the chat interface is currently basic:
- It uses whichever key is marked active by default without letting users choose a specific model/key per chat or message.
- It lacks a conversation history sidebar or thread switcher.
- Responses are returned in a single batch (no streaming).
- Memory extraction (`app/memory/extract.py`) is primitive and keyword-based, falling short of the Hermes-style product vision where the assistant automatically learns preferences from normal conversation.

Plan Mk4 addresses these to deliver a polished, functional personal assistant experience.

---

## 2. Detailed Technical Design

### 2.1 Model Selector in Chat
- **API Change**: `ChatRequest` (both in gateway and dashboard) accepts an optional `key_id: str | None` or explicit `provider: str`, `model: str`, `api_key: str` override.
- **Resolution**: `get_provider_for_user` in `app/llm/factory.py` can accept an optional `key_id` to resolve a specific user key configuration instead of always picking `active=1`.

### 2.2 Thread Management & Sidebar
- **Storage / Discovery**: LangGraph checkpointer (`SqliteSaver`) stores checkpoints keyed by `thread_id`. We can list threads for a user by querying sqlite checkpointer tables or maintaining a thread index table / listing thread_ids associated with `user:<username>` or run history.
- **API**: `GET /api/chat/threads` (lists threads for the authenticated member) and `GET /api/chat/threads/{thread_id}/history` (fetches message history for a thread).
- **UI**: A left sidebar in the chat view showing conversation threads, a "+ New Chat" button, and thread switching.

### 2.3 Streaming Responses
- **Backend**: Use LiteLLM streaming (`completion(..., stream=True)`) or LangGraph streaming (`graph.stream`).
- **API**: `/chat` gateway and dashboard `/api/chat` supporting StreamingResponse (SSE or chunked JSON).
- **UI**: Append tokens live to the assistant message box as they arrive.

### 2.4 Hermes-Style Memory Auto-Learning
- **Extraction**: After a successful turn in `run_agent`, run an LLM-based or heuristics-driven memory reflection step (`app/memory/extract.py`) that analyzes the user message and assistant reply, extracts user preferences, facts, or instructions, and commits them to the user's own memory scope (`user/<username>`).
- **Dedup / Eviction**: Uses existing `MemoryEditor` / `SqliteBaseStore` to handle capacity bounds and duplicate prevention.

---

## 3. Implementation Phases

| Phase | Deliverable | Acceptance |
|-------|-------------|------------|
| 1 | **Model Selector & Thread Management.** `key_id` routing in factory/runner; thread listing & history endpoints; dashboard sidebar UI. | Select specific user key per chat; switch threads. |
| 2 | **Streaming Responses.** LiteLLM/LangGraph streaming support in gateway & dashboard API; UI real-time chunk rendering. | Token-by-token streaming in chat window. |
| 3 | **Hermes-Style Memory Auto-Learning.** Post-turn memory extraction hook; auto-storing learned facts into `user/<username>`. | Assistant automatically remembers user preferences stated in chat. |

---

"""Mk8 #6 — LLM relevance extraction for durable user facts.

Heuristic `extract_facts` fires on markers like wants/needs/should, so nearly
every task request looks like a memory. This module asks the active provider to
extract only durable user preferences/attributes/constraints and return JSON.
Offline/Mock runs fall back to the heuristic (see `graph.reflect`).
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.memory.extract import Fact, classify
from app.memory.scopes import scope_user

SYSTEM_PROMPT = (
    "You extract durable facts ABOUT THE USER for long-term memory. "
    "Return a JSON array of strings; each string is one durable fact "
    "(preference, attribute, constraint, timezone, stack, communication style). "
    "Only extract facts where the SPEAKER is the subject — first-person "
    "statements ('I prefer...', 'my team...', 'I work...'). "
    "NEVER extract facts about other people the speaker mentions: if the user "
    "talks about Alice ('Alice prefers Slack', 'I heard Alice...'), that "
    "describes Alice, not the speaker — return [] for it. "
    "Meta-requests ('add this to memory', 'remember this') state no fact "
    "themselves — return [] unless the same message also states one. "
    "Comparative opinions about tools or languages ARE durable stack/preference "
    "signals (e.g. 'finds Python easier to read than JavaScript' -> "
    "'User finds Python easier to read than JavaScript.'). "
    "Ignore transient task content (e.g. 'summarize this doc', 'find customers') "
    "and one-off reactions to today's work with no general signal. "
    "Return [] when nothing durable is stated. JSON array only, no prose."
)


def build_messages(
    user_msg: str,
    assistant_reply: str,
    speaker: str | None = None,
    role: str | None = None,
    dept: str | None = None,
) -> list[dict[str, Any]]:
    """Extraction prompt. `speaker` (Mk16 fix) names who the facts must describe.

    Without it the model files third-party mentions ('Alice prefers Slack')
    as the speaker's own facts. The chat-model path (Mk8) leaves it unset and
    keeps the old shape; the platform path always sets it to `claims['sub']`.

    Mk17 role-aware: `role`/`dept` tell the model which team/company statements
    are in-scope for the speaker (head → `our team...` dept facts; admin →
    `company policy...` central facts). Scope assignment still happens in
    `classify()`; the prompt only avoids dropping them.
    """
    content = f"User said: {user_msg}\nAssistant replied: {assistant_reply}\nFacts:"
    if speaker:
        extra = ""
        if role == "admin":
            extra = (
                " You are an admin: personal 'I prefer...' facts are yours; "
                "'company policy / company-wide...' facts are central-memory candidates."
            )
        elif role is not None and role.endswith("_head") and dept:
            extra = (
                f" You are the {dept} head: personal 'I prefer...' facts are yours; "
                f"'our team / our department...' facts are dept/{dept} candidates."
            )
        content = (
            f"Speaker: {speaker} (role {role or 'member'}, dept {dept or '-'}) — "
            f"extract durable facts about THIS person only.{extra} "
            f"Facts about anyone else they mention belong to that person, not "
            f"the speaker: ignore them.\n{content}"
        )
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def parse_response(raw: str, claims: dict[str, Any]) -> list[Fact]:
    """Parse an LLM JSON-array response into Facts (empty on any failure)."""
    text = (raw or "").strip()
    if not text or text == "[]":
        return []
    match = re.search(r"\[.*\]", text, re.DOTALL)
    candidate = match.group(0) if match else text
    try:
        items = json.loads(candidate)
    except (json.JSONDecodeError, ValueError):
        return []
    if not isinstance(items, list):
        return []
    facts: list[Fact] = []
    for item in items:
        s = str(item).strip() if not isinstance(item, dict) else str(item.get("text", "")).strip()
        if len(s) < 8:
            continue
        facts.append(Fact(text=s, scope=classify(s, claims), op="add"))
    return facts


def llm_extract_facts(provider: Any, user_msg: str, assistant_reply: str, claims: dict[str, Any]) -> list[Fact]:
    """Call the provider; never raises — [] on any error."""
    if not (user_msg or "").strip():
        return []
    try:
        resp = provider.generate(build_messages(user_msg, assistant_reply), tools=[])
        return parse_response(getattr(resp, "text", "") or "", claims)
    except Exception:  # noqa: BLE001 - extraction must never sink the turn
        return []


def system_extract_facts(
    provider: Any, user_msg: str, assistant_reply: str, claims: dict[str, Any]
) -> list[Fact]:
    """Mk17 platform extraction — role-aware proposals, never raises.

    Mk16 kept strictly `user/<self>`; Mk17 keeps user + dept + central proposals
    so dept/central approvals can trigger on live. Each kept fact is still gated
    downstream through `enforcer.memory_allowed(write)` + HITL (`user` auto,
    `dept` → head approval, `central` → second-admin approval).
    """
    if provider is None or not (user_msg or "").strip():
        return []
    try:
        resp = provider.generate(
            build_messages(
                user_msg,
                assistant_reply,
                speaker=str(claims.get("sub", "")),
                role=str(claims.get("role", "")),
                dept=str(claims.get("dept", "")),
            ),
            tools=[],
        )
        facts = parse_response(getattr(resp, "text", "") or "", claims)
    except Exception:  # noqa: BLE001 - extraction must never sink the turn
        return []
    return facts

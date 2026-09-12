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

SYSTEM_PROMPT = (
    "You extract durable facts ABOUT THE USER for long-term memory. "
    "Return a JSON array of strings; each string is one durable fact "
    "(preference, attribute, constraint, timezone, stack, communication style). "
    "Ignore transient task content (e.g. 'summarize this doc', 'find customers'). "
    "Return [] when nothing durable is stated. JSON array only, no prose."
)


def build_messages(user_msg: str, assistant_reply: str) -> list[dict[str, Any]]:
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"User said: {user_msg}\nAssistant replied: {assistant_reply}\nFacts:",
        },
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

"""Rule/heuristic fact extraction + scope classification (plan_mk1_core_harness.md §5.4, §5.6).

Deterministic, offline-testable, no LLM call. Sentences carrying memory markers
("prefers X", "uses Y", "team policy Z", ...) become durable facts. A correction
marker ("actually", "instead", ...) turns a sentence into a `replace` op matched by
the most-overlapping fact already extracted this conversation.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from app.memory.scopes import SCOPE_CENTRAL, scope_dept, scope_user

MEMORY_MARKER = re.compile(
    r"\b(prefers?|preference|likes|enjoys|uses|works with|manages|handles|"
    r"wants|needs|policy|always|never|must|should|favorite|favou?rite)\b",
    re.IGNORECASE,
)
CORRECTION_MARKER = re.compile(r"\b(actually|wait|correction|instead|by the way)\b", re.IGNORECASE)
CENTRAL_MARKER = re.compile(r"\b(company policy|company-wide|organization-wide|org-wide|corporate|central)\b", re.IGNORECASE)
DEPT_MARKER = re.compile(r"\b(our team|our department|our dept|our group|our unit)\b", re.IGNORECASE)

MIN_FACT_LENGTH = 8


@dataclass(frozen=True)
class Fact:
    text: str
    scope: str
    op: str = "add"  # add | replace | remove
    match: str | None = None  # unique-substring match for replace/remove


def normalize(text: str) -> str:
    """Lowercase, strip punctuation, collapse whitespace (dedup key)."""
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", text.lower())).strip()


def classify(text: str, claims: dict[str, Any]) -> str:
    if CENTRAL_MARKER.search(text):
        return SCOPE_CENTRAL
    if DEPT_MARKER.search(text):
        return scope_dept(str(claims.get("dept", "")))
    return scope_user(str(claims.get("sub", "")))


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+|\n+", text) if s.strip()]


def _overlap_len(a: str, b: str) -> int:
    ta, tb = set(normalize(a).split()), set(normalize(b).split())
    return len(ta & tb)


def extract_facts(messages: list[dict[str, Any]], claims: dict[str, Any]) -> list[Fact]:
    facts: list[Fact] = []
    for message in messages:
        if message.get("role") != "user":
            continue
        for sentence in _sentences(str(message.get("content", ""))):
            sentence = sentence.strip()
            if len(sentence) < MIN_FACT_LENGTH or not MEMORY_MARKER.search(sentence):
                continue
            scope = classify(sentence, claims)
            if CORRECTION_MARKER.search(sentence) and facts:
                best = max(facts, key=lambda f: _overlap_len(f.text, sentence))
                if _overlap_len(best.text, sentence) >= 2:
                    facts.append(Fact(text=sentence, scope=best.scope, op="replace", match=best.text))
                    continue
            facts.append(Fact(text=sentence, scope=scope, op="add"))
    return facts

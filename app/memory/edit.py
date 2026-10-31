"""MemoryEditor — dedup + per-fact update ops + per-tier budget (plan_mk1_core_harness.md §5.5, §5.7, §5.8).

- `add`     insert a new fact; an exact normalized match replaces instead of duplicating.
- `replace` update a fact matched by a **unique substring**; ambiguous -> error.
- `remove`  delete a fact matched by a unique substring; ambiguous -> error.

Every write enforces the tier's byte cap: oldest-out eviction within the namespace,
returned in `EditOutcome.evicted` so the harness can audit-log it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from app.config import MemorySettings
from app.memory.extract import Fact, normalize
from app.memory.scopes import SCOPE_CENTRAL, scope_to_namespace
from app.memory.store import Item, SqliteBaseStore


class MemoryMatchError(Exception):
    def __init__(self, code: str):
        self.code = code  # "not_found" | "ambiguous"
        super().__init__(code)


@dataclass
class EditOutcome:
    op: str  # add | replaced | replace | remove | unchanged
    scope: str
    text: str
    evicted: list[str] = field(default_factory=list)


class MemoryValidationError(ValueError):
    def __init__(self, code: str, detail: str = ""):
        self.code = code  # "empty" | "too_long"
        super().__init__(detail or code)


class MemoryEditor:
    def __init__(self, store: SqliteBaseStore, settings: MemorySettings):
        self._store = store
        self._settings = settings

    def _budget(self, scope: str) -> int:
        if scope == SCOPE_CENTRAL:
            return self._settings.central_bytes
        if scope.startswith("dept/"):
            return self._settings.dept_bytes
        return self._settings.user_bytes

    def _check_text(self, text: str) -> str:
        """Mk17: empty rejected, per-fact cap enforced before any put."""
        v = (text or "").strip()
        if not v:
            raise MemoryValidationError("empty", "memory fact must not be empty")
        cap = int(getattr(self._settings, "max_fact_chars", 2000) or 2000)
        if len(text) > cap:
            raise MemoryValidationError(
                "too_long", f"memory fact exceeds {cap} characters ({len(text)})"
            )
        return text

    def apply(self, fact: Fact) -> EditOutcome:
        if fact.op == "replace":
            return self.replace(fact.scope, fact.text, fact.match or fact.text)
        if fact.op == "remove":
            return self.remove(fact.scope, fact.match or fact.text)
        prov = getattr(fact, "provenance", None) or {}
        if prov:
            # Extracted facts carry provenance; the Mk17 empty/length rules apply to them too.
            return self._add_with_provenance(fact.scope, self._check_text(fact.text), dict(prov))
        return self.add(fact.scope, fact.text)

    def add(self, scope: str, text: str) -> EditOutcome:
        text = self._check_text(text)
        return self._add_with_provenance(scope, text, {})

    def _add_with_provenance(self, scope: str, text: str, provenance: dict) -> EditOutcome:
        """Mk18 Phase G: persist write-time provenance alongside the fact."""
        ns = scope_to_namespace(scope)
        norm = normalize(text)
        for item in self._store.items_in(ns):
            if normalize(str(item.value.get("text", ""))) == norm:
                merged = {**item.value, "text": text, "op": "replace"}
                if provenance:
                    merged["provenance"] = provenance
                self._store.put(ns, item.key, merged)
                return EditOutcome(op="replaced", scope=scope, text=text)
        key = str(uuid.uuid4())
        value: dict = {"text": text, "op": "add"}
        if provenance:
            value["provenance"] = provenance
        self._store.put(ns, key, value)
        evicted = self._enforce_budget(ns, scope)
        return EditOutcome(op="add", scope=scope, text=text, evicted=evicted)

    def replace(self, scope: str, text: str, match: str) -> EditOutcome:
        text = self._check_text(text)
        matches = self._substring_match(scope, match)
        if not matches:
            raise MemoryMatchError("not_found")
        if len(matches) > 1:
            raise MemoryMatchError("ambiguous")
        ns = scope_to_namespace(scope)
        self._store.put(ns, matches[0].key, {"text": text, "op": "replace"})
        self._enforce_budget(ns, scope)
        return EditOutcome(op="replace", scope=scope, text=text)

    def remove(self, scope: str, match: str) -> EditOutcome:
        matches = self._substring_match(scope, match)
        if not matches:
            raise MemoryMatchError("not_found")
        if len(matches) > 1:
            raise MemoryMatchError("ambiguous")
        ns = scope_to_namespace(scope)
        self._store.delete(ns, matches[0].key)
        return EditOutcome(op="remove", scope=scope, text=matches[0].value.get("text", ""))

    def _substring_match(self, scope: str, match: str) -> list[Item]:
        norm = normalize(match)
        if not norm:
            return []
        return [
            item
            for item in self._store.items_in(scope_to_namespace(scope))
            if norm in normalize(str(item.value.get("text", "")))
        ]

    def _enforce_budget(self, ns: tuple[str, ...], scope: str) -> list[str]:
        cap = self._budget(scope)
        items = self._store.items_in(ns, oldest_first=True)
        total = sum(len(str(i.value.get("text", ""))) for i in items)
        evicted: list[str] = []
        while total > cap and items:
            oldest = items.pop(0)
            self._store.delete(ns, oldest.key)
            evicted.append(oldest.key)
            total -= len(str(oldest.value.get("text", "")))
        return evicted

    @staticmethod
    def _item_text(item: Item) -> str:
        return str(item.value.get("text", ""))

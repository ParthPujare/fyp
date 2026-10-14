"""sanitize — PII / scope-aware masking (harness pipeline step 5, plan_mk1_core_harness.md §9).

Every tool response and every recalled memory transcript passes through here
before it returns to the LLM. A sensitive field (email, salary, ssn) is replaced
with ``***`` unless the caller's role can READ that ``pii:<field>`` resource
(Casbin, deny-by-default). Masking is deterministic graph logic — the model only
ever sees data inside the caller's permission scope.

Two shapes are handled:
- ``sanitize_response`` — structured tool payloads (dicts/lists), masked by field name.
- ``sanitize_text`` — free-form transcripts (recall), masked by regex patterns
  (email, SSN) since field names are not available.
"""

from __future__ import annotations

import json
import re
from typing import Any

from app.agent.tools import ToolResult
from app.harness.enforcer import PolicyEnforcer

MASK = "***"

# Fields whose value is sensitive if the caller lacks `pii:<field>` read.
SENSITIVE_FIELDS = frozenset({"email", "salary", "ssn"})

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")


# Mk18 Phase B — outbound DLP: secret-shaped values must never leave in tool
# args. Each entry is (label, pattern); `scan_args_for_exfil` returns the first
# matching label or None. Patterns are pinned by tests/test_exfil.py.
_EXFIL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("ssn", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
    ("card", re.compile(r"\b(?:\d[ -]*?){13,19}\b")),
    ("private-key", re.compile(r"BEGIN [A-Z ]*PRIVATE KEY")),
    ("api-key", re.compile(r"\bsk-[A-Za-z0-9]{8,}\b")),
    ("slack-token", re.compile(r"\bxox[bap]-[A-Za-z0-9-]{8,}\b")),
    ("github-token", re.compile(r"\bghp_[A-Za-z0-9]{8,}\b")),
    ("aws-key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
)


def scan_args_for_exfil(args: Any) -> str | None:
    """Return the exfil label if serialized args carry secret-shaped values."""
    try:
        blob = json.dumps(args, sort_keys=True, default=str)
    except (TypeError, ValueError):
        blob = str(args)
    for label, pattern in _EXFIL_PATTERNS:
        if pattern.search(blob):
            return label
    return None


def pii_read_allowed(enforcer: PolicyEnforcer, claims: dict[str, Any], field: str) -> bool:
    """Can (sub, dom) read this PII field? Obj = `pii_<field>`, act = `read`.

    Obj names use underscores (not `pii:<field>`) because Casbin's `keyMatch2`
    treats `:` as a route-param delimiter — `pii:salary` would match `pii:email`.
    """
    return enforcer.enforce(
        str(claims.get("sub", "")),
        str(claims.get("dept", "")),
        f"pii_{field}",
        "read",
    )


def _mask_value(field: str, enforcer: PolicyEnforcer, claims: dict[str, Any]) -> bool:
    """True if a sensitive field must be masked for this caller."""
    if field not in SENSITIVE_FIELDS:
        return False
    return not pii_read_allowed(enforcer, claims, field)


def sanitize_response(
    data: Any,
    claims: dict[str, Any],
    enforcer: PolicyEnforcer,
) -> Any:
    """Recursively mask sensitive fields in a structured tool response (in place
    on lists/dicts; scalars are returned as-is)."""
    if isinstance(data, dict):
        for key, value in data.items():
            if _mask_value(str(key), enforcer, claims):
                data[key] = MASK
            else:
                data[key] = sanitize_response(value, claims, enforcer)
        return data
    if isinstance(data, list):
        return [sanitize_response(item, claims, enforcer) for item in data]
    return data


def sanitize_text(
    text: str,
    claims: dict[str, Any],
    enforcer: PolicyEnforcer,
) -> str:
    """Mask unambiguous PII patterns in free text (emails, SSNs) unless allowed."""
    if not pii_read_allowed(enforcer, claims, "email"):
        text = _EMAIL_RE.sub(MASK, text)
    if not pii_read_allowed(enforcer, claims, "ssn"):
        text = _SSN_RE.sub(MASK, text)
    return text


def redact_for_retention(obj: Any) -> Any:
    """Mk18 Phase G: deny-all scrub for retained trace payloads.

    Field-name masking (email/salary/ssn, any depth) + email/SSN regex on all
    strings. Structure and non-PII text survive; the chain is rehashed forward
    by the caller (`prune --redact`), so `verify_integrity` stays green.
    """
    if isinstance(obj, dict):
        return {
            key: (MASK if str(key).lower() in SENSITIVE_FIELDS else redact_for_retention(value))
            for key, value in obj.items()
        }
    if isinstance(obj, list):
        return [redact_for_retention(item) for item in obj]
    if isinstance(obj, str):
        return _SSN_RE.sub(MASK, _EMAIL_RE.sub(MASK, obj))
    return obj


def sanitize_tool_result(
    result: ToolResult,
    claims: dict[str, Any],
    enforcer: PolicyEnforcer,
) -> ToolResult:
    """Return a ToolResult whose summary (the part the LLM sees) has PII masked.

    If the response parsed to structured data, fields are masked by name and the
    summary is regenerated from the masked data (the raw text would still leak
    the value). Otherwise the free text is masked by regex patterns.
    """
    if result.data is not None:
        sanitize_response(result.data, claims, enforcer)
        result.content = json.dumps(result.data, sort_keys=True, default=str)
    elif result.content:
        result.content = sanitize_text(result.content, claims, enforcer)
    return result

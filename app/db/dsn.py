"""Connection-string helpers that never leak the password (logs, CLI output, error text)."""

from __future__ import annotations

import re
from urllib.parse import urlsplit

_KV_PASSWORD = re.compile(r"(password\s*=\s*)('[^']*'|\S+)", re.IGNORECASE)


def redact_dsn(dsn: str) -> str:
    """The DSN with its password replaced by `***` (URL form and `key=value` form)."""
    text = (dsn or "").strip()
    if "://" not in text:
        return _KV_PASSWORD.sub(r"\1***", text)
    parts = urlsplit(text)
    if parts.password is None:
        return text
    user = parts.username or ""
    host = parts.hostname or ""
    if ":" in host:  # IPv6 literal
        host = f"[{host}]"
    port = f":{parts.port}" if parts.port else ""
    netloc = f"{user}:***@{host}{port}"
    return parts._replace(netloc=netloc).geturl()


def describe_dsn(dsn: str) -> dict[str, str]:
    """Non-secret facts about a DSN for `python -m app.db check`."""
    text = (dsn or "").strip()
    if "://" in text:
        parts = urlsplit(text)
        host = parts.hostname or ""
        return {
            "host": host,
            "port": str(parts.port or 5432),
            "database": parts.path.lstrip("/") or "",
            "user": parts.username or "",
            "pooled": "yes" if "-pooler" in host else "no",
            "sslmode": _query_value(parts.query, "sslmode") or "(default)",
        }
    kv = dict(re.findall(r"(\w+)\s*=\s*('[^']*'|\S+)", text))
    host = kv.get("host", "").strip("'")
    return {
        "host": host,
        "port": kv.get("port", "5432"),
        "database": kv.get("dbname", "").strip("'"),
        "user": kv.get("user", "").strip("'"),
        "pooled": "yes" if "-pooler" in host else "no",
        "sslmode": kv.get("sslmode", "(default)"),
    }


def _query_value(query: str, key: str) -> str | None:
    for pair in query.split("&"):
        name, _, value = pair.partition("=")
        if name == key:
            return value
    return None

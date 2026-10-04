"""Platform extractor health (Mk16 fix) — in-memory, per-process.

Records the last platform-extraction failure so the dashboard can tell the
human *why* learning degraded (quota vs outage vs unconfigured) instead of
silently saving nothing. Deliberately in-memory: it is a health signal, not
state — a restart resets it, and each instance tracks its own calls (every
laptop calls OpenRouter directly with the shared key).
"""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from typing import Any

_lock = threading.Lock()
_last_error: str | None = None
_last_error_at: str | None = None
_total_errors: int = 0


def record_failure(kind: str) -> None:
    """Remember a platform-extraction failure (`rate-limited` | `error`). Never raises."""
    global _last_error, _last_error_at, _total_errors
    try:
        with _lock:
            _last_error = kind
            _last_error_at = datetime.now(UTC).isoformat()
            _total_errors += 1
    except Exception:  # noqa: BLE001, S110 - health must never sink a turn
        pass


def reset() -> None:
    """Clear recorded state (tests)."""
    global _last_error, _last_error_at, _total_errors
    with _lock:
        _last_error, _last_error_at, _total_errors = None, None, 0


def get_status(
    configured: bool, source: str = "none", model: str | None = None
) -> dict[str, Any]:
    """Dashboard view-model. `configured` = platform key resolvable (DB or env);
    `source` in {"db", "env", "none"}; `model` names the active free id (never key material)."""
    with _lock:
        return {
            "configured": configured,
            "source": source,
            "model": model,
            "last_error": _last_error,
            "last_error_at": _last_error_at,
            "total_errors": _total_errors,
        }

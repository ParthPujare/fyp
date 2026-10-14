"""Per-user/day LLM spend metering (Mk18 Phase H).

`MeteredProvider` wraps any real provider: every `chat` call records one
generate call + estimated cost, then delegates. When the cap is already hit
it serves a Mock-shaped degraded reply instead of spending — the turn
completes (with a `spend_cap` trace event recorded by the caller) rather
than sinking. MockLLM is never wrapped (offline/tests spend nothing).

The gateway ALSO pre-checks spend before starting a run and answers 429, so
interactive abuse gets a clean rate-limit instead of degraded turns.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from app.llm.base import LLMProvider, LLMResponse, Message, ToolSchema


class SpendCapExceeded(Exception):
    """Raised pre-run when today's spend already exceeds a cap (→ HTTP 429)."""


def spend_capped(members: Any, username: str, settings: Any) -> bool:
    """True iff the user is over either daily cap. Never raises (fail open)."""
    try:
        status = members.spend_status(
            username,
            int(settings.llm.daily_call_cap),
            int(settings.llm.daily_cost_cents_cap),
        )
        return bool(status.get("capped", False))
    except Exception:  # noqa: BLE001
        return False


class MeteredProvider(LLMProvider):
    """Delegating spend-counting wrapper (PROVIDER_NAME forwarded)."""

    def __init__(
        self,
        inner: LLMProvider,
        *,
        members: Any,
        username: str,
        cost_cents_per_call: int = 1,
        on_cap: Callable[[], None] | None = None,
    ):
        self._inner = inner
        self._members = members
        self._username = username
        self._cost = int(cost_cents_per_call or 0)
        self._on_cap = on_cap
        self._cap_fired = False
        try:
            self.PROVIDER_NAME: str = getattr(inner, "PROVIDER_NAME", "")
        except Exception:  # noqa: BLE001
            self.PROVIDER_NAME = ""

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(object.__getattribute__(self, "_inner"), name)

    @property
    def model(self) -> Any:
        return getattr(self._inner, "model", None)

    def _capped_now(self) -> bool:
        try:
            from app.config import get_settings as _gs

            settings = _gs()
            return spend_capped(self._members, self._username, settings)
        except Exception:  # noqa: BLE001
            return False

    def _fire_cap_once(self) -> None:
        if self._cap_fired:
            return
        self._cap_fired = True
        if self._on_cap is not None:
            try:
                self._on_cap()
            except Exception:  # noqa: BLE001, S110
                pass

    def chat(
        self,
        messages: Sequence[Message | dict[str, Any]],
        tools: Sequence[ToolSchema | dict[str, Any]] | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        top_p: float | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        if self._capped_now():
            # Degraded completion: no spend, no sink.
            self._fire_cap_once()
            from app.llm.mock import MockLLM

            return MockLLM().generate(
                [{"role": m.get("role", "user") if isinstance(m, dict) else "user",
                  "content": m.get("content", "") if isinstance(m, dict) else str(m)}
                 for m in list(messages)[-1:]],
                tools=[],
            )
        try:
            self._members.record_llm_call(self._username, self._cost)
        except Exception:  # noqa: BLE001, S110
            pass
        chat = getattr(self._inner, "chat", None)
        if callable(chat):
            return chat(
                messages,
                tools=tools,
                model=model,
                temperature=temperature,
                max_tokens=max_tokens,
                top_p=top_p,
                **kwargs,
            )
        # Providers exposing only the harness adapter (e.g. MockLLM).
        return self._inner.generate(list(messages), tools, **kwargs)

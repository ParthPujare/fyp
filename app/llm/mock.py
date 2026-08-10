"""MockLLM — deterministic, scripted, zero-network provider for tests/CI."""

from typing import Any

from app.llm.base import LLMResponse, ToolCall


class MockLLM:
    """Returns canned text/tool_calls keyed by the last user message.

    script = {"<last user message>": {"text": "...", "tool_calls": [{"name": ..., "args": {...}}]}}
    Any message not in the script gets a deterministic echo reply.
    """

    def __init__(self, script: dict[str, dict] | None = None):
        self._script = script or {}

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        last_user = ""
        for message in reversed(messages):
            if message.get("role") == "user":
                last_user = str(message.get("content", ""))
                break
        spec = self._script.get(last_user, {})
        text = spec.get("text", f"mock reply to: {last_user[:80]}")
        tool_calls = [
            ToolCall(name=tc["name"], args=tc.get("args", {}))
            for tc in spec.get("tool_calls", [])
        ]
        return LLMResponse(text=text, tool_calls=tool_calls)

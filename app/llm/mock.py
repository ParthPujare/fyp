"""MockLLM — deterministic, scripted, zero-network provider for tests/CI."""

from typing import Any

from app.llm.base import LLMResponse, ToolCall


class MockLLM:
    """Returns canned text/tool_calls.

    Two scripted modes:
    - `script` = {"<last user message>": {"text": ..., "tool_calls": [...]}} — keyed by the
      last user message; useful for deny-storm (the same call repeats every iteration).
    - `sequence` = list[dict] — consumed one response per generate() call, in order. Useful
      for multi-step runs where the model must call tool A, then tool B, then answer.

    Any message/step not covered falls back to a deterministic echo reply.
    """

    def __init__(self, script: dict[str, dict] | None = None, sequence: list[dict] | None = None):
        self._script = script or {}
        self._sequence = list(sequence) if sequence is not None else None
        self.last_tools: list[dict[str, Any]] | None = None

    def _last_user(self, messages: list[dict[str, Any]]) -> str:
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content", ""))
        return ""

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        self.last_tools = tools
        if self._sequence is not None:
            if self._sequence:
                spec = self._sequence.pop(0)
            else:
                return LLMResponse(text="mock final answer")
            return LLMResponse(
                text=spec.get("text", ""),
                tool_calls=[
                    ToolCall(name=tc["name"], args=tc.get("args", {}))
                    for tc in spec.get("tool_calls", [])
                ],
            )

        last_user = self._last_user(messages)
        spec = self._script.get(last_user, {})
        text = spec.get("text", f"mock reply to: {last_user[:80]}")
        tool_calls = [
            ToolCall(name=tc["name"], args=tc.get("args", {}))
            for tc in spec.get("tool_calls", [])
        ]
        return LLMResponse(text=text, tool_calls=tool_calls)

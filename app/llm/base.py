"""LLM provider ABC, message structures, tool schemas, and response types (Hermes-style, no LiteLLM)."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Message:
    role: str  # "system", "user", "assistant", "tool"
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


@dataclass
class ToolSchema:
    name: str
    description: str
    parameters: dict[str, Any]  # JSON Schema


@dataclass
class ToolCall:
    name: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class LLMResponse:
    model: str = ""
    provider: str = ""
    text: str = ""
    content: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: dict[str, Any] | None = None
    finish_reason: str | None = None
    reasoning: str | None = None

    def __post_init__(self):
        if self.content and not self.text:
            self.text = self.content
        elif self.text and not self.content:
            self.content = self.text


class LLMProvider(ABC):
    @abstractmethod
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
        pass

    def generate(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        """Compatibility adapter for existing harness agent runner."""
        return self.chat(messages=messages, tools=tools, **kwargs)


def strip_routing_prefix(model: str, *prefixes: str) -> str:
    """Drop a `<provider>/` routing prefix from a stored model id.

    Saved keys carry LiteLLM-style prefixes (`groq/llama-3.3-70b-versatile`,
    `anthropic/claude-…`) because that is what the dashboard's model
    normalization writes. The provider APIs themselves want the bare id. Only the
    listed prefixes are stripped — OpenRouter slugs are `vendor/model` and must
    be sent whole.
    """
    for prefix in prefixes:
        if model.startswith(f"{prefix}/"):
            return model.split("/", 1)[1]
    return model


def classify_llm_error(exc: Exception, *, provider: str = "", model: str = "") -> str:
    """A short, sanitized reason for a provider failure.

    `provider`/`model` are appended when known: "model not found" is unactionable
    without saying which model was asked for.
    """
    return f"{_classify(exc)}{_error_context(provider, model)}"


def _error_context(provider: str, model: str) -> str:
    if not provider and not model:
        return ""
    return f" [provider={provider or 'unknown'}, model={model or 'unknown'}]"


def _classify(exc: Exception) -> str:
    msg = str(exc).lower()
    if any(k in msg for k in ["auth", "api key", "unauthorized", "invalid key", "authentication", "401", "incorrect api key"]):
        return "LLM provider authentication failed: invalid API key."
    if any(k in msg for k in ["rate limit", "quota", "exceeded", "too many requests", "429"]):
        return "LLM provider rate limit or quota exceeded (check your account balance/credits)."
    if any(k in msg for k in ["403", "permission denied", "denied access"]):
        return "LLM provider access denied (403): check your region availability or API key permissions."
    if any(k in msg for k in ["404", "not found", "no longer available"]):
        return "LLM provider model not found (404): the requested model name is invalid or deprecated."
    if any(k in msg for k in ["timeout", "timed out", "deadline"]):
        return "LLM provider request timed out."
    return f"LLM provider error: {exc.__class__.__name__} - {exc}"

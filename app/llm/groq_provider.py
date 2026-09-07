"""GroqProvider — Groq's OpenAI-compatible endpoint via the openai SDK."""

from typing import Any

from app.llm.openai_provider import OpenAIProvider


class GroqProvider(OpenAIProvider):
    """Groq serves the OpenAI chat-completions API (tools included) at its own
    base URL, so the OpenAI client drives it unchanged.

    Model ids are bare (`llama-3.3-70b-versatile`); a stored `groq/` routing
    prefix — what the dashboard's model normalization writes — is stripped before
    the call.
    """

    PROVIDER_NAME = "groq"
    DEFAULT_MODEL = "llama-3.3-70b-versatile"
    ROUTING_PREFIXES = ("groq",)
    BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, api_key: str, base_url: str | None = None, **kwargs: Any):
        super().__init__(api_key=api_key, base_url=base_url or self.BASE_URL, **kwargs)

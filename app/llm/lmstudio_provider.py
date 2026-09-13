"""Local OpenAI-compatible providers: LM Studio, Ollama, llama.cpp (plan Mk11).

All three speak the OpenAI chat-completions schema; they differ only in
defaults. LM Studio is the tested path (`http://localhost:1234/v1`, no key);
Ollama (`http://localhost:11434/v1`) and llama.cpp (`http://localhost:8080/v1`)
are untested alternates following the same pattern.

Keyless: these servers need no API key, but the OpenAI client requires a
non-empty string — an empty key becomes a harmless placeholder at build time.
The placeholder is never a real credential.
"""

from typing import Any

from app.llm.openai_provider import OpenAIProvider

DUMMY_API_KEY = "lm-studio"


class LmStudioProvider(OpenAIProvider):
    PROVIDER_NAME = "lmstudio"
    DEFAULT_MODEL = "local-model"
    ROUTING_PREFIXES = ("lmstudio",)
    BASE_URL = "http://localhost:1234/v1"

    def __init__(self, api_key: str, base_url: str | None = None, **kwargs: Any):
        super().__init__(
            api_key=api_key or DUMMY_API_KEY, base_url=base_url or self.BASE_URL, **kwargs
        )


class OllamaProvider(OpenAIProvider):
    PROVIDER_NAME = "ollama"
    DEFAULT_MODEL = "local-model"
    ROUTING_PREFIXES = ("ollama",)
    BASE_URL = "http://localhost:11434/v1"

    def __init__(self, api_key: str, base_url: str | None = None, **kwargs: Any):
        super().__init__(
            api_key=api_key or DUMMY_API_KEY, base_url=base_url or self.BASE_URL, **kwargs
        )


class LlamaCppProvider(OpenAIProvider):
    PROVIDER_NAME = "llamacpp"
    DEFAULT_MODEL = "local-model"
    ROUTING_PREFIXES = ("llamacpp", "llama.cpp", "llama_cpp")
    BASE_URL = "http://localhost:8080/v1"

    def __init__(self, api_key: str, base_url: str | None = None, **kwargs: Any):
        super().__init__(
            api_key=api_key or DUMMY_API_KEY, base_url=base_url or self.BASE_URL, **kwargs
        )


LOCAL_PROVIDERS = ("lmstudio", "ollama", "llamacpp")

LOCAL_BASE_URLS = {
    "lmstudio": LmStudioProvider.BASE_URL,
    "ollama": OllamaProvider.BASE_URL,
    "llamacpp": LlamaCppProvider.BASE_URL,
}

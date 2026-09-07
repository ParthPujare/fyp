"""Pluggable LLM backend: provider protocol, MockLLM (offline/CI), and the direct
provider implementations (OpenAI / Gemini / Anthropic / OpenRouter / custom
OpenAI-compatible) behind `registry.get_provider_instance` (plan mk5)."""

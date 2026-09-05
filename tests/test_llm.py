"""LLM: MockLLM determinism + factory routing (no network)."""

from app.config import Settings
from app.llm.factory import get_provider
from app.llm.gemini_provider import GeminiProvider
from app.llm.mock import MockLLM
from app.llm.openai_provider import OpenAIProvider


def test_mock_echo_default():
    llm = MockLLM()
    resp = llm.generate([{"role": "user", "content": "hello"}])
    assert resp.text.startswith("mock reply to: hello")
    assert resp.tool_calls == []


def test_mock_scripted_response():
    llm = MockLLM(
        {
            "find customer": {
                "text": "searching...",
                "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}],
            }
        }
    )
    resp = llm.generate([{"role": "user", "content": "find customer"}])
    assert resp.text == "searching..."
    assert len(resp.tool_calls) == 1
    assert resp.tool_calls[0].name == "crm_search_customer"
    assert resp.tool_calls[0].args == {"query": "acme"}


def test_mock_determinism():
    llm = MockLLM({"hi": {"text": "hello!"}})
    messages = [{"role": "user", "content": "hi"}]
    assert llm.generate(messages) == llm.generate(messages)


def test_factory_defaults_to_mock():
    assert isinstance(get_provider(Settings()), MockLLM)


def test_factory_routes_to_openai():
    settings = Settings(llm={"provider": "openai", "model": "gpt-4o-mini", "api_key": "sk-test"})
    provider = get_provider(settings)
    assert isinstance(provider, OpenAIProvider)
    assert provider.model == "gpt-4o-mini"


def test_gemini_aq_token_provider():
    provider = GeminiProvider(
        api_key="AQ.dummy_key_for_testing",
        model="gemini-2.0-flash",
    )
    assert provider.model == "gemini-2.0-flash"
    assert provider.api_key.startswith("AQ.")

"""LLM: MockLLM determinism + factory routing (no network)."""

from app.config import Settings
from app.llm.factory import get_provider
from app.llm.litellm import LiteLLMProvider
from app.llm.mock import MockLLM


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


def test_factory_routes_to_litellm():
    settings = Settings(llm={"provider": "litellm", "model": "gpt-4o-mini"})
    provider = get_provider(settings)
    assert isinstance(provider, LiteLLMProvider)
    assert provider.model == "gpt-4o-mini"
    assert provider.temperature == settings.llm.temperature

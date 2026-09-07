"""LLM: MockLLM determinism + factory routing + saved-model routing (no network)."""

import json
from types import SimpleNamespace

from app.agent.tools import TOOL_REGISTRY, tool_schema
from app.config import Settings
from app.llm import gemini_provider as gemini_module
from app.llm.anthropic_provider import AnthropicProvider
from app.llm.base import classify_llm_error
from app.llm.factory import get_provider
from app.llm.gemini_provider import GeminiProvider
from app.llm.groq_provider import GroqProvider
from app.llm.mock import MockLLM
from app.llm.openai_provider import OpenAIProvider
from app.llm.openrouter_provider import OpenRouterProvider
from app.llm.registry import get_provider_instance, is_supported


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


def test_factory_passes_settings_model_and_decoding():
    """Global config (model/temperature/max_tokens) must reach the provider."""
    settings = Settings(
        llm={
            "provider": "openai",
            "model": "gpt-4o",
            "api_key": "sk-test",
            "temperature": 0.5,
            "max_tokens": 256,
        }
    )
    provider = get_provider(settings)
    assert provider.model == "gpt-4o"
    assert provider.temperature == 0.5
    assert provider.max_tokens == 256


# ---- saved model / decoding settings reach the request (BYOK, plan mk3-mk5) ----


class _FakeOpenAIClient:
    """Captures the params the provider sends, without any network."""

    def __init__(self):
        self.captured: dict = {}
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.captured = params
        message = SimpleNamespace(content="ok", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
        )


class _FakeAnthropicClient:
    def __init__(self):
        self.captured: dict = {}
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **params):
        self.captured = params
        return SimpleNamespace(
            content=[SimpleNamespace(type="text", text="ok")],
            usage=None,
            stop_reason="end_turn",
        )


def test_openai_uses_saved_model_and_decoding_defaults():
    provider = OpenAIProvider(api_key="sk-test", model="gpt-4o", temperature=0.2, max_tokens=1024)
    provider.client = _FakeOpenAIClient()
    provider.generate([{"role": "user", "content": "hi"}])
    assert provider.client.captured["model"] == "gpt-4o"
    assert provider.client.captured["temperature"] == 0.2
    assert provider.client.captured["max_tokens"] == 1024


def test_openai_per_call_model_overrides_saved_model():
    provider = OpenAIProvider(api_key="sk-test", model="gpt-4o")
    provider.client = _FakeOpenAIClient()
    provider.chat([{"role": "user", "content": "hi"}], model="gpt-4o-mini")
    assert provider.client.captured["model"] == "gpt-4o-mini"


# The shape the harness graph actually produces once a tool runs.
TOOL_CONVERSATION = [
    {"role": "system", "content": "memory snapshot"},
    {"role": "user", "content": "find acme"},
    {
        "role": "assistant",
        "content": "searching",
        "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}],
    },
    {"role": "tool", "content": '[{"id": 1}]', "name": "crm_search_customer"},
]


def test_openai_formats_harness_tool_round_trip():
    """A `tool` message with no tool_call_id is a 400 ("must be a response to a
    preceding message with tool_calls") — every multi-step run would fail."""
    provider = OpenAIProvider(api_key="sk-test", model="gpt-4o")
    provider.client = _FakeOpenAIClient()
    provider.generate(TOOL_CONVERSATION)

    sent = provider.client.captured["messages"]
    assistant = next(m for m in sent if m["role"] == "assistant")
    call = assistant["tool_calls"][0]
    assert call["type"] == "function"
    assert call["function"]["name"] == "crm_search_customer"
    assert json.loads(call["function"]["arguments"]) == {"query": "acme"}

    tool_msg = next(m for m in sent if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == call["id"]
    assert tool_msg["content"] == '[{"id": 1}]'


def test_unanswered_tool_call_is_dropped_from_history():
    """A guardrail ending a run mid-batch leaves a declared call with no result;
    both APIs reject that, poisoning every later turn on the thread."""
    truncated = [
        {"role": "user", "content": "find acme"},
        {
            "role": "assistant",
            "content": "searching",
            "tool_calls": [
                {"name": "crm_search_customer", "args": {"query": "acme"}},
                {"name": "crm_list_campaigns", "args": {}},  # budget hit; never ran
            ],
        },
        {"role": "tool", "content": "[]", "name": "crm_search_customer"},
        {"role": "user", "content": "and now?"},
    ]

    openai_provider = OpenAIProvider(api_key="sk-test")
    openai_provider.client = _FakeOpenAIClient()
    openai_provider.generate(truncated)
    assistant = next(
        m for m in openai_provider.client.captured["messages"] if m["role"] == "assistant"
    )
    assert [c["function"]["name"] for c in assistant["tool_calls"]] == ["crm_search_customer"]

    anthropic_provider = AnthropicProvider(api_key="sk-ant-test")
    anthropic_provider.client = _FakeAnthropicClient()
    anthropic_provider.generate(truncated)
    blocks = [
        b
        for m in anthropic_provider.client.captured["messages"]
        for b in m["content"]
        if b.get("type") == "tool_use"
    ]
    assert [b["name"] for b in blocks] == ["crm_search_customer"]


def test_openai_orphan_tool_result_degrades_instead_of_400():
    provider = OpenAIProvider(api_key="sk-test")
    provider.client = _FakeOpenAIClient()
    provider.generate([{"role": "tool", "content": "stale", "name": "crm_search_customer"}])
    sent = provider.client.captured["messages"]
    assert sent[0]["role"] == "user" and "stale" in sent[0]["content"]


def test_anthropic_formats_tool_use_and_result_blocks():
    """Tool results used to be sent as a second assistant turn — Anthropic requires
    alternating roles, and the model never saw its own call's output."""
    provider = AnthropicProvider(api_key="sk-ant-test", model="claude-3-5-sonnet-20241022")
    provider.client = _FakeAnthropicClient()
    provider.generate(TOOL_CONVERSATION)

    params = provider.client.captured
    assert params["system"] == "memory snapshot"
    msgs = params["messages"]
    assert [m["role"] for m in msgs] == ["user", "assistant", "user"]

    tool_use = next(b for b in msgs[1]["content"] if b["type"] == "tool_use")
    tool_result = next(b for b in msgs[2]["content"] if b["type"] == "tool_result")
    assert tool_use["name"] == "crm_search_customer"
    assert tool_use["input"] == {"query": "acme"}
    assert tool_result["tool_use_id"] == tool_use["id"]
    assert tool_result["content"] == '[{"id": 1}]'


def test_classify_llm_error_names_provider_and_model():
    msg = classify_llm_error(RuntimeError("404 model not found"), provider="groq", model="gpt-oss-20b")
    assert "model not found" in msg
    assert "provider=groq" in msg and "model=gpt-oss-20b" in msg


def test_groq_provider_is_registered_and_openai_compatible():
    provider = get_provider_instance(provider_type="groq", api_key="gsk-test")
    assert isinstance(provider, GroqProvider)
    assert provider.base_url == "https://api.groq.com/openai/v1"
    assert provider.model == "llama-3.3-70b-versatile"
    assert is_supported("groq") and is_supported("groqcloud")


def test_groq_strips_routing_prefix_from_saved_model():
    """Saved keys carry `groq/…` (dashboard normalization); Groq wants the bare id."""
    provider = GroqProvider(api_key="gsk-test", model="groq/llama-3.3-70b-versatile")
    provider.client = _FakeOpenAIClient()
    provider.generate([{"role": "user", "content": "hi"}])
    assert provider.client.captured["model"] == "llama-3.3-70b-versatile"


def test_openrouter_slug_keeps_its_vendor_prefix():
    """OpenRouter ids are `vendor/model` — the prefix strip must not touch them."""
    provider = OpenRouterProvider(api_key="sk-or-test", model="openai/gpt-4o-mini")
    provider.client = _FakeOpenAIClient()
    provider.generate([{"role": "user", "content": "hi"}])
    assert provider.client.captured["model"] == "openai/gpt-4o-mini"


def test_openrouter_default_model_is_namespaced():
    """A bare OpenAI slug 404s on OpenRouter — the default must be `vendor/model`."""
    assert OpenRouterProvider(api_key="sk-or-test").model == "openai/gpt-4o-mini"
    assert OpenRouterProvider(api_key="sk-or-test", model="anthropic/claude-sonnet-4-5").model == (
        "anthropic/claude-sonnet-4-5"
    )


def test_anthropic_uses_saved_model_and_strips_routing_prefix():
    provider = AnthropicProvider(
        api_key="sk-ant-test", model="anthropic/claude-sonnet-4-5", max_tokens=2048
    )
    provider.client = _FakeAnthropicClient()
    provider.generate([{"role": "user", "content": "hi"}])
    assert provider.client.captured["model"] == "claude-sonnet-4-5"
    assert provider.client.captured["max_tokens"] == 2048


def test_gemini_uses_saved_model_and_decoding_defaults(monkeypatch):
    captured: dict = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {
                "candidates": [
                    {"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}
                ]
            }

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None):
            captured["url"] = url
            captured["body"] = json
            return _FakeResponse()

    monkeypatch.setattr(gemini_module.httpx, "Client", _FakeClient)
    provider = GeminiProvider(
        api_key="k", model="gemini/gemini-2.5-pro", temperature=0.2, max_tokens=1024
    )
    provider.generate([{"role": "user", "content": "hi"}])
    assert "models/gemini-2.5-pro:generateContent" in captured["url"]
    assert captured["body"]["generationConfig"] == {"temperature": 0.2, "maxOutputTokens": 1024}


def test_gemini_omits_parameters_for_no_argument_tools(monkeypatch):
    """Gemini 400s the WHOLE request on an OBJECT `parameters` with no properties,
    so a single no-arg tool (crm_list_campaigns) would break every chat."""
    captured: dict = {}

    class _FakeResponse:
        status_code = 200
        text = ""

        @staticmethod
        def json():
            return {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}

    class _FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def post(self, url, json=None):
            captured["body"] = json
            return _FakeResponse()

    monkeypatch.setattr(gemini_module.httpx, "Client", _FakeClient)
    tools = [
        tool_schema(TOOL_REGISTRY["crm_list_campaigns"]),  # takes no arguments
        tool_schema(TOOL_REGISTRY["crm_search_customer"]),  # takes `query`
    ]
    GeminiProvider(api_key="k").generate([{"role": "user", "content": "hi"}], tools=tools)

    declarations = captured["body"]["tools"][0]["functionDeclarations"]
    by_name = {d["name"]: d for d in declarations}
    assert "parameters" not in by_name["crm_list_campaigns"]
    assert by_name["crm_search_customer"]["parameters"]["properties"]["query"]
    assert all(d.get("parameters", {"properties": {"x": 1}}).get("properties") for d in declarations)

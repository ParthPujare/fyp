"""Unit + BYOK e2e tests for Plan Mk10 OpenCode Zen provider."""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.auth.jwt import create_token
from app.config import get_settings
from app.dashboard.app import _normalize_model
from app.dashboard.app import app as dashboard_app
from app.llm.factory import get_provider_for_user
from app.llm.registry import get_provider_instance, is_supported, normalize_provider_type
from app.llm.zen_provider import (
    ZenProvider,
    _format_responses_input,
    is_responses_model,
)


class _FakeChatCompletions:
    def __init__(self, outer):
        self._outer = outer

    def create(self, **params):
        self._outer.chat_captured = params
        message = SimpleNamespace(content="chat-ok", tool_calls=None)
        return SimpleNamespace(
            choices=[SimpleNamespace(message=message, finish_reason="stop")], usage=None
        )


class _FakeResponses:
    def __init__(self, outer, tool_call=None):
        self._outer = outer
        self._tool_call = tool_call

    def create(self, **params):
        self._outer.responses_captured = params
        output = [SimpleNamespace(type="message", text="resp-ok")]
        if self._tool_call:
            name, args = self._tool_call
            output.append(SimpleNamespace(
                type="function_call", name=name, arguments=json.dumps(args)
            ))
        return SimpleNamespace(
            output=output,
            output_text="resp-ok",
            usage=SimpleNamespace(input_tokens=3, output_tokens=5, total_tokens=8),
        )


class _FakeZenClient:
    def __init__(self, tool_call=None):
        self.chat_captured: dict = {}
        self.responses_captured: dict = {}
        self.chat = SimpleNamespace(completions=_FakeChatCompletions(self))
        self.responses = SimpleNamespace(create=_FakeResponses(self, tool_call).create)


def _provider(model="muse-spark-1.3-contributor-free", **kw):
    p = ZenProvider(api_key="z-test", model=model, **kw)
    p.client = _FakeZenClient()
    return p


def test_zen_registered_with_opencode_alias():
    assert is_supported("zen")
    assert normalize_provider_type("opencode") == "zen"
    inst = get_provider_instance("opencode", api_key="z-k", model="big-pickle")
    assert isinstance(inst, ZenProvider)


def test_zen_defaults_to_zen_base_url():
    p = ZenProvider(api_key="z-k")
    assert p.base_url == "https://opencode.ai/zen/v1"
    assert p.model == "muse-spark-1.3-contributor-free"


def test_responses_routing_by_prefix():
    assert is_responses_model("muse-spark-1.3-contributor-free")
    assert is_responses_model("gpt-5.5")
    assert is_responses_model("grok-4.5")
    assert not is_responses_model("big-pickle")
    assert not is_responses_model("mimo-v2.5-free")
    assert not is_responses_model("deepseek-v4-flash-free")


def test_zen_chat_family_uses_chat_completions():
    p = _provider(model="big-pickle")
    out = p.chat([{"role": "user", "content": "hi"}])
    assert out.provider == "zen"
    assert out.text == "chat-ok"
    assert p.client.chat_captured["model"] == "big-pickle"
    assert p.client.responses_captured == {}


def test_zen_responses_family_uses_responses_api():
    p = _provider()
    out = p.chat([{"role": "user", "content": "hi"}])
    assert out.provider == "zen"
    assert out.text == "resp-ok"
    assert out.usage == {"prompt_tokens": 3, "completion_tokens": 5, "total_tokens": 8}
    assert p.client.responses_captured["model"] == "muse-spark-1.3-contributor-free"
    assert p.client.chat_captured == {}


def test_zen_strips_zen_and_opencode_prefixes():
    p = _provider(model="zen/big-pickle")
    p.chat([{"role": "user", "content": "hi"}])
    assert p.client.chat_captured["model"] == "big-pickle"
    assert _normalize_model("zen", "zen/big-pickle") == "big-pickle"
    assert _normalize_model("zen", "opencode/muse-spark-1.3-contributor-free") == (
        "muse-spark-1.3-contributor-free"
    )


def test_zen_responses_tool_round_trip():
    p = _provider()
    p.client = _FakeZenClient(tool_call=("crm_search_customer", {"query": "acme"}))
    out = p.chat(
        [{"role": "user", "content": "find acme"}],
        tools=[{
            "type": "function",
            "function": {
                "name": "crm_search_customer",
                "description": "search",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
            },
        }],
    )
    assert out.tool_calls[0].name == "crm_search_customer"
    assert out.tool_calls[0].args == {"query": "acme"}
    sent_tools = p.client.responses_captured["tools"]
    assert sent_tools[0]["type"] == "function"
    assert sent_tools[0]["name"] == "crm_search_customer"


def test_zen_responses_history_replays_calls_and_outputs():
    conv = [
        {"role": "user", "content": "find acme"},
        {"role": "assistant", "content": "searching",
         "tool_calls": [{"name": "crm_search_customer", "args": {"query": "acme"}}]},
        {"role": "tool", "content": '[{"id": 1}]', "name": "crm_search_customer"},
        {"role": "user", "content": "and now?"},
    ]
    instructions, items = _format_responses_input(conv)
    assert instructions is None
    calls = [i for i in items if i.get("type") == "function_call"]
    outputs = [i for i in items if i.get("type") == "function_call_output"]
    assert len(calls) == 1 and len(outputs) == 1
    assert outputs[0]["call_id"] == calls[0]["call_id"]
    assert outputs[0]["output"] == '[{"id": 1}]'


def test_zen_responses_drops_unanswered_calls():
    conv = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "x",
         "tool_calls": [{"name": "crm_list_campaigns", "args": {}}]},
        {"role": "user", "content": "and now?"},
    ]
    _, items = _format_responses_input(conv)
    assert [i for i in items if i.get("type") == "function_call"] == []


def test_zen_empty_input_raises():
    p = _provider()
    try:
        p.chat([{"role": "user", "content": ""}])
    except ValueError as exc:
        assert "provider=zen" in str(exc)
    else:
        raise AssertionError("expected ValueError")


def _token(username, member_store):
    settings = get_settings()
    user = member_store.get(username)
    return create_token(user, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)


def test_zen_byok_save_without_base_url_applies_default(member_store, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    client = TestClient(dashboard_app)
    headers = {"Authorization": f"Bearer {_token('alice@acme.com', member_store)}"}
    resp = client.post(
        "/api/account/llm-keys",
        json={"provider": "zen", "model": "muse-spark-1.3-contributor-free", "api_key": "z-test"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["base_url"] == "https://opencode.ai/zen/v1"
    provider = get_provider_for_user(get_settings(), "alice@acme.com", member_store)
    assert isinstance(provider, ZenProvider)
    assert provider.model == "muse-spark-1.3-contributor-free"


def test_zen_byok_open_ids_allowed(member_store, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    client = TestClient(dashboard_app)
    headers = {"Authorization": f"Bearer {_token('alice@acme.com', member_store)}"}
    resp = client.post(
        "/api/account/llm-keys",
        json={"provider": "zen", "model": "some-future-model-9", "api_key": "z-test"},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["model"] == "some-future-model-9"

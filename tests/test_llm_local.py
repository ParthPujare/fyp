"""Unit + BYOK e2e tests for Plan Mk11 local providers (LM Studio + alternates)."""

from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.auth.jwt import create_token
from app.config import get_settings
from app.dashboard.app import _normalize_model
from app.dashboard.app import app as dashboard_app
from app.llm.base import tool_unsupported_error
from app.llm.factory import get_provider_for_user
from app.llm.lmstudio_provider import (
    DUMMY_API_KEY,
    LlamaCppProvider,
    LmStudioProvider,
    OllamaProvider,
)
from app.llm.registry import get_provider_instance, is_supported, normalize_provider_type


class _FakeClient:
    def __init__(self, error=None):
        self.captured: dict = {}
        self._error = error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **params):
        self.captured = params
        if self._error:
            raise self._error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="local-ok", tool_calls=None),
                                      finish_reason="stop")],
            usage=None,
        )


def test_local_providers_registered():
    for name, cls in (("lmstudio", LmStudioProvider), ("ollama", OllamaProvider),
                      ("llamacpp", LlamaCppProvider)):
        assert is_supported(name)
        assert isinstance(get_provider_instance(name, api_key="", model="m"), cls)
    assert normalize_provider_type("lm-studio") == "lmstudio"
    assert normalize_provider_type("llama.cpp") == "llamacpp"


def test_local_defaults_and_keyless():
    p = LmStudioProvider(api_key="", model="my-model")
    assert p.base_url == "http://localhost:1234/v1"
    assert p.api_key == DUMMY_API_KEY
    assert OllamaProvider(api_key="", model="m").base_url == "http://localhost:11434/v1"
    assert LlamaCppProvider(api_key="", model="m").base_url == "http://localhost:8080/v1"


def test_local_prefix_stripped():
    assert _normalize_model("lmstudio", "lmstudio/my-model") == "my-model"
    assert _normalize_model("ollama", "ollama/llama3.1") == "llama3.1"


def test_tool_unsupported_error_names_cause():
    err = tool_unsupported_error(
        RuntimeError("400 Bad request: 'tools' is not supported by this model"),
        provider="lmstudio", model="tiny",
    )
    assert err is not None
    assert "does not support tool calling" in err
    assert "lmstudio" in err
    assert tool_unsupported_error(RuntimeError("connection refused"), provider="lmstudio") is None
    assert tool_unsupported_error(ValueError("quota exceeded"), provider="openai") is None


def test_openai_provider_surfaces_tool_unsupported():
    p = LmStudioProvider(api_key="", model="tiny")
    p.client = _FakeClient(error=RuntimeError("400: unexpected field 'tools', no function support"))
    try:
        p.chat([{"role": "user", "content": "hi"}],
               tools=[{"type": "function", "function": {"name": "x", "description": "x",
                                                       "parameters": {}}}])
    except RuntimeError as exc:
        assert "does not support tool calling" in str(exc)
    else:
        raise AssertionError("expected RuntimeError")


def _token(username, member_store):
    settings = get_settings()
    user = member_store.get(username)
    return create_token(user, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)


def test_lmstudio_byok_keyless_save(member_store, monkeypatch):
    from cryptography.fernet import Fernet

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()
    client = TestClient(dashboard_app)
    headers = {"Authorization": f"Bearer {_token('alice@acme.com', member_store)}"}
    resp = client.post(
        "/api/account/llm-keys",
        json={"provider": "lmstudio", "model": "my-loaded-model", "api_key": ""},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["base_url"] == "http://localhost:1234/v1"
    provider = get_provider_for_user(get_settings(), "alice@acme.com", member_store)
    assert isinstance(provider, LmStudioProvider)
    assert provider.model == "my-loaded-model"


def test_local_models_detection(member_store, monkeypatch):
    from cryptography.fernet import Fernet

    import app.dashboard.app as dash

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()

    seen = {}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"data": [{"id": "model-a"}, {"id": "model-b"}, {}]}

    def _fake_get(url, timeout):
        seen["url"] = url
        seen["timeout"] = timeout
        return _Resp()

    monkeypatch.setattr(dash.httpx, "get", _fake_get)
    client = TestClient(dashboard_app)
    headers = {"Authorization": f"Bearer {_token('alice@acme.com', member_store)}"}
    resp = client.get("/api/account/local-models?provider=lmstudio", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["models"] == ["model-a", "model-b"]
    assert seen["url"] == "http://localhost:1234/v1/models"


def test_local_models_unreachable_is_502(member_store, monkeypatch):
    import httpx as _httpx
    from cryptography.fernet import Fernet

    import app.dashboard.app as dash

    monkeypatch.setenv("LLM__FERNET_KEY", Fernet.generate_key().decode("ascii"))
    get_settings.cache_clear()

    def _boom(url, timeout):
        raise _httpx.ConnectError("refused")

    monkeypatch.setattr(dash.httpx, "get", _boom)
    client = TestClient(dashboard_app)
    headers = {"Authorization": f"Bearer {_token('alice@acme.com', member_store)}"}
    resp = client.get("/api/account/local-models?provider=lmstudio", headers=headers)
    assert resp.status_code == 502
    assert "unreachable" in resp.json()["detail"]

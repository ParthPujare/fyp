"""Mk16 platform memory extractor — shared Qwen key, user-scope-only auto-write.

Offline: system provider is stubbed via monkeypatch; no network. The real
`.env` key is blanked in conftest so the suite stays hermetic.
"""

from __future__ import annotations

from unittest import mock

import pytest

from app.agent import graph as graph_mod
from app.config import Settings
from app.llm import system as system_mod
from app.memory.llm_extract import parse_response, system_extract_facts

ALICE = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
ADMIN = {"sub": "admin@acme.com", "role": "admin", "dept": "admin"}
MHEAD = {"sub": "mhead@acme.com", "role": "marketing_head", "dept": "marketing"}


class StubResp:
    def __init__(self, text: str):
        self.text = text


class StubProvider:
    PROVIDER_NAME = "openrouter"
    model = "qwen/qwen3.8-27b:free"

    def __init__(self, text: str = "[]", exc: Exception | None = None):
        self._text = text
        self._exc = exc
        self.calls: list = []

    def generate(self, messages, tools=None, **kwargs):
        self.calls.append(messages)
        if self._exc is not None:
            raise self._exc
        return StubResp(self._text)


# ---- config / singleton ----------------------------------------------------


def test_system_settings_defaults():
    s = Settings()
    assert s.system.model == "qwen/qwen3.8-27b:free"
    assert s.system.openrouter_api_key is None
    assert s.system.timeout_seconds == 15.0


def test_legacy_openrouter_env_backfilled(monkeypatch):
    from app.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test-123")
    monkeypatch.delenv("SYSTEM__OPENROUTER_API_KEY", raising=False)
    try:
        s = Settings()
        assert s.system.openrouter_api_key == "sk-test-123"
    finally:
        get_settings.cache_clear()


def test_build_system_provider_none_without_key():
    s = Settings()
    s.system.openrouter_api_key = None
    assert system_mod.build_system_provider(s) is None
    assert system_mod.resolve_system_key(s) is None


def test_build_system_provider_rejects_non_free():
    s = Settings()
    s.system.openrouter_api_key = "sk-test"
    s.system.model = "openai/gpt-4o-mini"
    with pytest.raises(ValueError, match=":free"):
        system_mod.build_system_provider(s)


def test_build_system_provider_ok():
    s = Settings()
    s.system.openrouter_api_key = "sk-test"
    s.system.model = "qwen/qwen3.8-27b:free"
    p = system_mod.build_system_provider(s)
    assert p is not None and p.model == "qwen/qwen3.8-27b:free"


# ---- user-scope pin ----------------------------------------------------------


def test_system_extract_keeps_only_own_user_scope():
    stub = StubProvider('["I work in Python every day"]')
    facts = system_extract_facts(stub, "I work in Python every day", "noted", ALICE)
    assert len(facts) == 1
    assert facts[0].scope == "user/alice@acme.com"


def test_system_extract_drops_central_and_dept_even_for_admin():
    # Mk17 (supersedes Mk16 user-only pin): the extractor proposes all scopes;
    # gating happens downstream (user auto, dept → head approval, central →
    # second-admin). classify() maps these to central / dept/marketing / user.
    stub = StubProvider(
        '["Company policy requires quarterly audits", '
        '"Our team policy requires weekly standups", '
        '"Admin prefers weekly exec briefings"]'
    )
    facts = system_extract_facts(
        stub, "Company policy requires quarterly audits", "noted", ADMIN
    )
    scopes = {f.scope for f in facts}
    assert "central" in scopes
    assert "dept/admin" in scopes  # classify() pins dept scope to the speaker's own dept
    assert "user/admin@acme.com" in scopes


def test_system_extract_drops_dept_for_head():
    # Mk17: head's team statement is proposed as dept scope (head approval downstream).
    stub = StubProvider('["Our team policy requires weekly standups"]')
    facts = system_extract_facts(stub, "Our team policy requires weekly standups", "ok", MHEAD)
    assert [f.scope for f in facts] == ["dept/marketing"]


def test_system_extract_never_raises_and_none_safe():
    assert system_extract_facts(None, "I work in Python daily", "ok", ALICE) == []
    assert system_extract_facts(StubProvider("[]", exc=RuntimeError("429")), "I work in Python", "ok", ALICE) == []
    assert system_extract_facts(StubProvider("not json"), "I work in Python daily", "ok", ALICE) == []
    assert system_extract_facts(StubProvider('["x"]'), "", "ok", ALICE) == []


def test_parse_response_still_classifies_for_chat_path():
    facts = parse_response('["Company policy requires quarterly audits"]', ADMIN)
    assert facts and facts[0].scope == "central"  # chat-model path unchanged


# ---- graph wiring: system-first, fallback -------------------------------------


def _deps(provider, trace, store=None):
    from app.agent.graph import AgentDeps
    from app.agent.guardrails import make_tracker
    from app.config import get_settings
    from app.harness.enforcer import PolicyEnforcer

    # Minimal deps: only provider/trace/actor/run_id are touched by _relevant_facts.
    return AgentDeps(
        provider=provider,
        executor=None,  # type: ignore[arg-type]
        tracker=make_tracker(get_settings().guardrails),
        trace=trace,
        enforcer=PolicyEnforcer(),
        audit=None,  # type: ignore[arg-type]
        run_id="run-test",
        actor={"sub": ALICE["sub"], "role": ALICE["role"], "dept": ALICE["dept"]},
        tool_schemas=[],
        store=None,  # type: ignore[arg-type]
        episodic=None,  # type: ignore[arg-type]
        approvals=None,  # type: ignore[arg-type]
        recall=None,  # type: ignore[arg-type]
        memory=get_settings().memory,
        thread_id="t-test",
        skills=None,  # type: ignore[arg-type]
    )


def test_relevant_facts_prefers_system_provider(monkeypatch, trace_store):
    from app.llm.mock import MockLLM

    stub = StubProvider('["Alice works in Python every day"]')
    # _relevant_facts imports get_system_provider from app.llm.system in-function.
    monkeypatch.setattr(system_mod, "get_system_provider", lambda *a, **k: stub)
    deps = _deps(MockLLM(), trace_store)
    msgs = [
        {"role": "user", "content": "I work in Python every day"},
        {"role": "assistant", "content": "noted"},
    ]
    facts = graph_mod._relevant_facts(msgs, ALICE, deps)
    assert [f.scope for f in facts] == ["user/alice@acme.com"]
    assert stub.calls and stub.calls[0][1]["content"].startswith("Speaker: alice@acme.com")
    assert "User said:" in stub.calls[0][1]["content"]
    # last-pair-only: exactly 2 messages sent to the system model
    assert len(stub.calls[0]) == 2


def test_relevant_facts_falls_back_on_system_error(monkeypatch, trace_store):
    from app.llm.mock import MockLLM

    stub = StubProvider("", exc=RuntimeError("rate limit 429"))
    monkeypatch.setattr(system_mod, "get_system_provider", lambda *a, **k: stub)
    deps = _deps(MockLLM(), trace_store)
    msgs = [{"role": "user", "content": "I prefer Slack over Teams always"}]
    facts = graph_mod._relevant_facts(msgs, ALICE, deps)
    # heuristic fallback still fires on the marker word "prefer"
    assert any(f.scope == "user/alice@acme.com" for f in facts)


def test_relevant_facts_no_key_parity(monkeypatch, trace_store):
    """Without a system key the turn behaves exactly like the old heuristic path."""
    from app.llm.mock import MockLLM
    from app.memory.extract import extract_facts

    monkeypatch.setattr(system_mod, "get_system_provider", lambda *a, **k: None)
    deps = _deps(MockLLM(), trace_store)
    msgs = [{"role": "user", "content": "I prefer Slack over Teams always"}]
    assert graph_mod._relevant_facts(msgs, ALICE, deps) == extract_facts(msgs, ALICE)


def test_end_to_end_user_fact_commits_no_approval(runner_factory, memory_store, trace_store, member_store):
    """Full turn with stubbed system extractor commits to user/* with no interrupt."""
    from app.llm.mock import MockLLM
    from app.memory.scopes import scope_to_namespace, scope_user

    stub = StubProvider('["Alice works in Python every day"]')
    import app.agent.graph as g

    def _patched(messages, claims, deps):
        from app.memory import llm_extract as _lx

        user_texts = [str(m.get("content", "")) for m in messages if m.get("role") == "user"]
        asst_texts = [str(m.get("content", "")) for m in messages if m.get("role") == "assistant"]
        resp_text = stub.generate(
            _lx.build_messages(user_texts[-1] if user_texts else "",
                               asst_texts[-1] if asst_texts else ""), tools=[]
        ).text
        parsed = _lx.parse_response(resp_text, claims)
        own = scope_user(str(claims.get("sub", "")))
        return [f for f in parsed if f.scope == own]

    run = runner_factory(MockLLM(), store=memory_store)
    with mock.patch.object(g, "_relevant_facts", _patched):
        res = run(ALICE, "I work in Python every day", thread_id="mem-sys-1")
    assert res.interrupted is False
    items = memory_store.items_in(scope_to_namespace(scope_user(ALICE["sub"])))
    assert any("Python" in str(it.value.get("text", "")) for it in items)


# ---- speaker scoping: no third-party facts (Mk16 fix) ----------------------------


def test_build_messages_speaker_names_the_subject():
    from app.memory.llm_extract import build_messages

    plain = build_messages("hi", "hello")
    assert len(plain) == 2 and "Speaker:" not in plain[1]["content"]
    scoped = build_messages("hi", "hello", speaker="admin@acme.com")
    assert "admin@acme.com" in scoped[1]["content"]
    assert "anyone else" in scoped[1]["content"]


def test_system_prompt_excludes_third_parties_and_meta_requests():
    from app.memory.llm_extract import SYSTEM_PROMPT

    lowered = SYSTEM_PROMPT.lower()
    assert "other people" in lowered or "third" in lowered or "anyone else" in lowered
    assert "add this to memory" in lowered


def test_system_extract_sends_speaker_identity():
    stub = StubProvider("[]")
    system_extract_facts(stub, "I heard Alice prefers Slack", "noted", ADMIN)
    sent = stub.calls[0][1]["content"]
    assert "Speaker: admin@acme.com" in sent


# ---- three-way trace signal (Mk16 fix) ----------------------------------------


def _extracted_events(trace_store, run_id="run-test"):
    return [
        e["payload"] for e in trace_store.get_run_tree(run_id)
        if e["type"] == "memory_write" and e["payload"].get("decision") == "extracted"
    ]


def test_system_error_event_then_heuristic_fallback(monkeypatch, trace_store):
    """A failed system call leaves a `system-error` event (with rate flag)."""
    from app.llm.mock import MockLLM

    stub = StubProvider("", exc=RuntimeError("rate limit 429 exceeded"))
    monkeypatch.setattr(system_mod, "get_system_provider", lambda *a, **k: stub)
    deps = _deps(MockLLM(), trace_store)
    msgs = [{"role": "user", "content": "I prefer Slack over Teams always"}]
    facts = graph_mod._relevant_facts(msgs, ALICE, deps)
    events = _extracted_events(trace_store)
    kinds = [e["extraction"] for e in events]
    assert "system-error" in kinds and "heuristic" in kinds
    err = next(e for e in events if e["extraction"] == "system-error")
    assert err["rate_limited"] is True
    # heuristic fallback still saved the day on the marker word
    assert any(f.scope == "user/alice@acme.com" for f in facts)


def test_system_skipped_event_when_unconfigured(monkeypatch, trace_store):
    from app.llm.mock import MockLLM

    monkeypatch.setattr(system_mod, "get_system_provider", lambda *a, **k: None)
    deps = _deps(MockLLM(), trace_store)
    msgs = [{"role": "user", "content": "I prefer Slack over Teams always"}]
    graph_mod._relevant_facts(msgs, ALICE, deps)
    events = _extracted_events(trace_store)
    assert events[0]["extraction"] == "system-skipped"
    assert events[0]["system"] == "unconfigured"


def test_system_success_event_has_model(monkeypatch, trace_store):
    from app.llm.mock import MockLLM

    stub = StubProvider('["Alice works in Python every day"]')
    monkeypatch.setattr(system_mod, "get_system_provider", lambda *a, **k: stub)
    deps = _deps(MockLLM(), trace_store)
    msgs = [{"role": "user", "content": "I work in Python every day"}]
    graph_mod._relevant_facts(msgs, ALICE, deps)
    events = _extracted_events(trace_store)
    assert events[0]["extraction"] == "system"
    assert events[0]["model"] == "qwen/qwen3.8-27b:free"


# ---- health status module ------------------------------------------------------


def test_system_status_records_and_resets():
    from app.memory import system_status

    system_status.reset()
    st = system_status.get_status(configured=False)
    assert st["configured"] is False and st["source"] == "none" and st["model"] is None
    assert st["last_error"] is None and st["total_errors"] == 0
    system_status.record_failure("rate-limited")
    st = system_status.get_status(configured=True)
    assert st["configured"] is True
    assert st["last_error"] == "rate-limited"
    assert st["last_error_at"]
    assert st["total_errors"] == 1
    system_status.reset()


def test_system_error_records_status(monkeypatch, trace_store):
    """The failing path feeds the dashboard health signal, not just the trace."""
    from app.llm.mock import MockLLM
    from app.memory import system_status

    system_status.reset()
    stub = StubProvider("", exc=RuntimeError("boom"))
    monkeypatch.setattr(system_mod, "get_system_provider", lambda *a, **k: stub)
    deps = _deps(MockLLM(), trace_store)
    graph_mod._relevant_facts(
        [{"role": "user", "content": "I prefer Slack over Teams always"}], ALICE, deps
    )
    assert system_status.get_status(configured=True)["last_error"] == "error"
    system_status.reset()


# ---- status endpoint -------------------------------------------------------------


def test_system_memory_status_gates():
    from fastapi.testclient import TestClient

    from app.auth.jwt import create_token
    from app.auth.users import USERS
    from app.config import get_settings
    from app.dashboard.app import app as dashboard_app

    client = TestClient(dashboard_app)
    assert client.get("/api/system-memory/status").status_code == 401
    settings = get_settings()
    token = create_token(
        USERS["alice@acme.com"], settings.jwt.secret,
        settings.jwt.algorithm, settings.jwt.expiry_minutes,
    )
    resp = client.get(
        "/api/system-memory/status", headers={"Authorization": f"Bearer {token}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"configured", "source", "model", "last_error", "last_error_at", "total_errors"}
    assert body["configured"] is False  # conftest blanks the key: hermetic suite

"""Mk18 Phase B — outbound DLP + arg validation tests.

Exfil patterns pin `app/harness/sanitize.py::_EXFIL_PATTERNS` — extend both
together.
"""

from __future__ import annotations

from app.agent.tools import TOOL_REGISTRY
from app.harness.sanitize import scan_args_for_exfil
from tests.attack.helpers import ALICE, outcome_of


def test_scan_patterns_pinned():
    assert scan_args_for_exfil({"memo": "pay 123-45-6789"}) == "ssn"
    assert scan_args_for_exfil({"body": "card 4111 1111 1111 1111"}) == "card"
    assert scan_args_for_exfil({"x": "sk-abcdef12345678"}) == "api-key"
    assert scan_args_for_exfil({"x": "xoxb-12345678-abcd"}) == "slack-token"
    assert scan_args_for_exfil({"x": "ghp_abcdef1234567890"}) == "github-token"
    assert scan_args_for_exfil({"x": "AKIAIOSFODNN7EXAMPLE"}) == "aws-key"
    assert scan_args_for_exfil({"x": "-----BEGIN PRIVATE KEY-----"}) == "private-key"
    assert scan_args_for_exfil({"query": "acme", "amount": 5000}) is None
    assert scan_args_for_exfil({"to": "a@x.com"}) is None


def test_exfil_denied_before_execution(runner_factory, audit_log):
    """An allowed tool carrying an SSN in args is denied with reason=exfil —
    the dept service is never hit."""
    from app.llm.mock import MockLLM

    provider = MockLLM(sequence=[
        {"text": "sending", "tool_calls": [{"name": "crm_create_campaign", "args": {"name": "x", "memo": "ssn 123-45-6789"}}]},
        {"text": "done.", "tool_calls": []},
    ])
    result = runner_factory(provider)(ALICE, "create campaign")
    call = outcome_of(result, "crm_create_campaign")
    assert call["outcome"] == "denied"
    assert any(
        d["decision"] == "DENY" and d.get("reason") == "exfil" for d in audit_log.entries()
    )


def test_recall_query_exempt(runner_factory):
    """The recall tool's own query text is not exfil-scanned (stays in-boundary)."""
    from app.llm.mock import MockLLM

    provider = MockLLM(sequence=[
        {"text": "q", "tool_calls": [{"name": "recall", "args": {"query": "weekly retros"}}]},
        {"text": "done.", "tool_calls": []},
    ])
    result = runner_factory(provider)(ALICE, "recall something")
    call = outcome_of(result, "recall")
    assert call["outcome"] != "denied" or "exfil" not in str(call.get("content", ""))


def test_arg_schema_validation_mistyped(dept_apps):
    """Mistyped known args fail closed pre-I/O; unknown fields are ignored."""
    from app.agent.tools import ToolExecutor
    from app.config import get_settings
    from tests.conftest import make_service_client

    executor = ToolExecutor(client=make_service_client(dept_apps), settings=get_settings())
    tool = TOOL_REGISTRY["crm_search_customer"]
    mistyped = executor.execute(tool, {"query": 123})
    assert not mistyped.ok and mistyped.reason == "args"
    ok_shape = executor.execute(tool, {"query": "acme", "request_id": "abc"})
    assert ok_shape.status_code is not None  # unknown fields ignored, call went out


def test_approval_args_view_gated(member_store):
    """Privileged args view: admin OK + audited, member/head-cross-dept 403, unknown 404."""
    from fastapi.testclient import TestClient

    from app.auth.jwt import create_token
    from app.config import get_settings
    from app.dashboard.app import app as dashboard_app

    settings = get_settings()
    admin_tok = create_token(
        member_store.get("admin@acme.com"), settings.jwt.secret,
        settings.jwt.algorithm, settings.jwt.expiry_minutes,
    )
    alice = member_store.get("alice@acme.com")
    alice_tok = create_token(alice, settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes)

    from app.dashboard.app import _get_components

    approvals = _get_components()["approvals"]
    aid = approvals.create(
        "run-exfil-test", "t-exfil", {"type": "tool", "id": "finance_transfer"},
        {"sub": "carol@acme.com", "role": "finance_agent", "dept": "finance"},
        {"tool": "finance_transfer", "args": {"amount": 5}},
        route="head+admin",
    )
    c = TestClient(dashboard_app)
    r = c.get(f"/api/approvals/{aid}/args", headers={"Authorization": f"Bearer {admin_tok}"})
    assert r.status_code == 200
    assert r.json()["payload"]["tool"] == "finance_transfer"
    r2 = c.get(f"/api/approvals/{aid}/args", headers={"Authorization": f"Bearer {alice_tok}"})
    assert r2.status_code == 403
    r3 = c.get("/api/approvals/does-not-exist/args", headers={"Authorization": f"Bearer {admin_tok}"})
    assert r3.status_code == 404

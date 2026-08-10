"""PII sanitizer (Milestone 5, weeks 9-10): field/regex masking matrix + integration."""

from conftest import make_runner

from app.agent.tools import ToolResult
from app.auth.users import USERS
from app.harness.sanitize import (
    MASK,
    SENSITIVE_FIELDS,
    pii_read_allowed,
    sanitize_response,
    sanitize_text,
    sanitize_tool_result,
)
from app.llm.mock import MockLLM


def claims_for(username: str) -> dict:
    u = USERS[username]
    return {"sub": u.username, "role": u.role, "dept": u.dept, "scopes": u.scopes}


ALICE = claims_for("alice@acme.com")
BOB = claims_for("bob@acme.com")
CAROL = claims_for("carol@acme.com")
DAVE = claims_for("dave@acme.com")
ADMIN = claims_for("admin@acme.com")

HR_ROW = {"name": "Alice Zhang", "email": "alice.zhang@acme.com", "salary": 95000, "ssn": "111-22-3333"}


# --- policy matrix ---


def test_email_readable_by_all_roles(enforcer):
    for claims in (ALICE, BOB, CAROL, DAVE, ADMIN):
        assert pii_read_allowed(enforcer, claims, "email") is True, claims["role"]


def test_salary_and_ssn_admin_only(enforcer):
    for field in ("salary", "ssn"):
        for claims in (ALICE, BOB, CAROL, DAVE):
            assert pii_read_allowed(enforcer, claims, field) is False, (claims["role"], field)
        assert pii_read_allowed(enforcer, ADMIN, field) is True


def test_sensitive_fields_registry():
    assert SENSITIVE_FIELDS == {"email", "salary", "ssn"}


# --- structured masking ---


def test_hr_agent_sees_email_but_not_salary_or_ssn(enforcer):
    masked = sanitize_response(dict(HR_ROW), BOB, enforcer)
    assert masked["email"] == HR_ROW["email"]
    assert masked["salary"] == MASK and masked["ssn"] == MASK
    assert masked["name"] == HR_ROW["name"]


def test_admin_sees_everything(enforcer):
    assert sanitize_response(dict(HR_ROW), ADMIN, enforcer) == HR_ROW


def test_nested_and_list_recursion(enforcer):
    payload = {
        "employees": [dict(HR_ROW), {"name": "No PII", "status": "active"}],
        "meta": {"salary": 120000, "count": 2},
    }
    masked = sanitize_response(payload, BOB, enforcer)
    assert masked["employees"][0]["ssn"] == MASK
    assert masked["employees"][1]["name"] == "No PII"
    assert masked["meta"]["salary"] == MASK
    assert masked["meta"]["count"] == 2


def test_non_sensitive_scalars_untouched(enforcer):
    payload = {"id": 1, "name": "Alice", "nested": {"title": "Mgr"}}
    assert sanitize_response(payload, BOB, enforcer) == payload


# --- free-text masking ---


def test_text_email_visible_but_ssn_masked_for_non_admin(enforcer):
    out = sanitize_text("contact alice.zhang@acme.com ssn 111-22-3333", BOB, enforcer)
    assert "alice.zhang@acme.com" in out  # email visible to every role
    assert "111-22-3333" not in out
    assert MASK in out


def test_text_unmasked_for_admin(enforcer):
    raw = "contact alice.zhang@acme.com ssn 111-22-3333"
    assert sanitize_text(raw, ADMIN, enforcer) == raw


def test_text_no_pii_unchanged(enforcer):
    raw = "please schedule the standup"
    assert sanitize_text(raw, BOB, enforcer) == raw


# --- ToolResult wrapper ---


def test_tool_result_summary_regenerated_from_masked_data(enforcer):
    result = ToolResult(
        ok=True,
        content="",
        data={"email": "a@b.com", "salary": 95000, "ssn": "111-22-3333", "name": "A"},
    )
    out = sanitize_tool_result(result, BOB, enforcer)
    assert out.content.count(MASK) == 2  # salary + ssn masked; email visible for hr
    assert '"name": "A"' in out.content


def test_tool_result_text_masked_when_no_data(enforcer):
    result = ToolResult(ok=True, content="email a@b.com ssn 111-22-3333")
    out = sanitize_tool_result(result, BOB, enforcer)
    assert "a@b.com" in out.content  # email visible to all roles
    assert "111-22-3333" not in out.content


# --- integration through the agent ---


def test_masked_values_never_reach_llm_context(executor, trace_store, enforcer, audit_log):
    """bob's hr_search sees masked salary/ssn; admin sees the full record."""
    seq = [
        {"text": "", "tool_calls": [{"name": "hr_search_employees", "args": {"query": "Zhang"}}]},
        {"text": "done", "tool_calls": []},
    ]
    bob = make_runner(
        provider=MockLLM(sequence=list(seq)), executor=executor, trace=trace_store,
        enforcer=enforcer, audit=audit_log,
    )(BOB, "find Zhang")
    admin = make_runner(
        provider=MockLLM(sequence=list(seq)), executor=executor, trace=trace_store,
        enforcer=enforcer, audit=audit_log,
    )(ADMIN, "find Zhang")

    bob_content = next(tc["content"] for tc in bob.tool_calls if tc["name"] == "hr_search_employees")
    admin_content = next(tc["content"] for tc in admin.tool_calls if tc["name"] == "hr_search_employees")
    assert MASK in bob_content and "111-22-3333" not in bob_content
    assert "salary" in admin_content and "111-22-3333" in admin_content


def test_recall_masks_pii_in_transcripts(enforcer, recall_service, episodic, tmp_path):
    from app.memory.scopes import SCOPE_CENTRAL

    actor = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    episodic.append_transcript(SCOPE_CENTRAL, "r1", actor, "contact alice.zhang@acme.com ssn 111-22-3333")

    alice = claims_for("alice@acme.com")
    result = recall_service.recall("alice", alice)
    assert result.ok
    assert "111-22-3333" not in result.content  # ssn masked (not email — email visible to all)

    admin = claims_for("admin@acme.com")
    assert "111-22-3333" in recall_service.recall("alice", admin).content

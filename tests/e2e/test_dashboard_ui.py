"""Dashboard UI end to end in a real browser (Playwright): every flow goes page -> dashboard
API -> stores, and each test asserts both what the page shows and what the backend stored.

Demo logins are the seeded accounts of the per-test tmp member store (README).
"""

from __future__ import annotations

import re

import pytest
from playwright.sync_api import Page, expect

from app.ops.schema import TraceEventType

ADMIN = ("admin@acme.com", "admin123")
ALICE = ("alice@acme.com", "alice123")
FAKE_KEY = "sk-e2e-not-a-real-key-123"  # must never be rendered back

LOGGED_OUT = re.compile(r"\blogged-out\b")


def login(page: Page, dash, who: tuple[str, str]) -> None:
    page.goto(dash.url + "/")
    expect(page.locator("body")).to_have_class(LOGGED_OUT)
    page.fill("#user", who[0])
    page.fill("#pass", who[1])
    page.click("#signin")
    expect(page.locator("body")).not_to_have_class(LOGGED_OUT)
    expect(page.locator("#nav .nav-item").first).to_be_visible()


def open_tab(page: Page, label: str) -> None:
    page.locator("#nav .nav-item", has_text=re.compile(rf"^\s*{re.escape(label)}\s*$")).click()


def nav_labels(page: Page) -> list[str]:
    return [t.strip() for t in page.locator("#nav .nav-item").all_inner_texts()]


def accept_dialogs(page: Page) -> list[str]:
    seen: list[str] = []

    def _handle(dialog) -> None:
        seen.append(dialog.message)
        dialog.accept()

    page.on("dialog", _handle)
    return seen


# ---- shell, login, navigation ---------------------------------------------------------


def test_bad_password_stays_on_the_login_gate(page: Page, dash):
    messages = accept_dialogs(page)
    page.goto(dash.url + "/")
    expect(page.locator(".login-hero")).to_be_visible()
    page.fill("#user", ADMIN[0])
    page.fill("#pass", "wrong-password")
    page.click("#signin")
    for _ in range(50):  # the alert arrives after the login round trip
        if messages:
            break
        page.wait_for_timeout(100)
    assert messages == ["invalid credentials"]
    expect(page.locator("body")).to_have_class(LOGGED_OUT)
    expect(page.locator("#sidebar")).to_be_hidden()


def test_admin_console_navigates_every_tab(page: Page, dash):
    login(page, dash, ADMIN)
    expect(page.locator("#crumb-view")).to_have_text("approvals")
    expect(page.locator("#sys")).to_contain_text("dashboard online")
    assert nav_labels(page) == [
        "Chat", "Account", "Approvals", "Members", "Departments",
        "Always allowed", "Skills", "Memory", "Ops", "Log out",
    ]
    for label, view in [
        ("Chat", "chat"), ("Account", "account"), ("Approvals", "approvals"), ("Members", "users"),
        ("Departments", "depts"), ("Always allowed", "allowlist"), ("Skills", "skills"),
        ("Memory", "memory"), ("Ops", "ops"),
    ]:
        open_tab(page, label)
        expect(page.locator(f"#{view}-view")).to_be_visible()
        expect(page.locator("#nav .nav-item.active")).to_have_text(label)
        expect(page.locator("#crumb-view")).to_have_text(label.lower())
    # data actually arrived from the backend
    open_tab(page, "Members")
    expect(page.locator("#users tr", has_text="alice@acme.com")).to_be_visible()
    open_tab(page, "Departments")
    expect(page.locator("#depts tr", has_text="marketing")).to_be_visible()


def test_member_console_is_scoped_and_fits_a_phone(page: Page, dash):
    login(page, dash, ALICE)
    assert nav_labels(page) == ["Chat", "Account", "Memory", "Log out"]
    open_tab(page, "Account")
    expect(page.locator("#int-catalog-admin")).to_be_hidden()
    page.set_viewport_size({"width": 375, "height": 812})
    page.wait_for_timeout(200)
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth"), "page scrolls sideways"
    box = page.locator("#sidebar").bounding_box()
    assert box and box["width"] >= 370, "sidebar should become a full-width top bar on phones"


def test_log_out_returns_to_the_gate(page: Page, dash):
    login(page, dash, ALICE)
    open_tab(page, "Log out")
    expect(page.locator("body")).to_have_class(LOGGED_OUT)
    expect(page.locator("#login-view")).to_be_visible()


# ---- approvals: masked card, audited full-args reveal ---------------------------------


def test_approval_card_masks_args_and_reveal_is_audited(page: Page, dash):
    dash.approvals.create(
        "r1", "t1",
        {"type": "tool", "id": "finance_transfer"},
        {"sub": "carol@acme.com", "role": "finance_agent", "dept": "finance"},
        {"tool": "finance_transfer",
         "args": {"from_account": "1000", "to_account": "2000", "amount": 5000, "memo": "vendor payment"}},
    )
    login(page, dash, ADMIN)
    card = page.locator("#approvals .card").first
    expect(card).to_contain_text('"memo": "***"')
    expect(card).to_contain_text("transfer amount=5000")
    expect(card).not_to_contain_text("vendor payment")
    assert "approval:args_view" not in (dash.audit_path.read_text() if dash.audit_path.exists() else "")

    card.get_by_role("button", name="Reveal full arguments").click()
    full = card.locator("pre[data-full]")
    expect(full).to_be_visible()
    expect(full).to_contain_text('"memo": "vendor payment"')
    expect(full).to_contain_text('"from_account": "1000"')
    assert "approval:args_view" in dash.audit_path.read_text(), "reveal must be audit-logged"

    card.get_by_role("button", name="Hide full arguments").click()
    expect(full).to_be_hidden()


# ---- LLM keys: admin manages a member's, member edits their own -----------------------


def test_admin_manages_a_members_llm_keys(page: Page, dash):
    assert dash.members.count_active_admins() == 1  # single admin: writes apply directly
    accept_dialogs(page)
    login(page, dash, ADMIN)
    open_tab(page, "Members")
    page.locator("#users tr", has_text="alice@acme.com").get_by_role("button", name="Keys").click()
    panel = page.locator("#member-keys")
    expect(panel).to_be_visible()
    expect(page.locator("#member-keys-title")).to_have_text("LLM keys — alice@acme.com")
    expect(page.locator("#member-keys-list")).to_contain_text("No keys for this member")

    # add
    page.select_option("#mk-provider", "openai")
    page.fill("#mk-model", "gpt-4o-mini")
    page.fill("#mk-key", FAKE_KEY)
    page.click("#mk-save")
    expect(page.locator("#member-keys-status")).to_contain_text("Key saved and set active for alice@acme.com")
    row = page.locator("#member-keys-list tr", has_text="openai")
    expect(row).to_contain_text("gpt-4o-mini")
    expect(row).to_contain_text("Active")
    assert FAKE_KEY not in page.content()
    keys = dash.members.list_llm_keys("alice@acme.com")
    assert [(k.provider, k.model, k.active) for k in keys] == [("openai", "gpt-4o-mini", True)]

    # edit the model inline (Enter saves)
    row.get_by_role("button", name="Edit").click()
    row.locator(".model-input").fill("gpt-4o")
    row.locator(".model-input").press("Enter")
    expect(page.locator("#member-keys-status")).to_have_text("Model updated.")
    expect(page.locator("#member-keys-list tr", has_text="openai")).to_contain_text("gpt-4o")
    assert dash.members.list_llm_keys("alice@acme.com")[0].model == "gpt-4o"

    # a second key becomes active; re-activate the first from the table
    page.select_option("#mk-provider", "groq")
    page.fill("#mk-model", "llama-3.3-70b-versatile")
    page.fill("#mk-key", FAKE_KEY + "-2")
    page.click("#mk-save")
    expect(page.locator("#member-keys-list tr")).to_have_count(2)
    openai_row = page.locator("#member-keys-list tr", has_text="openai")
    openai_row.get_by_role("button", name="Activate").click()
    expect(page.locator("#member-keys-status")).to_have_text("Key activated.")
    active = {k.provider: k.active for k in dash.members.list_llm_keys("alice@acme.com")}
    assert active == {"openai": True, "groq": False}

    # delete (confirm accepted)
    page.locator("#member-keys-list tr", has_text="groq").get_by_role("button", name="Delete").click()
    expect(page.locator("#member-keys-status")).to_have_text("Key deleted.")
    expect(page.locator("#member-keys-list tr")).to_have_count(1)
    assert [k.provider for k in dash.members.list_llm_keys("alice@acme.com")] == ["openai"]

    page.click("#member-keys-close")
    expect(panel).to_be_hidden()


def test_member_key_write_waits_for_a_second_admin(page: Page, dash):
    dash.members.create("admin2@acme.com", "longpw123", role="admin", dept="admin", actor="admin@acme.com")
    login(page, dash, ADMIN)
    open_tab(page, "Members")
    page.locator("#users tr", has_text="alice@acme.com").get_by_role("button", name="Keys").click()
    page.select_option("#mk-provider", "openai")
    page.fill("#mk-model", "gpt-4o-mini")
    page.fill("#mk-key", FAKE_KEY)
    page.click("#mk-save")
    expect(page.locator("#member-keys-status")).to_contain_text("Sent to a second admin to confirm")
    assert dash.members.list_llm_keys("alice@acme.com") == []  # nothing written yet
    open_tab(page, "Approvals")
    queue = page.locator("#second-eyes")
    expect(queue).to_contain_text("Save a member's LLM key")
    expect(queue).to_contain_text("a different admin must confirm")
    expect(queue).to_contain_text('"api_key_enc": "***"')  # the secret never reaches the page
    assert FAKE_KEY not in page.content()


def test_member_edits_own_key_model(page: Page, dash):
    login(page, dash, ALICE)
    open_tab(page, "Account")
    page.select_option("#acc-provider", "openai")
    page.fill("#acc-model", "gpt-4o-mini")
    page.fill("#acc-key", FAKE_KEY)
    page.click("#acc-save")
    row = page.locator("#acc-keys tr", has_text="openai")
    expect(row).to_contain_text("gpt-4o-mini")
    # regression: the model cell used to render a stray "}" and Edit/Save never worked
    assert "}" not in row.locator("td").nth(1).inner_text()
    row.get_by_role("button", name="Edit").click()
    row.locator(".model-input").fill("gpt-4.1-mini")
    row.get_by_role("button", name="Save").click()
    expect(page.locator("#acc-status")).to_have_text("Model updated.")
    expect(page.locator("#acc-keys tr", has_text="openai")).to_contain_text("gpt-4.1-mini")
    assert dash.members.list_llm_keys("alice@acme.com")[0].model == "gpt-4.1-mini"
    # Cancel restores the saved value without a request
    row = page.locator("#acc-keys tr", has_text="openai")
    row.get_by_role("button", name="Edit").click()
    row.locator(".model-input").fill("discard-me")
    row.get_by_role("button", name="Cancel").click()
    expect(row.locator(".model-display")).to_contain_text("gpt-4.1-mini")
    assert dash.members.list_llm_keys("alice@acme.com")[0].model == "gpt-4.1-mini"


# ---- integrations catalog: enable / disable ------------------------------------------


def test_admin_disables_and_re_enables_a_catalog_app(page: Page, dash):
    dash.members.upsert_toolkit("notion", enabled=True, auth_config_id="ac_e2e", actor="test")
    login(page, dash, ADMIN)
    open_tab(page, "Account")
    row = page.locator("#int-catalog tr", has_text="notion")
    expect(row).to_contain_text("enabled")
    expect(row).to_contain_text("ready")
    expect(page.locator("#int-apps")).to_contain_text("notion")

    row.get_by_role("button", name="Disable").click()
    expect(page.locator("#int-catalog-status")).to_contain_text("notion disabled")
    expect(page.locator("#int-catalog tr", has_text="notion")).to_contain_text("disabled")
    expect(page.locator("#int-apps")).not_to_contain_text("notion")  # gone for members too
    assert [t["enabled"] for t in dash.members.list_toolkits(enabled_only=False) if t["toolkit"] == "notion"] == [False]

    # loadCatalog() drops stale responses, so the row on screen is the final one
    expect(page.locator("#int-catalog tr", has_text="notion").get_by_role("button", name="Enable")).to_be_enabled()
    page.locator("#int-catalog tr", has_text="notion").get_by_role("button", name="Enable").click()
    expect(page.locator("#int-catalog-status")).to_contain_text("notion enabled")
    expect(page.locator("#int-apps")).to_contain_text("notion")
    assert [t["enabled"] for t in dash.members.list_toolkits(enabled_only=False) if t["toolkit"] == "notion"] == [True]
    rows = [r.replace(" ", "") for r in dash.policy_path.read_text().splitlines()]
    assert "p,marketing_agent,marketing,notion,write" in rows, "re-enabling must restore the policy rows"


# ---- ops: trajectory timeline + source filter -----------------------------------------


def test_ops_trajectory_timeline_and_source_filter(page: Page, dash):
    actor = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    run_id = dash.trace.start_run(actor, {"message": "launch the campaign"})
    dash.trace.add_event(run_id, TraceEventType.TOOL_CALL, actor, {"tool": "crm_create_campaign"}, source="tool")
    dash.trace.add_event(run_id, TraceEventType.GUARDRAIL, actor, {"type": "pii"}, source="guardrail")
    dash.trace.end_run(run_id, {"reply": "done"})
    dash.trace.flush()

    login(page, dash, ADMIN)
    open_tab(page, "Ops")
    run_row = page.locator("#ops-runs tr", has_text="alice@acme.com")
    expect(run_row).to_be_visible()
    run_row.get_by_role("button", name="View").click()
    expect(page.locator("#ops-run-title")).to_have_text("Run " + run_id[:8])
    events = page.locator("#ops-trajectory .card.event")
    expect(events.first).to_be_visible()
    assert events.count() >= 4
    assert page.locator("#ops-trajectory .card.event .node").count() == events.count()
    expect(page.locator('#ops-trajectory .card.event[data-source="guardrail"]')).to_have_count(1)

    page.locator("#ops-filters button", has_text="tool").click()
    expect(page.locator("#ops-trajectory .card.event")).to_have_count(1)
    expect(page.locator("#ops-trajectory .card.event")).to_have_attribute("data-source", "tool")

    page.click("#ops-back")
    expect(page.locator("#ops-runs-view")).to_be_visible()


@pytest.mark.parametrize("width", [375, 768, 1280])
def test_no_sideways_page_scroll_on_admin_views(page: Page, dash, width):
    login(page, dash, ADMIN)
    page.set_viewport_size({"width": width, "height": 900})
    for label in ("Approvals", "Members", "Departments", "Ops", "Account"):
        open_tab(page, label)
        page.wait_for_timeout(150)
        overflow = page.evaluate("document.documentElement.scrollWidth - window.innerWidth")
        assert overflow <= 0, f"{label} at {width}px scrolls sideways by {overflow}px"

"""Mk21 dashboard refresh, in a real browser.

Chat: the plan notes chat had no automated coverage (tests/test_chat_web.py drives the API with
TestClient only). Here the page itself is exercised — composer, keyboard, bubbles, layout,
attachments — with `/api/chat*` answered by Playwright route stubs, so no gateway or LLM is
needed. The server-side chat path stays covered by tests/test_chat_web.py.
"""

from __future__ import annotations

import json
import re

from playwright.sync_api import Page, Route, expect

from app.ops.schema import TraceEventType

from .test_dashboard_ui import ADMIN, ALICE, login, open_tab

THREAD = "user:alice@acme.com:mk21"


def stub_chat(page: Page, reply: str = "Noted.") -> list[dict]:
    """Answer the chat endpoints in the browser; return the list of sent bodies."""
    sent: list[dict] = []
    threads: list[str] = []

    def chat(route: Route) -> None:
        if route.request.method != "POST":
            route.fallback()
            return
        body = json.loads(route.request.post_data or "{}")
        sent.append(body)
        threads[:] = [THREAD]
        route.fulfill(json={"reply": reply, "thread_id": THREAD, "tool_calls": []})

    page.route("**/api/chat", chat)
    page.route("**/api/chat/threads", lambda route: route.fulfill(json=list(threads)))
    return sent


# ---- Phase 2: layout ------------------------------------------------------------------


def test_chat_composer_sends_on_enter_and_keeps_shift_enter_newlines(page: Page, dash):
    sent = stub_chat(page, reply="Budget noted for the launch.")
    login(page, dash, ALICE)  # members land on Chat
    expect(page.locator("#chat-view h2")).to_have_text("Chat")  # no "(chats as …)" suffix
    box = page.locator("#chat-msg")
    expect(box).to_have_attribute("placeholder", "message…")

    box.fill("first line")
    box.press("Shift+Enter")
    box.type("second line")
    assert sent == [], "Shift+Enter must not send"
    assert box.input_value() == "first line\nsecond line"

    box.press("Enter")
    expect(page.locator(".chat-user")).to_have_text("first line\nsecond line")
    expect(page.locator(".chat-assistant")).to_contain_text("Budget noted for the launch.")
    assert sent[0]["message"] == "first line\nsecond line"
    expect(box).to_have_value("")


def test_send_is_a_round_icon_button(page: Page, dash):
    stub_chat(page)
    login(page, dash, ALICE)
    send = page.get_by_role("button", name="Send")
    expect(send.locator("svg")).to_have_count(1)
    b = send.bounding_box()
    assert b and abs(b["width"] - b["height"]) <= 1 and b["width"] <= 48
    assert send.evaluate("el => getComputedStyle(el).borderTopLeftRadius") == "50%"


def test_transcript_fills_the_height_not_60vh(page: Page, dash):
    stub_chat(page)
    login(page, dash, ALICE)
    viewport = page.viewport_size["height"]
    t = page.locator("#chat-transcript").bounding_box()
    composer = page.locator("#chat-msg").bounding_box()
    assert t["height"] > 0.6 * viewport, f"transcript {t['height']}px is still capped near 60vh of {viewport}px"
    assert composer["y"] + composer["height"] <= viewport, "composer must stay on screen"


def test_long_model_name_never_leaves_the_chat_card(page: Page, dash):
    long_model = "openrouter/" + "very-long-model-identifier-" * 6 + "free"
    dash.members.set_llm_key("alice@acme.com", "openrouter", long_model, "sk-e2e-not-real",
                             active=True, actor="alice@acme.com")
    stub_chat(page)
    login(page, dash, ALICE)
    select = page.locator("#chat-model-select")
    expect(select).to_contain_text("very-long-model-identifier")
    s, view = select.bounding_box(), page.locator("#chat-view").bounding_box()
    assert s["x"] + s["width"] <= view["x"] + view["width"] + 0.5
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_department_identifiers_do_not_break_mid_word(page: Page, dash):
    login(page, dash, ADMIN)
    open_tab(page, "Departments")
    cell = page.locator("#depts tr", has_text="marketing").locator("td").nth(5)  # Role
    expect(cell).to_contain_text("marketing_agent")
    lines = cell.evaluate(
        "el => { const r = document.createRange(); r.selectNodeContents(el);"
        " return new Set([...r.getClientRects()].map(x => Math.round(x.top))).size; }")
    assert lines == 1, "marketing_agent wrapped onto several lines"
    assert re.search(r"marketing_agent", cell.inner_text())


# ---- Phase 3: collapsible navigation ---------------------------------------------------

ADMIN_NAV = ["Chat", "Account", "Approvals", "Members", "Departments",
             "Always allowed", "Skills", "Memory", "Ops", "Log out"]


def test_nav_collapses_to_an_icon_rail_and_restores_after_reload(page: Page, dash):
    from .test_dashboard_ui import nav_labels

    login(page, dash, ADMIN)
    toggle = page.locator("#topbar #nav-toggle")
    wide = page.locator("#sidebar").bounding_box()["width"]
    toggle.click()
    expect(page.locator("body")).to_have_class(re.compile(r"\bnav-collapsed\b"))
    expect(toggle).to_have_attribute("aria-expanded", "false")
    page.wait_for_timeout(350)  # width transition (instant under reduced motion)
    rail = page.locator("#sidebar").bounding_box()["width"]
    assert rail <= 80 < wide, (rail, wide)

    # labels are clipped, not removed: every item still reads and still works
    assert nav_labels(page) == ADMIN_NAV
    assert page.locator("#nav .nav-item", has_text="Ops").get_attribute("title") == "Ops"
    open_tab(page, "Ops")
    expect(page.locator("#ops-view")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")

    page.reload()
    expect(page.locator("body")).to_have_class(re.compile(r"\bnav-collapsed\b"))
    assert page.locator("#sidebar").bounding_box()["width"] <= 80

    page.locator("#nav-toggle").click()
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bnav-collapsed\b"))
    page.reload()
    expect(page.locator("body")).not_to_have_class(re.compile(r"\bnav-collapsed\b"))


def test_collapsed_state_is_ignored_on_phones(page: Page, dash):
    login(page, dash, ADMIN)
    page.locator("#nav-toggle").click()  # remembered as collapsed
    page.set_viewport_size({"width": 375, "height": 812})
    page.wait_for_timeout(350)
    expect(page.locator("#nav-toggle")).to_be_hidden()
    assert page.locator("#sidebar").bounding_box()["width"] >= 370
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# ---- Phase 4: single session ----------------------------------------------------------

MEMBER_NAV = ["Chat", "Account", "Memory", "Log out"]


def _cookie_names(page: Page) -> set[str]:
    return {c["name"] for c in page.context.cookies()}


def test_no_sessions_section_or_second_sign_in(page: Page, dash):
    login(page, dash, ADMIN)
    for gone in (".side-foot", "#switcher", "#who", "#login-cancel"):
        expect(page.locator(gone)).to_have_count(0)
    expect(page.locator("#sidebar")).not_to_contain_text("Sessions")
    expect(page.locator("#login-view")).to_be_hidden()


def test_signed_in_browser_survives_a_reload(page: Page, dash):
    from .test_dashboard_ui import nav_labels

    login(page, dash, ALICE)
    assert "harness_token_member" in _cookie_names(page), "the httpOnly session cookie must be stored"
    stored = page.evaluate("JSON.parse(localStorage.getItem('harness_claims')).sub")
    assert stored == "alice@acme.com"
    # the first data request after a reload is answered by the cookie session alone
    with page.expect_response(lambda r: "/api/account/llm-keys" in r.url) as first:
        page.reload()
    assert first.value.status == 200, "cookie session must survive the reload"
    assert nav_labels(page) == MEMBER_NAV
    expect(page.locator("body")).not_to_have_class(re.compile(r"\blogged-out\b"))  # cookie still valid


def test_a_fresh_login_replaces_the_previous_identity(page: Page, dash):
    from .test_dashboard_ui import nav_labels

    login(page, dash, ALICE)
    with page.expect_response(lambda r: r.url.endswith("/api/logout")) as bye:
        open_tab(page, "Log out")
    assert bye.value.status == 200, "logout must pass the CSRF gate (it used to 403 and keep the cookies)"
    expect(page.locator("body")).to_have_class(re.compile(r"\blogged-out\b"))
    assert not {"harness_token_member", "harness_token_admin"} & _cookie_names(page), "logout clears every slot"
    assert page.evaluate("localStorage.getItem('harness_claims')") is None

    login(page, dash, ADMIN)
    assert nav_labels(page) == ADMIN_NAV
    assert "harness_token_member" not in _cookie_names(page), "the first session is gone"
    me = page.request.get(dash.url + "/api/memory/me").json()
    assert any(x["scope"] == "user/admin@acme.com" for x in me)
    assert not any(x["scope"] == "user/alice@acme.com" for x in me)


def test_pre_mk21_browser_storage_is_migrated_once(page: Page, dash):
    from .test_dashboard_ui import nav_labels

    login(page, dash, ADMIN)  # real cookie session for the admin slot
    page.evaluate("""() => {
        const claims = localStorage.getItem('harness_claims');
        localStorage.removeItem('harness_claims');
        localStorage.setItem('harness_admin_claims', claims);
        localStorage.setItem('harness_member_claims', JSON.stringify({sub: 'alice@acme.com', role: 'marketing_agent', dept: 'marketing'}));
        localStorage.setItem('harness_active', 'admin');
        localStorage.setItem('harness_admin_token', 'stale');
    }""")
    with page.expect_response(lambda r: "/api/approvals/pending" in r.url) as first:
        page.reload()
    assert first.value.status == 200
    assert nav_labels(page) == ADMIN_NAV
    keys = page.evaluate("Object.keys(localStorage)")
    assert "harness_claims" in keys
    for legacy in ("harness_admin_claims", "harness_member_claims", "harness_active", "harness_admin_token"):
        assert legacy not in keys, legacy
    assert page.evaluate("JSON.parse(localStorage.getItem('harness_claims')).sub") == "admin@acme.com"


# ---- Phase 5: members dropdowns -------------------------------------------------------


def _open_member(page: Page, username: str) -> None:
    open_tab(page, "Members")
    page.locator("#users tr", has_text=username).get_by_role("button", name="Edit").click()
    expect(page.locator("#member-editor")).to_be_visible()
    expect(page.locator("#mem-role")).not_to_have_value("")  # both lists loaded and filled


def test_member_editor_offers_roles_and_department_scoped_scopes(page: Page, dash):
    login(page, dash, ADMIN)
    expect(page.locator("#users-view")).not_to_contain_text("hashed (scrypt)")
    _open_member(page, "alice@acme.com")

    roles = page.locator("#mem-role option").all_inner_texts()
    for role in ("admin", "marketing_agent", "marketing_head", "finance_agent", "finance_head"):
        assert role in roles, role
    expect(page.locator("#mem-role")).to_have_value("marketing_agent")
    expect(page.locator("#mem-dept")).to_have_value("marketing")

    chips = page.locator("#mem-scope-chips .chip")
    expect(chips).to_have_count(2)  # crm:read, crm:create
    candidates = page.locator("#mem-scope-add option").all_inner_texts()
    assert "crm:delete" in candidates and "memory_dept" in candidates
    assert not any(c.startswith("finance:") for c in candidates), "marketing must not see finance scopes"

    # add one, remove one, save: the backend gets the comma list the API always took
    page.select_option("#mem-scope-add", "crm:delete")
    page.locator("#mem-scope-chips .chip", has_text="crm:create").get_by_role("button").click()
    expect(page.locator("#mem-scopes")).to_have_value("crm:read, crm:delete")
    page.click("#member-save")
    expect(page.locator("#member-editor")).to_be_hidden()
    assert sorted(dash.members.get("alice@acme.com").scopes) == ["crm:delete", "crm:read"]
    row = page.locator("#users tr", has_text="alice@acme.com")
    expect(row.locator(".chip")).to_have_count(2)


def test_choosing_a_department_suggests_its_role_and_drops_foreign_scopes(page: Page, dash):
    login(page, dash, ADMIN)
    open_tab(page, "Members")
    page.click("#add-member")
    expect(page.locator("#mem-role")).not_to_have_value("")
    page.select_option("#mem-dept", "finance")
    expect(page.locator("#mem-role")).to_have_value("finance_agent")
    page.select_option("#mem-scope-add", "finance:transfer")
    expect(page.locator("#mem-scope-chips .chip")).to_have_count(1)
    page.select_option("#mem-dept", "marketing")
    expect(page.locator("#mem-role")).to_have_value("marketing_agent")
    expect(page.locator("#mem-scope-chips .chip")).to_have_count(0)  # finance:transfer dropped

    page.fill("#mem-username", "newbie@acme.com")
    page.fill("#mem-password", "longpass123")
    page.select_option("#mem-scope-add", "crm:read")
    page.click("#member-save")
    expect(page.locator("#users tr", has_text="newbie@acme.com")).to_be_visible()
    m = dash.members.get("newbie@acme.com")
    assert (m.role, m.dept, list(m.scopes)) == ("marketing_agent", "marketing", ["crm:read"])


def test_many_scopes_never_push_the_page_sideways(page: Page, dash):
    login(page, dash, ADMIN)
    page.set_viewport_size({"width": 375, "height": 812})
    _open_member(page, "admin@acme.com")
    for value in page.locator("#mem-scope-add option").evaluate_all("os => os.map(o => o.value).filter(Boolean)")[:12]:
        page.select_option("#mem-scope-add", value)
    picker = page.locator("#mem-scope-chips").bounding_box()
    assert picker["height"] <= 120, "the chip area must scroll inside itself"
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


# ---- Phase 6: departments readability -------------------------------------------------


def test_department_lists_are_tags_with_an_inline_detail_row(page: Page, dash):
    login(page, dash, ADMIN)
    open_tab(page, "Departments")
    row = page.locator("#depts tr:not(.dept-detail)", has_text="marketing")
    resources = row.locator("td").nth(3)
    expect(resources.locator(".chip:not(.more)")).to_have_count(3)  # at most three tags
    more = resources.locator("button.chip.more")
    expect(more).to_have_text(re.compile(r"^\+\d+$"))
    hidden_count = int(more.inner_text()[1:])

    detail = page.locator('#depts tr.dept-detail[data-detail="marketing"]')
    expect(detail).to_be_hidden()
    more.click()
    expect(detail).to_be_visible()
    expect(more).to_have_attribute("aria-expanded", "true")
    expect(detail.locator(".detail-block").first.locator(".chip")).to_have_count(3 + hidden_count)
    expect(detail).to_contain_text("alice@acme.com")  # members listed in full
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    more.click()
    expect(detail).to_be_hidden()


def test_add_department_form_is_labelled_and_works(page: Page, dash):
    login(page, dash, ADMIN)
    open_tab(page, "Departments")
    expect(page.locator("#depts-view")).not_to_contain_text("Register department")
    page.get_by_role("button", name="+ Add department").click()
    editor = page.locator("#dept-editor")
    expect(editor.locator("h3")).to_have_text("Add department")
    for label in ("Name", "Service label", "Service URL", "Resources", "Member role", "Approvals"):
        expect(editor.get_by_label(label, exact=True)).to_be_visible()
    expect(editor).to_contain_text("also adds the member and head policy rows")  # moved into the form

    editor.get_by_label("Name", exact=True).fill("legal")
    editor.get_by_label("Resources", exact=True).fill("contracts")
    editor.get_by_role("button", name="Add", exact=True).click()
    expect(page.locator("#depts tr:not(.dept-detail)", has_text="legal")).to_be_visible()
    assert dash.members.get_department("legal") is not None
    rows = [r.replace(" ", "") for r in dash.policy_path.read_text().splitlines()]
    assert any(r.startswith("p,legal_agent,legal,contracts,") for r in rows)


# ---- Phase 7: memory department view --------------------------------------------------


def _seed_memory(dash) -> None:
    from app.memory.extract import Fact

    for scope, text in [
        ("central", "Company holiday falls on Friday"),
        ("dept/marketing", "Our team prefers morning stand-ups"),
        ("user/alice@acme.com", "Alice likes green tea"),
        ("dept/hr", "HR reviews leave on Mondays"),
        ("user/bob@acme.com", "Bob prefers email over chat"),
    ]:
        dash.memory.apply(Fact(text=text, scope=scope))


def _facts_shown(page: Page) -> str:
    """Every fact on screen: editable ones are <input> values, read-only ones are text."""
    return page.locator("#memory-scopes").evaluate(
        "el => [...el.querySelectorAll('input[data-text]')].map(i => i.value).join(' | ') + ' | ' + el.innerText")


def _memory_scope_titles(page: Page) -> list[str]:
    # textContent, not innerText: the <h3> is text-transform: uppercase on screen
    return [t.strip() for t in page.locator("#memory-scopes .card h3 code").all_text_contents()]


def test_memory_shows_one_department_at_a_time_and_searches_within_it(page: Page, dash):
    _seed_memory(dash)
    login(page, dash, ADMIN)
    open_tab(page, "Memory")
    sel = page.locator("#memory-view-select")
    expect(sel.locator("option")).to_have_count(6)  # admin, devops, finance, hr, marketing + Central
    expect(page.locator("#memory")).not_to_contain_text("saved in shared DB")
    expect(page.locator("#plat-key")).to_be_visible()  # the key/model controls stay

    sel.select_option("marketing")
    expect(page.locator("#memory-scopes .card h3 code").first).to_have_text("dept/marketing")
    titles = _memory_scope_titles(page)
    assert titles[0] == "dept/marketing" and "user/alice@acme.com" in titles
    assert not any(t in titles for t in ("central", "dept/hr", "user/bob@acme.com"))
    assert "Our team prefers morning stand-ups" in _facts_shown(page)

    page.fill("#memory-search", "GREEN")  # case-insensitive substring
    expect(page.locator("#memory-count")).to_have_text("1 fact")
    assert _memory_scope_titles(page) == ["user/alice@acme.com"]
    page.fill("#memory-search", "holiday")  # central is not in this selection
    expect(page.locator("#memory-scopes")).to_contain_text("No facts match")

    sel.select_option(label="Central")
    assert _memory_scope_titles(page) == ["central"]
    assert "Company holiday falls on Friday" in _facts_shown(page)
    page.fill("#memory-search", "")
    expect(page.locator("#memory-scopes input[placeholder='new fact...']")).to_be_visible()  # admin writes central


def test_a_head_sees_only_their_department_and_central(page: Page, dash):
    _seed_memory(dash)
    dash.members.create("mona@acme.com", "longpw123", role="marketing_head", dept="marketing", actor="admin@acme.com")
    login(page, dash, ("mona@acme.com", "longpw123"))
    open_tab(page, "Memory")
    expect(page.locator("#memory-view-select option")).to_have_count(2)
    labels = page.locator("#memory-view-select option").all_text_contents()
    assert labels == ["Department: marketing", "Central"]
    expect(page.locator("#memory-scopes .card").first).to_be_visible()
    shown = _facts_shown(page)
    assert "Our team prefers morning stand-ups" in shown and "HR reviews leave" not in shown
    dept_card = page.locator("#memory-scopes .card", has=page.locator("h3 code", has_text="dept/marketing"))
    expect(dept_card.locator("input[placeholder='new fact...']")).to_be_visible()  # own dept is editable


# ---- Phase 8: skills page + generation ------------------------------------------------

E2E_DRAFT = """---
name: e2e_followup
trigger: Follow up with a customer
scope: dept/marketing
owner_role: marketing_agent
version: 1
args_schema:
  customer: {type: string, description: customer name}
steps:
  - Search for the customer
tool_calls:
  - tool: crm_search_customer
    args: {query: "{{customer}}"}
---

# Follow up

Finds the customer so the **team** can follow up.
"""


def test_skill_cards_show_markdown_with_tool_calls_on_demand(page: Page, dash):
    login(page, dash, ADMIN)
    open_tab(page, "Skills")
    card = page.locator("#skills .skill-card", has_text="launch_campaign")
    expect(card.locator(".skill-body h4")).to_have_text("Launch campaign")  # markdown, rendered
    expect(card.locator(".chip", has_text="granted to marketing_agent")).to_be_visible()
    expect(page.locator("#skills-view")).not_to_contain_text("policy row")
    json_block = card.locator("pre[data-json]")
    expect(json_block).to_be_hidden()
    card.get_by_role("button", name="Tool calls").click()
    expect(json_block).to_be_visible()
    expect(json_block).to_contain_text("crm_create_campaign")


def test_generated_draft_is_saved_only_by_a_human_and_granted_explicitly(page: Page, dash):
    sent: list[dict] = []

    def generate(route: Route) -> None:
        sent.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"draft": E2E_DRAFT, "valid": True, "error": None, "name": "e2e_followup",
                            "exists": False, "scope": "dept/marketing", "default_grant": ["marketing_agent"],
                            "model": "qwen/test:free"})

    page.route("**/api/skills/generate", generate)
    login(page, dash, ADMIN)
    open_tab(page, "Skills")
    page.get_by_role("button", name="Generate draft").click()
    page.select_option("#gen-scope", "dept/marketing")
    boxes = page.locator("#gen-grant input[type=checkbox]")
    expect(boxes.first).to_be_attached()
    checked = [b.get_attribute("value") for b in boxes.all() if b.is_checked()]
    assert checked == ["marketing_agent"], "default grant is the scope's own role only"

    page.fill("#gen-brief", "follow up with a customer")
    page.get_by_role("button", name="Generate", exact=True).click()
    expect(page.locator("#skill-editor")).to_be_visible()
    assert sent == [{"scope": "dept/marketing", "brief": "follow up with a customer"}]
    expect(page.locator("#skill-content")).to_have_value(E2E_DRAFT)
    expect(page.locator("#skill-grant-note")).to_contain_text("marketing_agent")

    # drafted, not written: the registry and the policy file do not know it yet
    names = [s["name"] for s in page.request.get(dash.url + "/api/skills").json()["skills"]]
    assert "e2e_followup" not in names
    assert "skill_e2e_followup" not in dash.policy_path.read_text()

    page.click("#save-skill")
    card = page.locator("#skills .skill-card", has_text="e2e_followup")
    expect(card.locator(".chip", has_text="granted to marketing_agent")).to_be_visible()
    rows = [r.replace(" ", "") for r in dash.policy_path.read_text().splitlines()]
    assert "p,marketing_agent,marketing,skill_e2e_followup,use" in rows
    assert not any(r.startswith("p,marketing_head,") and "skill_e2e_followup" in r for r in rows)


def test_cancelling_a_draft_grants_nothing(page: Page, dash):
    page.route("**/api/skills/generate", lambda route: route.fulfill(json={
        "draft": E2E_DRAFT, "valid": True, "error": None, "name": "e2e_followup", "exists": False,
        "scope": "dept/marketing", "default_grant": ["marketing_agent"], "model": ""}))
    login(page, dash, ADMIN)
    open_tab(page, "Skills")
    page.get_by_role("button", name="Generate draft").click()
    page.fill("#gen-brief", "anything")
    page.get_by_role("button", name="Generate", exact=True).click()
    page.click("#cancel-skill")
    expect(page.locator("#skills")).to_be_visible()
    assert "skill_e2e_followup" not in dash.policy_path.read_text()


# ---- Phase 9: ops monitoring ----------------------------------------------------------


def _seed_runs(dash, who: list[tuple[str, str, bool]]) -> None:
    for sub, role, resumed in who:
        actor = {"sub": sub, "role": role, "dept": role.split("_")[0]}
        rid = dash.trace.start_run(actor, {"message": "hello"})
        dash.trace.add_event(rid, TraceEventType.TOOL_CALL, actor, {"tool": "crm_search_customer"}, source="tool")
        dash.trace.end_run(rid, {"resumed": True} if resumed else {"reply": "ok"})
    dash.trace.flush()


def _mark_rows(page: Page) -> None:
    page.locator("#ops-runs tr").evaluate_all("rows => rows.forEach(r => { r.__kept = true; })")


def _rows_kept(page: Page) -> bool:
    return page.locator("#ops-runs tr").evaluate_all("rows => rows.length > 0 && rows.every(r => r.__kept === true)")


def test_ops_summary_search_and_filters(page: Page, dash):
    from datetime import UTC, datetime, timedelta

    _seed_runs(dash, [("alice@acme.com", "marketing_agent", False), ("bob@acme.com", "hr_agent", False),
                      ("carol@acme.com", "finance_agent", True)])
    login(page, dash, ADMIN)
    open_tab(page, "Ops")
    expect(page.locator("#ops-runs-view h2")).to_have_text("Run trajectories")  # no subtitle
    summary = page.locator("#ops-summary")
    expect(summary).to_contain_text("3 runs")
    expect(summary.locator('[data-outcome="completed"]')).to_have_text("completed 2")
    expect(summary.locator('[data-outcome="resumed"]')).to_have_text("resumed 1")
    expect(summary.locator("[data-recent]")).to_have_text("last 24 h: 3")
    expect(summary).to_contain_text("Last updated")
    expect(page.locator("#ops-count")).to_have_text("Showing 3 of 3 runs")

    page.fill("#ops-search", "BOB")
    expect(page.locator("#ops-runs tr")).to_have_count(1)
    expect(page.locator("#ops-runs tr")).to_contain_text("bob@acme.com")
    page.fill("#ops-search", "")
    page.select_option("#ops-outcome", "resumed")
    expect(page.locator("#ops-count")).to_have_text("Showing 1 of 3 runs")
    page.select_option("#ops-outcome", "")
    page.select_option("#ops-actor", "alice@acme.com")
    expect(page.locator("#ops-runs tr")).to_have_count(1)
    page.select_option("#ops-actor", "")

    today = datetime.now(UTC).date()
    page.fill("#ops-from", (today + timedelta(days=1)).isoformat())
    expect(page.locator("#ops-runs")).to_contain_text("No runs match these filters.")
    page.fill("#ops-from", today.isoformat())
    page.fill("#ops-to", today.isoformat())
    expect(page.locator("#ops-count")).to_have_text("Showing 3 of 3 runs")
    # the original anchors still work: View opens the trajectory
    page.locator("#ops-runs tr", has_text="alice@acme.com").get_by_role("button", name="View").click()
    expect(page.locator("#ops-filters button", has_text="tool")).to_be_visible()


def test_refresh_does_not_rebuild_rows_until_the_data_changes(page: Page, dash):
    _seed_runs(dash, [("alice@acme.com", "marketing_agent", False)])
    login(page, dash, ADMIN)
    open_tab(page, "Ops")
    expect(page.locator("#ops-runs tr")).to_have_count(1)
    _mark_rows(page)
    with page.expect_response(lambda r: r.url.endswith("/api/ops/runs")):
        page.get_by_role("button", name="Refresh").click()
    assert _rows_kept(page), "unchanged data must not rebuild the rows"

    _seed_runs(dash, [("bob@acme.com", "hr_agent", False)])
    with page.expect_response(lambda r: r.url.endswith("/api/ops/runs")):
        page.get_by_role("button", name="Refresh").click()
    expect(page.locator("#ops-runs tr")).to_have_count(2)
    assert not _rows_kept(page), "new data must re-render"


def test_ops_polls_for_new_runs_without_touching_unchanged_rows(page: Page, dash):
    page.add_init_script("window.__OPS_POLL_MS = 300")  # the real interval is 15 s
    _seed_runs(dash, [("alice@acme.com", "marketing_agent", False)])
    login(page, dash, ADMIN)
    open_tab(page, "Ops")
    expect(page.locator("#ops-runs tr")).to_have_count(1)
    _mark_rows(page)
    first = page.locator("#ops-updated").inner_text()
    page.wait_for_timeout(1500)  # several polls, no new data
    assert _rows_kept(page)
    expect(page.locator("#ops-updated")).not_to_have_text(first)  # the poll did run

    _seed_runs(dash, [("dave@acme.com", "devops_agent", False)])
    expect(page.locator("#ops-runs tr", has_text="dave@acme.com")).to_be_visible()  # picked up by the poll


# ---- Phase 10: chat file attachments --------------------------------------------------


def _file(name: str, text: str = "", size: int | None = None) -> dict:
    data = text.encode() if size is None else b"a" * size
    return {"name": name, "mimeType": "text/plain", "buffer": data}


def test_a_text_file_is_attached_sent_inside_the_message_and_cleared(page: Page, dash):
    sent = stub_chat(page, reply="Read your notes.")
    login(page, dash, ALICE)
    expect(page.get_by_role("button", name="Attach text files")).to_be_visible()
    page.set_input_files("#chat-file", [_file("notes.md", "# Launch\n- budget 5000\n")])
    chip = page.locator("#chat-attachments .chip")
    expect(chip).to_have_count(1)
    expect(chip).to_contain_text("notes.md")

    page.fill("#chat-msg", "summarise this")
    page.get_by_role("button", name="Send").click()
    expect(page.locator(".chat-assistant")).to_contain_text("Read your notes.")
    assert sent[0]["message"] == "summarise this\n\n```notes.md\n# Launch\n- budget 5000\n```"
    expect(page.locator(".chat-user .chat-files")).to_contain_text("notes.md")  # seen in the thread
    expect(page.locator("#chat-attachments")).to_be_hidden()  # cleared after a successful send
    assert set(sent[0]) == {"message", "thread_id", "key_id"}, "ChatRequest is unchanged"


def test_attachments_alone_can_be_sent_and_fences_cannot_be_broken(page: Page, dash):
    sent = stub_chat(page)
    login(page, dash, ALICE)
    tricky = "line one\n```\nnot the end\n````\n"
    page.set_input_files("#chat-file", [_file("trap.txt", tricky)])
    page.get_by_role("button", name="Send").click()
    expect(page.locator(".chat-assistant")).to_be_visible()
    body = sent[0]["message"]
    assert body.startswith("`````trap.txt\n") and body.endswith("\n`````"), body
    assert tricky.rstrip() in body


def test_attachment_rules_type_size_count_and_removal(page: Page, dash):
    sent = stub_chat(page)
    login(page, dash, ALICE)
    status = page.locator("#chat-status")

    page.set_input_files("#chat-file", [_file("photo.png", "x")])
    expect(status).to_contain_text("photo.png: only")
    page.set_input_files("#chat-file", [_file("big.log", size=200 * 1024 + 1)])
    expect(status).to_contain_text("big.log: larger than 200 KB")
    page.set_input_files("#chat-file", [_file("bin.txt", "a\u0000b")])
    expect(status).to_contain_text("bin.txt: not a text file")
    expect(page.locator("#chat-attachments .chip")).to_have_count(0)

    page.set_input_files("#chat-file", [_file(f"f{i}.csv", "a,b\n") for i in range(4)])
    expect(page.locator("#chat-attachments .chip")).to_have_count(3)
    expect(status).to_contain_text("f3.csv: at most 3 files per message")
    page.set_input_files("#chat-file", [_file("exact.json", size=200 * 1024)])  # exactly 200 KB is allowed...
    expect(status).to_contain_text("exact.json: at most 3")  # ...but the message is full

    page.locator("#chat-attachments .chip", has_text="f1.csv").get_by_role("button").click()
    expect(page.locator("#chat-attachments .chip")).to_have_count(2)
    page.fill("#chat-msg", "two files")
    page.get_by_role("button", name="Send").click()
    expect(page.locator(".chat-assistant")).to_be_visible()
    assert "```f0.csv" in sent[0]["message"] and "```f2.csv" in sent[0]["message"]
    assert "f1.csv" not in sent[0]["message"]
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")


def test_a_failed_send_keeps_the_attachments(page: Page, dash):
    page.route("**/api/chat/threads", lambda route: route.fulfill(json=[]))
    page.route("**/api/chat", lambda route: route.fulfill(status=502, json={"detail": "gateway down"}))
    login(page, dash, ALICE)
    page.set_input_files("#chat-file", [_file("keep.txt", "hello")])
    page.fill("#chat-msg", "try")
    page.get_by_role("button", name="Send").click()
    expect(page.locator("#chat-status")).to_contain_text("gateway down")
    expect(page.locator("#chat-attachments .chip")).to_have_count(1)

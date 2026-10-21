"""Mk21 — dashboard UI refresh: acceptance criteria that do not need a browser.

Browser-level behaviour (layout, collapse, attachments, filters) lives in tests/e2e.
These checks read the shipped page and the API so a later plan cannot silently undo Mk21.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.dashboard.app import app as dashboard_app

STATIC = Path(__file__).resolve().parents[1] / "app" / "dashboard" / "static"
INDEX = (STATIC / "index.html").read_text(encoding="utf-8")
GUIDE = (STATIC / "composio-setup.html").read_text(encoding="utf-8")
CSS = (STATIC / "console.css").read_text(encoding="utf-8")


def _visible_text(html: str) -> str:
    """Markup minus <script>/<style> bodies, tags and comments: what a reader can see."""
    html = re.sub(r"<(script|style)\b.*?</\1>", " ", html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r"<!--.*?-->", " ", html, flags=re.DOTALL)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html))


# ---- Phase 1: copy-only removals ------------------------------------------------------


def test_page_opts_out_of_browser_translation():
    assert re.search(r'<html[^>]*\btranslate="no"', INDEX)
    assert '<meta name="google" content="notranslate">' in INDEX


def test_html_responses_declare_content_language():
    client = TestClient(dashboard_app)
    page = client.get("/static/index.html")
    assert page.status_code == 200
    assert page.headers["content-language"] == "en"
    # JSON is untouched
    assert "content-language" not in client.get("/health").headers


def test_no_internal_jargon_or_seeded_credentials_on_screen():
    text = _visible_text(INDEX)
    for word in ("BYOK", "Composio", "Tip: pick a model", "Mock login", "admin123", "alice123"):
        assert word not in text, word
    for gone in ('id="login-hint"', 'id="chat-identity"'):
        assert gone not in INDEX, gone
    for subtitle in ("folder/markdown registry", "approve-once rules", "records everything the model saw",
                     "row in policy.csv"):
        assert subtitle not in text, subtitle
    assert "#login-hint" not in CSS
    # Rule 4: model autocompletion stays
    assert '<datalist id="acc-model-list">' in INDEX


def test_status_strings_in_script_drop_the_provider_name():
    script = re.search(r"<script>(.*)</script>", INDEX, re.DOTALL).group(1)
    assert "Composio" not in script
    assert 'composio_key: "Integration key"' in script


def test_key_prompts_from_the_api_are_neutral_too():
    """The page shows API errors as `error: <detail>`, so the member-facing "paste/save your key"
    prompts must not reintroduce the provider name the UI copy dropped. Messages that name what
    to act on (unreachable service, package to install, where to re-copy a key) keep it."""
    root = Path(__file__).resolve().parents[1] / "app"
    for path in (root / "dashboard" / "app.py", root / "integrations" / "client.py"):
        src = path.read_text(encoding="utf-8")
        for prompt in re.findall(r'"((?:paste|save) your [^"]*key[^"]*)"', src):
            assert "omposio" not in prompt, (path.name, prompt)


def test_setup_guide_uses_the_console_theme_and_neutral_headings():
    assert '<link rel="stylesheet" href="console.css">' in GUIDE
    assert 'class="card"' in GUIDE and 'class="pill' in GUIDE
    headings = " ".join(re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", GUIDE, re.DOTALL))
    assert "omposio" not in headings
    assert "<style>body{" not in GUIDE  # the old light inline theme is gone
    # the fetch step still points at the real site (Decision 2A)
    assert 'href="https://app.composio.dev"' in GUIDE


# ---- Phase 8: skills generation (draft only) + explicit grants --------------------------

DRAFT = """```markdown
---
name: weekly_report
trigger: Build the weekly marketing report
scope: central
owner_role: someone_else
version: 7
args_schema:
  week: {type: string, description: ISO week}
steps:
  - Search customers
tool_calls:
  - tool: crm_search_customer
    args: {query: "{{week}}"}
---

# Weekly report

Collects this week's customers for the report.
```"""


def _token(username: str) -> str:
    from app.auth.jwt import create_token
    from app.auth.users import USERS
    from app.config import get_settings

    s = get_settings()
    return create_token(USERS[username], s.jwt.secret, s.jwt.algorithm, 60)


def _auth(username: str) -> dict[str, str]:
    return {"Authorization": "Bearer " + _token(username)}


class _FakeProvider:
    def __init__(self, text: str):
        self.text, self.calls = text, []

    def chat(self, messages, **kwargs):
        from app.llm.base import LLMResponse

        self.calls.append((messages, kwargs))
        return LLMResponse(text=self.text, model="qwen/test:free")


@pytest.fixture
def skills_env(tmp_path, monkeypatch, member_store):
    """Temp skills folder + temp policy.csv + temp audit: nothing in the repo is touched."""
    import shutil

    import app.dashboard.app as dash
    from app.config import get_settings
    from app.harness.audit import AuditLog
    from app.harness.enforcer import POLICY_PATH, PolicyEnforcer
    from app.skills.registry import SkillRegistry

    policy = tmp_path / "policy.csv"
    shutil.copy(POLICY_PATH, policy)
    for cfg in {id(get_settings()): get_settings(), id(dash.settings): dash.settings}.values():
        monkeypatch.setattr(cfg.services, "policy_path", policy)
    audit = AuditLog(tmp_path / "audit.jsonl")
    registry = SkillRegistry(tmp_path / "skills")
    monkeypatch.setattr(dash, "_components", {"skills": registry, "audit": audit, "enforcer": PolicyEnforcer()})
    return {"policy": policy, "registry": registry, "audit": tmp_path / "audit.jsonl", "store": member_store}


def _use_provider(monkeypatch, provider) -> None:
    from app.llm import system

    monkeypatch.setattr(system, "get_system_provider", lambda *_a, **_k: provider)


def _policy_rows(path) -> list[str]:
    return [r.replace(" ", "") for r in path.read_text(encoding="utf-8").splitlines()]


def test_generate_returns_a_valid_draft_and_writes_nothing(skills_env, monkeypatch):
    provider = _FakeProvider(DRAFT)
    _use_provider(monkeypatch, provider)
    before = skills_env["policy"].read_text(encoding="utf-8")
    resp = TestClient(dashboard_app).post(
        "/api/skills/generate", json={"scope": "dept/marketing", "brief": "weekly marketing report"},
        headers=_auth("admin@acme.com"))
    assert resp.status_code == 200, resp.text
    out = resp.json()
    assert out["valid"] is True and out["name"] == "weekly_report" and out["exists"] is False
    assert out["default_grant"] == ["marketing_agent"]
    # the requested scope/owner win; the fence is stripped; version resets to 1
    assert "scope: dept/marketing" in out["draft"] and "owner_role: marketing_agent" in out["draft"]
    assert "version: 1" in out["draft"] and "```" not in out["draft"]
    # the prompt names real tools and the requested scope; the brief is the user turn
    messages, kwargs = provider.calls[0]
    assert "crm_search_customer" in messages[0]["content"] and "dept/marketing" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "weekly marketing report"}
    assert kwargs["max_tokens"] >= 1000
    # a draft only: no skill on disk, no policy row, but the generation is audited
    assert skills_env["registry"].get("weekly_report") is None
    assert skills_env["policy"].read_text(encoding="utf-8") == before
    assert "skill:generate_draft" in skills_env["audit"].read_text(encoding="utf-8")


def test_generate_reports_an_invalid_draft_instead_of_failing(skills_env, monkeypatch):
    _use_provider(monkeypatch, _FakeProvider("sorry, I cannot write YAML"))
    out = TestClient(dashboard_app).post(
        "/api/skills/generate", json={"scope": "central", "brief": "x"}, headers=_auth("admin@acme.com")).json()
    assert out["valid"] is False and "no_frontmatter" in out["error"]
    assert out["draft"] == "sorry, I cannot write YAML"  # handed to the editor to fix


def test_generate_needs_a_platform_model_an_admin_and_a_real_scope(skills_env, monkeypatch):
    client = TestClient(dashboard_app)
    _use_provider(monkeypatch, None)
    resp = client.post("/api/skills/generate", json={"scope": "central", "brief": "x"}, headers=_auth("admin@acme.com"))
    assert resp.status_code == 503 and "Platform extractor" in resp.json()["detail"]
    _use_provider(monkeypatch, _FakeProvider(DRAFT))
    assert client.post("/api/skills/generate", json={"scope": "central", "brief": "x"},
                       headers=_auth("alice@acme.com")).status_code == 403
    for scope in ("dept/nowhere", "dept/admin", "user/alice@acme.com", "../central"):
        assert client.post("/api/skills/generate", json={"scope": scope, "brief": "x"},
                           headers=_auth("admin@acme.com")).status_code == 422, scope
    assert client.post("/api/skills/generate", json={"scope": "central", "brief": "  "},
                       headers=_auth("admin@acme.com")).status_code == 422


def _draft_for(scope: str) -> str:
    return DRAFT.replace("```markdown\n", "").replace("\n```", "").replace("scope: central", f"scope: {scope}")


def test_save_grants_exactly_the_ticked_roles_and_the_agent_honours_it(skills_env):
    from app.harness.enforcer import PolicyEnforcer

    client = TestClient(dashboard_app)
    resp = client.post("/api/skills", json={"content": _draft_for("dept/marketing"), "grant_to": ["marketing_agent"]},
                       headers=_auth("admin@acme.com"))
    assert resp.status_code == 200, resp.text
    assert resp.json()["granted"] == ["marketing_agent"]
    rows = _policy_rows(skills_env["policy"])
    assert "p,admin,admin,skill_weekly_report,use" in rows
    assert "p,marketing_agent,marketing,skill_weekly_report,use" in rows
    assert not any(r.startswith("p,marketing_head,") and "skill_weekly_report" in r for r in rows)

    enforcer = PolicyEnforcer(policy_path=skills_env["policy"])
    alice = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing"}
    bob = {"sub": "bob@acme.com", "role": "hr_agent", "dept": "hr"}
    assert enforcer.skill_allowed("weekly_report", alice) is True
    assert enforcer.skill_allowed("weekly_report", bob) is False  # never granted

    listed = {s["name"]: s for s in client.get("/api/skills", headers=_auth("admin@acme.com")).json()["skills"]}
    assert listed["weekly_report"]["granted_roles"] == ["marketing_agent"]
    assert listed["weekly_report"]["body"].startswith("# Weekly report")


def test_save_without_grant_to_keeps_the_old_behaviour(skills_env):
    resp = TestClient(dashboard_app).post("/api/skills", json={"content": _draft_for("central")},
                                          headers=_auth("admin@acme.com"))
    assert resp.status_code == 200 and resp.json()["granted"] == []
    rows = [r for r in _policy_rows(skills_env["policy"]) if "skill_weekly_report" in r]
    assert rows == ["p,admin,admin,skill_weekly_report,use"]


def test_unknown_grant_role_is_rejected_before_anything_is_saved(skills_env):
    resp = TestClient(dashboard_app).post(
        "/api/skills", json={"content": _draft_for("central"), "grant_to": ["root_agent"]},
        headers=_auth("admin@acme.com"))
    assert resp.status_code == 422 and "root_agent" in resp.json()["detail"]
    assert skills_env["registry"].get("weekly_report") is None


def test_second_admin_confirmation_carries_the_grants(skills_env):
    skills_env["store"].create("admin2@acme.com", "admin234", role="admin", dept="admin", actor="admin@acme.com")
    client = TestClient(dashboard_app)
    staged = client.post("/api/skills", json={"content": _draft_for("dept/hr"), "grant_to": ["hr_agent", "hr_head"]},
                         headers=_auth("admin@acme.com"))
    assert staged.status_code == 202
    assert skills_env["registry"].get("weekly_report") is None
    assert not any("skill_weekly_report" in r and "hr_" in r for r in _policy_rows(skills_env["policy"]))
    confirmed = client.post(f"/api/second-eyes/{staged.json()['proposal_id']}/confirm", headers=_auth("admin2@acme.com"))
    assert confirmed.status_code == 200, confirmed.text
    rows = _policy_rows(skills_env["policy"])
    assert "p,hr_agent,hr,skill_weekly_report,use" in rows and "p,hr_head,hr,skill_weekly_report,use" in rows
    assert skills_env["registry"].get("weekly_report") is not None

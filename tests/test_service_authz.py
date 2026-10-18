"""Mk19 Phase 3 — Casbin inside the dept services (plan Mk19, finding 3).

The gateway's intercept gate was the ONLY decision point; reaching a dept port
directly (published port, host network) skipped policy entirely. These tests
prove: direct-port traffic gets the same (obj, act) decision as the gateway,
members can never escalate to acts their role lacks, finance raw writes stay
deny-for-everyone, and the two decision points provably cannot drift.
"""

from __future__ import annotations

import re

from fastapi.testclient import TestClient

from app.agent.tools import TOOL_REGISTRY
from app.services import crm, finance, hr
from app.services.authz import act_for
from tests.attack.helpers import service_headers


def test_direct_port_member_cannot_delete_crm(tmp_path, member_store):
    """alice (marketing_agent, crm:read+create only) can read/write her dept via
    the direct port but hits 403 on DELETE — policy, not just the gateway."""
    c = TestClient(crm.create_app(tmp_path / "crm.sqlite"))
    h = service_headers("alice@acme.com")
    assert c.get("/customers", headers=h).status_code == 200
    created = c.post("/customers", json={"name": "Acme"}, headers=h)
    assert created.status_code == 200
    cid = created.json()["id"]
    assert c.delete(f"/customers/{cid}", headers=h).status_code == 403


def test_direct_port_admin_delete_allowed(tmp_path, member_store):
    """Mk19 new rows: admin DELETE crm/hr passes the service-side check too."""
    c = TestClient(crm.create_app(tmp_path / "crm.sqlite"))
    h = service_headers("admin@acme.com")
    created = c.post("/customers", json={"name": "Acme"}, headers=h)
    cid = created.json()["id"]
    assert c.delete(f"/customers/{cid}", headers=h).status_code == 200
    remaining = {row["id"] for row in c.get("/customers", headers=h).json()}
    assert cid not in remaining


def test_finance_raw_writes_denied_for_everyone(tmp_path, member_store):
    """Unbacked POST /accounts maps to act `create`, which policy grants to NO
    role — not even admin. Locked decision: deny for everyone."""
    c = TestClient(finance.create_app(tmp_path / "finance.sqlite"))
    for who in ("carol@acme.com", "admin@acme.com"):
        resp = c.post(
            "/accounts",
            json={"account_number": "9999", "owner": "x", "balance": "0"},
            headers=service_headers(who),
        )
        assert resp.status_code == 403, f"{who} must be denied"


def test_route_act_map_matches_tools(tmp_path, member_store):
    """Single source of truth: every ToolSpec (dept, method, path) resolves to
    the same act at the service as its required_scope at the gateway."""
    for tool in TOOL_REGISTRY.values():
        if tool.dept not in {"crm", "hr", "devops", "finance"}:
            continue
        # instantiate the path template with a dummy id segment
        concrete = re.sub(r"\{[^}]+\}", "1", tool.path_template)
        expected = tool.required_scope.partition(":")[2] or "read"
        assert act_for(tool.dept, tool.http_method, concrete) == expected, tool.name


def test_fallback_covers_unbacked_routes():
    # GET with no tool backing → read (members with crm:read pass)
    assert act_for("crm", "GET", "/accounts/whatever") == "read"
    # POST with no tool backing → create (finance members lack it)
    assert act_for("finance", "POST", "/accounts") == "create"
    # DELETE with no tool backing → delete (members lack it everywhere)
    assert act_for("hr", "DELETE", "/employees/7") == "delete"


def test_member_role_edit_applies_immediately(tmp_path, member_store):
    """Fresh MemberStore record drives the service decision: no re-login needed
    to lose (or gain) rights — same semantics as the gateway's live g rows."""
    c = TestClient(crm.create_app(tmp_path / "crm.sqlite"))
    h = service_headers("alice@acme.com")
    assert c.get("/customers", headers=h).status_code == 200
    # valid role switch (Mk17 strict role/dept pairs): marketing_agent → hr_agent
    # drops crm:read immediately; no re-login needed.
    member_store.update("alice@acme.com", role="hr_agent", dept="hr", actor="admin@acme.com")
    assert c.get("/customers", headers=h).status_code == 403
    member_store.update(
        "alice@acme.com", role="marketing_agent", dept="marketing", actor="admin@acme.com"
    )
    assert c.get("/customers", headers=h).status_code == 200


def test_deactivated_member_denied_at_service(tmp_path, member_store):
    c = TestClient(crm.create_app(tmp_path / "crm.sqlite"))
    h = service_headers("alice@acme.com")
    assert c.get("/customers", headers=h).status_code == 200
    member_store.set_active("alice@acme.com", False, actor="admin@acme.com")
    assert c.get("/customers", headers=h).status_code == 403


def test_service_audit_records_direct_port_traffic(tmp_path, member_store, monkeypatch):
    """Direct-port ALLOW and DENY both land in the shared chain under svc:<name>."""
    from app.config import get_settings

    audit_path = tmp_path / "audit.jsonl"
    monkeypatch.setenv("SERVICES__AUDIT_PATH", str(audit_path))
    # settings are cached — force a fresh view of the env override
    get_settings.cache_clear()
    try:
        c = TestClient(hr.create_app(tmp_path / "hr.sqlite"))
        h = service_headers("admin@acme.com")
        assert c.get("/employees", headers=h).status_code == 200
        assert c.delete("/employees/1", headers=h).status_code == 200  # admin may

        b = TestClient(hr.create_app(tmp_path / "hr2.sqlite"))
        assert b.delete("/employees/1", headers=service_headers("bob@acme.com")).status_code == 403

        import json

        # each line is {"prev_hash":…, "hash":…, "entry":{…}}
        records = [json.loads(line) for line in audit_path.read_text().splitlines() if line.strip()]
        routes = [r["entry"] for r in records if r["entry"]["resource"]["type"] == "route"]
        assert any(e["decision"] == "ALLOW" and e["run_id"] == "svc:hr" for e in routes), "admin ALLOW missing"
        assert any(e["decision"] == "DENY" and e["run_id"] == "svc:hr" for e in routes), "bob DENY missing"
        assert all(e["reason"] == "rbac" for e in routes)
    finally:
        get_settings.cache_clear()

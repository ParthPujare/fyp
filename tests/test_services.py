"""Dept mock services: seeded data, CRUD, validation, PII present for later masking.

Mk18 Phase C: table routes require the operator's JWT (passthrough); `/health`
stays open. Tests mint an admin token via `service_headers()` against the
isolated `member_store` fixture — never the real DB.
"""

from fastapi.testclient import TestClient

from app.services import crm, devops, finance, hr
from tests.attack.helpers import service_headers


def _client(app):
    return TestClient(app), service_headers()


def test_crm_list_and_search(tmp_path, member_store):
    client, headers = _client(crm.create_app(tmp_path / "crm.sqlite"))
    rows = client.get("/customers", headers=headers).json()
    assert len(rows) >= 5
    assert client.get("/customers/1", headers=headers).json()["id"] == 1

    search = client.get("/customers", params={"q": "acme"}, headers=headers).json()
    assert all("acme" in c["company"].lower() for c in search)


def test_crm_create_and_delete(tmp_path, member_store):
    client, headers = _client(crm.create_app(tmp_path / "crm.sqlite"))
    resp = client.post(
        "/customers",
        json={"name": "New Client", "company": "N Co", "email": "n@n.com", "region": "NA", "status": "lead"},
        headers=headers,
    )
    assert resp.status_code == 200
    new_id = resp.json()["id"]
    assert client.get(f"/customers/{new_id}", headers=headers).status_code == 200
    assert client.delete(f"/customers/{new_id}", headers=headers).json()["deleted"] is True
    assert client.get(f"/customers/{new_id}", headers=headers).status_code == 404


def test_crm_rejects_unknown_field(tmp_path, member_store):
    client, headers = _client(crm.create_app(tmp_path / "crm.sqlite"))
    resp = client.post("/customers", json={"name": "X", "evil": "inject"}, headers=headers)
    assert resp.status_code == 422


def test_crm_404_on_missing(tmp_path, member_store):
    client, headers = _client(crm.create_app(tmp_path / "crm.sqlite"))
    assert client.get("/customers/9999", headers=headers).status_code == 404
    assert client.delete("/customers/9999", headers=headers).status_code == 404


def test_services_reject_anonymous(tmp_path, member_store):
    """Mk18: no token → 401; garbage token → 401; health stays open."""
    client, _ = _client(crm.create_app(tmp_path / "crm.sqlite"))
    assert client.get("/customers").status_code == 401
    assert client.get("/customers", headers={"Authorization": "Bearer nope"}).status_code == 401
    assert client.get("/health").json()["status"] == "ok"


def test_hr_contains_pii_for_later_masking(tmp_path, member_store):
    client, headers = _client(hr.create_app(tmp_path / "hr.sqlite"))
    employees = client.get("/employees", headers=headers).json()
    assert employees
    assert "salary" in employees[0] and "ssn" in employees[0] and "email" in employees[0]
    assert client.get("/leave_requests", headers=headers).status_code == 200


def test_devops_seeded(tmp_path, member_store):
    client, headers = _client(devops.create_app(tmp_path / "devops.sqlite"))
    incidents = client.get("/incidents", headers=headers).json()
    assert incidents and incidents[0]["severity"] == "critical"
    assert client.get("/deployments", headers=headers).status_code == 200


def test_finance_transfer_creates_record(tmp_path, member_store):
    client, headers = _client(finance.create_app(tmp_path / "finance.sqlite"))
    resp = client.post(
        "/transfers",
        json={
            "from_account": "1000",
            "to_account": "2000",
            "amount": 500.0,
            "memo": "test transfer",
            "actor": "carol@acme.com",
            "status": "pending",
        },
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["amount"] == 500.0
    assert client.get("/invoices", headers=headers).status_code == 200
    assert client.get("/ledger", headers=headers).status_code == 200


def test_services_health(tmp_path):
    for module in (crm, hr, devops, finance):
        client = TestClient(module.create_app(tmp_path / f"{module.__name__}.sqlite"))
        assert client.get("/health").json()["status"] == "ok"

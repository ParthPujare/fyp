"""Dept mock services: seeded data, CRUD, validation, PII present for later masking."""

from fastapi.testclient import TestClient

from app.services import crm, devops, finance, hr


def test_crm_list_and_search(tmp_path):
    client = TestClient(crm.create_app(tmp_path / "crm.sqlite"))
    rows = client.get("/customers").json()
    assert len(rows) >= 5
    assert client.get("/customers/1").json()["id"] == 1

    search = client.get("/customers", params={"q": "acme"}).json()
    assert all("acme" in c["company"].lower() for c in search)


def test_crm_create_and_delete(tmp_path):
    client = TestClient(crm.create_app(tmp_path / "crm.sqlite"))
    resp = client.post(
        "/customers",
        json={"name": "New Client", "company": "N Co", "email": "n@n.com", "region": "NA", "status": "lead"},
    )
    assert resp.status_code == 200
    new_id = resp.json()["id"]
    assert client.get(f"/customers/{new_id}").status_code == 200
    assert client.delete(f"/customers/{new_id}").json()["deleted"] is True
    assert client.get(f"/customers/{new_id}").status_code == 404


def test_crm_rejects_unknown_field(tmp_path):
    client = TestClient(crm.create_app(tmp_path / "crm.sqlite"))
    resp = client.post("/customers", json={"name": "X", "evil": "inject"})
    assert resp.status_code == 422


def test_crm_404_on_missing(tmp_path):
    client = TestClient(crm.create_app(tmp_path / "crm.sqlite"))
    assert client.get("/customers/9999").status_code == 404
    assert client.delete("/customers/9999").status_code == 404


def test_hr_contains_pii_for_later_masking(tmp_path):
    client = TestClient(hr.create_app(tmp_path / "hr.sqlite"))
    employees = client.get("/employees").json()
    assert employees
    assert "salary" in employees[0] and "ssn" in employees[0] and "email" in employees[0]
    assert client.get("/leave_requests").status_code == 200


def test_devops_seeded(tmp_path):
    client = TestClient(devops.create_app(tmp_path / "devops.sqlite"))
    incidents = client.get("/incidents").json()
    assert incidents and incidents[0]["severity"] == "critical"
    assert client.get("/deployments").status_code == 200


def test_finance_transfer_creates_record(tmp_path):
    client = TestClient(finance.create_app(tmp_path / "finance.sqlite"))
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
    )
    assert resp.status_code == 200
    assert resp.json()["amount"] == 500.0
    assert client.get("/invoices").status_code == 200
    assert client.get("/ledger").status_code == 200


def test_services_health(tmp_path):
    for module in (crm, hr, devops, finance):
        client = TestClient(module.create_app(tmp_path / f"{module.__name__}.sqlite"))
        assert client.get("/health").json()["status"] == "ok"

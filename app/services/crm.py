"""CRM service (Marketing dept) — FastAPI :8002."""

from pathlib import Path

from app.config import get_settings
from app.services.lib import make_app

SCHEMA = {
    "customers": ["id", "name", "company", "email", "region", "status"],
    "campaigns": ["id", "name", "budget", "status", "region"],
}

SEED = {
    "customers": [
        {"id": 1, "name": "Alice Zhang", "company": "Acme Corp", "email": "alice.zhang@acme.com", "region": "NA", "status": "active"},
        {"id": 2, "name": "Bruno Silva", "company": "Beta Ltd", "email": "bruno@beta.com", "region": "EU", "status": "active"},
        {"id": 3, "name": "Chen Wei", "company": "Gamma Inc", "email": "chen@gammainc.com", "region": "APAC", "status": "lead"},
        {"id": 4, "name": "Diana Ross", "company": "Delta Co", "email": "diana@deltaco.com", "region": "NA", "status": "inactive"},
        {"id": 5, "name": "Emre Kaya", "company": "Epsilon AG", "email": "emre@epsilon.com", "region": "EU", "status": "active"},
        {"id": 6, "name": "Fatima Noor", "company": "Zeta LLC", "email": "fatima@zeta.io", "region": "APAC", "status": "lead"},
    ],
    "campaigns": [
        {"id": 1, "name": "Q3 Product Launch", "budget": 50000, "status": "running", "region": "NA"},
        {"id": 2, "name": "EU Winter Promo", "budget": 20000, "status": "planned", "region": "EU"},
        {"id": 3, "name": "APAC Enterprise Outreach", "budget": 35000, "status": "paused", "region": "APAC"},
        {"id": 4, "name": "Retention Winback", "budget": 10000, "status": "completed", "region": "NA"},
    ],
}


def create_app(db_path: Path | None = None):
    if db_path is None:
        db_path = get_settings().services.data_dir / "crm.sqlite"
    return make_app("CRM Service", SCHEMA, SEED, db_path)


app = create_app()

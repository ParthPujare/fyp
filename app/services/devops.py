"""DevOps service — FastAPI :8004."""

from pathlib import Path

from app.config import get_settings
from app.services.lib import make_app

SCHEMA = {
    "incidents": ["id", "service", "severity", "status", "summary", "opened_by"],
    "deployments": ["id", "service", "version", "environment", "status", "deployed_by"],
}

SEED = {
    "incidents": [
        {"id": 1, "service": "checkout-api", "severity": "critical", "status": "open", "summary": "5xx spike on checkout", "opened_by": "dave@acme.com"},
        {"id": 2, "service": "auth-service", "severity": "high", "status": "monitoring", "summary": "login latency p99 > 2s", "opened_by": "dave@acme.com"},
        {"id": 3, "service": "crm-sync", "severity": "low", "status": "resolved", "summary": "stale customer sync", "opened_by": "alice@acme.com"},
        {"id": 4, "service": "payments", "severity": "critical", "status": "open", "summary": "duplicate charge reports", "opened_by": "carol@acme.com"},
    ],
    "deployments": [
        {"id": 1, "service": "checkout-api", "version": "v2.4.1", "environment": "staging", "status": "success", "deployed_by": "dave@acme.com"},
        {"id": 2, "service": "auth-service", "version": "v1.9.0", "environment": "staging", "status": "pending", "deployed_by": "dave@acme.com"},
        {"id": 3, "service": "crm-sync", "version": "v3.0.2", "environment": "production", "status": "success", "deployed_by": "dave@acme.com"},
    ],
}


def create_app(db_path: Path | None = None):
    if db_path is None:
        db_path = get_settings().services.data_dir / "devops.sqlite"
    return make_app("DevOps Service", SCHEMA, SEED, db_path, scope_obj="devops")


app = create_app()

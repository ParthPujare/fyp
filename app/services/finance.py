"""Finance service — FastAPI :8005. Transfers will later be HITL-gated (risk=critical)."""

from pathlib import Path

from app.config import get_settings
from app.services.lib import make_app

SCHEMA = {
    "accounts": ["id", "account", "name", "currency", "balance"],
    "invoices": ["id", "invoice_no", "customer", "amount", "status", "due_date"],
    "transfers": ["id", "from_account", "to_account", "amount", "memo", "actor", "status"],
    "ledger": ["id", "from_account", "to_account", "amount", "memo", "ts", "actor"],
}

SEED = {
    "accounts": [
        {"id": 1, "account": "1000", "name": "Cash", "currency": "USD", "balance": 250000.00},
        {"id": 2, "account": "1100", "name": "Accounts Receivable", "currency": "USD", "balance": 185000.00},
        {"id": 3, "account": "2000", "name": "Accounts Payable", "currency": "USD", "balance": 92000.00},
        {"id": 4, "account": "3000", "name": "Equity", "currency": "USD", "balance": 400000.00},
    ],
    "invoices": [
        {"id": 1, "invoice_no": "INV-2026-001", "customer": "Acme Corp", "amount": 12500.00, "status": "open", "due_date": "2026-09-15"},
        {"id": 2, "invoice_no": "INV-2026-002", "customer": "Beta Ltd", "amount": 8200.00, "status": "paid", "due_date": "2026-08-30"},
        {"id": 3, "invoice_no": "INV-2026-003", "customer": "Gamma Inc", "amount": 31000.00, "status": "open", "due_date": "2026-10-01"},
        {"id": 4, "invoice_no": "INV-2026-004", "customer": "Delta Co", "amount": 550.00, "status": "overdue", "due_date": "2026-07-20"},
    ],
    "transfers": [
        {"id": 1, "from_account": "1000", "to_account": "2000", "amount": 12000.00, "memo": "vendor payment", "actor": "carol@acme.com", "status": "pending"},
    ],
    "ledger": [
        {"id": 1, "from_account": "1000", "to_account": "2000", "amount": 12000.00, "memo": "vendor payment", "ts": "2026-08-01T09:00:00Z", "actor": "carol@acme.com"},
        {"id": 2, "from_account": "1100", "to_account": "1000", "amount": 8200.00, "memo": "invoice INV-2026-002 collected", "ts": "2026-08-05T14:30:00Z", "actor": "carol@acme.com"},
    ],
}


def create_app(db_path: Path | None = None):
    if db_path is None:
        db_path = get_settings().services.data_dir / "finance.sqlite"
    return make_app("Finance Service", SCHEMA, SEED, db_path, scope_obj="finance")


app = create_app()

"""HR service — FastAPI :8003. Employees carry PII (salary, ssn) for the later sanitize milestone."""

from pathlib import Path

from app.config import get_settings
from app.services.lib import make_app

SCHEMA = {
    "employees": ["id", "name", "dept", "title", "email", "salary", "ssn", "status"],
    "leave_requests": ["id", "employee_id", "start_date", "end_date", "status", "type"],
}

SEED = {
    "employees": [
        {"id": 1, "name": "Alice Zhang", "dept": "Marketing", "title": "Marketing Manager", "email": "alice.zhang@acme.com", "salary": 95000, "ssn": "111-22-3333", "status": "active"},
        {"id": 2, "name": "Bob Smith", "dept": "Human Resources", "title": "HR Generalist", "email": "bob.smith@acme.com", "salary": 82000, "ssn": "222-33-4444", "status": "active"},
        {"id": 3, "name": "Carol Nguyen", "dept": "Finance", "title": "Finance Analyst", "email": "carol.nguyen@acme.com", "salary": 88000, "ssn": "333-44-5555", "status": "active"},
        {"id": 4, "name": "Dave Kumar", "dept": "DevOps", "title": "SRE", "email": "dave.kumar@acme.com", "salary": 120000, "ssn": "444-55-6666", "status": "active"},
        {"id": 5, "name": "Eve Torres", "dept": "Marketing", "title": "Content Lead", "email": "eve.torres@acme.com", "salary": 78000, "ssn": "555-66-7777", "status": "on_leave"},
        {"id": 6, "name": "Frank Li", "dept": "Finance", "title": "Controller", "email": "frank.li@acme.com", "salary": 135000, "ssn": "666-77-8888", "status": "active"},
    ],
    "leave_requests": [
        {"id": 1, "employee_id": 5, "start_date": "2026-09-01", "end_date": "2026-09-14", "status": "approved", "type": "vacation"},
        {"id": 2, "employee_id": 3, "start_date": "2026-08-20", "end_date": "2026-08-21", "status": "pending", "type": "sick"},
        {"id": 3, "employee_id": 1, "start_date": "2026-10-05", "end_date": "2026-10-06", "status": "pending", "type": "personal"},
    ],
}


def create_app(db_path: Path | None = None):
    if db_path is None:
        db_path = get_settings().services.data_dir / "hr.sqlite"
    return make_app("HR Service", SCHEMA, SEED, db_path, scope_obj="hr")


app = create_app()

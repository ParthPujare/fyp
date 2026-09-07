"""Tool registry + executor: route mapping, HTTP calls, Casbin gate, timeouts."""

import httpx

from app.agent.tools import TOOL_REGISTRY, ToolExecutor
from app.services import crm, devops, finance, hr

APPS = {"crm": crm.app, "hr": hr.app, "devops": devops.app, "finance": finance.app}

ALICE = {"sub": "alice@acme.com", "role": "marketing_agent", "dept": "marketing", "scopes": ["crm:read", "crm:create"]}
BOB = {"sub": "bob@acme.com", "role": "hr_agent", "dept": "hr", "scopes": ["hr:read", "hr:create"]}
ADMIN = {"sub": "admin@acme.com", "role": "admin", "dept": "admin", "scopes": ["*"]}


def test_registry_has_expected_tools():
    names = set(TOOL_REGISTRY)
    assert {"crm_search_customer", "crm_create_campaign", "hr_delete_employee",
            "finance_transfer", "devops_list_incidents"} <= names


def test_registry_maps_to_service_routes():
    for tool in TOOL_REGISTRY.values():
        if tool.handler != "http":  # memory tools have no HTTP endpoint
            continue
        app = APPS[tool.dept]
        path = tool.path_template.replace("{id}", "{row_id}")
        found = any(
            getattr(route, "path", None) == path
            and route.methods
            and tool.http_method in route.methods
            for route in app.routes
        )
        assert found, f"{tool.name} -> {tool.http_method} {path} not found in {tool.dept} service"


def test_casbin_tool_allowed_by_role(enforcer):
    tool = TOOL_REGISTRY["crm_create_campaign"]
    assert enforcer.tool_allowed(tool, ALICE) is True
    assert enforcer.tool_allowed(tool, BOB) is False
    assert enforcer.tool_allowed(tool, ADMIN) is True


def test_casbin_denied_for_unknown_user(enforcer):
    assert enforcer.tool_allowed(TOOL_REGISTRY["crm_search_customer"], {"sub": "x", "role": "y"}) is False


def test_executor_search_customer(dept_apps, executor):
    result = executor.execute(TOOL_REGISTRY["crm_search_customer"], {"query": "acme"})
    assert result.ok and not result.denied
    assert result.data and any(c["id"] == 1 and "Acme" in c["company"] for c in result.data)


def test_executor_search_actually_filters(dept_apps, executor):
    """The `query` arg must reach the service as its `q` param — otherwise the
    filter is dropped and every row comes back (param_aliases in ToolSpec)."""
    hit = executor.execute(TOOL_REGISTRY["crm_search_customer"], {"query": "acme"})
    every = executor.execute(TOOL_REGISTRY["crm_search_customer"], {"query": ""})
    assert [c["company"] for c in hit.data] == ["Acme Corp"]
    assert len(every.data) > len(hit.data)


def test_executor_search_employees_filters(dept_apps, executor):
    result = executor.execute(TOOL_REGISTRY["hr_search_employees"], {"query": "devops"})
    assert result.ok
    assert [row["name"] for row in result.data] == ["Dave Kumar"]


def test_executor_get_customer(dept_apps, executor):
    result = executor.execute(TOOL_REGISTRY["crm_get_customer"], {"id": 1})
    assert result.ok and result.data["name"] == "Alice Zhang"


def test_executor_create_campaign(dept_apps, executor):
    result = executor.execute(
        TOOL_REGISTRY["crm_create_campaign"],
        {"name": "Q4 Promo", "budget": 10000, "status": "planned", "region": "NA"},
    )
    assert result.ok and result.data["name"] == "Q4 Promo"


def test_executor_delete_employee(dept_apps, executor):
    result = executor.execute(TOOL_REGISTRY["hr_delete_employee"], {"id": 6})
    assert result.ok and result.data == {"deleted": True, "id": 6}


def test_executor_transfer(dept_apps, executor):
    result = executor.execute(
        TOOL_REGISTRY["finance_transfer"],
        {"from_account": "1000", "to_account": "2000", "amount": 100.0,
         "memo": "test", "actor": "carol@acme.com", "status": "pending"},
    )
    assert result.ok and result.data["amount"] == 100.0


def test_executor_http_error_is_not_denied(dept_apps, executor):
    result = executor.execute(TOOL_REGISTRY["crm_get_customer"], {"id": 9999})
    assert not result.ok and not result.denied and result.reason == "http"
    assert result.status_code == 404


class _TimeoutClient:
    def __init__(self):
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        raise httpx.ReadTimeout("boom", request=None)


def test_executor_call_timeout(executor):
    client = _TimeoutClient()
    tool_exec = ToolExecutor(client=client)  # type: ignore[arg-type]
    result = tool_exec.execute(TOOL_REGISTRY["crm_search_customer"], {"query": "acme"})
    assert not result.ok and result.timeout and result.reason == "timeout"
    assert client.calls and "timeout" in client.calls[0][2]

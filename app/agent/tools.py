"""Tool registry + HTTP executor.

Each tool maps a name to one of the dept services' generic CRUD endpoints
(GET list/search, GET id, POST create, DELETE id — see app/services/lib.py).
The registry is the seed of the harness's filter/intercept layer; the
`required_scope` field is enforced by the scope gate in enforce.py until the
Casbin policy engine replaces it in weeks 5-6.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx

from app.config import Settings, get_settings


@dataclass(frozen=True)
class ToolSpec:
    name: str
    dept: str  # "crm" | "hr" | "devops" | "finance"  → settings.services.<dept>_url
    description: str
    http_method: str  # GET | POST | DELETE
    path_template: str  # e.g. "/customers/{id}"
    args_schema: dict[str, Any]
    required_scope: str  # e.g. "crm:read"; admin "*" passes
    risk_level: str = "read"  # read | write | critical (critical → HITL in wk 9-10)
    path_params: tuple[str, ...] = ()
    query_params: tuple[str, ...] = ()
    body_fields: tuple[str, ...] = ()
    handler: str = "http"  # "http" | "recall" — routes execution (memory tools have no HTTP endpoint)
    # (arg name -> wire name) for query params whose service-side name differs from
    # the LLM-facing arg, e.g. the readable `query` arg -> the services' `q` param.
    # Without the mapping the param is sent under a name the service ignores, and
    # the search silently degrades to "return everything".
    param_aliases: tuple[tuple[str, str], ...] = ()


def _t(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


TOOL_REGISTRY: dict[str, ToolSpec] = {t.name: t for t in [
    # ---- CRM (Marketing) ----
    ToolSpec(
        name="crm_search_customer",
        dept="crm",
        description="Search CRM customers by a query term (matches name/company/email/region/status).",
        http_method="GET",
        path_template="/customers",
        args_schema=_t({"query": {"type": "string", "description": "search term"}}, ["query"]),
        required_scope="crm:read",
        risk_level="read",
        query_params=("query",),
        param_aliases=(("query", "q"),),
    ),
    ToolSpec(
        name="crm_get_customer",
        dept="crm",
        description="Fetch a single CRM customer by id.",
        http_method="GET",
        path_template="/customers/{id}",
        args_schema=_t({"id": {"type": "integer", "description": "customer id"}}, ["id"]),
        required_scope="crm:read",
        risk_level="read",
        path_params=("id",),
    ),
    ToolSpec(
        name="crm_list_campaigns",
        dept="crm",
        description="List marketing campaigns.",
        http_method="GET",
        path_template="/campaigns",
        args_schema=_t({}, []),
        required_scope="crm:read",
        risk_level="read",
    ),
    ToolSpec(
        name="crm_create_campaign",
        dept="crm",
        description="Create a new marketing campaign.",
        http_method="POST",
        path_template="/campaigns",
        args_schema=_t(
            {
                "name": {"type": "string"},
                "budget": {"type": "number"},
                "status": {"type": "string"},
                "region": {"type": "string"},
            },
            ["name", "budget", "status", "region"],
        ),
        required_scope="crm:create",
        risk_level="write",
        body_fields=("name", "budget", "status", "region"),
    ),
    # ---- HR ----
    ToolSpec(
        name="hr_search_employees",
        dept="hr",
        description="Search employees by a query term (name/dept/title/email).",
        http_method="GET",
        path_template="/employees",
        args_schema=_t({"query": {"type": "string", "description": "search term"}}, ["query"]),
        required_scope="hr:read",
        risk_level="read",
        query_params=("query",),
        param_aliases=(("query", "q"),),
    ),
    ToolSpec(
        name="hr_get_employee",
        dept="hr",
        description="Fetch a single employee record by id.",
        http_method="GET",
        path_template="/employees/{id}",
        args_schema=_t({"id": {"type": "integer", "description": "employee id"}}, ["id"]),
        required_scope="hr:read",
        risk_level="read",
        path_params=("id",),
    ),
    ToolSpec(
        name="hr_list_leave_requests",
        dept="hr",
        description="List leave requests.",
        http_method="GET",
        path_template="/leave_requests",
        args_schema=_t({}, []),
        required_scope="hr:read",
        risk_level="read",
    ),
    ToolSpec(
        name="hr_create_leave_request",
        dept="hr",
        description="Create a leave request.",
        http_method="POST",
        path_template="/leave_requests",
        args_schema=_t(
            {
                "employee_id": {"type": "integer"},
                "start_date": {"type": "string"},
                "end_date": {"type": "string"},
                "status": {"type": "string"},
                "type": {"type": "string"},
            },
            ["employee_id", "start_date", "end_date", "status", "type"],
        ),
        required_scope="hr:create",
        risk_level="write",
        body_fields=("employee_id", "start_date", "end_date", "status", "type"),
    ),
    ToolSpec(
        name="hr_delete_employee",
        dept="hr",
        description="Delete an employee record. Destructive — gated to hr:delete.",
        http_method="DELETE",
        path_template="/employees/{id}",
        args_schema=_t({"id": {"type": "integer", "description": "employee id"}}, ["id"]),
        required_scope="hr:delete",
        risk_level="critical",
        path_params=("id",),
    ),
    # ---- DevOps ----
    ToolSpec(
        name="devops_list_incidents",
        dept="devops",
        description="List current infrastructure incidents.",
        http_method="GET",
        path_template="/incidents",
        args_schema=_t({}, []),
        required_scope="devops:read",
        risk_level="read",
    ),
    ToolSpec(
        name="devops_list_deployments",
        dept="devops",
        description="List deployments.",
        http_method="GET",
        path_template="/deployments",
        args_schema=_t({}, []),
        required_scope="devops:read",
        risk_level="read",
    ),
    ToolSpec(
        name="devops_create_deployment",
        dept="devops",
        description="Register a new deployment.",
        http_method="POST",
        path_template="/deployments",
        args_schema=_t(
            {
                "service": {"type": "string"},
                "version": {"type": "string"},
                "environment": {"type": "string"},
                "status": {"type": "string"},
                "deployed_by": {"type": "string"},
            },
            ["service", "version", "environment", "status", "deployed_by"],
        ),
        required_scope="devops:create",
        risk_level="write",
        body_fields=("service", "version", "environment", "status", "deployed_by"),
    ),
    # ---- Finance ----
    ToolSpec(
        name="finance_list_accounts",
        dept="finance",
        description="List finance accounts and balances.",
        http_method="GET",
        path_template="/accounts",
        args_schema=_t({}, []),
        required_scope="finance:read",
        risk_level="read",
    ),
    ToolSpec(
        name="finance_list_invoices",
        dept="finance",
        description="List invoices.",
        http_method="GET",
        path_template="/invoices",
        args_schema=_t({}, []),
        required_scope="finance:read",
        risk_level="read",
    ),
    ToolSpec(
        name="finance_transfer",
        dept="finance",
        description="Create a finance transfer. High-risk: HITL approval in weeks 9-10.",
        http_method="POST",
        path_template="/transfers",
        args_schema=_t(
            {
                "from_account": {"type": "string"},
                "to_account": {"type": "string"},
                "amount": {"type": "number"},
                "memo": {"type": "string"},
                "actor": {"type": "string"},
                "status": {"type": "string"},
            },
            ["from_account", "to_account", "amount", "memo", "actor", "status"],
        ),
        required_scope="finance:transfer",
        risk_level="critical",
        body_fields=("from_account", "to_account", "amount", "memo", "actor", "status"),
    ),
    ToolSpec(
        name="finance_list_ledger",
        dept="finance",
        description="List the finance ledger.",
        http_method="GET",
        path_template="/ledger",
        args_schema=_t({}, []),
        required_scope="finance:read",
        risk_level="read",
    ),
    # ---- Memory (weeks 7-8): FTS5 episodic recall tool ----
    # Gated by Casbin obj `memory_recall`; routed to the RecallService (not HTTP)
    # via `handler="recall"` in execute_tools.
    ToolSpec(
        name="recall",
        dept="memory",
        description="Search past conversations (full-text recall) within your permitted memory scopes.",
        http_method="GET",
        path_template="",
        args_schema=_t({"query": {"type": "string", "description": "topic to search past conversations for"}}, ["query"]),
        required_scope="memory_recall:use",
        risk_level="read",
        query_params=("query",),
        handler="recall",
    ),
]}


def tool_schema(tool: ToolSpec) -> dict[str, Any]:
    """OpenAI-style tool declaration passed to the LLM (see proposed_architecture.md §6)."""
    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.args_schema,
        },
    }


@dataclass
class ToolResult:
    ok: bool
    denied: bool = False
    timeout: bool = False
    content: str = ""
    data: Any = None
    latency_ms: int = 0
    status_code: int | None = None
    reason: str = ""  # "rbac" | "http" | "timeout" | "args" | ""

    @property
    def summary(self) -> str:
        return self.content


class ToolExecutor:
    """Calls dept services over HTTP (the same call shape a real internal gateway would use).

    The httpx client is injectable so tests can route to in-process FastAPI apps
    via httpx.MockTransport instead of opening sockets.
    """

    def __init__(self, client: httpx.Client | None = None, settings: Settings | None = None):
        self._settings = settings or get_settings()
        self._timeout = self._settings.guardrails.call_timeout_seconds
        self._client = client or httpx.Client(timeout=self._timeout)

    def _base_url(self, dept: str) -> str:
        return getattr(self._settings.services, f"{dept}_url")

    def _build_url(self, tool: ToolSpec, args: dict[str, Any]) -> str:
        path = tool.path_template
        for param in tool.path_params:
            if param not in args:
                raise ValueError(f"missing path param {param!r} for {tool.name}")
            path = path.replace("{" + param + "}", str(args[param]))
        return self._base_url(tool.dept) + path

    def execute(self, tool: ToolSpec, args: dict[str, Any]) -> ToolResult:
        try:
            url = self._build_url(tool, args)
        except ValueError as exc:
            return ToolResult(ok=False, reason="args", content=str(exc))

        aliases = dict(tool.param_aliases)
        params = {aliases.get(k, k): args[k] for k in tool.query_params if k in args}
        body = {k: args[k] for k in tool.body_fields if k in args}

        started = time.monotonic()
        try:
            resp = self._client.request(
                tool.http_method, url, params=params or None, json=body or None, timeout=self._timeout
            )
        except httpx.TimeoutException:
            return ToolResult(ok=False, timeout=True, reason="timeout", content="tool call timed out")
        except httpx.HTTPError as exc:
            return ToolResult(ok=False, reason="http", content=f"http error: {exc}")

        latency_ms = int((time.monotonic() - started) * 1000)
        content = resp.text if resp.text else str(resp.status_code)
        data: Any = None
        try:
            data = resp.json()
        except ValueError:
            pass
        return ToolResult(
            ok=200 <= resp.status_code < 300,
            content=content,
            data=data,
            latency_ms=latency_ms,
            status_code=resp.status_code,
            reason="http" if resp.status_code >= 400 else "",
        )

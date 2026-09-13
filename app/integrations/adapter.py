"""Allow-listed Composio actions -> ToolSpec (Mk9 Phase 2, MVP = Gmail + GitHub)."""

from __future__ import annotations

from typing import Any

from app.agent.tools import ToolSpec

SUPPORTED_APPS = ("gmail", "github")

_WRITE_HINTS = ("SEND", "CREATE", "UPDATE", "DELETE", "POST", "REPLY", "TRIGGER")
_CRITICAL_HINTS = ("SEND", "DELETE")


def risk_for_slug(slug: str) -> str:
    s = (slug or "").upper()
    if any(h in s for h in _CRITICAL_HINTS):
        return "critical"
    if any(h in s for h in _WRITE_HINTS):
        return "write"
    return "read"


def action_for_slug(slug: str) -> str:
    """Map a Composio slug to a Casbin act: read vs write (critical shares write gate + HITL by risk)."""
    return "read" if risk_for_slug(slug) == "read" else "write"


def tool_name_for(toolkit: str, slug: str) -> str:
    return f"{toolkit}_{slug.lower()}"


def spec_from_discovered(toolkit: str, slug: str, description: str, schema: dict) -> ToolSpec:
    risk = risk_for_slug(slug)
    act = action_for_slug(slug)
    props = schema if isinstance(schema, dict) else {"type": "object"}
    return ToolSpec(
        name=tool_name_for(toolkit, slug),
        dept=toolkit,
        description=description or f"{slug} via connected {toolkit} account.",
        http_method="EXTERNAL",
        path_template="",
        args_schema=props if props.get("type") == "object" else {"type": "object", "properties": {}},
        required_scope=f"{toolkit}:{act}",
        risk_level=risk,
        handler="external",
    )


def _t(props: dict[str, Any], required: list[str]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": required}


# slug mapping confirmed in Mk9 Phase-0 spike (docs.composio.dev/toolkits).
EXTERNAL_TOOLS: dict[str, dict[str, Any]] = {
    "gmail_fetch": {
        "spec": ToolSpec(
            name="gmail_fetch",
            dept="gmail",
            description="Fetch recent Gmail messages matching a query (read-only).",
            http_method="EXTERNAL",
            path_template="",
            args_schema=_t(
                {
                    "query": {"type": "string", "description": "Gmail search query"},
                    "max_results": {"type": "integer", "description": "max messages (1-20)"},
                },
                ["query"],
            ),
            required_scope="gmail:read",
            risk_level="read",
            query_params=("query",),
            handler="external",
        ),
        "slug": "GMAIL_FETCH_EMAILS",
        "app": "gmail",
    },
    "gmail_send": {
        "spec": ToolSpec(
            name="gmail_send",
            dept="gmail",
            description="Send an email via Gmail. High-risk: requires admin HITL approval.",
            http_method="EXTERNAL",
            path_template="",
            args_schema=_t(
                {
                    "to": {"type": "string", "description": "recipient email"},
                    "subject": {"type": "string"},
                    "body": {"type": "string"},
                },
                ["to", "subject", "body"],
            ),
            required_scope="gmail:send",
            risk_level="critical",
            body_fields=("to", "subject", "body"),
            handler="external",
        ),
        "slug": "GMAIL_SEND_EMAIL",
        "app": "gmail",
    },
    "github_list_repos": {
        "spec": ToolSpec(
            name="github_list_repos",
            dept="github",
            description="List GitHub repositories visible to the connected account.",
            http_method="EXTERNAL",
            path_template="",
            args_schema=_t({"query": {"type": "string", "description": "filter by name"}}, []),
            required_scope="github:read",
            risk_level="read",
            query_params=("query",),
            handler="external",
        ),
        "slug": "GITHUB_LIST_REPOSITORIES",
        "app": "github",
    },
    "github_create_issue": {
        "spec": ToolSpec(
            name="github_create_issue",
            dept="github",
            description="Create a GitHub issue in a repository.",
            http_method="EXTERNAL",
            path_template="",
            args_schema=_t(
                {
                    "repo": {"type": "string", "description": "owner/repo, e.g. acme/web"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                ["repo", "title"],
            ),
            required_scope="github:create",
            risk_level="write",
            body_fields=("repo", "title", "body"),
            handler="external",
        ),
        "slug": "GITHUB_CREATE_ISSUE",
        "app": "github",
    },
}


def spec_for(name: str) -> ToolSpec | None:
    entry = EXTERNAL_TOOLS.get(name)
    return entry["spec"] if entry else None


def slug_for(name: str) -> str | None:
    entry = EXTERNAL_TOOLS.get(name)
    return entry["slug"] if entry else None


def app_for(name: str) -> str | None:
    entry = EXTERNAL_TOOLS.get(name)
    return entry["app"] if entry else None


def all_specs() -> list[ToolSpec]:
    return [e["spec"] for e in EXTERNAL_TOOLS.values()]


def to_composio_args(name: str, args: dict[str, Any]) -> dict[str, Any]:
    """LLM-facing args -> Composio action arguments."""
    args = dict(args or {})
    if name == "gmail_fetch":
        out: dict[str, Any] = {"query": str(args.get("query", ""))}
        try:
            n = int(args.get("max_results", 5))
        except (TypeError, ValueError):
            n = 5
        out["max_results"] = max(1, min(20, n))
        return out
    if name == "gmail_send":
        return {
            "recipient_email": str(args.get("to", "")),
            "subject": str(args.get("subject", "")),
            "body": str(args.get("body", "")),
        }
    if name == "github_list_repos":
        return {"query": str(args.get("query", ""))} if args.get("query") else {}
    if name == "github_create_issue":
        out2: dict[str, Any] = {
            "repository": str(args.get("repo", "")),
            "title": str(args.get("title", "")),
        }
        if args.get("body"):
            out2["body"] = str(args.get("body"))
        return out2
    return dict(args)

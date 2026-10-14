"""Orchestration: visible external tools per user + enforced execution (Mk9)."""

from __future__ import annotations

import json
import time
from typing import Any

from app.agent.tools import ToolResult
from app.config import get_settings
from app.integrations.adapter import EXTERNAL_TOOLS, all_specs, app_for, slug_for, to_composio_args
from app.integrations.client import FakeComposioClient, build_client
from app.integrations.vault import get_composio_key


def entity_id_for(claims: dict[str, Any]) -> str:
    return str(claims.get("sub", ""))


def _is_connected(username: str, app: str) -> bool:
    try:
        from app.auth.users import get_store

        return get_store().is_app_connected(username, app)
    except Exception:  # noqa: BLE001 - store miss means not connected
        return False


def _catalog_toolkits() -> list[dict[str, Any]]:
    try:
        from app.auth.users import get_store

        return get_store().list_toolkits(enabled_only=True)
    except Exception:  # noqa: BLE001 - store miss means seeds only
        return [{"toolkit": "gmail"}, {"toolkit": "github"}]


def visible_external_tools(claims: dict[str, Any]) -> list:
    """Enabled-catalog tools for apps the user connected (status=active).

    Dynamic cache first (any admin-added toolkit); hardcoded seeds as fallback
    so offline/CI keeps working with zero discovery.
    """
    from app.integrations.adapter import spec_from_discovered

    username = str(claims.get("sub", ""))
    settings = get_settings()
    api_key = get_composio_key(username, settings.composio.api_key)
    try:
        from app.auth.users import get_store

        store = get_store()
        cached = store.list_cached_tools()
    except Exception:  # noqa: BLE001
        store = None
        cached = []
    out = []
    seen: set[str] = set()
    # Mk18 Phase F: no key → invisible, unconditionally. (The old
    # `has_any_key` carve-out showed tools whose execution would then fail.)
    if api_key is None:
        return out
    for item in cached:
        toolkit = str(item.get("toolkit", ""))
        if not _is_connected(username, toolkit):
            continue
        spec = spec_from_discovered(
            toolkit, str(item.get("slug", "")),
            str(item.get("description", "")), item.get("input_schema") or {"type": "object"},
        )
        try:
            spec = spec.__class__(**{**spec.__dict__, "risk_level": str(item.get("risk", spec.risk_level))})
        except Exception:  # noqa: BLE001, S110 - keep discovered risk on rebuild failure
            pass
        out.append(spec)
        seen.add(spec.name)
    for spec in all_specs():
        if spec.name in seen:
            continue
        app = app_for(spec.name) or ""
        enabled = any(t.get("toolkit") == app for t in _catalog_toolkits())
        if not enabled or not _is_connected(username, app):
            continue
        out.append(spec)
    return out


def connect_toolkit_for(tool_name: str) -> str:
    """Toolkit a tool belongs to (dynamic `toolkit_slug` names or seed map)."""
    if "__" in tool_name or "_" in tool_name:
        pass
    entry = EXTERNAL_TOOLS.get(tool_name)
    if entry:
        return str(entry["app"])
    toolkit, _, _rest = tool_name.partition("_")
    return toolkit


def execute_external(tool_name: str, args: dict[str, Any], claims: dict[str, Any]) -> ToolResult:
    entry = EXTERNAL_TOOLS.get(tool_name)
    slug: str | None = slug_for(tool_name) if entry else None
    app = str(entry["app"]) if entry else connect_toolkit_for(tool_name)
    if entry is None:
        try:
            from app.auth.users import get_store

            for item in get_store().list_cached_tools():
                from app.integrations.adapter import tool_name_for as _tn

                if _tn(str(item.get("toolkit", "")), str(item.get("slug", ""))) == tool_name:
                    slug = str(item.get("slug", ""))
                    app = str(item.get("toolkit", ""))
                    entry = {"slug": slug, "app": app}
                    break
        except Exception:  # noqa: BLE001, S110 - cache miss falls through
            pass
    if entry is None or not slug:
        return ToolResult(ok=False, reason="args", content=f"unknown external tool: {tool_name}")
    # Mk18 Phase B: shape-check external args too (seed specs carry schemas;
    # discovered ones default to open object).
    try:
        from app.agent.tools import _validate_args as _check_args
        from app.integrations.adapter import spec_for as _spec_for

        _spec = _spec_for(tool_name)
        if _spec is not None:
            _problem = _check_args(_spec, args or {})
            if _problem is not None:
                return ToolResult(ok=False, reason="args", content=_problem)
    except Exception:  # noqa: BLE001, S110 - validation never blocks on validator errors
        pass
    username = str(claims.get("sub", ""))
    if not _is_connected(username, app):
        return ToolResult(
            ok=False, denied=True, reason="auth",
            content=f"{app} is not connected. Tap Connect for {app}, then Refresh.",
        )
    settings = get_settings()
    api_key = get_composio_key(username, settings.composio.api_key)
    if not api_key:
        # Mk18 Phase F: never execute against the Fake client with a missing
        # key (it would fake-send). Fail closed with a save-key prompt.
        return ToolResult(
            ok=False, denied=True, reason="auth",
            content="Composio key missing. Save your key in Account → Integrations first.",
        )
    # Mk18 Phase F: provenance travels with external data (source identity for
    # the untrusted-data boundary + audit). URLs stay intact (display_url must
    # remain clickable) — no defanging.
    import datetime as _dt

    provenance = {
        "_provenance": {
            "toolkit": app,
            "tool": tool_name,
            "user": username,
            "ts": _dt.datetime.now(_dt.UTC).isoformat(),
        }
    }

    def _with_provenance(data: Any) -> Any:
        if isinstance(data, dict):
            return {**data, **provenance}
        return data
    client = build_client(api_key, settings.composio.base_url, settings.composio.timeout_seconds)
    comp_args = to_composio_args(tool_name, args) if tool_name in EXTERNAL_TOOLS else dict(args or {})
    started = time.monotonic()
    # Fake client executes synchronously; real SDK call is also sync here.
    if isinstance(client, FakeComposioClient):
        res = client.execute(slug=slug, user_id=entity_id_for(claims), arguments=comp_args)
        latency_ms = int((time.monotonic() - started) * 1000)
        if not res.ok:
            return ToolResult(ok=False, reason="http", content=res.error, latency_ms=latency_ms)
        data = _with_provenance(res.data)
        return ToolResult(
            ok=True, content=json.dumps(data)[:4000], data=data, latency_ms=latency_ms
        )
    try:
        res = client.execute(slug=slug, user_id=entity_id_for(claims), arguments=comp_args)  # type: ignore[union-attr]
    except RuntimeError as exc:
        return ToolResult(ok=False, reason="auth", content=str(exc))
    latency_ms = int((time.monotonic() - started) * 1000)
    if not res.ok:
        reason = "timeout" if "timed out" in res.error.lower() else "http"
        timeout = reason == "timeout"
        return ToolResult(ok=False, timeout=timeout, reason=reason, content=res.error, latency_ms=latency_ms)
    data = _with_provenance(res.data)
    return ToolResult(ok=True, content=json.dumps(data)[:4000], data=data, latency_ms=latency_ms)

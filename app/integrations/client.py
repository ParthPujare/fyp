"""Composio client wrapper — lazy SDK import, Fake for offline/tests (Mk9 Phase 0/2).

Real SDK (pinned separately): `pip install composio`
  from composio import Composio
  client = Composio(api_key=...)
  result = client.tools.execute(slug="GMAIL_FETCH_EMAILS", user_id=entity_id, arguments={...})

All SDK access stays in this file so a version drift is a one-file fix.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ComposioResult:
    ok: bool
    data: Any = None
    error: str = ""


class FakeComposioClient:
    """Deterministic offline stand-in (tests/demo/CI without network or key)."""

    def __init__(self) -> None:
        self.executed: list[dict[str, Any]] = []

    def execute(self, slug: str, user_id: str, arguments: dict[str, Any]) -> ComposioResult:
        self.executed.append({"slug": slug, "user_id": user_id, "arguments": arguments})
        if slug == "GMAIL_FETCH_EMAILS":
            q = str(arguments.get("query", ""))
            return ComposioResult(
                ok=True,
                data={
                    "messages": [
                        {
                            "messageId": "msg-001",
                            "threadId": "thr-001",
                            "from": "teammate@acme.com",
                            "subject": f"Re: {q or 'weekly update'}",
                            "snippet": "Standup moved to 10am. No SSN 123-45-6789 in snippet.",
                        }
                    ]
                },
            )
        if slug == "GMAIL_SEND_EMAIL":
            return ComposioResult(ok=True, data={"id": "sent-001", "threadId": "thr-001"})
        if slug in ("GITHUB_LIST_REPOS", "GITHUB_LIST_REPOSITORIES"):
            return ComposioResult(
                ok=True, data={"repos": [{"full_name": "acme/web", "stars": 42}]}
            )
        if slug == "GITHUB_CREATE_ISSUE":
            return ComposioResult(
                ok=True,
                data={"number": 7, "url": "https://github.com/acme/web/issues/7"},
            )
        return ComposioResult(ok=False, error=f"unknown slug: {slug}")

    def get_connect_url(self, app: str, user_id: str) -> str:
        return f"https://composio.dev/connect/{app}?user_id={user_id}&state=fake"

    def list_connections(self, user_id: str) -> list[dict[str, Any]]:  # pragma: no cover - live only
        return []

    def delete_connection(self, user_id: str, connection_id: str) -> tuple[bool, str]:
        """Fake remote revoke — always succeeds (offline stand-in)."""
        _ = (user_id, connection_id)
        return True, "revoked (fake)"


@dataclass
class ComposioClient:
    api_key: str | None = None
    base_url: str | None = None
    timeout_seconds: float = 30.0
    _sdk: Any = field(default=None, repr=False)

    def _get_sdk(self) -> Any | None:
        if self._sdk is not None:
            return self._sdk
        try:
            from composio import Composio  # type: ignore[import-not-found]
        except ImportError:
            return None
        kwargs: dict[str, Any] = {}
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["base_url"] = self.base_url
        try:
            self._sdk = Composio(**kwargs)
        except Exception:  # noqa: BLE001 - SDK init failure means offline mode
            return None
        return self._sdk

    @property
    def available(self) -> bool:
        return self.api_key is not None and self._get_sdk() is not None

    @staticmethod
    def _version_error(message: str) -> bool:
        msg = (message or "").lower()
        return "toolkit version" in msg or "dangerously_skip_version_check" in msg

    def execute(self, slug: str, user_id: str, arguments: dict[str, Any]) -> ComposioResult:
        sdk = self._get_sdk()
        if sdk is None or not self.api_key:
            raise RuntimeError("composio SDK not configured (missing key or package)")
        # Manual execution rejects the default "latest" version — resolve the
        # tool's pinned version first (SDK caches schemas, so this is cheap).
        version: str | None = None
        try:
            tool = sdk.tools.get_raw_composio_tool_by_slug(slug)  # type: ignore[attr-defined]
            version = getattr(tool, "version", None) or None
        except Exception:  # noqa: BLE001 - fall through to skip-check retry
            version = None
        if version:
            try:
                res = sdk.tools.execute(  # type: ignore[attr-defined]
                    slug=slug, user_id=user_id, arguments=arguments, version=version
                )
            except Exception as exc:  # noqa: BLE001 - Mk18: fail closed, no skip-check retry
                return ComposioResult(ok=False, error=f"tool version check failed, not executed: {exc}")
        else:
            # Mk18: without a pinned version the call is refused rather than
            # executed with dangerously_skip_version_check=True.
            return ComposioResult(ok=False, error=f"unknown version for {slug}: not executed")
        data = getattr(res, "data", res)
        if isinstance(data, dict) and data.get("error"):
            return ComposioResult(ok=False, error=str(data.get("error")))
        return ComposioResult(ok=True, data=data)

    def get_connect_url(self, app: str, user_id: str, auth_config_id: str | None = None) -> str:
        if not self.api_key:
            raise RuntimeError("save your integration API key in Account → Integrations first")
        sdk = self._get_sdk()
        if sdk is None:
            raise RuntimeError("server is missing the Composio package — run `uv pip install composio` and restart")
        if not auth_config_id:
            raise RuntimeError(
                f"no auth config for {app!r} (set COMPOSIO__{app.upper()}_AUTH_CONFIG "
                "from Composio dashboard → Auth Configs)."
            )
        try:
            link = sdk.connected_accounts.link(user_id, auth_config_id)  # type: ignore[attr-defined]
        except Exception as exc:
            raise RuntimeError(f"composio link failed for {app!r}: {exc}") from exc
        url = getattr(link, "redirect_url", None) or getattr(link, "redirectUrl", None)
        if not url:
            raise RuntimeError(f"composio link for {app!r} returned no redirect URL")
        return str(url)

    @staticmethod
    def _toolkit_slug(value: Any) -> str:
        """Normalize a toolkit ref (str slug, dict, or SDK object with .slug)."""
        if isinstance(value, str):
            return value
        if isinstance(value, dict):
            return str(value.get("slug", value.get("name", "")))
        return str(getattr(value, "slug", getattr(value, "name", "")))

    def list_for_user(self, user_id: str) -> list[dict[str, Any]]:
        sdk = self._get_sdk()
        if sdk is None or not self.api_key:
            return []
        try:
            accounts = sdk.connected_accounts.list(user_ids=[user_id])  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - list failure means unknown status
            return []
        items = getattr(accounts, "items", accounts)
        if not isinstance(items, list):
            return []
        out: list[dict[str, Any]] = []
        for a in items:
            if isinstance(a, dict):
                out.append(
                    {
                        "id": str(a.get("id", "")),
                        "toolkit": self._toolkit_slug(a.get("toolkit", a.get("app_name", ""))),
                        "status": str(a.get("status", a.get("state", ""))),
                    }
                )
                continue
            out.append(
                {
                    "id": str(getattr(a, "id", "")),
                    "toolkit": self._toolkit_slug(getattr(a, "toolkit", getattr(a, "app_name", ""))),
                    "status": str(getattr(a, "status", getattr(a, "state", ""))),
                }
            )
        return out

    def delete_connection(self, user_id: str, connection_id: str) -> tuple[bool, str]:
        """Best-effort remote revoke. Never raises — (ok, note).

        Mk18 Phase F: Disconnect calls this first; only a success flips the
        local row to revoked. An SDK without a delete API yields a skipped
        note (local revoke still stands); an explicit API failure is (False).
        """
        _ = user_id
        sdk = self._get_sdk()
        if sdk is None or not self.api_key:
            return False, "composio SDK not configured"
        delete = getattr(getattr(sdk, "connected_accounts", None), "delete", None)
        if not callable(delete):
            return True, "remote revoke unsupported by SDK version — local revoke only"
        if not connection_id:
            return True, "no remote connection id recorded — local revoke only"
        try:
            delete(connection_id)
        except TypeError:
            try:
                delete(id=connection_id)
            except Exception as exc:  # noqa: BLE001
                return False, f"remote revoke failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            return False, f"remote revoke failed: {exc}"
        return True, "revoked remotely"

    def validate_key(self) -> tuple[bool, str]:
        """Health check for the key wizard — never raises, never leaks the key."""
        if not self.api_key:
            return False, "paste your integration API key first"
        sdk = self._get_sdk()
        if sdk is None:
            return False, "server is missing the Composio package — run `uv pip install composio` and restart (`make run`)"
        try:
            toolkits = sdk.toolkits.get()  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - health check surfaces message
            msg = str(exc).lower()
            if "401" in msg or "unauthorized" in msg or "forbidden" in msg or "invalid" in msg:
                return False, "key rejected by Composio (401) — copy it again from app.composio.dev"
            return False, f"could not reach Composio: {exc}"
        items = getattr(toolkits, "items", toolkits)
        try:
            n = len(items or [])
        except TypeError:
            n = 0
        return True, f"key works ({n} toolkits visible)" if n else "key works"

    def toolkit_exists(self, toolkit: str) -> tuple[bool, str]:
        sdk = self._get_sdk()
        if sdk is None or not self.api_key:
            return False, "missing key or package"
        try:
            found = sdk.toolkits.get(toolkit)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - fall back to full list
            try:
                all_tk = sdk.toolkits.list()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - surface reachability
                return False, f"could not reach Composio: {exc}"
            items = getattr(all_tk, "items", all_tk)
            names = set()
            for t in items if isinstance(items, list) else []:
                names.add(self._toolkit_slug(t).lower())
            if toolkit.lower() in names:
                return True, "found"
            return False, f"unknown toolkit {toolkit!r} — check spelling (e.g. gmail, slack, notion)"
        return True, "found" if found else f"unknown toolkit {toolkit!r}"

    def ensure_auth_config(self, toolkit: str) -> tuple[str | None, str]:
        """Fetch-or-create a managed-OAuth auth config for a toolkit; returns (id, note)."""
        sdk = self._get_sdk()
        if sdk is None or not self.api_key:
            return None, "missing key or package"
        ac = getattr(sdk, "auth_configs", None)
        if ac is None:
            return None, "SDK has no auth_configs — upgrade `pip install -U composio`"
        try:
            existing = ac.list(toolkit_slug=toolkit)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - retry unfiltered list
            try:
                existing = ac.list()  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - surface list failure
                return None, f"could not list auth configs: {exc}"
        items = getattr(existing, "items", existing)
        if isinstance(items, list):
            for c in items:
                if isinstance(c, str):
                    continue
                tk = self._toolkit_slug(c.get("toolkit") if isinstance(c, dict) else getattr(c, "toolkit", ""))
                cid = str(c.get("id") if isinstance(c, dict) else getattr(c, "id", ""))
                if tk.lower() == toolkit.lower() and cid:
                    return cid, "reused existing auth config"
        try:
            created = ac.create(  # type: ignore[attr-defined]
                toolkit, {"type": "use_composio_managed_auth"}
            )
        except Exception as exc:  # noqa: BLE001 - guide user to dashboard fallback
            return None, f"create an Auth Config for {toolkit!r} in Composio dashboard, then retry: {exc}"
        cid = created if isinstance(created, str) else str(
            created.get("id") if isinstance(created, dict) else getattr(created, "id", "")
        )
        return (cid or None), "created new managed auth config" if cid else "created, but no id returned"

    def discover_tools(self, toolkit: str, user_id: str = "") -> tuple[list[dict[str, Any]], str]:
        sdk = self._get_sdk()
        if sdk is None or not self.api_key:
            return [], "missing key or package"
        try:
            raw = sdk.tools.get_raw_composio_tools(toolkits=[toolkit])  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 - fall back to provider-format listing
            raw = None
        if raw is None:
            try:
                raw = sdk.tools.get(user_id or "health-check", toolkits=[toolkit])  # type: ignore[attr-defined]
            except Exception as exc:  # noqa: BLE001 - surface discovery failure
                return [], f"could not list tools for {toolkit!r}: {exc}"
        items = getattr(raw, "items", raw)
        if isinstance(items, dict):
            items = [items]
        if not isinstance(items, list):
            try:
                items = list(items)
            except TypeError:
                return [], f"unexpected tools response for {toolkit!r}"
        out: list[dict[str, Any]] = []
        for t in items:
            if isinstance(t, dict):
                fn = t.get("function", {}) if "function" in t else t
                out.append(
                    {
                        "slug": str(
                            t.get("slug", t.get("name", fn.get("name", "")))
                            if "function" not in t
                            else fn.get("name", "")
                        ),
                        "description": str(t.get("description", fn.get("description", ""))),
                        "input_schema": (
                            t.get("input_parameters") or t.get("parameters")
                            or fn.get("parameters") or {"type": "object"}
                        ),
                    }
                )
                continue
            out.append(
                {
                    "slug": str(getattr(t, "slug", getattr(t, "name", ""))),
                    "description": str(getattr(t, "description", "")),
                    "input_schema": getattr(
                        t, "input_parameters",
                        getattr(t, "parameters", getattr(t, "input_schema", {"type": "object"})),
                    ),
                }
            )
        out = [t for t in out if t["slug"]]
        return out, f"found {len(out)} tools"


def build_client(
    api_key: str | None, base_url: str | None = None, timeout_seconds: float = 30.0
) -> ComposioClient | FakeComposioClient:
    if not api_key:
        return FakeComposioClient()
    return ComposioClient(api_key=api_key, base_url=base_url, timeout_seconds=timeout_seconds)

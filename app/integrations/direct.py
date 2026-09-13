"""Generic direct REST connector — per-user header injection (Mk9 hybrid path)."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass
class DirectResult:
    ok: bool
    status_code: int | None = None
    content: str = ""
    data: Any = None
    latency_ms: int = 0
    error: str = ""


def call_direct(
    *,
    base_url: str,
    method: str,
    path: str,
    token: str | None = None,
    params: dict[str, Any] | None = None,
    body: dict[str, Any] | None = None,
    timeout_seconds: float = 30.0,
    client: httpx.Client | None = None,
) -> DirectResult:
    started = time.monotonic()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    own = client is None
    client = client or httpx.Client(timeout=timeout_seconds)
    try:
        resp = client.request(
            method.upper(),
            base_url.rstrip("/") + path,
            params=params or None,
            json=body or None,
            headers=headers or None,
            timeout=timeout_seconds,
        )
    except httpx.TimeoutException:
        return DirectResult(ok=False, error="tool call timed out")
    except httpx.HTTPError as exc:
        return DirectResult(ok=False, error=f"http error: {exc}")
    finally:
        if own:
            client.close()
    latency_ms = int((time.monotonic() - started) * 1000)
    data: Any = None
    try:
        data = resp.json()
    except ValueError:
        pass
    return DirectResult(
        ok=200 <= resp.status_code < 300,
        status_code=resp.status_code,
        content=resp.text if resp.text else str(resp.status_code),
        data=data,
        latency_ms=latency_ms,
        error="" if 200 <= resp.status_code < 300 else f"http {resp.status_code}",
    )

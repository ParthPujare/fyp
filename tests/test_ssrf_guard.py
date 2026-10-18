"""Mk19 Phase 2 — SSRF guard: outbound URL policy + dashboard enforcement.

Plan Mk19 acceptance #2: the local-models probe and every llm-key `base_url`
accept a stub local LLM, but reject metadata IP, own-service ports and
non-http schemes (422), and no response ever echoes upstream body text.

Unit half = `validate_outbound_url` rule-by-rule; endpoint half drives the real
dashboard with a member JWT (incl. a live stdlib HTTP stub proving the allow path).
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app.auth.jwt import create_token
from app.auth.users import USERS
from app.config import get_settings
from app.dashboard.app import app as dashboard_app
from app.harness.url_policy import UrlPolicyError, validate_outbound_url

settings = get_settings()
SERVICE_PORTS = {8000, 8001, 8002, 8003, 8004, 8005, 8006}


def _auth(username: str) -> dict[str, str]:
    tok = create_token(
        USERS[username], settings.jwt.secret, settings.jwt.algorithm, settings.jwt.expiry_minutes
    )
    return {"Authorization": f"Bearer {tok}"}


def _deny(url: str) -> str:
    with pytest.raises(UrlPolicyError) as exc:
        validate_outbound_url(url, settings)
    return str(exc.value)


# ---- rule-by-rule unit coverage ----


def test_allowed_urls_strict_mode():
    """Probe/fetch mode (allow_public=False): loopback + RFC1918 + ULA only."""
    # local providers
    assert validate_outbound_url("http://127.0.0.1:1234/v1", settings)
    assert validate_outbound_url("http://localhost:11434", settings)
    assert validate_outbound_url("http://[::1]:1234/v1", settings)
    assert validate_outbound_url("http://192.168.1.10:1234", settings)
    assert validate_outbound_url("http://10.1.2.3:8080", settings)
    assert validate_outbound_url("http://172.16.0.5:9000", settings)
    assert validate_outbound_url("http://[fd00::1]:1234", settings)  # IPv6 ULA
    # host.docker.internal for local models from inside Docker (only when it resolves)
    from app.harness.url_policy import _resolve

    if _resolve("host.docker.internal"):
        assert validate_outbound_url("http://host.docker.internal:1234/v1", settings)


def test_public_urls_split_by_mode():
    """Locked decision (2026-10-18): the live PROBE is strict; STORED BYOK
    endpoints may be public (OpenRouter-style custom gateways keep working)."""
    public = "https://openrouter.ai/api/v1"
    # stored-key mode: public allowed
    assert validate_outbound_url(public, settings, allow_public=True)
    # probe/fetch mode: public denied
    with pytest.raises(UrlPolicyError, match="blocked address"):
        validate_outbound_url(public, settings, allow_public=False)
    # ...but metadata/link-local/CGNAT stay dead in BOTH modes
    for mode in (True, False):
        with pytest.raises(UrlPolicyError):
            validate_outbound_url("http://169.254.169.254/latest", settings, allow_public=mode)
        with pytest.raises(UrlPolicyError):
            validate_outbound_url("http://100.64.0.1/", settings, allow_public=mode)
        with pytest.raises(UrlPolicyError):
            validate_outbound_url("http://[fe80::1]/", settings, allow_public=mode)
        # ...and own-service ports + credentials + bad schemes too
        with pytest.raises(UrlPolicyError):
            validate_outbound_url("http://127.0.0.1:8001/", settings, allow_public=mode)
        with pytest.raises(UrlPolicyError):
            validate_outbound_url("http://user:pass@example.com/", settings, allow_public=mode)
        with pytest.raises(UrlPolicyError):
            validate_outbound_url("file:///etc/passwd", settings, allow_public=mode)


def test_denied_schemes_and_shape():
    assert _deny("file:///etc/passwd") == "URL scheme must be http or https"
    assert _deny("ftp://127.0.0.1/x") == "URL scheme must be http or https"
    assert _deny("gopher://127.0.0.1/x") == "URL scheme must be http or https"
    assert _deny("http:///no-host") == "URL host is required"
    assert _deny("http://user:pass@127.0.0.1:1234") == "URL must not contain credentials"
    assert _deny("http://127.0.0.1:99999/") == "URL port is invalid"
    with pytest.raises(UrlPolicyError):
        validate_outbound_url("not a url at all \x00", settings)


def test_denied_metadata_and_linklocal():
    # cloud metadata IP (link-local169.254/16 → outside the allowlist by construction)
    assert _deny("http://169.254.169.254/latest/meta-data/") == "URL resolves to a blocked address"
    assert _deny("http://169.254.1.1/") == "URL resolves to a blocked address"
    # metadata hostname dies BEFORE any DNS lookup (no resolver-side oracle)
    assert _deny("http://metadata.google.internal/computeMetadata/v1/") == "URL host is not allowed"
    assert _deny("http://metadata.example.com/") == "URL host is not allowed"
    # IPv6 link-local + CGNAT + wildcard + public + multicast
    assert _deny("http://[fe80::1]/") == "URL resolves to a blocked address"
    assert _deny("http://100.64.0.1/") == "URL resolves to a blocked address"
    assert _deny("http://0.0.0.0:1234/") == "URL resolves to a blocked address"
    assert _deny("http://[::]/") == "URL resolves to a blocked address"
    assert _deny("http://8.8.8.8/") == "URL resolves to a blocked address"
    assert _deny("http://224.0.0.1/") == "URL resolves to a blocked address"


def test_denied_own_service_ports():
    # exact configured endpoint (config default: gateway on 127.0.0.1:8001)
    assert _deny("http://127.0.0.1:8001/") == "URL points at an internal service"
    assert _deny("http://localhost:8001/") == "URL points at an internal service"
    for port in sorted(SERVICE_PORTS - {8001}):
        assert _deny(f"http://127.0.0.1:{port}/") == "URL points at an internal service"
    # service port on a different private host + IPv6 loopback form
    assert _deny("http://192.168.1.9:8005/") == "URL points at an internal service"
    assert _deny("http://[::1]:8000/") == "URL points at an internal service"
    # ...but an allowed-range URL on a non-service port keeps working
    assert validate_outbound_url("http://127.0.0.1:1234/v1", settings)


def test_v4_mapped_v6_cannot_dodge_rules():
    assert _deny("http://[::ffff:169.254.169.254]/") == "URL resolves to a blocked address"
    assert _deny("http://[::ffff:127.0.0.1]:8001/") == "URL points at an internal service"


def test_error_messages_never_echo_input():
    for bad in (
        "http://169.254.169.254/latest/secret-token=abc",
        "http://metadata.google.internal/SA-SECRET",
        "http://127.0.0.1:8001/admin?password=hunter2",
    ):
        msg = _deny(bad)
        assert "169.254" not in msg and "SA-SECRET" not in msg
        assert "hunter2" not in msg and "metadata.google" not in msg


# ---- live stub server (the allow path, end to end) ----


@pytest.fixture
def stub_llm():
    """A stdlib local LLM endpoint serving GET /models — the legit probe target."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path.endswith("/models"):
                body = json.dumps({"data": [{"id": "local-model-7b"}]}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args):  # keep test output clean
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    assert port not in SERVICE_PORTS  # ephemeral port, never one of ours
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


@pytest.fixture
def error_stub():
    """A local endpoint that answers 500 with a secret-laden body (exfil bait)."""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"SECRET-BODY-CANARY: pg-password-leaked")

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    port = srv.server_address[1]
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{port}"
    srv.shutdown()
    srv.server_close()


def _client() -> TestClient:
    return TestClient(dashboard_app)


# ---- endpoint enforcement ----


def test_probe_accepts_stub_local_llm(stub_llm):
    resp = _client().get(
        "/api/account/local-models",
        params={"provider": "custom_openai", "base_url": stub_llm},
        headers=_auth("alice@acme.com"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["models"] == ["local-model-7b"]
    assert body["base_url"] == stub_llm


def test_probe_rejects_metadata_ip_without_echo():
    resp = _client().get(
        "/api/account/local-models",
        params={"provider": "custom_openai", "base_url": "http://169.254.169.254/latest"},
        headers=_auth("alice@acme.com"),
    )
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail == "URL resolves to a blocked address"
    assert "169.254" not in detail and "latest" not in detail


def test_probe_rejects_own_service_ports():
    for port in (8000, 8001, 8006):
        resp = _client().get(
            "/api/account/local-models",
            params={"provider": "custom_openai", "base_url": f"http://127.0.0.1:{port}"},
            headers=_auth("alice@acme.com"),
        )
        assert resp.status_code == 422, (port, resp.text)
        assert resp.json()["detail"] == "URL points at an internal service"


def test_probe_rejects_non_http_scheme():
    resp = _client().get(
        "/api/account/local-models",
        params={"provider": "custom_openai", "base_url": "file:///etc/passwd"},
        headers=_auth("alice@acme.com"),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "URL scheme must be http or https"


def test_probe_502_never_echoes_upstream_body(error_stub):
    resp = _client().get(
        "/api/account/local-models",
        params={"provider": "custom_openai", "base_url": error_stub},
        headers=_auth("alice@acme.com"),
    )
    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail == "local server returned 500"
    assert "SECRET-BODY-CANARY" not in detail and "pg-password-leaked" not in detail


def test_probe_requires_member_jwt():
    resp = _client().get(
        "/api/account/local-models",
        params={"provider": "custom_openai", "base_url": "http://127.0.0.1:1234"},
    )
    assert resp.status_code == 401


def test_key_save_rejects_bad_base_url():
    resp = _client().post(
        "/api/account/llm-keys",
        json={
            "provider": "custom_openai",
            "model": "m",
            "api_key": "k",
            "base_url": "http://169.254.169.254/latest",
        },
        headers=_auth("alice@acme.com"),
    )
    assert resp.status_code == 422
    assert "169.254" not in resp.json()["detail"]


def test_key_save_accepts_local_base_url():
    """The allow path persists without any live round-trip (no verify on save)."""
    resp = _client().post(
        "/api/account/llm-keys",
        json={
            "provider": "custom_openai",
            "model": "local-model-7b",
            "api_key": "sk-local",
            "base_url": "http://127.0.0.1:1234/v1",
        },
        headers=_auth("alice@acme.com"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_url"] == "http://127.0.0.1:1234/v1"


def test_probe_rejects_public_host_but_key_save_allows_it():
    """The two-mode split, end to end: the strict probe refuses a public host,
    while the same URL persists fine as a stored BYOK endpoint."""
    resp = _client().get(
        "/api/account/local-models",
        params={"provider": "custom_openai", "base_url": "https://openrouter.ai/api/v1"},
        headers=_auth("alice@acme.com"),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "URL resolves to a blocked address"

    saved = _client().post(
        "/api/account/llm-keys",
        json={
            "provider": "custom_openai",
            "model": "or-model",
            "api_key": "sk-or-test",
            "base_url": "https://openrouter.ai/api/v1",
        },
        headers=_auth("alice@acme.com"),
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["base_url"] == "https://openrouter.ai/api/v1"


def test_metadata_still_denied_at_key_save():
    """allow_public mode must not open the metadata path via key save."""
    resp = _client().post(
        "/api/account/llm-keys",
        json={
            "provider": "custom_openai",
            "model": "m",
            "api_key": "k",
            "base_url": "http://169.254.169.254/latest",
        },
        headers=_auth("admin@acme.com"),
    )
    assert resp.status_code == 422
    assert resp.json()["detail"] == "URL resolves to a blocked address"

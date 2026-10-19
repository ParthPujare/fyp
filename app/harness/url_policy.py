"""Mk19 Phase 2 — outbound URL policy (SSRF guard).

Every member-/admin-supplied URL that the dashboard fetches or stores as a
local-model endpoint must pass :func:`validate_outbound_url` first:

* scheme ``http``/``https`` only (no ``file:``, ``ftp:``, ``gopher:``, …);
* no credentials in the URL (``user:pass@``);
* **metadata-adjacent and special ranges are always denied** (``169.254.0.0/16``,
  ``fe80::/10``, ``100.64.0.0/10``, ``0.0.0.0/8``, ``::``, multicast, broadcast);
* ``metadata.*`` hostnames are rejected before any DNS lookup;
* the app's own service ports (the ``SERVICES__*_url`` endpoints, e.g.
  8000-8006) are unreachable from member-supplied URLs — caught three ways:
  exact host+port match, resolved-IP+port match, and "allowed-range address on
  a configured service port" (so ``http://127.0.0.1:8001`` is blocked even when
  the configured gateway URL is ``http://gateway:8001``, and vice versa).

**Two modes** (locked decision 2026-10-18, "strict probe, internal-blocked keys"):

* ``allow_public=False`` (default) — the **probe/fetch** mode used when the server
  pulls a URL *right now and returns data* (``/api/account/local-models``):
  every resolved address must be inside loopback + RFC1918 + IPv6 ULA. Public
  internet, link-local/metadata and CGNAT are all denied **by construction**.
* ``allow_public=True`` — the **stored-endpoint** mode used when persisting a
  BYOK ``base_url`` the server will later call as that user's LLM gateway:
  public addresses are fine (OpenRouter-style custom endpoints keep working),
  but the always-denied ranges above, own-service ports, credentials and bad
  schemes still fail. Denying RFC1918 here would break legitimate BYOK use
  without adding safety (LLM calls are supposed to leave the host).

Mk20: a URL that resolves to loopback / a private network must ALSO use one of the
local-model ports (``SECURITY__LOCAL_LLM_PORTS``, default 1234 / 11434 / 8080), in both
modes, unless ``SECURITY__ALLOW_ANY_PRIVATE_PORT=true``. That closes the remaining
internal-port-scan oracle (the probe's "unreachable" vs "returned N" answer) and stops a
stored BYOK endpoint from pointing chat POSTs at arbitrary internal APIs.

Accepted residual (documented in plan Mk19): DNS rebinding — the address is
resolved here and again by httpx at connect time; pinning the connection to the
validated IP is explicitly out of scope for a single-host deployment.

Violation messages are fixed strings: the offending URL is never echoed back
(prevents response-smuggling the probe result out through error details).
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = frozenset({"http", "https"})

# ALWAYS denied, in both modes: link-local + metadata (169.254/16, fe80::/10),
# CGNAT (100.64/10), unspecified (0.0.0.0/8, ::), multicast and broadcast space.
# Deliberately not ipaddress's is_private — that property is True for link-local
# and CGNAT too, which is exactly what a metadata SSRF lands on.
_V4_DENY = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
)
_V6_DENY = (
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
    ipaddress.ip_network("::/128"),
)

# The ONLY ranges allow_public=False mode resolves into: loopback + RFC1918 (v4),
# ::1 + ULA fc00::/7 (v6).
_V4_ALLOW = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
)
_V6_ALLOW = (
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
)

_SERVICE_URL_ATTRS = (
    "auth_url",
    "gateway_url",
    "dashboard_url",
    "crm_url",
    "hr_url",
    "devops_url",
    "finance_url",
)

# Ports where a plain HTTP port number alone proves nothing about ownership —
# never blanket-blocked (a local LLM proxy could legitimately sit on 80/443).
_DEFAULT_PORTS = frozenset({80, 443})


class UrlPolicyError(ValueError):
    """The URL is not allowed for outbound fetch/storage. Message is a fixed string."""


def _unmapped(ip: ipaddress.IPv6Address | ipaddress.IPv4Address) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """``::ffff:127.0.0.1`` → ``127.0.0.1`` so mapped v4 literals can't dodge the v4 rules."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    return ip


def _is_private(raw: str) -> bool:
    """Loopback / RFC1918 / ULA — the ranges a member must not roam freely."""
    try:
        ip = _unmapped(ipaddress.ip_address(raw))
    except ValueError:
        return False
    nets = _V4_ALLOW if ip.version == 4 else _V6_ALLOW
    return any(ip in net for net in nets)


def _ip_allowed(raw: str, allow_public: bool) -> bool:
    try:
        ip = _unmapped(ipaddress.ip_address(raw))
    except ValueError:
        return False
    deny_nets = _V4_DENY if ip.version == 4 else _V6_DENY
    if any(ip in net for net in deny_nets):
        return False  # link-local/metadata/CGNAT/etc. — dead in both modes
    allow_nets = _V4_ALLOW if ip.version == 4 else _V6_ALLOW
    if any(ip in net for net in allow_nets):
        return True
    return allow_public  # public address: fine for stored endpoints, not for fetch


def _resolve(host: str) -> list[str]:
    """All addresses `host` resolves to; [] when it cannot be resolved (fail closed)."""
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return [str(literal)]
    try:
        infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return []
    return [info[4][0] for info in infos]


def _service_endpoints(settings) -> tuple[set[tuple[str, int]], set[tuple[str, int]], set[int]]:
    """`(host, port)` pairs, `(ip, port)` pairs and bare ports of the app's own services."""
    hosts: set[tuple[str, int]] = set()
    addrs: set[tuple[str, int]] = set()
    ports: set[int] = set()
    services = settings.services
    for attr in _SERVICE_URL_ATTRS:
        raw = str(getattr(services, attr, "") or "")
        try:
            parts = urlsplit(raw)
            host = (parts.hostname or "").lower()
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            continue  # malformed config entry — ignore rather than block member URLs
        if not host:
            continue
        hosts.add((host, port))
        ports.add(port)
        for ip in _resolve(host):
            addrs.add((ip, port))
    return hosts, addrs, ports


def validate_outbound_url(url: str, settings=None, *, allow_public: bool = False) -> str:
    """Return `url` unchanged when it may be fetched/stored server-side.

    `allow_public=False` (the probe/fetch mode): every resolved address must be
    loopback/RFC1918/ULA — the public internet is denied too.
    `allow_public=True` (the stored-BYOK-endpoint mode): public addresses pass,
    the always-denied ranges and the own-service-port rules still apply.
    Raises :class:`UrlPolicyError` (fixed message) otherwise. Callers translate
    the error into their own 422.
    """
    if settings is None:
        from app.config import get_settings

        settings = get_settings()

    try:
        parts = urlsplit(url)
    except ValueError as exc:
        raise UrlPolicyError("URL is malformed") from exc
    scheme = (parts.scheme or "").lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise UrlPolicyError("URL scheme must be http or https")
    try:
        host = (parts.hostname or "").lower()
        port = parts.port or (443 if scheme == "https" else 80)
    except ValueError as exc:
        raise UrlPolicyError("URL port is invalid") from exc
    if not host:
        raise UrlPolicyError("URL host is required")
    if parts.username is not None or parts.password is not None:
        raise UrlPolicyError("URL must not contain credentials")
    if host.startswith("metadata."):
        raise UrlPolicyError("URL host is not allowed")

    resolved = _resolve(host)
    if not resolved:
        raise UrlPolicyError("URL host could not be resolved")
    if not all(_ip_allowed(ip, allow_public) for ip in resolved):
        raise UrlPolicyError("URL resolves to a blocked address")

    # Own-service block: (a) exact configured host+port, (b) resolved ip+port,
    # (c) any allowed-range address on a configured service port (non-80/443).
    svc_hosts, svc_addrs, svc_ports = _service_endpoints(settings)
    if (host, port) in svc_hosts or any((ip, port) in svc_addrs for ip in resolved):
        raise UrlPolicyError("URL points at an internal service")
    if port not in _DEFAULT_PORTS and port in svc_ports:
        raise UrlPolicyError("URL points at an internal service")

    # Mk20: private / loopback targets only on local-model ports.
    security = getattr(settings, "security", None)
    if any(_is_private(ip) for ip in resolved) and not getattr(security, "allow_any_private_port", False):
        allowed_ports = set(getattr(security, "local_llm_ports", None) or (1234, 11434, 8080))
        if port not in allowed_ports:
            raise UrlPolicyError("URL uses a port that is not allowed for a local model")
    return url

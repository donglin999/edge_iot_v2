"""Network safety helpers.

SSRF guard for user-supplied connection targets. The connection/storage *test*
endpoints let a caller ask the server to open a TCP connection (or HTTP request)
to an arbitrary ``host:port`` taken straight from the request body. Without a
guard, that turns the server into a proxy for probing internal services and,
critically, cloud metadata endpoints (``169.254.169.254``).

``assert_config_targets_allowed`` resolves every candidate host in a connection
config and rejects any that lands on a loopback, link-local, or RFC1918 private
address — unless ``settings.ALLOW_PRIVATE_NETWORK_TESTS`` is enabled (default
``True`` for backward-compatible local/dev use; set it to ``False`` in any
environment reachable by untrusted callers).
"""
from __future__ import annotations

import ipaddress
import socket
from typing import Iterator
from urllib.parse import urlparse

from django.conf import settings


class PrivateNetworkNotAllowed(Exception):
    """Raised when a connection target resolves to a blocked (loopback /
    link-local / private) address while private-network tests are disabled."""


# Config keys that carry a bare host / IP.
_HOST_KEYS = (
    "source_ip",
    "host",
    "hostname",
    "broker",
    "broker_ip",
    "server",
    "address",
)
# Config keys that carry a URL / endpoint we must parse the host out of.
_URL_KEYS = ("url", "endpoint_url", "endpoint", "uri")


def private_network_tests_allowed() -> bool:
    """Whether tests against private/loopback/link-local addresses are permitted."""
    return bool(getattr(settings, "ALLOW_PRIVATE_NETWORK_TESTS", True))


def is_blocked_ip(ip: "ipaddress._BaseAddress") -> bool:
    """True for loopback (127/8, ::1), link-local (169.254/16 incl. the cloud
    metadata address 169.254.169.254), RFC1918 private ranges (10/8, 172.16/12,
    192.168/16 + IPv6 ULA), and the unspecified address (0.0.0.0 / ::)."""
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_unspecified
    )


def resolve_host_ips(host: str) -> list["ipaddress._BaseAddress"]:
    """Resolve ``host`` to the list of IPs it maps to.

    A bare IP literal is returned directly (no DNS). Hostnames are resolved via
    ``getaddrinfo`` so that a name pointing at a private address is still caught.
    """
    host = host.strip()
    if host.startswith("[") and host.endswith("]"):  # bracketed IPv6 literal
        host = host[1:-1]
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass

    ips: list[ipaddress._BaseAddress] = []
    for info in socket.getaddrinfo(host, None):
        addr = info[4][0]
        # Strip IPv6 zone id (e.g. "fe80::1%eth0").
        addr = addr.split("%", 1)[0]
        try:
            ips.append(ipaddress.ip_address(addr))
        except ValueError:
            continue
    return ips


def assert_host_allowed(host: str) -> None:
    """Raise :class:`PrivateNetworkNotAllowed` if ``host`` resolves to a blocked
    address while private-network tests are disabled. No-op when allowed or when
    the host cannot be resolved (the real connection attempt will fail later)."""
    if not host or private_network_tests_allowed():
        return
    try:
        ips = resolve_host_ips(host)
    except socket.gaierror:
        # Unresolvable name: no SSRF reachable, let the downstream call fail.
        return
    for ip in ips:
        if is_blocked_ip(ip):
            raise PrivateNetworkNotAllowed(
                f"目标地址 '{host}' 解析到受限网络地址 {ip} "
                f"(loopback/link-local/private)。如需在内网测试，请设置环境变量 "
                f"ALLOW_PRIVATE_NETWORK_TESTS=true。"
            )


def iter_config_targets(config: object) -> Iterator[str]:
    """Yield every candidate host found in a connection/storage config dict."""
    if not isinstance(config, dict):
        return
    for key in _HOST_KEYS:
        val = config.get(key)
        if isinstance(val, str) and val.strip():
            yield val.strip()
    for key in _URL_KEYS:
        val = config.get(key)
        if isinstance(val, str) and val.strip():
            raw = val if "://" in val else f"//{val}"
            hostname = urlparse(raw).hostname
            if hostname:
                yield hostname


def assert_config_targets_allowed(config: object) -> None:
    """Validate every host referenced by a connection/storage config.

    Raises :class:`PrivateNetworkNotAllowed` for the first blocked target.
    A no-op when ``ALLOW_PRIVATE_NETWORK_TESTS`` is enabled.
    """
    if private_network_tests_allowed():
        return
    for host in iter_config_targets(config):
        assert_host_allowed(host)

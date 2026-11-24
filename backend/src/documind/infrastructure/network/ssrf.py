"""Outbound host policy for customer-supplied endpoints (ADR 0021).

Database hosts, storage endpoints and (later) custom AI endpoints are chosen
by customers, so DocuMind must not be steered into its own network: every
resolved address must be public unless an operator allowlists the host
(on-premises deployments). Checked at validation and again before connecting,
which limits DNS rebinding.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlsplit

_CGNAT = ipaddress.ip_network("100.64.0.0/10")
_EXTRA_BLOCKED = (
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fd00:ec2::/32"),  # cloud metadata over IPv6
)


class BlockedHostError(ValueError):
    """The host isn't allowed. The message is safe to show to users."""


def blocked_reason(address: str) -> str | None:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (including cloud metadata)"
    if ip in _CGNAT or any(ip in net for net in _EXTRA_BLOCKED):
        return "reserved"
    if ip.is_private:
        return "private network"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return "reserved"
    return None


async def resolve(host: str, port: int) -> list[str]:
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise BlockedHostError(f"The host '{host}' couldn't be resolved.") from exc
    return sorted({str(info[4][0]) for info in infos})


async def check_host(host: str, port: int, *, allowed: frozenset[str]) -> list[str]:
    """Raise ``BlockedHostError`` unless every address of ``host`` is public."""
    host = host.strip().lower().rstrip(".")
    if host in allowed:
        return await resolve(host, port)
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    addresses = [str(literal)] if literal is not None else await resolve(host, port)
    for address in addresses:
        reason = blocked_reason(address)
        if reason is not None:
            raise BlockedHostError(
                f"The host '{host}' points to a {reason} address, which isn't allowed. "
                "On-premises deployments can allowlist it (INFRASTRUCTURE_ALLOWED_PRIVATE_HOSTS)."
            )
    return addresses


async def check_url(url: str, *, allowed: frozenset[str]) -> tuple[str, int]:
    """HTTPS endpoints only (plain HTTP only for allowlisted hosts)."""
    parts = urlsplit(url)
    if parts.scheme not in ("https", "http") or not parts.hostname:
        raise BlockedHostError("The endpoint must be an https:// URL.")
    if parts.username or parts.password:
        raise BlockedHostError("Don't put credentials in the endpoint URL.")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise BlockedHostError("The endpoint must not include a path or query.")
    host = parts.hostname.lower()
    if parts.scheme == "http" and host not in allowed:
        raise BlockedHostError("The endpoint must use https.")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    await check_host(host, port, allowed=allowed)
    return host, port

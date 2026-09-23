"""SSRF (Server-Side Request Forgery) guard.

Tessera makes outbound HTTP calls in exactly two places:

1. ``src/rag/router.py::tavily_search`` — fixed URL ``https://api.tavily.com/search``.
   The user-supplied *question* is placed in the POST *body* (not the URL), so
   there is no URL injection surface on that path.

2. ``src/orchestrator/a2a_supervisor.py::A2AJsonRpcClient.send_message`` — the
   base URL is read from ``TESSERA_DRAFTER_URL`` / ``TESSERA_JUDGE_URL`` env vars
   at *startup*, not from request bodies.

Neither path allows a caller to supply an arbitrary outbound URL at request time.

This module is provided as an explicit negative-control guard: if a future
contributor adds user-supplied URL fetching, they MUST call ``validate_outbound_url``
before making the request.
"""
from __future__ import annotations

import ipaddress
import re
from urllib.parse import urlparse


# Allowlist of approved external hostnames.  Update if new providers are added.
_APPROVED_HOSTS: frozenset[str] = frozenset(
    {
        "api.tavily.com",
        "api.openai.com",
        "api.deepseek.com",
        "openrouter.ai",
    }
)

# Private / link-local / loopback CIDR ranges that must never be reachable.
_BLOCKED_CIDRS: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),  # link-local
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),        # ULA
    ipaddress.ip_network("fe80::/10"),       # link-local IPv6
]


class SSRFBlockedError(ValueError):
    """Raised when a URL is rejected by the SSRF guard."""


def validate_outbound_url(url: str, *, approved_hosts: frozenset[str] | None = None) -> None:
    """Raise ``SSRFBlockedError`` if *url* must not be fetched.

    Checks (in order):
    1. Scheme must be ``https`` (no ``http``, ``file``, ``ftp``, etc.).
    2. Host must be in *approved_hosts* (defaults to ``_APPROVED_HOSTS``).
    3. If the host is a raw IP address, it must not fall in a private/loopback CIDR.

    Args:
        url: The URL that will be fetched.
        approved_hosts: Override the built-in allowlist (useful in tests).

    Raises:
        SSRFBlockedError: If the URL is blocked for any reason.
    """
    approved = approved_hosts if approved_hosts is not None else _APPROVED_HOSTS
    parsed = urlparse(url)

    if parsed.scheme != "https":
        raise SSRFBlockedError(f"SSRF blocked: scheme '{parsed.scheme}' is not allowed (only https)")

    host = (parsed.hostname or "").lower()
    if not host:
        raise SSRFBlockedError("SSRF blocked: could not parse hostname")

    if host not in approved:
        raise SSRFBlockedError(f"SSRF blocked: host '{host}' is not in the approved-hosts allowlist")

    # Extra check: if the host *is* approved but resolves to an IP literal,
    # verify it is not in a private range.  (Defends against DNS rebinding for
    # IP-literal URLs — real DNS rebinding is out of scope here.)
    # NOTE: SSRFBlockedError is a ValueError subclass, so we must NOT wrap the
    # CIDR check inside the same try/except that catches ValueError from ip_address().
    is_ip_literal = False
    addr = None
    try:
        addr = ipaddress.ip_address(host)
        is_ip_literal = True
    except ValueError:
        pass  # host is a domain name, not an IP literal — skip the IP check
    if is_ip_literal and addr is not None:
        for cidr in _BLOCKED_CIDRS:
            if addr in cidr:
                raise SSRFBlockedError(f"SSRF blocked: IP address {host} is in a private/reserved range")

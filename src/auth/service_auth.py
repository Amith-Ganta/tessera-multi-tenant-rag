"""Service-to-service authentication for Tessera A2A agents.

Tessera's Drafter and Judge agents run as standalone HTTP servers.  Any caller
that can reach those ports can supply an arbitrary ``tenant_slug`` in the
JSON-RPC metadata, which would let them retrieve documents from any tenant's
corpus without holding a valid user token.

This module closes that gap with a shared-secret scheme:

* ``TESSERA_A2A_SERVICE_TOKEN`` (env var) — a secret string provisioned at
  deploy time (e.g. via K8s Secret / .env file).  When absent the system runs
  in *open* mode, which is acceptable for fully isolated local development
  environments but logged as a warning.
* ``get_service_token()`` — returns the configured token or ``None``.
* ``verify_service_token(value)`` — constant-time compare against the
  configured token; returns ``True`` when authentication is satisfied.  Returns
  ``True`` unconditionally when no token is configured so the fail-open dev
  path is explicit and not a silent miss.
* ``SERVICE_AUTH_HEADER`` — the HTTP header name (``X-Tessera-Service-Token``)
  that agents check and the supervisor injects.

Fail-closed principle: when a token IS configured and the inbound header does
not match, the agent returns HTTP 401 before dispatching to the skill handler.
"""
from __future__ import annotations

import hmac
import os

SERVICE_AUTH_HEADER: str = "X-Tessera-Service-Token"
_ENV_KEY: str = "TESSERA_A2A_SERVICE_TOKEN"


def get_service_token() -> str | None:
    """Return the configured service token, or None if not set."""
    return os.environ.get(_ENV_KEY) or None


def verify_service_token(provided: str | None) -> bool:
    """Return True if ``provided`` matches the configured service token.

    Uses ``hmac.compare_digest`` to prevent timing-based side-channels.
    Returns True unconditionally when no token is configured (dev/test mode).
    Returns False when a token is configured and ``provided`` is missing or
    does not match.
    """
    expected = get_service_token()
    if expected is None:
        return True
    if not provided:
        return False
    try:
        return hmac.compare_digest(expected.encode(), provided.encode())
    except (AttributeError, TypeError):
        return False

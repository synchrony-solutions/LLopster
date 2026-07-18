"""Shared-secret authentication for the inbound write surfaces.

A single bearer/basic shared secret guards the network surfaces that can spend
LLM money or open PRs with the write-scoped GitHub token: the AlertManager
``/webhook``, the manual-trigger routes, and the dashboard's settings/license
mutations. The secret resolves **settings-override → env**, mirroring the
license-key pattern: an ``api_auth_token`` Setting (DB, no redeploy) wins over
the ``LLOPSTER_API_TOKEN`` env var.

Fail-safe contract (degrade to LESS access, never crash):
  - No secret configured → the check is DISABLED (a loud startup warning is
    logged once) so local eval keeps working.
  - Secret configured → any request without a matching credential is rejected
    401 *before* the route body runs. The compare is constant-time.
  - A DB error while reading the override degrades to the env var rather than
    raising — a transient DB hiccup must not lock AlertManager out *or* crash.

The secret is accepted as ``Authorization: Bearer <token>`` or HTTP Basic (the
password component; the username is ignored), so machine clients (AlertManager,
the dashboard→agent proxy, curl) and browsers (native Basic-auth prompt via the
``WWW-Authenticate`` header) all work with one mechanism.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import logging

from fastapi import HTTPException, Request

from src.config import config
from src.db import repository as repo

log = logging.getLogger("llopster.auth")

API_TOKEN_SETTING = "api_auth_token"

# Prompt browsers to send Basic credentials; also advertise Bearer for machines.
_UNAUTHORIZED_HEADERS = {"WWW-Authenticate": 'Basic realm="llopster", Bearer'}


async def resolve_api_token(sessionmaker) -> str:
    """Active shared secret: ``api_auth_token`` Setting override → env var.

    Returns "" when no secret is configured (auth disabled). Never raises — a
    DB error degrades to the env var so a transient DB problem can't crash the
    request path or silently lock out a deploy-time-configured secret.
    """
    override: str | None = None
    try:
        async with sessionmaker() as session:
            override = await repo.get_setting(session, API_TOKEN_SETTING)
    except Exception as e:  # noqa: BLE001 — DB surface; degrade to env
        log.warning("auth: could not read %s setting: %s", API_TOKEN_SETTING, e)
    if override and override.strip():
        return override.strip()
    return config.api_auth_token


def _presented_secret(request: Request) -> str | None:
    """Pull the shared secret out of the Authorization header (Bearer/Basic)."""
    header = request.headers.get("authorization", "")
    if not header:
        return None
    scheme, _, value = header.partition(" ")
    scheme = scheme.strip().lower()
    value = value.strip()
    if scheme == "bearer":
        return value or None
    if scheme == "basic":
        try:
            decoded = base64.b64decode(value).decode("utf-8", "replace")
        except (binascii.Error, ValueError):
            return None
        # The shared secret is the password component; the username is ignored.
        _, _, password = decoded.partition(":")
        return password or None
    return None


async def require_inbound_auth(request: Request) -> None:
    """FastAPI dependency: enforce the shared secret on a write surface.

    No-op when no secret is configured (auth disabled). Otherwise requires a
    Bearer/Basic credential matching the active token, compared in constant
    time; mismatch/absence → 401 with a ``WWW-Authenticate`` challenge.
    """
    expected = await resolve_api_token(request.app.state.sessionmaker)
    if not expected:
        return  # auth disabled — startup logged the warning
    presented = _presented_secret(request)
    if presented is None or not hmac.compare_digest(presented, expected):
        raise HTTPException(
            status_code=401,
            detail="missing or invalid API credentials",
            headers=_UNAUTHORIZED_HEADERS,
        )


def log_auth_status(service_name: str) -> None:
    """Log the inbound-auth posture once at startup.

    Only the env var is checked here (the deploy-time default); a Setting-table
    override can still enable auth at runtime, but the loud 'DISABLED' warning
    is about what the operator shipped with."""
    if config.api_auth_token:
        log.info("%s: inbound auth ENABLED (LLOPSTER_API_TOKEN set)", service_name)
    else:
        log.warning(
            "%s: inbound auth DISABLED — no LLOPSTER_API_TOKEN set. The /webhook, "
            "trigger, and settings/license surfaces are UNAUTHENTICATED. Set "
            "LLOPSTER_API_TOKEN (or an api_auth_token setting) before exposing "
            "this beyond a local/trusted network.",
            service_name,
        )

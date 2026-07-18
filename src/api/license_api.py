"""License status endpoint owned by the agent.

The agent is the source of truth for the active license, exactly as it is for
the Slack/GitHub/Anthropic credentials (see ``integrations_api.py``): the
dashboard pod carries no secrets and proxies here to render the license card.

Two precedence sources feed the active license (see ``src/agent/license.py``):
the ``license_key`` Setting (UI-pasted, no redeploy) overrides the
``LLOPSTER_LICENSE_KEY`` env var. ``refresh_active_license`` reads the Setting
and applies the override; it runs at startup and on every status fetch, so a
key pasted in the dashboard takes effect on the agent without a restart.

Never returns the raw JWT — only the decoded, safe-to-display fields.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from src.agent import license as license_mod
from src.db import repository as repo

log = logging.getLogger("llopster.license_api")

router = APIRouter(prefix="/api/license", tags=["license"])

LICENSE_KEY_SETTING = "license_key"


async def refresh_active_license(sessionmaker) -> None:
    """Apply the ``license_key`` Setting override (DB → in-memory).

    A non-empty Setting becomes the active override; an empty/absent Setting
    clears the override so the env var (or Community fallback) takes over.
    Never raises — a DB hiccup leaves the current active license in place.
    """
    try:
        async with sessionmaker() as session:
            raw = await repo.get_setting(session, LICENSE_KEY_SETTING)
    except Exception as e:  # noqa: BLE001 — DB surface; degrade, don't crash startup
        log.warning("license: could not read %s setting: %s", LICENSE_KEY_SETTING, e)
        return
    license_mod.set_active_license_key(raw if (raw and raw.strip()) else None)


def serialize_license(lic: license_mod.License) -> dict:
    """Decoded, safe-to-display license fields. Never includes the raw JWT."""
    return {
        "tier": lic.tier,
        "valid": lic.valid,
        "source": lic.source,  # embedded | license | invalid
        "features": sorted(lic.features),
        "license_id": lic.license_id,
        "expires_at": lic.expires_at.isoformat() if lic.expires_at else None,
        "clusters": lic.clusters,
        "reason": lic.reason,
    }


@router.get("/status")
async def license_status(request: Request) -> dict:
    """Decoded status of the active license (after applying the DB override)."""
    await refresh_active_license(request.app.state.sessionmaker)
    return serialize_license(license_mod.get_active_license())

"""Entitlement seam for premium packs.

A *pack* is a bundle of tuned prompts (and operator-applied alert rules)
that ships **outside** this source-available repo. The engine loads pack
content at runtime (see ``src/agent/packs.py``) but only *applies* a pack's
prompt overlay when the deployment is entitled to it. This module is the
single gate that decides "is this customer allowed to use pack X?".

Resolution order
----------------
``is_pack_enabled`` now routes through the license framework (ROADMAP #8,
``src/agent/license.py``): a signed JWT whose ``features`` claim contains
``pack:<id>`` entitles that pack. The interim ``LLOPSTER_PACK_TOKEN`` env
allow-list is **retained as a fallback** for deployments that carry a pack
token but no full license key yet (and for the pack tests). Precedence:

1. License feature ``pack:<id>`` granted → entitled.
2. Otherwise, ``<id>`` present in the ``LLOPSTER_PACK_TOKEN`` allow-list →
   entitled.
3. Otherwise → not entitled → the pack falls back to the baked-in Community
   prompt.

No license and no token → no entitlements (Community behavior). This mirrors
LLopster's graceful-degradation contract: a missing credential disables a
feature, it never crashes.

    # Full license (preferred):
    LLOPSTER_LICENSE_KEY=<signed JWT with features=["pack:jvm-pack", ...]>
    # Interim allow-list (fallback):
    LLOPSTER_PACK_TOKEN="jvm-pack,postgres-pack"

The call sites in ``packs.py`` / ``prompts.py`` are unchanged — this function's
signature is the stable seam.
"""

from __future__ import annotations

import logging
import os

from src.agent import license as license_mod

log = logging.getLogger("llopster.entitlements")

# Env var carrying the (interim) entitlement token. In the chart this is
# mounted from a Secret; absent in every Community deployment.
PACK_TOKEN_ENV = "LLOPSTER_PACK_TOKEN"


def _entitled_pack_ids() -> set[str]:
    """Parse the interim allow-list token into a set of pack ids.

    Empty / unset token → empty set → no entitlements (Community behavior).
    """
    raw = os.getenv(PACK_TOKEN_ENV, "").strip()
    if not raw:
        return set()
    return {part.strip() for part in raw.split(",") if part.strip()}


def is_pack_enabled(pack_id: str) -> bool:
    """Return True iff this deployment is entitled to apply ``pack_id``.

    Checks the license first (``pack:<id>`` feature), then the interim env
    allow-list. Fail-closed on entitlement (no license, no token → False) but
    the *caller* fails *open* to the Community prompt — an unentitled pack is
    simply ignored, never an error.
    """
    if not pack_id:
        return False
    if license_mod.is_feature_enabled(f"pack:{pack_id}"):
        return True
    return pack_id in _entitled_pack_ids()

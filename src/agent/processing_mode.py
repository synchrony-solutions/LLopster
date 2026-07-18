"""Global processing-mode kill switch.

The agent's webhook can operate in one of two modes:

  * **autopilot** (default) — every incoming alert is dispatched to the
    full Loki + Prometheus + LLM pipeline immediately. This is the
    historical behavior.

  * **manual** — incoming alerts are still persisted as Run rows for
    visibility, but no LLM work is dispatched. Status is parked at
    ``queued`` until an operator explicitly processes the run from the
    dashboard. Designed as a panic-button when the agent is bleeding
    tokens and the operator needs to inspect what's flowing in before
    spending more.

The mode is a single row in the ``settings`` table so it can be flipped
at runtime without redeploying.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.db import repository as repo

SETTING_KEY = "processing_mode"

AUTOPILOT = "autopilot"
MANUAL = "manual"
VALID_MODES = frozenset({AUTOPILOT, MANUAL})

DEFAULT_MODE = AUTOPILOT


async def get_processing_mode(session: AsyncSession) -> str:
    """Return the currently configured mode, falling back to default.

    An unrecognized stored value also falls back to ``AUTOPILOT`` — we'd
    rather process the alert than silently drop work on a typo.
    """
    stored = await repo.get_setting(session, SETTING_KEY)
    if stored in VALID_MODES:
        return stored
    return DEFAULT_MODE


async def set_processing_mode(session: AsyncSession, mode: str) -> None:
    if mode not in VALID_MODES:
        raise ValueError(
            f"processing_mode must be one of {sorted(VALID_MODES)}; got {mode!r}"
        )
    await repo.set_setting(session, SETTING_KEY, mode)

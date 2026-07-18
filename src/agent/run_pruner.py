"""Periodic background task that deletes Run rows older than a retention
window. Runs as a long-lived asyncio task inside the FastAPI lifespan,
mirroring the `pr_poller` pattern.

Configuration:
  - `RUN_RETENTION_DAYS` env var (default 90; 0 disables pruning)
  - `RUN_PRUNE_INTERVAL_SECONDS` env var (default 3600 = 1 hour)
  - Both are runtime-overridable via the `run_retention_days` and
    `run_prune_interval_seconds` keys in the settings table.

The pruner records `last_pruned_at` on every iteration so the dashboard
can show "last sweep: 12 minutes ago" — visibility matters for an
operation that silently drops data.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from src.config import config
from src.db import repository as repo

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker, AsyncSession

log = logging.getLogger("llopster.pruner")


async def _read_retention_days(sessionmaker: "async_sessionmaker[AsyncSession]") -> int:
    """Settings-table override wins; env var is the fallback."""
    async with sessionmaker() as session:
        raw = await repo.get_setting(session, "run_retention_days")
    if raw is not None:
        try:
            return int(raw)
        except ValueError:
            log.warning(
                "invalid run_retention_days setting %r; falling back to env (%d)",
                raw, config.run_retention_days,
            )
    return config.run_retention_days


async def prune_once(sessionmaker: "async_sessionmaker[AsyncSession]") -> tuple[int, int]:
    """Run a single prune iteration.

    Returns `(retention_days, deleted_count)` for the caller to log.
    Always records `last_pruned_at` on the settings table (even when
    nothing was deleted) so the dashboard can show a fresh timestamp.
    Returns `(0, 0)` when retention is disabled.
    """
    retention_days = await _read_retention_days(sessionmaker)
    deleted = 0
    if retention_days > 0:
        async with sessionmaker() as session:
            deleted = await repo.delete_runs_older_than(session, retention_days)
    async with sessionmaker() as session:
        await repo.set_setting(
            session, "last_pruned_at", datetime.now(timezone.utc).isoformat(),
        )
    return retention_days, deleted


async def run_pruner(
    sessionmaker: "async_sessionmaker[AsyncSession]",
    *,
    interval_seconds: int = 3600,
) -> None:
    """Loop forever. Prune on each tick, record outcome, sleep, repeat.

    Exceptions in a single iteration are logged but never escape — the
    loop must keep running so transient DB blips don't permanently halt
    pruning.
    """
    log.info("run pruner started (interval=%ds)", interval_seconds)
    while True:
        try:
            retention_days, deleted = await prune_once(sessionmaker)
            if retention_days <= 0:
                log.debug("retention disabled (run_retention_days=%d); skipping", retention_days)
            elif deleted:
                log.info("pruned %d run(s) older than %d days", deleted, retention_days)
            else:
                log.debug("no runs older than %d days; nothing to prune", retention_days)
        except asyncio.CancelledError:
            log.info("run pruner stopping")
            return
        except Exception:
            log.exception("prune iteration failed; will retry next tick")
        try:
            await asyncio.sleep(interval_seconds)
        except asyncio.CancelledError:
            log.info("run pruner stopping during sleep")
            return

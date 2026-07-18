"""Background task that polls GitHub for PR status updates.

Runs as a long-lived asyncio task inside the FastAPI lifespan. Every
`POLL_INTERVAL_SECONDS` it loads all runs that have an open PR and queries
the GitHub API to check whether each PR has been closed or merged, updating
the `pr_status` column in the DB so the dashboard can display it.

The poller shuts down cleanly when the task is cancelled (lifespan teardown).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from src.integrations.github_client import GitHubClient
    from src.services_registry import ServiceRegistry

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 60


async def pr_poller(
    sessionmaker: "async_sessionmaker",
    github: "GitHubClient",
    services: "ServiceRegistry",
) -> None:
    """Infinite loop — poll open PRs and update pr_status in the DB."""
    from src.db import repository as repo

    logger.info("PR poller started (interval=%ds)", POLL_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(POLL_INTERVAL_SECONDS)
            async with sessionmaker() as session:
                runs = await repo.list_open_prs(session)
            for run in runs:
                # Determine which GitHub repo to query
                svc_cfg = services.get(run.service) if run.service else None
                gh_repo = svc_cfg.github_repo if svc_cfg else None
                if not gh_repo or run.pr_number is None:
                    continue
                try:
                    status = await github.get_pr_status(gh_repo, run.pr_number)
                    if status != (run.pr_status or "open"):
                        async with sessionmaker() as session:
                            await repo.update_pr_status(session, run.id, status)
                        logger.info(
                            "PR #%s for run %s → %s", run.pr_number, run.id, status
                        )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "Failed to check PR #%s for run %s: %s",
                        run.pr_number, run.id, exc,
                    )
        except asyncio.CancelledError:
            logger.info("PR poller stopping")
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("PR poller unexpected error: %s", exc)
            await asyncio.sleep(10)

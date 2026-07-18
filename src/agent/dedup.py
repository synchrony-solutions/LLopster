"""Duplicate-alert suppression.

When an alert fires repeatedly while a PR for it is still open, every
re-firing would otherwise trigger another full Loki + Prometheus + LLM
pipeline. That cost-bombed the first live cluster run, so this module
short-circuits the pipeline when there's already a Run with an open PR for
the same alert fingerprint.

Dedup key shape: ``f"{alertname}:{service}:{severity}"`` lowercased. We
deliberately do NOT include pod / instance labels — operators want one
investigation per alertname, not one per pod.

Suppression window: until the blocking PR closes or merges (no time cap).
Once the PR is closed/merged, the post-deploy re-evaluation path will
allow the next firing through with prior-attempt context attached.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.alert_handler import ParsedAlert
from src.db.models import Run


# Default grace window between a PR merging and the deploy actually rolling
# out (ArgoCD/Flux sync, image pull, pod restart). Within this window the
# alert is expected to keep firing and we suppress to avoid re-investigating
# something that's already on its way to being fixed.
DEFAULT_POST_MERGE_GRACE_MINUTES = 30


def compute_dedup_key(alert: ParsedAlert) -> str:
    """Stable identifier for "the same alert" across firings.

    Lowercased to defend against AlertManager / Prometheus label-case
    drift between configurations.
    """
    alertname = (alert.alertname or "unknown").strip().lower()
    service = (alert.service or "unknown").strip().lower()
    severity = (alert.severity or "unknown").strip().lower()
    return f"{alertname}:{service}:{severity}"


# Statuses that mean "this alert is already being worked on (or waiting to be)".
# A new webhook for the same dedup_key while one of these is open should not
# create another Run — that's how 2,419 queued rows accumulate in manual mode.
_OPEN_STATUSES = ("queued", "pending", "collecting", "generating", "posting")


async def find_open_run_by_dedup_key(
    session: AsyncSession,
    dedup_key: str,
) -> Run | None:
    """Most recent non-terminal Run with this dedup_key, if any.

    Used by the webhook to drop duplicate AlertManager firings BEFORE
    inserting a row — both in manual mode (where it would otherwise create
    a fresh `queued` row per firing) and in autopilot (where it prevents
    races between concurrent webhook deliveries of the same alert).
    """
    stmt = (
        select(Run)
        .where(Run.dedup_key == dedup_key)
        .where(Run.processing_status.in_(_OPEN_STATUSES))
        .order_by(desc(Run.created_at))
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalars().first()


async def find_blocking_open_pr_run(
    session: AsyncSession,
    dedup_key: str,
    *,
    exclude_run_id: str | None = None,
) -> Run | None:
    """Return the most recent Run with the same dedup_key that has an open PR.

    "Open" means ``pr_opened=True`` and ``pr_status`` is neither ``closed``
    nor ``merged`` (NULL pr_status is treated as still-open — the PR poller
    backfills it on the next tick).

    ``exclude_run_id`` lets the caller skip the Run it's about to process so
    self-matches don't happen if a Run row is created before this check
    runs.
    """
    stmt = (
        select(Run)
        .where(Run.dedup_key == dedup_key)
        .where(Run.pr_opened.is_(True))
        .where(Run.pr_url.isnot(None))
        .where((Run.pr_status.is_(None)) | (Run.pr_status.not_in(["closed", "merged"])))
        .order_by(desc(Run.created_at))
        .limit(1)
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    result = await session.execute(stmt)
    return result.scalars().first()


# Terminal statuses that mean "we already spent the expensive pipeline on this
# alert and it did NOT open a PR": a `done` run that fell below the confidence
# threshold or produced no actionable patch, and a `failed` run that spent Opus
# before erroring (e.g. a deterministically-failing patch-apply/validation).
# Re-running the full Haiku→Sonnet→Opus pipeline on the very next firing is the
# uncapped cost loop blocker 8 describes. We deliberately do NOT anchor on
# `skipped` — a prior backoff/triage skip is cheap and, if it counted, the
# backoff window would roll forward on every firing and suppress forever.
_UNPRODUCTIVE_STATUSES = ("done", "failed")


async def find_recent_unproductive_run(
    session: AsyncSession,
    dedup_key: str,
    *,
    since: datetime,
    exclude_run_id: str | None = None,
) -> Run | None:
    """Most recent Run for this dedup_key that finished WITHOUT opening a PR at
    or after `since`.

    Anchors the post-firing backoff. A below-threshold / no-patch alert never
    opens a PR, so ``find_blocking_open_pr_run`` never matches it; without this,
    every re-firing re-runs the whole pipeline at full cost. Anchoring only on
    real pipeline runs (``done``/``failed``) means the window is measured from
    the last actual investigation — one re-investigation is allowed per window
    rather than the window rolling forward on each firing.
    """
    stmt = (
        select(Run)
        .where(Run.dedup_key == dedup_key)
        .where(Run.pr_opened.is_(False))
        .where(Run.processing_status.in_(_UNPRODUCTIVE_STATUSES))
        .where(Run.created_at >= since)
        .order_by(desc(Run.created_at))
        .limit(1)
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    result = await session.execute(stmt)
    return result.scalars().first()


@dataclass
class PreviousAttempt:
    """Snapshot of an earlier merged fix for the same alert.

    Passed into the patch generator when the same alert fires again after
    the previous fix has been merged AND the deploy grace window has
    elapsed — i.e. the fix is live and didn't work. Letting the model see
    its own prior diagnosis + diff prevents it from re-proposing the same
    losing patch.
    """

    run_id: str
    pr_url: str
    pr_merged_at: datetime
    parsed_root_cause: str | None
    parsed_diff: str | None


def previous_attempt_from_run(run: Run) -> PreviousAttempt:
    return PreviousAttempt(
        run_id=run.id,
        pr_url=run.pr_url or "",
        pr_merged_at=run.pr_merged_at,
        parsed_root_cause=run.parsed_root_cause,
        parsed_diff=run.parsed_diff,
    )


async def find_previous_merged_attempt(
    session: AsyncSession,
    dedup_key: str,
    *,
    exclude_run_id: str | None = None,
) -> Run | None:
    """Most recent Run with the same dedup_key whose PR was merged.

    Returns the Run regardless of how long ago the merge happened — the
    caller decides whether to treat it as "within grace, suppress" or "past
    grace, attach as previous_attempt context".
    """
    stmt = (
        select(Run)
        .where(Run.dedup_key == dedup_key)
        .where(Run.pr_status == "merged")
        .where(Run.pr_merged_at.isnot(None))
        .order_by(desc(Run.pr_merged_at))
        .limit(1)
    )
    if exclude_run_id is not None:
        stmt = stmt.where(Run.id != exclude_run_id)
    result = await session.execute(stmt)
    return result.scalars().first()


def is_within_grace_window(
    merged_at: datetime,
    grace_minutes: int,
    *,
    now: datetime | None = None,
) -> bool:
    """True if `merged_at + grace_minutes` is still in the future.

    `now` is injectable for deterministic tests.
    """
    current = now or datetime.now(timezone.utc)
    # Defend against naive datetimes coming back from SQLite (it strips
    # tzinfo on round-trip). Treat them as UTC, since that's what we wrote.
    if merged_at.tzinfo is None:
        merged_at = merged_at.replace(tzinfo=timezone.utc)
    return current < merged_at + timedelta(minutes=grace_minutes)

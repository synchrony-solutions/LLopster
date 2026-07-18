"""Tests for run-history retention pruning.

Covers both the repository helper (`delete_runs_older_than`) and the
pruner background task (`prune_once` + `run_pruner`). The background-loop
test runs at sub-second intervals so we can observe two iterations
quickly without slowing the suite.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.agent.run_pruner import _read_retention_days, prune_once, run_pruner
from src.db import repository as repo
from src.db.models import Base, Run


def _alert(name: str = "TestAlert") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="f",
        status="firing",
        alertname=name,
        severity="warning",
        service="demo-app",
        summary="x",
        description="y",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": "demo-app"},
        annotations={},
        generator_url="",
    )


@pytest.fixture
async def sessionmaker_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


async def _backdate_run(session, run_id: str, days_old: int) -> None:
    """Force a Run's created_at into the past so the pruner sees it as old."""
    run = await session.get(Run, run_id)
    run.created_at = datetime.now(timezone.utc) - timedelta(days=days_old)
    await session.commit()


# ---------------------------------------------------------------------------
# Repo: delete_runs_older_than
# ---------------------------------------------------------------------------

async def test_delete_runs_older_than_drops_only_old(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        old = await repo.create_run_from_alert(s, _alert("Old"), raw_payload={})
        new = await repo.create_run_from_alert(s, _alert("New"), raw_payload={})
        await _backdate_run(s, old.id, days_old=100)

    async with sm() as s:
        deleted = await repo.delete_runs_older_than(s, days=90)
    assert deleted == 1

    async with sm() as s:
        remaining = await repo.list_runs(s)
    assert len(remaining) == 1
    assert remaining[0].alertname == "New"


async def test_delete_runs_older_than_returns_zero_when_nothing_old(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        await repo.create_run_from_alert(s, _alert(), raw_payload={})

    async with sm() as s:
        deleted = await repo.delete_runs_older_than(s, days=90)
    assert deleted == 0


async def test_delete_runs_older_than_handles_empty_table(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        deleted = await repo.delete_runs_older_than(s, days=90)
    assert deleted == 0


async def test_delete_runs_older_than_uses_inclusive_cutoff(sessionmaker_fixture):
    """Boundary: a run aged exactly N days should still be deleted."""
    sm = sessionmaker_fixture
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        # 90 days + 1 second old — definitely older than the 90-day cutoff
        run_obj = await s.get(Run, run.id)
        run_obj.created_at = datetime.now(timezone.utc) - timedelta(days=90, seconds=1)
        await s.commit()

    async with sm() as s:
        deleted = await repo.delete_runs_older_than(s, days=90)
    assert deleted == 1


# ---------------------------------------------------------------------------
# Pruner: read_retention_days
# ---------------------------------------------------------------------------

async def test_read_retention_days_falls_back_to_env(sessionmaker_fixture):
    sm = sessionmaker_fixture
    with patch("src.agent.run_pruner.config") as mock_config:
        mock_config.run_retention_days = 30
        days = await _read_retention_days(sm)
    assert days == 30


async def test_read_retention_days_setting_overrides_env(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        await repo.set_setting(s, "run_retention_days", "7")
    with patch("src.agent.run_pruner.config") as mock_config:
        mock_config.run_retention_days = 90
        days = await _read_retention_days(sm)
    assert days == 7


async def test_read_retention_days_invalid_setting_falls_back(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        await repo.set_setting(s, "run_retention_days", "garbage")
    with patch("src.agent.run_pruner.config") as mock_config:
        mock_config.run_retention_days = 90
        days = await _read_retention_days(sm)
    assert days == 90


# ---------------------------------------------------------------------------
# Pruner: prune_once
# ---------------------------------------------------------------------------

async def test_prune_once_deletes_old_and_records_timestamp(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        old = await repo.create_run_from_alert(s, _alert("Old"), raw_payload={})
        await repo.create_run_from_alert(s, _alert("New"), raw_payload={})
        await _backdate_run(s, old.id, days_old=100)

    with patch("src.agent.run_pruner.config") as mock_config:
        mock_config.run_retention_days = 90
        retention, deleted = await prune_once(sm)

    assert retention == 90
    assert deleted == 1

    # last_pruned_at should be set to a recent ISO timestamp.
    async with sm() as s:
        ts = await repo.get_setting(s, "last_pruned_at")
    assert ts is not None
    parsed = datetime.fromisoformat(ts)
    age = datetime.now(timezone.utc) - parsed
    assert age < timedelta(seconds=5)


async def test_prune_once_disabled_skips_delete_but_still_stamps(sessionmaker_fixture):
    """retention_days <= 0 means "keep forever" — no DELETE issued, but the
    last_pruned_at timestamp still advances so the dashboard doesn't show
    a stale "last pruned 3 weeks ago" forever."""
    sm = sessionmaker_fixture
    async with sm() as s:
        old = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await _backdate_run(s, old.id, days_old=100)

    with patch("src.agent.run_pruner.config") as mock_config:
        mock_config.run_retention_days = 0
        retention, deleted = await prune_once(sm)

    assert retention == 0
    assert deleted == 0
    # The 100-day-old row must still exist
    async with sm() as s:
        remaining = await repo.list_runs(s)
    assert len(remaining) == 1
    # But the timestamp should still update
    async with sm() as s:
        assert await repo.get_setting(s, "last_pruned_at") is not None


async def test_prune_once_records_timestamp_even_when_nothing_to_delete(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        await repo.create_run_from_alert(s, _alert(), raw_payload={})  # fresh

    with patch("src.agent.run_pruner.config") as mock_config:
        mock_config.run_retention_days = 90
        _, deleted = await prune_once(sm)

    assert deleted == 0
    async with sm() as s:
        assert await repo.get_setting(s, "last_pruned_at") is not None


# ---------------------------------------------------------------------------
# Pruner: run_pruner loop
# ---------------------------------------------------------------------------

async def test_run_pruner_iterates_and_cancels_cleanly(sessionmaker_fixture):
    """Run the loop with a tiny interval, verify it ticks at least once and
    then cancels cleanly."""
    sm = sessionmaker_fixture
    async with sm() as s:
        old = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await _backdate_run(s, old.id, days_old=100)

    with patch("src.agent.run_pruner.config") as mock_config:
        mock_config.run_retention_days = 90
        task = asyncio.create_task(run_pruner(sm, interval_seconds=10))
        # Give it just enough time to do one iteration before sleeping
        await asyncio.sleep(0.1)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    # The old row should be gone after the first iteration
    async with sm() as s:
        assert len(await repo.list_runs(s)) == 0


async def test_run_pruner_survives_iteration_exceptions(sessionmaker_fixture):
    """If prune_once raises, the loop must log and continue, not die."""
    sm = sessionmaker_fixture

    call_count = 0

    async def boom(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated DB hiccup")
        return (90, 0)

    with patch("src.agent.run_pruner.prune_once", side_effect=boom):
        task = asyncio.create_task(run_pruner(sm, interval_seconds=0))
        # Wait long enough for the failing iteration AND a recovery iteration
        await asyncio.sleep(0.2)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    assert call_count >= 2  # the loop kept going after the exception

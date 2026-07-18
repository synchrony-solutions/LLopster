"""Tests for the automatic cost circuit breaker.

Covers trip / no-trip / error for both the runs/hour and USD/day ceilings,
the settings-override, and the disabled-by-default behavior. No live API calls.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent import cost_breaker as cb
from src.agent.alert_handler import ParsedAlert
from src.agent.cost_breaker import check_cost_breaker
from src.db import repository as repo
from src.db.models import Base, Run


@pytest.fixture
async def sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


def _alert(name: str = "A", service: str = "demo-app") -> ParsedAlert:
    return ParsedAlert(
        fingerprint=f"fp-{name}",
        status="firing",
        alertname=name,
        severity="warning",
        service=service,
        summary="s",
        description="d",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": service},
        annotations={},
        generator_url="",
    )


async def _make_runs(sm, n: int) -> None:
    async with sm() as s:
        for i in range(n):
            await repo.create_run_from_alert(s, _alert(name=f"A{i}"), raw_payload={})


def _no_ceilings():
    return SimpleNamespace(max_runs_per_hour=0, max_usd_per_day=0.0)


# ---------------------------------------------------------------------------
# Shipped defaults are a non-zero safety net (blocker 8)
# ---------------------------------------------------------------------------

def test_shipped_defaults_are_non_zero():
    """Out-of-the-box there IS a spend cap — the origin incident was an uncapped
    runaway. A regression that zeroes these silently removes the safety net."""
    from src.config import config as real_config
    assert real_config.max_runs_per_hour > 0
    assert real_config.max_usd_per_day > 0


def test_log_cost_breaker_status_warns_on_defaults(caplog):
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="llopster.cost_breaker"):
        cb.log_cost_breaker_status("agent")
    assert any("cost breaker" in r.message.lower() for r in caplog.records)


def test_log_cost_breaker_status_warns_loudly_when_disabled(caplog, monkeypatch):
    import logging as _logging
    monkeypatch.setattr(cb, "config", _no_ceilings())
    with caplog.at_level(_logging.WARNING, logger="llopster.cost_breaker"):
        cb.log_cost_breaker_status("agent")
    assert any("DISABLED" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# 0 = disabled mechanism
# ---------------------------------------------------------------------------

async def test_disabled_by_default_never_trips(sm, monkeypatch):
    monkeypatch.setattr(cb, "config", _no_ceilings())
    await _make_runs(sm, 50)
    decision = await check_cost_breaker(sm)
    assert decision.tripped is False


# ---------------------------------------------------------------------------
# runs/hour ceiling
# ---------------------------------------------------------------------------

async def test_runs_per_hour_trips_when_at_ceiling(sm, monkeypatch):
    monkeypatch.setattr(cb, "config", SimpleNamespace(max_runs_per_hour=5, max_usd_per_day=0.0))
    await _make_runs(sm, 5)
    decision = await check_cost_breaker(sm)
    assert decision.tripped is True
    assert "runs in the last hour" in decision.reason


async def test_runs_per_hour_no_trip_below_ceiling(sm, monkeypatch):
    monkeypatch.setattr(cb, "config", SimpleNamespace(max_runs_per_hour=10, max_usd_per_day=0.0))
    await _make_runs(sm, 4)
    decision = await check_cost_breaker(sm)
    assert decision.tripped is False


async def test_runs_per_hour_setting_overrides_env(sm, monkeypatch):
    # Env would not trip (high ceiling); the DB setting lowers it so it does.
    monkeypatch.setattr(cb, "config", SimpleNamespace(max_runs_per_hour=999, max_usd_per_day=0.0))
    await _make_runs(sm, 3)
    async with sm() as s:
        await repo.set_setting(s, "max_runs_per_hour", "3")
    decision = await check_cost_breaker(sm)
    assert decision.tripped is True


# ---------------------------------------------------------------------------
# USD/day ceiling
# ---------------------------------------------------------------------------

async def _make_costly_run(sm, *, output_tokens: int) -> None:
    """Insert a run with Opus synthesis tokens that cost real money."""
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(name="costly"), raw_payload={})
        db_run = await s.get(Run, run.id)
        db_run.model = "claude-opus-4-7"
        db_run.input_tokens = 1000
        db_run.output_tokens = output_tokens
        db_run.cache_read_tokens = 0
        db_run.cache_creation_tokens = 0
        await s.commit()


async def test_usd_per_day_trips(sm, monkeypatch):
    monkeypatch.setattr(cb, "config", SimpleNamespace(max_runs_per_hour=0, max_usd_per_day=0.05))
    # Opus output is $75/Mtok → 1M output tokens ≈ $75, well over $0.05.
    await _make_costly_run(sm, output_tokens=1_000_000)
    decision = await check_cost_breaker(sm)
    assert decision.tripped is True
    assert "synthesis spend" in decision.reason


async def test_usd_per_day_no_trip_below_ceiling(sm, monkeypatch):
    monkeypatch.setattr(cb, "config", SimpleNamespace(max_runs_per_hour=0, max_usd_per_day=1000.0))
    await _make_costly_run(sm, output_tokens=1000)
    decision = await check_cost_breaker(sm)
    assert decision.tripped is False


# ---------------------------------------------------------------------------
# Fail-safe: a broken breaker never trips
# ---------------------------------------------------------------------------

async def test_breaker_error_does_not_trip(monkeypatch):
    monkeypatch.setattr(cb, "config", SimpleNamespace(max_runs_per_hour=1, max_usd_per_day=0.0))

    class _BoomSessionmaker:
        def __call__(self):
            raise RuntimeError("db is down")

    decision = await check_cost_breaker(_BoomSessionmaker())
    assert decision.tripped is False

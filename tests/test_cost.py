"""Tests for token → dollar conversion.

The dashboard's spend telemetry is only useful if the math is right —
under-reporting would defeat the kill-switch's whole point of letting
operators see what they're spending.
"""

import pytest
from datetime import datetime, timezone

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.agent.cost import (
    DEFAULT_PRICING,
    PRICING,
    cache_savings_usd,
    compute_cost_usd,
    pricing_for,
)
from src.db import repository as repo
from src.db.models import Base


# ---------------------------------------------------------------------------
# Pricing lookup
# ---------------------------------------------------------------------------

def test_pricing_lookup_known_model():
    p = pricing_for("claude-opus-4-7")
    assert p.input_per_mtok == 15.0
    assert p.output_per_mtok == 75.0


def test_pricing_lookup_normalises_case_and_whitespace():
    p = pricing_for("  CLAUDE-OPUS-4-7  ")
    assert p == PRICING["claude-opus-4-7"]


def test_pricing_lookup_unknown_falls_back_to_opus():
    """Unknown models fall back to the most expensive tier so we never
    silently under-report spend. Catches the "new model ships, dashboard
    quietly reports $0 for it" failure mode."""
    assert pricing_for("claude-vapor-99") == DEFAULT_PRICING
    assert pricing_for("") == DEFAULT_PRICING
    assert pricing_for(None) == DEFAULT_PRICING


# ---------------------------------------------------------------------------
# compute_cost_usd
# ---------------------------------------------------------------------------

def test_cost_zero_when_all_tokens_zero():
    """Runs that short-circuited (filtered / deduped / queued) leave the
    token columns NULL. compute_cost_usd treats those as 0 — important
    for the stats page's avg-cost-per-run calculation."""
    assert compute_cost_usd(
        input_tokens=None, output_tokens=None,
        cache_read_tokens=None, cache_creation_tokens=None,
        model="claude-opus-4-7",
    ) == 0.0


def test_cost_opus_million_input_tokens():
    """Sanity check the math: 1M input tokens on Opus = $15.00."""
    assert compute_cost_usd(
        input_tokens=1_000_000, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0,
        model="claude-opus-4-7",
    ) == pytest.approx(15.00)


def test_cost_opus_million_output_tokens():
    assert compute_cost_usd(
        input_tokens=0, output_tokens=1_000_000,
        cache_read_tokens=0, cache_creation_tokens=0,
        model="claude-opus-4-7",
    ) == pytest.approx(75.00)


def test_cost_opus_million_cache_read_tokens():
    assert compute_cost_usd(
        input_tokens=0, output_tokens=0,
        cache_read_tokens=1_000_000, cache_creation_tokens=0,
        model="claude-opus-4-7",
    ) == pytest.approx(1.50)


def test_cost_realistic_run_breakdown():
    """A typical run we observed in the cluster: 50k input, 2k output,
    600k cache-read (the cached codebase blob), 0 cache write."""
    cost = compute_cost_usd(
        input_tokens=50_000, output_tokens=2_000,
        cache_read_tokens=600_000, cache_creation_tokens=0,
        model="claude-opus-4-7",
    )
    # 0.75 (input) + 0.15 (output) + 0.90 (cache_read) = 1.80
    assert cost == pytest.approx(1.80)


def test_per_stage_costs_sum_to_full_run_total():
    """Phase A introduces a Haiku triage stage alongside the existing
    Opus synthesis stage. The dashboard renders them as separate cards;
    this test pins down that summing the per-stage costs equals the
    total cost — i.e. no rounding drift, no missed token category."""
    triage_cost = compute_cost_usd(
        input_tokens=400, output_tokens=80,
        cache_read_tokens=0, cache_creation_tokens=0,
        model="claude-haiku-4-5",
    )
    synthesis_cost = compute_cost_usd(
        input_tokens=50_000, output_tokens=2_000,
        cache_read_tokens=600_000, cache_creation_tokens=0,
        model="claude-opus-4-7",
    )
    # Haiku: 0.0004 (input) + 0.0004 (output) = 0.0008
    assert triage_cost == pytest.approx(0.0008)
    # Opus synthesis matches the realistic-run breakdown above: 1.80
    assert synthesis_cost == pytest.approx(1.80)
    # The dashboard's "per-run total" is the simple sum.
    assert triage_cost + synthesis_cost == pytest.approx(1.8008)
    # Sanity: the Haiku stage is much cheaper than the Opus stage even
    # though it runs on every alert. That's the Phase A value prop.
    assert triage_cost < synthesis_cost / 1000


def test_cost_unknown_model_uses_opus_pricing():
    cost_unknown = compute_cost_usd(
        input_tokens=1_000_000, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0,
        model="claude-some-future-model",
    )
    cost_opus = compute_cost_usd(
        input_tokens=1_000_000, output_tokens=0,
        cache_read_tokens=0, cache_creation_tokens=0,
        model="claude-opus-4-7",
    )
    assert cost_unknown == cost_opus


# ---------------------------------------------------------------------------
# cache_savings_usd
# ---------------------------------------------------------------------------

def test_cache_savings_is_input_price_minus_cache_read_price():
    """If 1M tokens were cache-reads instead of fresh input, on Opus
    we saved $15 - $1.50 = $13.50."""
    assert cache_savings_usd(
        cache_read_tokens=1_000_000, model="claude-opus-4-7",
    ) == pytest.approx(13.50)


def test_cache_savings_zero_when_no_cache_reads():
    assert cache_savings_usd(
        cache_read_tokens=0, model="claude-opus-4-7",
    ) == 0.0
    assert cache_savings_usd(
        cache_read_tokens=None, model="claude-opus-4-7",
    ) == 0.0


# ---------------------------------------------------------------------------
# daily_token_stats — the SQL aggregation feeding the chart
# ---------------------------------------------------------------------------

@pytest.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _alert(name: str = "X") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="f", status="firing", alertname=name, severity="warning",
        service="demo-app", summary="", description="",
        starts_at=datetime.now(timezone.utc), ends_at=None,
        labels={}, annotations={}, generator_url="",
    )


async def test_daily_token_stats_sums_by_day(db_session):
    """Two runs on the same day should aggregate; another day stays separate."""
    async with db_session() as s:
        r1 = await repo.create_run_from_alert(s, _alert("A"), raw_payload={"alerts": []})
        r2 = await repo.create_run_from_alert(s, _alert("B"), raw_payload={"alerts": []})

        r1_obj = await repo.get_run(s, r1.id)
        r1_obj.input_tokens = 1000
        r1_obj.output_tokens = 200
        r1_obj.cache_read_tokens = 5000
        r1_obj.cache_creation_tokens = 0
        r2_obj = await repo.get_run(s, r2.id)
        r2_obj.input_tokens = 2000
        r2_obj.output_tokens = 400
        r2_obj.cache_read_tokens = 10000
        r2_obj.cache_creation_tokens = 100
        await s.commit()

    async with db_session() as s:
        stats = await repo.daily_token_stats(s, days=30)
    assert len(stats) == 1  # both runs in the same day
    row = stats[0]
    assert row["input_tokens"] == 3000
    assert row["output_tokens"] == 600
    assert row["cache_read_tokens"] == 15000
    assert row["cache_creation_tokens"] == 100
    assert row["run_count"] == 2


async def test_daily_token_stats_excludes_runs_with_no_llm_call(db_session):
    """Filtered / queued / skipped runs leave input_tokens NULL; the avg
    cost per run on the dashboard would be wildly diluted if those were
    counted in the denominator."""
    async with db_session() as s:
        # A "real" LLM run
        real = await repo.create_run_from_alert(s, _alert("real"), raw_payload={"alerts": []})
        real_obj = await repo.get_run(s, real.id)
        real_obj.input_tokens = 1000
        real_obj.output_tokens = 100
        # A skipped run (no LLM call)
        await repo.create_run_from_alert(s, _alert("skip"), raw_payload={"alerts": []})
        await s.commit()

    async with db_session() as s:
        stats = await repo.daily_token_stats(s, days=30)
    assert len(stats) == 1
    assert stats[0]["run_count"] == 1  # the skipped one was excluded
    assert stats[0]["input_tokens"] == 1000


async def test_daily_token_stats_empty_for_no_runs(db_session):
    async with db_session() as s:
        stats = await repo.daily_token_stats(s, days=30)
    assert stats == []

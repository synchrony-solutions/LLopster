"""Tests for the /metrics Prometheus exposition (self-observability)."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.agent.processing_mode import MANUAL, set_processing_mode
from src.api.metrics import render_metrics
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


def _alert(name="HelmValuesMisconfigured") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="fp", status="firing", alertname=name, severity="warning",
        service="demo-app", summary="s", description="d",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc), ends_at=None,
        labels={}, annotations={}, generator_url="",
    )


async def _run(sm, name, status):
    async with sm() as s:
        r = await repo.create_run_from_alert(s, _alert(name), raw_payload={})
        await repo.update_status(s, r.id, status)
    return r


async def test_metrics_empty_db(sm):
    body = await render_metrics(sm)
    assert "llopster_scrape_ok 1" in body
    assert "llopster_backlog 0" in body
    assert "llopster_runs_processed_total 0" in body
    # Content is valid exposition text (HELP/TYPE precede series).
    assert "# TYPE llopster_backlog gauge" in body


async def test_metrics_counts_by_status_and_backlog(sm):
    await _run(sm, "A", "done")
    await _run(sm, "B", "failed")
    await _run(sm, "C", "queued")      # non-terminal → backlog
    await _run(sm, "D", "collecting")  # non-terminal → backlog

    body = await render_metrics(sm)
    assert 'llopster_runs_total{status="done"} 1' in body
    assert 'llopster_runs_total{status="failed"} 1' in body
    assert 'llopster_runs_total{status="queued"} 1' in body
    assert "llopster_runs_processed_total 4" in body
    assert "llopster_backlog 2" in body


async def test_metrics_reports_manual_mode(sm):
    async with sm() as s:
        await set_processing_mode(s, MANUAL)
    body = await render_metrics(sm)
    assert "llopster_processing_mode_manual 1" in body


async def test_metrics_reports_spend_and_ceilings(sm):
    async with sm() as s:
        r = await repo.create_run_from_alert(s, _alert("costly"), raw_payload={})
        db = await s.get(Run, r.id)
        db.model = "claude-opus-4-7"
        db.input_tokens = 1000
        db.output_tokens = 500_000
        await s.commit()

    body = await render_metrics(sm)
    assert "llopster_estimated_spend_usd_last_day" in body
    assert "llopster_max_runs_per_hour" in body
    assert "llopster_max_usd_per_day" in body
    # Non-zero spend recorded for the costly run.
    spend_line = next(
        l for l in body.splitlines()
        if l.startswith("llopster_estimated_spend_usd_last_day ")
    )
    assert float(spend_line.split()[1]) > 0

"""Tests for the manual-mode kill switch.

Covers the processing_mode setting helpers, the webhook's mode-aware
dispatch behavior, and the dispatch endpoint that processes a queued run
in place.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert, parse_alertmanager_payload
from src.agent.processing_mode import (
    AUTOPILOT,
    DEFAULT_MODE,
    MANUAL,
    get_processing_mode,
    set_processing_mode,
)
from src.api.main import alertmanager_webhook
from src.api.trigger_routes import router as trigger_router
from src.db import repository as repo
from src.db.models import Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

class _FakeServices:
    def names(self) -> list[str]:
        return ["demo-app"]

    def get(self, name: str):
        return None


@pytest.fixture
async def db_sessionmaker():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


@pytest.fixture
async def webhook_app(db_sessionmaker):
    """Minimal FastAPI app that exposes just the agent webhook route.

    The webhook reads from app.state; we provide just enough state to run.
    process_alert is patched per-test so we can observe dispatch decisions
    without spinning up the real pipeline.
    """
    app = FastAPI()
    app.post("/webhook")(alertmanager_webhook)
    app.state.sessionmaker = db_sessionmaker
    app.state.services = _FakeServices()
    app.state.collector = MagicMock()
    app.state.patcher = MagicMock()
    app.state.github = MagicMock()
    app.state.slack = MagicMock()
    app.state.background_tasks = set()
    return app


@pytest.fixture
async def trigger_app(db_sessionmaker):
    """Minimal app for the /trigger dispatch endpoint tests."""
    app = FastAPI()
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent.parent / "src" / "api" / "static")),
        name="static",
    )
    app.include_router(trigger_router)
    app.state.sessionmaker = db_sessionmaker
    app.state.services = _FakeServices()
    app.state.collector = MagicMock()
    app.state.patcher = MagicMock()
    app.state.github = MagicMock()
    app.state.slack = MagicMock()
    app.state.background_tasks = set()
    return app


def _alert_payload(alertname: str = "TestAlert", service: str = "demo-app") -> dict:
    return {
        "alerts": [{
            "fingerprint": "fp-1",
            "status": "firing",
            "labels": {"alertname": alertname, "severity": "warning", "service": service},
            "annotations": {"summary": "x", "description": "y"},
            "startsAt": "2026-05-23T10:00:00.000Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "",
        }]
    }


# ---------------------------------------------------------------------------
# Setting helpers
# ---------------------------------------------------------------------------

async def test_get_processing_mode_defaults_to_autopilot(db_sessionmaker):
    async with db_sessionmaker() as s:
        mode = await get_processing_mode(s)
    assert mode == AUTOPILOT
    assert DEFAULT_MODE == AUTOPILOT


async def test_set_processing_mode_roundtrip(db_sessionmaker):
    async with db_sessionmaker() as s:
        await set_processing_mode(s, MANUAL)
        mode = await get_processing_mode(s)
    assert mode == MANUAL


async def test_set_processing_mode_rejects_invalid(db_sessionmaker):
    async with db_sessionmaker() as s:
        with pytest.raises(ValueError):
            await set_processing_mode(s, "rocket-mode")


async def test_get_processing_mode_falls_back_on_garbage(db_sessionmaker):
    """A typo in the DB shouldn't silently drop incoming alerts."""
    async with db_sessionmaker() as s:
        await repo.set_setting(s, "processing_mode", "nonsense")
        mode = await get_processing_mode(s)
    assert mode == AUTOPILOT


# ---------------------------------------------------------------------------
# Webhook dispatch behavior
# ---------------------------------------------------------------------------

async def test_webhook_autopilot_dispatches(webhook_app, db_sessionmaker):
    """Default mode: incoming alerts go straight to process_alert."""
    with patch("src.api.main.process_alert", new_callable=AsyncMock) as mock_proc:
        async with AsyncClient(
            transport=ASGITransport(app=webhook_app), base_url="http://t",
        ) as c:
            r = await c.post("/webhook", json=_alert_payload())

    assert r.status_code == 200
    body = r.json()
    assert body["received"] == 1
    assert body["mode"] == AUTOPILOT
    assert body["alerts"][0]["status"] == "pending"
    mock_proc.assert_called_once()

    async with db_sessionmaker() as s:
        runs = await repo.list_runs(s)
    assert len(runs) == 1
    # Status is whatever process_alert leaves it; since we mocked it out,
    # the row stays at "pending" — the dispatch fact is what we care about.
    assert runs[0].processing_status == "pending"


async def test_webhook_manual_mode_does_not_dispatch(webhook_app, db_sessionmaker):
    """Manual mode: the Run is created and parked at queued; no dispatch."""
    async with db_sessionmaker() as s:
        await set_processing_mode(s, MANUAL)

    with patch("src.api.main.process_alert", new_callable=AsyncMock) as mock_proc:
        async with AsyncClient(
            transport=ASGITransport(app=webhook_app), base_url="http://t",
        ) as c:
            r = await c.post("/webhook", json=_alert_payload("CacheHitRateLow"))

    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == MANUAL
    assert body["alerts"][0]["status"] == "queued"
    mock_proc.assert_not_called()

    async with db_sessionmaker() as s:
        runs = await repo.list_runs(s)
    assert len(runs) == 1
    assert runs[0].processing_status == "queued"
    assert "manual" in (runs[0].error_message or "").lower()


async def test_webhook_manual_mode_dedups_repeat_firings(webhook_app, db_sessionmaker):
    """A second webhook for the same alert while one is queued must NOT
    create a second Run row. This is what stops AlertManager's 5min
    repeat_interval from accumulating thousands of queued rows.
    """
    async with db_sessionmaker() as s:
        await set_processing_mode(s, MANUAL)

    payload = _alert_payload("CacheHitRateLow")

    with patch("src.api.main.process_alert", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=webhook_app), base_url="http://t",
        ) as c:
            r1 = await c.post("/webhook", json=payload)
            r2 = await c.post("/webhook", json=payload)
            r3 = await c.post("/webhook", json=payload)

    for r in (r1, r2, r3):
        assert r.status_code == 200
    assert r2.json()["alerts"][0].get("deduped") is True
    assert r3.json()["alerts"][0].get("deduped") is True

    async with db_sessionmaker() as s:
        runs = await repo.list_runs(s)
    assert len(runs) == 1  # only the first firing made it past dedup


async def test_webhook_autopilot_dedups_inflight_firings(webhook_app, db_sessionmaker):
    """Same protection in autopilot — a webhook arriving while the
    previous run for this dedup_key is still in pending/collecting/etc.
    should be dropped to avoid races and duplicate work.
    """
    payload = _alert_payload("UpstreamTimeoutSpike")

    with patch("src.api.main.process_alert", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=webhook_app), base_url="http://t",
        ) as c:
            r1 = await c.post("/webhook", json=payload)
            r2 = await c.post("/webhook", json=payload)

    assert r1.json()["alerts"][0].get("deduped") is None
    assert r2.json()["alerts"][0].get("deduped") is True

    async with db_sessionmaker() as s:
        runs = await repo.list_runs(s)
    assert len(runs) == 1


async def test_webhook_manual_mode_handles_batch(webhook_app, db_sessionmaker):
    """All alerts in one webhook batch get the same mode treatment."""
    async with db_sessionmaker() as s:
        await set_processing_mode(s, MANUAL)

    payload = _alert_payload("A")
    payload["alerts"].append({
        "fingerprint": "fp-2",
        "status": "firing",
        "labels": {"alertname": "B", "severity": "warning", "service": "demo-app"},
        "annotations": {"summary": "x", "description": "y"},
        "startsAt": "2026-05-23T10:00:00.000Z",
        "endsAt": "0001-01-01T00:00:00Z",
        "generatorURL": "",
    })

    with patch("src.api.main.process_alert", new_callable=AsyncMock) as mock_proc:
        async with AsyncClient(
            transport=ASGITransport(app=webhook_app), base_url="http://t",
        ) as c:
            r = await c.post("/webhook", json=payload)

    assert r.status_code == 200
    assert r.json()["received"] == 2
    mock_proc.assert_not_called()

    async with db_sessionmaker() as s:
        runs = await repo.list_runs(s)
    assert {run.processing_status for run in runs} == {"queued"}


# ---------------------------------------------------------------------------
# Dispatch endpoint
# ---------------------------------------------------------------------------

async def test_dispatch_endpoint_processes_queued_run(trigger_app, db_sessionmaker):
    payload = _alert_payload("HelmValuesMisconfigured")
    alert = parse_alertmanager_payload(payload)[0]

    async with db_sessionmaker() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload=payload)
        await repo.update_status(s, run.id, "queued")

    with patch("src.api.trigger_routes.process_alert", new_callable=AsyncMock) as mock_proc:
        async with AsyncClient(
            transport=ASGITransport(app=trigger_app),
            base_url="http://t",
            follow_redirects=False,
        ) as c:
            r = await c.post(f"/trigger/dispatch/{run.id}")

    assert r.status_code == 303
    assert r.headers["location"] == f"/trigger/{run.id}"
    mock_proc.assert_called_once()
    # Crucially: the existing Run is advanced, not duplicated.
    called_run_id = mock_proc.call_args.kwargs.get("run_id") or mock_proc.call_args.args[0]
    assert called_run_id == run.id

    async with db_sessionmaker() as s:
        runs = await repo.list_runs(s)
        fetched = await repo.get_run(s, run.id)
    assert len(runs) == 1  # no new Run created
    # Status reset to pending so the processor can advance it cleanly.
    assert fetched.processing_status == "pending"
    assert fetched.error_message is None


async def test_dispatch_endpoint_rejects_non_queued_run(trigger_app, db_sessionmaker):
    payload = _alert_payload()
    alert = parse_alertmanager_payload(payload)[0]
    async with db_sessionmaker() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload=payload)
        await repo.update_status(s, run.id, "done")

    async with AsyncClient(transport=ASGITransport(app=trigger_app), base_url="http://t") as c:
        r = await c.post(f"/trigger/dispatch/{run.id}")
    assert r.status_code == 409


async def test_dispatch_endpoint_404_on_missing_run(trigger_app):
    async with AsyncClient(transport=ASGITransport(app=trigger_app), base_url="http://t") as c:
        r = await c.post("/trigger/dispatch/no-such-run")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Trigger page surfaces queued runs + banner
# ---------------------------------------------------------------------------

async def test_trigger_page_shows_queued_runs(trigger_app, db_sessionmaker):
    payload = _alert_payload("UpstreamTimeoutSpike")
    alert = parse_alertmanager_payload(payload)[0]
    async with db_sessionmaker() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload=payload)
        await repo.update_status(s, run.id, "queued")

    async with AsyncClient(transport=ASGITransport(app=trigger_app), base_url="http://t") as c:
        r = await c.get("/trigger")

    assert r.status_code == 200
    assert "Queued runs (1)" in r.text
    assert "UpstreamTimeoutSpike" in r.text
    assert f"/trigger/dispatch/{run.id}" in r.text


async def test_trigger_page_shows_manual_mode_banner(trigger_app, db_sessionmaker):
    async with db_sessionmaker() as s:
        await set_processing_mode(s, MANUAL)

    async with AsyncClient(transport=ASGITransport(app=trigger_app), base_url="http://t") as c:
        r = await c.get("/trigger")

    assert "Manual mode is on" in r.text


async def test_trigger_page_no_banner_in_autopilot(trigger_app):
    async with AsyncClient(transport=ASGITransport(app=trigger_app), base_url="http://t") as c:
        r = await c.get("/trigger")

    assert "Manual mode is on" not in r.text

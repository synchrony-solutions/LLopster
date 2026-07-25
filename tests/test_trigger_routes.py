"""Tests for the manual-trigger HTML routes (Phase C).

Verifies route responses, form submission flows, and HTMX polling behavior.
The process_alert pipeline is mocked — we only test route/DB logic here.
"""

from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.dashboard.runs_api import router as runs_router
from src.dashboard.settings_routes import router as settings_router
from src.api.trigger_routes import router as trigger_router
from src.dashboard.web_routes import router as web_router
from src.db import repository as repo
from src.db.models import Base
from src.services_registry import ServiceRegistry


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

def _alert(name: str = "TestAlert", service: str = "demo-app") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="fp-test",
        status="firing",
        alertname=name,
        severity="warning",
        service=service,
        summary="test summary",
        description="test description",
        starts_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
        ends_at=None,
        labels={"alertname": name, "severity": "warning", "service": service},
        annotations={"summary": "test summary", "description": "test description"},
        generator_url="",
    )


class _FakeServices:
    """Minimal ServiceRegistry stand-in for tests."""
    def names(self) -> list[str]:
        return ["demo-app", "payments"]

    def get(self, name: str):
        return None


@pytest.fixture
async def app_with_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.mount(
        "/static",
        StaticFiles(directory=str(Path(__file__).parent.parent / "src" / "dashboard" / "static")),
        name="static",
    )
    app.include_router(runs_router)
    app.include_router(settings_router)
    app.include_router(trigger_router)
    app.include_router(web_router)
    app.state.sessionmaker = sm
    app.state.services = _FakeServices()
    app.state.collector = None
    app.state.patcher = None
    app.state.github = None
    app.state.notifier = None
    app.state.background_tasks = set()
    yield app, sm
    await engine.dispose()


# ---------------------------------------------------------------------------
# GET /trigger — form page
# ---------------------------------------------------------------------------

async def test_trigger_form_renders(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/trigger")
    assert r.status_code == 200
    assert "Manual Trigger" in r.text
    assert "Replay" in r.text
    assert "Synthesize" in r.text


async def test_trigger_form_lists_services(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/trigger")
    assert "demo-app" in r.text
    assert "payments" in r.text


async def test_trigger_form_shows_recent_runs(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.create_run_from_alert(s, _alert("CacheHitRateLow"), raw_payload={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/trigger")
    assert "CacheHitRateLow" in r.text


# ---------------------------------------------------------------------------
# POST /trigger — replay mode
# ---------------------------------------------------------------------------

async def test_trigger_replay_creates_run_and_redirects(app_with_db):
    app, sm = app_with_db

    # Create a source run from a real alert payload
    alert = _alert("HelmValuesMisconfigured")
    stored_payload = {
        "alerts": [{
            "fingerprint": alert.fingerprint,
            "status": alert.status,
            "labels": alert.labels,
            "annotations": alert.annotations,
            "startsAt": "2026-05-04T00:00:00.000Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "",
        }]
    }
    async with sm() as s:
        source_run = await repo.create_run_from_alert(s, alert, raw_payload=stored_payload)

    with patch("src.api.trigger_routes.process_alert", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://t",
            follow_redirects=False,
        ) as c:
            r = await c.post("/trigger", data={"mode": "replay", "replay_run_id": source_run.id})

    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/trigger/")

    # Verify a new Run was created with trigger_source="manual"
    new_run_id = location.removeprefix("/trigger/")
    async with sm() as s:
        new_run = await repo.get_run(s, new_run_id)
    assert new_run is not None
    assert new_run.trigger_source == "manual"
    assert new_run.alertname == "HelmValuesMisconfigured"


async def test_trigger_replay_missing_run_id_422(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/trigger", data={"mode": "replay"})
    assert r.status_code == 422


async def test_trigger_replay_unknown_run_id_404(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/trigger", data={"mode": "replay", "replay_run_id": "does-not-exist"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /trigger — synthesize mode
# ---------------------------------------------------------------------------

async def test_trigger_synthesize_creates_run_and_redirects(app_with_db):
    app, sm = app_with_db

    with patch("src.api.trigger_routes.process_alert", new_callable=AsyncMock):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://t",
            follow_redirects=False,
        ) as c:
            r = await c.post("/trigger", data={
                "mode": "synthesize",
                "service": "demo-app",
                "alertname": "UpstreamTimeoutSpike",
                "severity": "critical",
                "summary": "upstream is timing out",
                "description": "lots of 504s",
                "lookback_minutes": "60",
            })

    assert r.status_code == 303
    new_run_id = r.headers["location"].removeprefix("/trigger/")

    async with sm() as s:
        run = await repo.get_run(s, new_run_id)
    assert run is not None
    assert run.trigger_source == "manual"
    assert run.alertname == "UpstreamTimeoutSpike"
    assert run.service == "demo-app"
    assert run.severity == "critical"


async def test_trigger_synthesize_missing_fields_422(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/trigger", data={"mode": "synthesize"})
    assert r.status_code == 422


async def test_trigger_unknown_mode_422(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/trigger", data={"mode": "invalid"})
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# GET /trigger/{run_id} — progress page
# ---------------------------------------------------------------------------

async def test_trigger_progress_pending_has_polling(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={}, trigger_source="manual")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/trigger/{run.id}")
    assert r.status_code == 200
    assert 'hx-trigger="every 2s"' in r.text
    assert "Running pipeline" in r.text


async def test_trigger_progress_terminal_no_polling(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={}, trigger_source="manual")
        await repo.update_status(s, run.id, "done")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/trigger/{run.id}")
    assert r.status_code == 200
    assert 'hx-trigger="every 2s"' not in r.text


async def test_trigger_progress_404(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/trigger/does-not-exist")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /trigger/{run_id}/partial — HTMX polling target
# ---------------------------------------------------------------------------

async def test_trigger_partial_non_terminal_no_redirect(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={}, trigger_source="manual")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/trigger/{run.id}/partial")
    assert r.status_code == 200
    assert "HX-Redirect" not in r.headers
    assert "pending" in r.text


async def test_trigger_partial_terminal_sends_hx_redirect(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={}, trigger_source="manual")
        await repo.update_status(s, run.id, "done")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/trigger/{run.id}/partial")
    assert r.status_code == 200
    assert r.headers.get("hx-redirect") == f"/runs/{run.id}"

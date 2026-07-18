"""Tests for the dashboard-hosted /trigger form + its proxy to the agent.

Background: the trigger UI used to live on the agent and the dashboard
linked to it cross-host. That broke as soon as the agent stopped being
browser-reachable (in-cluster DNS only). The dashboard now hosts the
form and proxies form submissions to the agent server-side.

The agent's POST /trigger is still the single place that creates the
Run row + dispatches process_alert — the dashboard is a pure proxy
that translates the agent's 303 → /trigger/{id} into a dashboard 303
→ /runs/{id}, so the operator stays on the host they can reach.
"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.dashboard.trigger_routes as _tr
from src.dashboard.runs_api import router as runs_router
from src.dashboard.settings_routes import router as settings_router
from src.dashboard.trigger_routes import router as trigger_router
from src.dashboard.web_routes import router as web_router
from src.db.models import Base


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
    app.state.http = MagicMock()
    yield app, sm
    await engine.dispose()


# ---------------------------------------------------------------------------
# GET /trigger
# ---------------------------------------------------------------------------

async def test_trigger_page_renders(app_with_db):
    """Renders even when the agent is unreachable — the service dropdown
    just becomes empty / disabled. Replay still works because it uses
    the dashboard's own runs DB."""
    app, _ = app_with_db
    fake_cfg = replace(_tr.config, agent_url="")  # no agent reachable
    with patch.object(_tr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/trigger")
    assert r.status_code == 200
    assert "Manual Trigger" in r.text
    assert "Replay" in r.text
    assert "Synthesize" in r.text
    # When the agent can't be reached, the synthesize form has a warning.
    assert "Could not load service list" in r.text


async def test_trigger_page_loads_services_from_agent(app_with_db):
    app, _ = app_with_db
    fake_cfg = replace(_tr.config, agent_url="http://llopster-agent:8000")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"services": ["demo-app", "api-svc"]})
    app.state.http.get = AsyncMock(return_value=mock_resp)

    with patch.object(_tr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/trigger")
    assert r.status_code == 200
    assert "demo-app" in r.text
    assert "api-svc" in r.text
    # The fetch URL must hit the /api/integrations/services endpoint so
    # there's no risk of being shadowed by a future top-level /api/services.
    called_url = app.state.http.get.await_args.args[0]
    assert called_url == "http://llopster-agent:8000/api/integrations/services"


# ---------------------------------------------------------------------------
# POST /trigger — proxy
# ---------------------------------------------------------------------------

async def test_trigger_submit_proxies_replay_and_redirects(app_with_db):
    """Happy path: dashboard forwards the form to the agent, the agent
    303s to /trigger/{new_id}, the dashboard rewrites that to
    /runs/{new_id} on its own host."""
    app, _ = app_with_db
    fake_cfg = replace(_tr.config, agent_url="http://llopster-agent:8000")

    agent_resp = MagicMock()
    agent_resp.status_code = 303
    agent_resp.headers = {"location": "/trigger/abc-123"}
    app.state.http.post = AsyncMock(return_value=agent_resp)

    with patch.object(_tr, "config", fake_cfg):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://dashboard",
            follow_redirects=False,
        ) as c:
            r = await c.post("/trigger", data={
                "mode": "replay",
                "replay_run_id": "some-old-run-id",
            })

    assert r.status_code == 303
    # Critical: operator lands on the DASHBOARD's run page, not the
    # agent's trigger progress page (which their browser can't reach).
    assert r.headers["location"] == "/runs/abc-123"

    # Form-encoded body, follow_redirects=False so we capture the 303.
    posted_url = app.state.http.post.await_args.args[0]
    assert posted_url == "http://llopster-agent:8000/trigger"
    assert app.state.http.post.await_args.kwargs["follow_redirects"] is False
    posted_data = app.state.http.post.await_args.kwargs["data"]
    assert posted_data["mode"] == "replay"
    assert posted_data["replay_run_id"] == "some-old-run-id"


async def test_trigger_submit_500_when_agent_url_missing(app_with_db):
    app, _ = app_with_db
    fake_cfg = replace(_tr.config, agent_url="")
    with patch.object(_tr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/trigger", data={"mode": "replay", "replay_run_id": "x"})
    assert r.status_code == 500
    assert "AGENT_URL" in r.text


async def test_trigger_submit_502_when_agent_unreachable(app_with_db):
    app, _ = app_with_db
    fake_cfg = replace(_tr.config, agent_url="http://llopster-agent:8000")
    app.state.http.post = AsyncMock(side_effect=ConnectionError("refused"))

    with patch.object(_tr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/trigger", data={"mode": "replay", "replay_run_id": "x"})
    assert r.status_code == 502
    assert "refused" in r.text


async def test_trigger_submit_propagates_agent_validation_error(app_with_db):
    """Agent returns 422 (e.g. missing required field). The dashboard
    surfaces that status code so the operator sees a meaningful error."""
    app, _ = app_with_db
    fake_cfg = replace(_tr.config, agent_url="http://llopster-agent:8000")

    agent_resp = MagicMock()
    agent_resp.status_code = 422
    agent_resp.text = "service is required for synthesize mode"
    agent_resp.headers = {}
    app.state.http.post = AsyncMock(return_value=agent_resp)

    with patch.object(_tr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/trigger", data={"mode": "synthesize"})
    assert r.status_code == 422
    assert "service is required" in r.text


async def test_trigger_submit_502_when_agent_redirects_with_no_location(app_with_db):
    """Defensive: if the agent's response shape changes and we get a
    303 without a Location header, fail visibly rather than redirect
    the operator to /runs/ (no id)."""
    app, _ = app_with_db
    fake_cfg = replace(_tr.config, agent_url="http://llopster-agent:8000")

    agent_resp = MagicMock()
    agent_resp.status_code = 303
    agent_resp.headers = {}  # no location
    app.state.http.post = AsyncMock(return_value=agent_resp)

    with patch.object(_tr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/trigger", data={"mode": "replay", "replay_run_id": "x"})
    assert r.status_code == 502

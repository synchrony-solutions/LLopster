"""Tests for the Settings HTML routes (Phase D).

Verifies the GET /settings page renders, POST saves to DB, and the
test-connection endpoints return appropriate HTMX fragments.
"""

from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.dashboard.settings_routes as _sr
from src.dashboard.runs_api import router as runs_router
from src.dashboard.settings_routes import router as settings_router
from src.api.trigger_routes import router as trigger_router
from src.dashboard.web_routes import router as web_router
from src.config import Config
from src.db import repository as repo
from src.db.models import Base


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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
    # Minimal http client mock
    app.state.http = AsyncMock()
    app.state.services = type("S", (), {"names": lambda self: []})()
    app.state.background_tasks = set()
    yield app, sm
    await engine.dispose()


# ---------------------------------------------------------------------------
# GET /settings
# ---------------------------------------------------------------------------

async def test_settings_page_renders(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings")
    assert r.status_code == 200
    assert "Settings" in r.text
    assert "Patch confidence threshold" in r.text
    assert "Log lookback" in r.text


async def test_settings_page_shows_defaults_when_db_empty(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings")
    assert str(_sr.config.patch_confidence_threshold) in r.text
    assert str(_sr.config.log_lookback_minutes) in r.text


# ---------------------------------------------------------------------------
# POST /settings
# ---------------------------------------------------------------------------

async def test_settings_save_persists_to_db(app_with_db):
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "3",
            "log_lookback_minutes": "60",
        })
    assert r.status_code == 303
    assert "/settings" in r.headers["location"]

    async with sm() as session:
        confidence = await repo.get_setting(session, "patch_confidence_threshold")
        lookback = await repo.get_setting(session, "log_lookback_minutes")
    assert confidence == "3"
    assert lookback == "60"


async def test_settings_save_reflects_on_reload(app_with_db):
    app, sm = app_with_db
    async with sm() as session:
        await repo.set_setting(session, "patch_confidence_threshold", "2")
        await repo.set_setting(session, "log_lookback_minutes", "15")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings")
    assert 'value="2"' in r.text
    assert 'value="15"' in r.text


# ---------------------------------------------------------------------------
# Run retention
# ---------------------------------------------------------------------------

async def test_settings_save_persists_retention(app_with_db):
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "4",
            "log_lookback_minutes": "30",
            "run_retention_days": "14",
        })
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "run_retention_days") == "14"


async def test_settings_retention_clamps_negative_to_zero(app_with_db):
    """Negative values are nonsense — coerce to 0 (= disabled) rather
    than crash or pass through to the pruner."""
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "4",
            "log_lookback_minutes": "30",
            "run_retention_days": "-5",
        })
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "run_retention_days") == "0"


async def test_settings_retention_clamps_huge_value(app_with_db):
    """Cap at ~5 years to avoid an operator typo creating an effectively
    permanent-retention DB through misclick."""
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "4",
            "log_lookback_minutes": "30",
            "run_retention_days": "999999",
        })
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "run_retention_days") == "1825"


async def test_settings_page_shows_retention_input(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.set_setting(s, "run_retention_days", "7")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings")
    assert "Run retention" in r.text
    assert 'value="7"' in r.text


# ---------------------------------------------------------------------------
# Draft PRs + cost-breaker ceilings
# ---------------------------------------------------------------------------

async def test_settings_save_persists_draft_and_cost_ceilings(app_with_db):
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "4",
            "log_lookback_minutes": "30",
            # open_prs_as_draft checkbox omitted = unchecked → "false"
            "max_runs_per_hour": "25",
            "max_usd_per_day": "12.50",
        })
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "open_prs_as_draft") == "false"
        assert await repo.get_setting(s, "max_runs_per_hour") == "25"
        assert await repo.get_setting(s, "max_usd_per_day") == "12.5"


async def test_settings_save_draft_checked(app_with_db):
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "4",
            "log_lookback_minutes": "30",
            "open_prs_as_draft": "true",
        })
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "open_prs_as_draft") == "true"


async def test_settings_cost_ceilings_clamp_negative(app_with_db):
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "4",
            "log_lookback_minutes": "30",
            "max_runs_per_hour": "-5",
            "max_usd_per_day": "-1",
        })
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "max_runs_per_hour") == "0"
        assert await repo.get_setting(s, "max_usd_per_day") == "0.0"


async def test_settings_page_shows_cost_breaker_inputs(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings")
    assert "Open PRs as draft" in r.text
    assert "max runs / hour" in r.text
    assert "max USD / day" in r.text


# ---------------------------------------------------------------------------
# API access token (inbound-auth shared secret)
# ---------------------------------------------------------------------------

async def test_settings_api_token_save_persists(app_with_db):
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings/api-token", data={"api_auth_token": "  s3cret-token  "})
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "api_auth_token") == "s3cret-token"  # trimmed


async def test_settings_api_token_save_blank_clears(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.set_setting(s, "api_auth_token", "old")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        # Auth is now active (token set), so clearing it requires the current
        # token — exactly the "once set, changing requires it" contract.
        r = await c.post(
            "/settings/api-token",
            data={"api_auth_token": ""},
            headers={"Authorization": "Bearer old"},
        )
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "api_auth_token") == ""


async def test_settings_api_token_save_blank_without_auth_rejected(app_with_db):
    """Sanity: once a token is set, an unauthenticated attempt to clear it 401s."""
    app, sm = app_with_db
    async with sm() as s:
        await repo.set_setting(s, "api_auth_token", "old")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings/api-token", data={"api_auth_token": ""})
    assert r.status_code == 401
    async with sm() as s:
        assert await repo.get_setting(s, "api_auth_token") == "old"  # unchanged


async def test_settings_page_shows_api_access_card(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings")
    assert "API access" in r.text
    # No token configured in the test env → the "disabled" warning shows.
    assert "auth disabled" in r.text


# ---------------------------------------------------------------------------
# License key
# ---------------------------------------------------------------------------

async def test_settings_license_save_persists(app_with_db):
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings/license", data={"license_key": "  eyJabc.def.ghi  "})
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "license_key") == "eyJabc.def.ghi"  # trimmed


async def test_settings_license_save_blank_clears(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.set_setting(s, "license_key", "eyJold.token")
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings/license", data={"license_key": ""})
    assert r.status_code == 303
    async with sm() as s:
        assert await repo.get_setting(s, "license_key") == ""


async def test_settings_page_shows_license_card(app_with_db):
    """With no AGENT_URL the dashboard can't reach the agent, so the card
    renders the unreachable state rather than crashing."""
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings")
    assert r.status_code == 200
    assert "License key" in r.text
    assert "Agent unreachable" in r.text


async def test_settings_page_shows_active_license(app_with_db):
    """Active-license branch: dashboard proxies the agent's status and renders
    tier + features."""
    fake_cfg = replace(_sr.config, agent_url="http://agent:8000")
    app, _ = app_with_db
    app.state.http.get = AsyncMock(return_value=_mock_agent_json({
        "tier": "business",
        "valid": True,
        "source": "license",
        "features": ["multi_cluster", "pack:jvm-pack"],
        "license_id": "acme-9",
        "expires_at": "2027-01-01T00:00:00+00:00",
        "clusters": 5,
        "reason": None,
    }))
    with patch.object(_sr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/settings")
    assert r.status_code == 200
    assert "business" in r.text
    assert "✓ active" in r.text
    assert "multi_cluster" in r.text
    assert "acme-9" in r.text


# ---------------------------------------------------------------------------
# Processing-mode toggle
# ---------------------------------------------------------------------------

async def test_settings_save_persists_processing_mode(app_with_db):
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "4",
            "log_lookback_minutes": "30",
            "processing_mode": "manual",
        })
    assert r.status_code == 303
    async with sm() as s:
        from src.agent.processing_mode import MANUAL, get_processing_mode
        mode = await get_processing_mode(s)
    assert mode == MANUAL


async def test_settings_save_rejects_invalid_processing_mode(app_with_db):
    """A bad value should be silently coerced to default rather than crash."""
    app, sm = app_with_db
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://t",
        follow_redirects=False,
    ) as c:
        r = await c.post("/settings", data={
            "patch_confidence_threshold": "4",
            "log_lookback_minutes": "30",
            "processing_mode": "rocket-mode",
        })
    assert r.status_code == 303
    async with sm() as s:
        from src.agent.processing_mode import AUTOPILOT, get_processing_mode
        mode = await get_processing_mode(s)
    assert mode == AUTOPILOT


async def test_settings_page_shows_processing_mode(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        from src.agent.processing_mode import MANUAL, set_processing_mode
        await set_processing_mode(s, MANUAL)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/settings")
    assert "Processing mode" in r.text
    # The manual radio is checked
    assert 'value="manual"' in r.text and "checked" in r.text


# ---------------------------------------------------------------------------
# Connection tests — dashboard proxies to the agent's /api/integrations/test/*
# endpoints. The dashboard no longer has SLACK_WEBHOOK_URL or GITHUB_TOKEN in
# its env (security fix); these tests mock the agent's JSON response instead.
# ---------------------------------------------------------------------------

def _mock_agent_json(payload: dict, *, status_code: int = 200) -> MagicMock:
    """Build an httpx-style response mock that returns the given JSON body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json = MagicMock(return_value=payload)
    return resp


async def test_settings_test_slack_success(app_with_db):
    """Agent reports ok=true → dashboard renders the green ✓ fragment."""
    fake_cfg = replace(_sr.config, agent_url="http://agent:8000")
    app, _ = app_with_db
    app.state.http.post = AsyncMock(
        return_value=_mock_agent_json({"ok": True, "detail": "Connected"})
    )

    with patch.object(_sr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/settings/test/slack")
    assert r.status_code == 200
    assert "Connected" in r.text
    assert "test-ok" in r.text
    # Verify the dashboard actually called the agent, not Slack directly.
    called_url = app.state.http.post.await_args.args[0]
    assert called_url == "http://agent:8000/api/integrations/test/slack"


async def test_settings_test_slack_agent_url_missing(app_with_db):
    """No AGENT_URL on dashboard → render error rather than crash."""
    fake_cfg = replace(_sr.config, agent_url="")
    app, _ = app_with_db

    with patch.object(_sr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/settings/test/slack")
    assert r.status_code == 200
    assert "AGENT_URL not set" in r.text
    assert "test-err" in r.text


async def test_settings_test_slack_agent_reports_not_configured(app_with_db):
    """Agent owns the secret check now — dashboard surfaces its message."""
    fake_cfg = replace(_sr.config, agent_url="http://agent:8000")
    app, _ = app_with_db
    app.state.http.post = AsyncMock(
        return_value=_mock_agent_json({"ok": False, "detail": "SLACK_WEBHOOK_URL not set"})
    )

    with patch.object(_sr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/settings/test/slack")
    assert r.status_code == 200
    assert "not set" in r.text
    assert "test-err" in r.text


async def test_settings_test_github_success(app_with_db):
    fake_cfg = replace(_sr.config, agent_url="http://agent:8000")
    app, _ = app_with_db
    app.state.http.post = AsyncMock(
        return_value=_mock_agent_json(
            {"ok": True, "detail": "Authenticated as octocat", "user": "octocat"},
        )
    )

    with patch.object(_sr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/settings/test/github")
    assert r.status_code == 200
    assert "octocat" in r.text
    assert "test-ok" in r.text
    called_url = app.state.http.post.await_args.args[0]
    assert called_url == "http://agent:8000/api/integrations/test/github"


async def test_settings_test_github_agent_reports_not_configured(app_with_db):
    fake_cfg = replace(_sr.config, agent_url="http://agent:8000")
    app, _ = app_with_db
    app.state.http.post = AsyncMock(
        return_value=_mock_agent_json({"ok": False, "detail": "GITHUB_TOKEN not set"})
    )

    with patch.object(_sr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/settings/test/github")
    assert r.status_code == 200
    assert "not set" in r.text
    assert "test-err" in r.text


async def test_settings_test_agent_unreachable(app_with_db):
    """Network failure to the agent → dashboard degrades gracefully."""
    fake_cfg = replace(_sr.config, agent_url="http://agent:8000")
    app, _ = app_with_db
    app.state.http.post = AsyncMock(side_effect=ConnectionError("connection refused"))

    with patch.object(_sr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/settings/test/slack")
    assert r.status_code == 200
    assert "agent unreachable" in r.text
    assert "test-err" in r.text

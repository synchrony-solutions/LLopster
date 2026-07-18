"""Tests for the inbound shared-secret auth dependency.

Covers the three states the launch reviewers asked for — authorized,
unauthorized, and unconfigured — plus the Bearer/Basic credential forms and
the settings-override → env resolution. No live HTTP; a tiny FastAPI app with
an in-memory SQLite sessionmaker stands in for the agent/dashboard.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest
from fastapi import Depends, FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.api import auth as auth_mod
from src.api.auth import require_inbound_auth, resolve_api_token
from src.db import repository as repo
from src.db.models import Base


@pytest.fixture
async def app_with_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()

    @app.post("/protected", dependencies=[Depends(require_inbound_auth)])
    async def protected() -> dict:
        return {"ok": True}

    app.state.sessionmaker = sm
    yield app, sm
    await engine.dispose()


def _client(app) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://t")


def _basic(user: str, password: str) -> str:
    raw = base64.b64encode(f"{user}:{password}".encode()).decode()
    return f"Basic {raw}"


# ---------------------------------------------------------------------------
# Unconfigured — auth disabled, backward compatible
# ---------------------------------------------------------------------------

async def test_unconfigured_allows_request(app_with_db, monkeypatch):
    app, _ = app_with_db
    # No env token, no setting → auth disabled.
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token=""))
    async with _client(app) as c:
        r = await c.post("/protected")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


# ---------------------------------------------------------------------------
# Configured via env var
# ---------------------------------------------------------------------------

async def test_env_token_authorized_bearer(app_with_db, monkeypatch):
    app, _ = app_with_db
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token="s3cret"))
    async with _client(app) as c:
        r = await c.post("/protected", headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200


async def test_env_token_authorized_basic(app_with_db, monkeypatch):
    app, _ = app_with_db
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token="s3cret"))
    async with _client(app) as c:
        # Username is ignored; the password component is the shared secret.
        r = await c.post("/protected", headers={"Authorization": _basic("ops", "s3cret")})
    assert r.status_code == 200


async def test_env_token_missing_credential_rejected(app_with_db, monkeypatch):
    app, _ = app_with_db
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token="s3cret"))
    async with _client(app) as c:
        r = await c.post("/protected")
    assert r.status_code == 401
    # Browsers need the challenge to prompt for Basic credentials.
    assert "WWW-Authenticate" in r.headers


async def test_env_token_wrong_credential_rejected(app_with_db, monkeypatch):
    app, _ = app_with_db
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token="s3cret"))
    async with _client(app) as c:
        r = await c.post("/protected", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401
        r2 = await c.post("/protected", headers={"Authorization": _basic("ops", "nope")})
        assert r2.status_code == 401


async def test_malformed_authorization_header_rejected(app_with_db, monkeypatch):
    app, _ = app_with_db
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token="s3cret"))
    async with _client(app) as c:
        # Not base64 / no scheme value.
        r = await c.post("/protected", headers={"Authorization": "Basic !!!notbase64"})
        assert r.status_code == 401
        r2 = await c.post("/protected", headers={"Authorization": "Bearer"})
        assert r2.status_code == 401


# ---------------------------------------------------------------------------
# Configured via the settings model (DB override wins over env)
# ---------------------------------------------------------------------------

async def test_setting_override_enables_auth(app_with_db, monkeypatch):
    app, sm = app_with_db
    # Env has no token, but a setting does → auth is enforced.
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token=""))
    async with sm() as s:
        await repo.set_setting(s, "api_auth_token", "db-token")

    async with _client(app) as c:
        denied = await c.post("/protected")
        assert denied.status_code == 401
        ok = await c.post("/protected", headers={"Authorization": "Bearer db-token"})
        assert ok.status_code == 200


async def test_setting_override_beats_env(app_with_db, monkeypatch):
    app, sm = app_with_db
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token="env-token"))
    async with sm() as s:
        await repo.set_setting(s, "api_auth_token", "db-token")

    async with _client(app) as c:
        # The env token no longer works once the DB override is set.
        stale = await c.post("/protected", headers={"Authorization": "Bearer env-token"})
        assert stale.status_code == 401
        ok = await c.post("/protected", headers={"Authorization": "Bearer db-token"})
        assert ok.status_code == 200


# ---------------------------------------------------------------------------
# resolve_api_token unit behavior
# ---------------------------------------------------------------------------

async def test_resolve_api_token_blank_setting_falls_back_to_env(app_with_db, monkeypatch):
    app, sm = app_with_db
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token="env-token"))
    async with sm() as s:
        await repo.set_setting(s, "api_auth_token", "   ")  # whitespace-only = unset
    assert await resolve_api_token(sm) == "env-token"


# ---------------------------------------------------------------------------
# Real inbound surfaces reject unauthenticated writes when auth is configured
# ---------------------------------------------------------------------------

async def _real_surface_app(sm):
    from src.api.trigger_routes import router as agent_trigger_router
    from src.dashboard.settings_routes import router as settings_router

    app = FastAPI()
    app.include_router(agent_trigger_router)
    app.include_router(settings_router)
    app.state.sessionmaker = sm
    return app


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("post", "/trigger", {"mode": "synthesize", "service": "x", "alertname": "y"}),
        ("post", "/trigger/dispatch/some-id", None),
        ("post", "/settings", {"patch_confidence_threshold": "4", "log_lookback_minutes": "30"}),
        ("post", "/settings/license", {"license_key": "whatever"}),
    ],
)
async def test_real_write_surfaces_require_auth(app_with_db, monkeypatch, method, path, data):
    _, sm = app_with_db
    monkeypatch.setattr(auth_mod, "config", SimpleNamespace(api_auth_token="s3cret"))
    app = await _real_surface_app(sm)
    async with _client(app) as c:
        denied = await c.request(method, path, data=data)
        assert denied.status_code == 401, f"{path} should reject unauthenticated writes"

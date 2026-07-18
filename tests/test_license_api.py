"""Tests for the agent's /api/license/status endpoint + DB-override refresh."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent import license as lic
from src.api.license_api import refresh_active_license, router as license_router
from src.db import repository as repo
from src.db.models import Base


@pytest.fixture
def keys() -> tuple[bytes, str]:
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = priv.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return priv_pem, pub_pem


def _sign(priv_pem: bytes, **claims) -> str:
    now = datetime.now(timezone.utc)
    base = {"iat": int(now.timestamp()), "exp": int((now + timedelta(days=365)).timestamp())}
    base.update(claims)
    return jwt.encode(base, priv_pem, algorithm="EdDSA")


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch):
    monkeypatch.delenv(lic.LICENSE_KEY_ENV, raising=False)
    lic.set_active_license_key(None)
    yield
    lic.set_active_license_key(None)


@pytest.fixture
async def app_with_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(license_router)
    app.state.sessionmaker = sm
    yield app, sm
    await engine.dispose()


async def test_status_is_community_with_no_key(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/license/status")
    assert r.status_code == 200
    body = r.json()
    assert body["tier"] == "community"
    assert body["source"] == "embedded"
    assert body["features"] == []


async def test_status_reflects_db_license_key(app_with_db, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(lic, "PUBLIC_KEY_PEM", pub)
    token = _sign(priv, tier="business", features=["multi_cluster", "pack:jvm-pack"], license_id="acme-9")

    app, sm = app_with_db
    async with sm() as s:
        await repo.set_setting(s, "license_key", token)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/license/status")
    body = r.json()
    assert body["tier"] == "business"
    assert body["source"] == "license"
    assert body["license_id"] == "acme-9"
    assert "multi_cluster" in body["features"]
    # The endpoint applied the override, so gating works app-wide afterwards.
    assert lic.is_feature_enabled("multi_cluster") is True


async def test_status_never_returns_raw_jwt(app_with_db, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(lic, "PUBLIC_KEY_PEM", pub)
    token = _sign(priv, tier="business", features=["multi_cluster"])
    app, sm = app_with_db
    async with sm() as s:
        await repo.set_setting(s, "license_key", token)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/license/status")
    assert token not in r.text


async def test_refresh_clears_override_when_setting_blank(app_with_db, keys, monkeypatch):
    priv, pub = keys
    monkeypatch.setattr(lic, "PUBLIC_KEY_PEM", pub)
    # Env key present; a non-blank DB key overrides it, a blank one clears it.
    monkeypatch.setenv(lic.LICENSE_KEY_ENV, _sign(priv, tier="business", features=["multi_cluster"]))
    _app, sm = app_with_db

    async with sm() as s:
        await repo.set_setting(s, "license_key", _sign(priv, tier="business", features=["sso"]))
    await refresh_active_license(sm)
    assert lic.is_feature_enabled("sso") is True
    assert lic.is_feature_enabled("multi_cluster") is False  # override won

    async with sm() as s:
        await repo.set_setting(s, "license_key", "")
    await refresh_active_license(sm)
    # Override cleared → env key applies again.
    assert lic.is_feature_enabled("multi_cluster") is True

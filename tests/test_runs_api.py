"""Tests for the /api/runs and /api/runs/{id} JSON endpoints."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.dashboard.runs_api import router
from src.db import repository as repo
from src.db.models import Base


def _alert(name: str = "TestAlert", service: str = "demo-app") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="f",
        status="firing",
        alertname=name,
        severity="warning",
        service=service,
        summary="x",
        description="y",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": service},
        annotations={},
        generator_url="",
    )


@pytest.fixture
async def app_with_db():
    """Build a tiny FastAPI app that just exposes the runs router with an
    in-memory sqlite session factory wired into app.state."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)

    app = FastAPI()
    app.include_router(router)
    app.state.sessionmaker = sm
    yield app, sm
    await engine.dispose()


async def test_list_empty(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/runs")
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 0
    assert body["items"] == []


async def test_list_returns_summary_fields(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.create_run_from_alert(s, _alert("A"), raw_payload={"k": "v"})
        await repo.create_run_from_alert(s, _alert("B"), raw_payload={})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/runs")
    body = r.json()
    assert body["total"] == 2
    # Newest first
    names = [item["alertname"] for item in body["items"]]
    assert names == ["B", "A"]
    first = body["items"][0]
    # Summary view should NOT include the heavy detail fields
    assert "alert_payload_json" not in first
    assert "llm_response_text" not in first
    # But should have the lifecycle/outcome fields the dashboard needs
    assert first["processing_status"] == "pending"
    assert first["pr_opened"] is False
    assert first["confidence"] is None


async def test_list_filters_by_service(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.create_run_from_alert(s, _alert(service="payments"), raw_payload={})
        await repo.create_run_from_alert(s, _alert(service="demo-app"), raw_payload={})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/runs?service=payments")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["service"] == "payments"


async def test_get_run_returns_detail(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(
            s, _alert("Z"), raw_payload={"alerts": [{"x": 1}]},
        )
        run_id = run.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/api/runs/{run_id}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == run_id
    # Detail view DOES include the heavy fields
    assert body["alert_payload_json"] == {"alerts": [{"x": 1}]}
    assert body["alertname"] == "Z"
    assert body["processing_status"] == "pending"


async def test_get_run_404(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/runs/does-not-exist")
    assert r.status_code == 404


async def test_summary_and_detail_expose_operator_label(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert("Labeled"), raw_payload={})
        await repo.set_operator_label(s, run.id, "correct", note="nailed it")
        run_id = run.id

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        listing = await c.get("/api/runs")
        detail = await c.get(f"/api/runs/{run_id}")

    assert listing.json()["items"][0]["operator_label"] == "correct"
    body = detail.json()
    assert body["operator_label"] == "correct"
    assert body["operator_label_note"] == "nailed it"
    assert body["operator_labeled_at"] is not None


async def test_list_filters_by_operator_label(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        a = await repo.create_run_from_alert(s, _alert("A"), raw_payload={})
        await repo.create_run_from_alert(s, _alert("B"), raw_payload={})
        await repo.set_operator_label(s, a.id, "wrong")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/runs?operator_label=wrong")
    body = r.json()
    assert body["total"] == 1
    assert body["items"][0]["alertname"] == "A"


async def test_pagination_limit_and_offset(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        for i in range(5):
            await repo.create_run_from_alert(s, _alert(f"A{i}"), raw_payload={})

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/runs?limit=2&offset=1")
    body = r.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert len(body["items"]) == 2

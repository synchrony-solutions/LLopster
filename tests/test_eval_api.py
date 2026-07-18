"""Tests for the eval flywheel persistence + the /api/eval/summary endpoint."""

from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.dashboard.runs_api import router
from src.db import repository as repo
from src.db.models import Base


@pytest.fixture
async def app_with_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    app = FastAPI()
    app.include_router(router)
    app.state.sessionmaker = sm
    yield app, sm
    await engine.dispose()


def _alert(name="DatabasePoolExhausted") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="f", status="firing", alertname=name, severity="critical",
        service="demo-app", summary="x", description="y",
        starts_at=datetime(2026, 4, 18, tzinfo=timezone.utc), ends_at=None,
        labels={"service": "demo-app"}, annotations={}, generator_url="",
    )


async def test_record_eval_run_derives_pass_rate(app_with_db):
    _, sm = app_with_db
    async with sm() as session:
        er = await repo.record_eval_run(
            session,
            scenario_count=5, correct_count=4, partial_count=1, wrong_count=0,
            results=[{"scenario_id": "db-pool-exhausted", "label": "correct"}],
            corpus_version="5:abcd1234", model="claude-opus-4-7",
        )
    assert er.pass_rate == pytest.approx(0.8)
    assert er.results_json[0]["scenario_id"] == "db-pool-exhausted"


async def test_latest_and_list_eval_runs_order(app_with_db):
    _, sm = app_with_db
    async with sm() as session:
        for c in (1, 2, 3):
            await repo.record_eval_run(
                session, scenario_count=5, correct_count=c,
                partial_count=0, wrong_count=5 - c, results=[],
            )
    async with sm() as session:
        latest = await repo.latest_eval_run(session)
        history = await repo.list_eval_runs(session, limit=10)
    assert latest.correct_count == 3
    assert len(history) == 3


async def test_operator_label_stats_consumes_labels(app_with_db):
    _, sm = app_with_db
    # Three labeled runs: correct, partial, wrong → pass-rate (1 + 0.5)/3.
    async with sm() as session:
        for label in ("correct", "partial", "wrong", None):
            run = await repo.create_run_from_alert(session, _alert(), {})
            if label:
                await repo.set_operator_label(session, run.id, label)
    async with sm() as session:
        stats = await repo.operator_label_stats(session)
    assert stats["labeled_total"] == 3
    assert stats["counts"]["correct"] == 1
    assert stats["pass_rate"] == pytest.approx((1 + 0.5) / 3)


async def test_eval_summary_endpoint_shape(app_with_db):
    app, sm = app_with_db
    async with sm() as session:
        await repo.record_eval_run(
            session, scenario_count=5, correct_count=5, partial_count=0,
            wrong_count=0, results=[{"scenario_id": "x", "label": "correct"}],
            corpus_version="5:abcd1234", model="claude-opus-4-7",
        )
        run = await repo.create_run_from_alert(session, _alert(), {})
        await repo.set_operator_label(session, run.id, "correct")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/eval/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["harness"]["latest"]["pass_rate"] == pytest.approx(1.0)
    assert body["harness"]["latest"]["results"][0]["scenario_id"] == "x"
    assert len(body["harness"]["trend"]) == 1
    assert body["operator_labels"]["labeled_total"] == 1


async def test_eval_summary_endpoint_empty_db(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/eval/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["harness"]["latest"] is None
    assert body["harness"]["trend"] == []
    assert body["operator_labels"]["labeled_total"] == 0

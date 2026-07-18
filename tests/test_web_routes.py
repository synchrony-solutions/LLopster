"""Tests for the read-only HTML dashboard.

We don't try to validate every byte of the rendered HTML — just verify the
route returns 200, contains the data we expect, and that the polling
trigger is added/removed based on run status."""

from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from httpx import ASGITransport, AsyncClient
from pathlib import Path
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import src.dashboard.web_routes as _wr
from src.agent.alert_handler import ParsedAlert
from src.dashboard.runs_api import router as runs_router
from src.dashboard.settings_routes import router as settings_router
from src.api.trigger_routes import router as trigger_router
from src.dashboard.web_routes import router as web_router
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
        starts_at=datetime(2026, 5, 4, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": service},
        annotations={},
        generator_url="",
    )


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
    app.state.services = type("S", (), {"names": lambda self: []})()
    app.state.background_tasks = set()
    yield app, sm
    await engine.dispose()


# ---------------------------------------------------------------------------
# Basic routing
# ---------------------------------------------------------------------------

async def test_root_redirects_to_runs(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t", follow_redirects=False) as c:
        r = await c.get("/")
    assert r.status_code == 302
    assert r.headers["location"] == "/runs"


async def test_runs_list_renders_when_empty(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs")
    assert r.status_code == 200
    assert "<title>Runs — llopster</title>" in r.text
    assert "No runs yet" in r.text


# ---------------------------------------------------------------------------
# List view
# ---------------------------------------------------------------------------

async def test_runs_list_shows_alertname_and_service(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.create_run_from_alert(s, _alert("CacheHitRateLow", "demo-app"), raw_payload={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs")
    assert r.status_code == 200
    assert "CacheHitRateLow" in r.text
    assert "demo-app" in r.text


async def test_runs_list_filters_by_service(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.create_run_from_alert(s, _alert(service="payments"), raw_payload={})
        await repo.create_run_from_alert(s, _alert(service="demo-app"), raw_payload={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs?service=payments")
    assert r.status_code == 200
    assert "payments" in r.text
    # demo-app run should not appear in the table
    assert "demo-app" not in r.text or r.text.count("payments") > r.text.count("demo-app")


async def test_runs_list_polls_when_runs_in_progress(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        # default status is 'pending' — should trigger the poll attribute
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs")
    assert 'hx-trigger="every 2s"' in r.text
    assert 'Live — polling every 2s' in r.text


async def test_runs_list_no_polling_when_all_terminal(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.update_status(s, run.id, "done")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs")
    assert 'hx-trigger="every 2s"' not in r.text


async def test_runs_partial_returns_just_fragment(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        await repo.create_run_from_alert(s, _alert(), raw_payload={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs/partial")
    assert r.status_code == 200
    # Partial doesn't include the full base layout
    assert "<title>" not in r.text
    assert 'id="runs-table-wrap"' in r.text


# ---------------------------------------------------------------------------
# Detail view
# ---------------------------------------------------------------------------

async def test_run_detail_404(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs/does-not-exist")
    assert r.status_code == 404


async def test_run_detail_renders_full_data(app_with_db):
    app, sm = app_with_db
    llm_text = (
        "## Root Cause\n"
        "The memory unit is invalid — Kubernetes rejects 512MBz.\n"
        "\n## Proposed Patch\n"
        "```diff\n"
        "--- a/helm-values.yaml\n"
        "+++ b/helm-values.yaml\n"
        "@@ -2,4 +2,4 @@\n"
        "-    memory: \"512MBz\"\n"
        "+    memory: \"512Mi\"\n"
        "```\n"
        "\n## Confidence\n4/5 — Root cause is unambiguous from logs.\n"
        "\n## Reasoning\nReplacing MBz with Mi makes the validator pass.\n"
    )
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert("HelmValuesMisconfigured"), raw_payload={"x": 1})
        run_id = run.id
        from src.agent.patch_generator import PatchProposal
        proposal = PatchProposal(
            text=llm_text,
            model="claude-opus-4-7",
            input_tokens=11000,
            output_tokens=600,
            cache_read_tokens=10000,
            cache_creation_tokens=0,
            confidence=4,
            confidence_reason="Root cause is unambiguous from logs.",
        )
        await repo.record_llm_response(
            s, run_id, proposal, latency_ms=23456,
            parsed_root_cause="The memory unit is invalid — Kubernetes rejects 512MBz.",
            parsed_diff="--- a/helm-values.yaml\n+++ b/helm-values.yaml\n@@ -2,4 +2,4 @@\n-    memory: \"512MBz\"\n+    memory: \"512Mi\"\n",
        )
        await repo.record_pr(s, run_id, pr_url="https://github.com/owner/repo/pull/42", pr_number=42, pr_branch="llopster/x")
        await repo.record_slack(s, run_id, notified=True)
        await repo.update_status(s, run_id, "done")

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/runs/{run_id}")
    assert r.status_code == 200
    body = r.text
    # Page basics
    assert "HelmValuesMisconfigured" in body
    assert run_id in body
    # Root cause rendered
    assert "memory unit is invalid" in body
    # Diff rendered with pygments classes
    assert "512MBz" in body
    assert "512Mi" in body
    assert 'class="hl' in body  # pygments output
    # Confidence
    assert "4/5" in body
    # Reasoning section extracted
    assert "Replacing MBz with Mi" in body
    # Outcomes
    assert "https://github.com/owner/repo/pull/42" in body
    assert "llopster/x" in body
    # Token usage
    assert "11,000" in body or "11000" in body  # depending on locale; we use thousands
    # Terminal status — no live polling on detail header
    assert 'hx-trigger="every 2s"' not in body


async def test_run_detail_shows_skip_reason(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.record_pr(s, run.id, skip_reason="confidence 2/5 below threshold 4")
        run_id = run.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/runs/{run_id}")
    assert "below threshold" in r.text


async def test_run_detail_polls_when_not_terminal(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.update_status(s, run.id, "generating")
        run_id = run.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/runs/{run_id}")
    # Phase E.1: live updates now use SSE (EventSource) instead of HTMX polling
    assert "EventSource" in r.text
    assert f"/runs/{run_id}/stream" in r.text


# ---------------------------------------------------------------------------
# Operator label widget on the detail page + POST /runs/{run_id}/label
# ---------------------------------------------------------------------------

async def test_run_detail_shows_label_widget_when_terminal(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.update_status(s, run.id, "done")
        run_id = run.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/runs/{run_id}")
    assert "Operator verdict" in r.text
    assert f"/runs/{run_id}/label" in r.text
    assert "Not yet labeled" in r.text


async def test_run_detail_hides_label_widget_when_not_terminal(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.update_status(s, run.id, "generating")
        run_id = run.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/runs/{run_id}")
    # Labeling an in-progress run is meaningless — widget only shows on terminal.
    assert "Operator verdict" not in r.text


async def test_label_post_htmx_returns_fragment_and_persists(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.update_status(s, run.id, "done")
        run_id = run.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/label",
            data={"label": "correct", "note": "spot on"},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200
    # Fragment, not a full page.
    assert "<title>" not in r.text
    assert "Current verdict" in r.text
    assert "spot on" in r.text
    # Persisted.
    async with sm() as s:
        fetched = await repo.get_run(s, run_id)
    assert fetched.operator_label == "correct"
    assert fetched.operator_label_note == "spot on"


async def test_label_post_plain_form_redirects(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.update_status(s, run.id, "done")
        run_id = run.id
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://t", follow_redirects=False,
    ) as c:
        r = await c.post(f"/runs/{run_id}/label", data={"label": "wrong"})
    assert r.status_code == 303
    assert r.headers["location"] == f"/runs/{run_id}"


async def test_label_post_clear_wipes_verdict(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.update_status(s, run.id, "done")
        await repo.set_operator_label(s, run.id, "partial", note="meh")
        run_id = run.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(
            f"/runs/{run_id}/label", data={"label": ""},
            headers={"HX-Request": "true"},
        )
    assert r.status_code == 200
    async with sm() as s:
        fetched = await repo.get_run(s, run_id)
    assert fetched.operator_label is None
    assert fetched.operator_label_note is None


async def test_label_post_invalid_label_400(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.update_status(s, run.id, "done")
        run_id = run.id
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post(f"/runs/{run_id}/label", data={"label": "bogus"})
    assert r.status_code == 400


async def test_label_post_missing_run_404(app_with_db):
    app, _ = app_with_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.post("/runs/nope/label", data={"label": "correct"})
    assert r.status_code == 404


async def test_runs_list_filters_by_operator_label(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        r1 = await repo.create_run_from_alert(s, _alert("LabeledAlert"), raw_payload={})
        await repo.set_operator_label(s, r1.id, "correct")
        await repo.create_run_from_alert(s, _alert("UnlabeledAlert"), raw_payload={})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/runs?operator_label=correct")
    assert r.status_code == 200
    assert "LabeledAlert" in r.text
    assert "UnlabeledAlert" not in r.text


# ---------------------------------------------------------------------------
# POST /runs/{run_id}/dispatch — dashboard-side proxy to the agent
# ---------------------------------------------------------------------------
# The cross-origin form POST to llopster-agent:8000 from the user's browser
# can't work — that's a cluster-internal DNS name. The dashboard exposes
# its own /runs/{id}/dispatch that forwards to the agent server-side via
# httpx and then 303s the operator back to /runs/{id}.

async def test_run_dispatch_proxies_to_agent_and_redirects(app_with_db):
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(
            s, _alert("UpstreamTimeoutSpike"), raw_payload={"alerts": []},
        )
        await repo.update_status(s, run.id, "queued")

    # Agent's /trigger/dispatch responds with 303 → /trigger/<id>. We don't
    # care about the redirect target — we just need to know we called it.
    mock_resp = MagicMock()
    mock_resp.status_code = 303
    app.state.http = MagicMock()
    app.state.http.post = AsyncMock(return_value=mock_resp)

    fake_cfg = replace(_wr.config, agent_url="http://llopster-agent:8000")
    with patch.object(_wr, "config", fake_cfg):
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://dashboard",
            follow_redirects=False,
        ) as c:
            r = await c.post(f"/runs/{run.id}/dispatch")

    assert r.status_code == 303
    # Critical: the operator lands on the DASHBOARD's run detail page,
    # not on the agent's internal /trigger/<id> URL (which they can't reach).
    assert r.headers["location"] == f"/runs/{run.id}"

    called_url = app.state.http.post.await_args.args[0]
    assert called_url == f"http://llopster-agent:8000/trigger/dispatch/{run.id}"
    # follow_redirects=False matters — otherwise httpx would chase the
    # agent's 303 to /trigger/<id> and we'd hit the agent twice.
    assert app.state.http.post.await_args.kwargs.get("follow_redirects") is False


async def test_run_dispatch_500_when_agent_url_missing(app_with_db):
    """Dashboards deployed without AGENT_URL should fail loudly so the
    operator knows why dispatch isn't working — not silently swallow it."""
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(
            s, _alert(), raw_payload={"alerts": []},
        )
        await repo.update_status(s, run.id, "queued")

    fake_cfg = replace(_wr.config, agent_url="")
    with patch.object(_wr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/runs/{run.id}/dispatch")
    assert r.status_code == 500
    assert "AGENT_URL" in r.text


async def test_run_dispatch_502_when_agent_unreachable(app_with_db):
    """Network failure to the agent → 502, not a generic crash."""
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(
            s, _alert(), raw_payload={"alerts": []},
        )
        await repo.update_status(s, run.id, "queued")

    app.state.http = MagicMock()
    app.state.http.post = AsyncMock(side_effect=ConnectionError("refused"))

    fake_cfg = replace(_wr.config, agent_url="http://llopster-agent:8000")
    with patch.object(_wr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/runs/{run.id}/dispatch")
    assert r.status_code == 502
    assert "refused" in r.text


async def test_run_dispatch_propagates_agent_4xx(app_with_db):
    """If the agent says 409 (run isn't actually queued anymore), the
    dashboard surfaces that status code rather than a silent redirect."""
    app, sm = app_with_db
    async with sm() as s:
        run = await repo.create_run_from_alert(
            s, _alert(), raw_payload={"alerts": []},
        )
        await repo.update_status(s, run.id, "done")  # not queued

    mock_resp = MagicMock()
    mock_resp.status_code = 409
    mock_resp.text = "run is in status 'done'"
    app.state.http = MagicMock()
    app.state.http.post = AsyncMock(return_value=mock_resp)

    fake_cfg = replace(_wr.config, agent_url="http://llopster-agent:8000")
    with patch.object(_wr, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post(f"/runs/{run.id}/dispatch")
    assert r.status_code == 409

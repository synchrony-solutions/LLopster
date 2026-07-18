"""HTML routes for the manual-trigger UI (Phase C).

Supports two modes:
  - Replay: re-runs the full pipeline from a stored alert_payload_json.
  - Synthesize: builds a ParsedAlert from form fields and runs it fresh.

Both modes create a Run row with trigger_source="manual", fire
process_alert() as a background task, then redirect to a progress page
that polls until the run reaches a terminal state, at which point
HTMX's HX-Redirect response header bounces the browser to /runs/{id}.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from src.agent.alert_handler import ParsedAlert, parse_alertmanager_payload
from src.agent.processing_mode import get_processing_mode
from src.agent.processor import alert_to_payload, process_alert
from src.api.auth import require_inbound_auth
from src.api.templating import templates
from src.db import repository as repo

router = APIRouter()

_TERMINAL = {"done", "skipped", "failed"}


# ---------------------------------------------------------------------------
# GET /trigger — form page
# ---------------------------------------------------------------------------

@router.get("/trigger", response_class=HTMLResponse, name="trigger_form")
async def trigger_form(request: Request) -> HTMLResponse:
    sm = request.app.state.sessionmaker
    async with sm() as session:
        recent_runs = await repo.list_runs(session, limit=20)
        mode = await get_processing_mode(session)
        # Queued runs are surfaced separately so the operator's primary
        # action in manual mode is one-click "Process" per row, not a
        # buried dropdown.
        queued_runs = await repo.list_queued_runs(session)

    services = list(request.app.state.services.names())

    return templates.TemplateResponse(
        request,
        "trigger.html",
        {
            "active": "trigger",
            "recent_runs": recent_runs,
            "queued_runs": queued_runs,
            "services": services,
            "processing_mode": mode,
        },
    )


# ---------------------------------------------------------------------------
# POST /trigger — create run and redirect to progress page
# ---------------------------------------------------------------------------

@router.post("/trigger", name="trigger_submit", dependencies=[Depends(require_inbound_auth)])
async def trigger_submit(
    request: Request,
    mode: Annotated[str, Form()],
    # Replay fields
    replay_run_id: Annotated[str | None, Form()] = None,
    # Synthesize fields
    service: Annotated[str | None, Form()] = None,
    alertname: Annotated[str | None, Form()] = None,
    severity: Annotated[str | None, Form()] = "warning",
    summary: Annotated[str | None, Form()] = "",
    description: Annotated[str | None, Form()] = "",
    lookback_minutes: Annotated[str | None, Form()] = None,
) -> Response:
    state = request.app.state

    # HTML forms submit empty fields as "" — coerce to None so downstream code
    # can rely on None meaning "use the default".
    _lb_raw = (lookback_minutes or "").strip()
    effective_lookback_int: int | None = int(_lb_raw) if _lb_raw else None

    if mode == "replay":
        if not replay_run_id:
            raise HTTPException(status_code=422, detail="replay_run_id is required for replay mode")

        async with state.sessionmaker() as session:
            source_run = await repo.get_run(session, replay_run_id)
        if source_run is None:
            raise HTTPException(status_code=404, detail=f"run {replay_run_id} not found")

        stored_payload = source_run.alert_payload_json
        parsed_alerts = parse_alertmanager_payload(stored_payload)

        # Find the specific alert that created the source run (match by fingerprint).
        alert = next(
            (a for a in parsed_alerts if a.fingerprint == source_run.fingerprint),
            parsed_alerts[0] if parsed_alerts else None,
        )
        if alert is None:
            raise HTTPException(status_code=422, detail="Could not extract alert from stored payload")

        effective_lookback = effective_lookback_int  # None → processor uses config default
        raw_payload = stored_payload

    elif mode == "synthesize":
        if not service or not alertname:
            raise HTTPException(status_code=422, detail="service and alertname are required for synthesize mode")

        now = datetime.now(timezone.utc)
        fingerprint = str(uuid.uuid4())
        alert = ParsedAlert(
            fingerprint=fingerprint,
            status="firing",
            alertname=alertname,
            severity=severity or "warning",
            service=service,
            summary=summary or "",
            description=description or "",
            starts_at=now,
            ends_at=None,
            labels={
                "alertname": alertname,
                "severity": severity or "warning",
                "service": service,
            },
            annotations={
                "summary": summary or "",
                "description": description or "",
            },
            generator_url="",
        )
        effective_lookback = effective_lookback_int
        raw_payload = {"alerts": [alert_to_payload(alert)]}

    else:
        raise HTTPException(status_code=422, detail=f"Unknown mode: {mode!r}")

    async with state.sessionmaker() as session:
        run = await repo.create_run_from_alert(
            session, alert, raw_payload=raw_payload, trigger_source="manual",
        )

    kwargs: dict = dict(
        run_id=run.id,
        alert=alert,
        sessionmaker=state.sessionmaker,
        collector=state.collector,
        services=state.services,
        patcher=state.patcher,
        github=state.github,
        slack=state.slack,
        triage=getattr(state, "triage", None),
        investigator=getattr(state, "investigator", None),
        # Operator-initiated: never blocked by the cost breaker so a tripped
        # agent can still be driven by hand.
        enforce_cost_breaker=False,
    )
    if effective_lookback is not None:
        kwargs["lookback_minutes"] = effective_lookback

    task = asyncio.create_task(process_alert(**kwargs))
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)

    return RedirectResponse(url=f"/trigger/{run.id}", status_code=303)


# ---------------------------------------------------------------------------
# POST /trigger/dispatch/{run_id} — dispatch a queued run in place
# ---------------------------------------------------------------------------

@router.post(
    "/trigger/dispatch/{run_id}",
    name="trigger_dispatch",
    dependencies=[Depends(require_inbound_auth)],
)
async def trigger_dispatch(request: Request, run_id: str) -> Response:
    """Run process_alert against an existing queued Run row.

    Unlike the replay path, this does NOT create a new Run — it advances
    the queued Run's status from `queued` → pending → ... so operators
    don't see duplicate rows.
    """
    state = request.app.state

    async with state.sessionmaker() as session:
        run = await repo.get_run(session, run_id)

    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    if run.processing_status != "queued":
        raise HTTPException(
            status_code=409,
            detail=f"run is in status {run.processing_status!r}, can only dispatch from 'queued'",
        )

    parsed = parse_alertmanager_payload(run.alert_payload_json)
    alert = next(
        (a for a in parsed if a.fingerprint == run.fingerprint),
        parsed[0] if parsed else None,
    )
    if alert is None:
        raise HTTPException(
            status_code=422, detail="Could not reconstruct alert from stored payload"
        )

    # Reset status so the processor's normal progression starts cleanly.
    async with state.sessionmaker() as session:
        await repo.update_status(session, run_id, "pending", error=None)

    task = asyncio.create_task(
        process_alert(
            run_id, alert,
            sessionmaker=state.sessionmaker,
            collector=state.collector,
            services=state.services,
            patcher=state.patcher,
            github=state.github,
            slack=state.slack,
            triage=getattr(state, "triage", None),
            investigator=getattr(state, "investigator", None),
            # Operator-initiated dispatch of a queued run — bypass the breaker
            # so the operator can drain the queue after a trip.
            enforce_cost_breaker=False,
        )
    )
    state.background_tasks.add(task)
    task.add_done_callback(state.background_tasks.discard)

    return RedirectResponse(url=f"/trigger/{run_id}", status_code=303)


# ---------------------------------------------------------------------------
# GET /trigger/{run_id} — progress page
# ---------------------------------------------------------------------------

@router.get("/trigger/{run_id}", response_class=HTMLResponse, name="trigger_progress")
async def trigger_progress(request: Request, run_id: str) -> HTMLResponse:
    sm = request.app.state.sessionmaker
    async with sm() as session:
        run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    return templates.TemplateResponse(
        request,
        "trigger_progress.html",
        {"run": run, "active": "trigger", "terminal": run.processing_status in _TERMINAL},
    )


# ---------------------------------------------------------------------------
# GET /trigger/{run_id}/partial — HTMX polling target
# ---------------------------------------------------------------------------

@router.get("/trigger/{run_id}/partial", response_class=HTMLResponse, name="trigger_progress_partial")
async def trigger_progress_partial(request: Request, run_id: str) -> HTMLResponse:
    """Returns the status fragment.  When the run reaches a terminal state,
    sets the HX-Redirect header so HTMX bounces the browser to the run detail."""
    sm = request.app.state.sessionmaker
    async with sm() as session:
        run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    is_terminal = run.processing_status in _TERMINAL
    headers: dict[str, str] = {}
    if is_terminal:
        headers["HX-Redirect"] = f"/runs/{run_id}"

    html_response = templates.TemplateResponse(
        request,
        "partials/_trigger_status.html",
        {"run": run, "terminal": is_terminal},
    )
    html_response.headers.update(headers)
    return html_response

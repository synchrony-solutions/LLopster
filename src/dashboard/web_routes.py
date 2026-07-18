"""HTML routes for the dashboard.

All routes are read-only. The dashboard has no /trigger endpoint — manual
triggers go through the agent's /trigger URL directly.  The nav bar shows a
link to the agent host when AGENT_URL is configured.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from src.api.auth import require_inbound_auth, resolve_api_token
from src.config import config
from src.dashboard.templating import templates
from src.db import repository as repo

router = APIRouter()


@router.get("/", include_in_schema=False)
async def root() -> RedirectResponse:
    return RedirectResponse(url="/runs", status_code=302)


def _build_filter_qs(*, service: str | None, alertname: str | None, limit: int, offset: int, q: str | None = None, status: str | None = None, operator_label: str | None = None) -> str:
    params: dict[str, str] = {}
    if service:
        params["service"] = service
    if alertname:
        params["alertname"] = alertname
    if status:
        params["status"] = status
    if operator_label:
        params["operator_label"] = operator_label
    if q:
        params["q"] = q
    params["limit"] = str(limit)
    params["offset"] = str(offset)
    return "?" + urlencode(params) if params else ""


async def _runs_context(
    request: Request,
    *,
    service: str | None,
    alertname: str | None,
    limit: int,
    offset: int,
    q: str | None = None,
    status: str | None = None,
    operator_label: str | None = None,
) -> dict:
    sm = request.app.state.sessionmaker
    async with sm() as session:
        rows = await repo.list_runs(
            session, limit=limit, offset=offset,
            service=service, alertname=alertname, status=status,
            operator_label=operator_label, q=q,
        )
        total = await repo.count_runs(
            session, service=service, alertname=alertname, status=status,
            operator_label=operator_label, q=q,
        )

    items = [
        {
            "id": r.id,
            "received_at": r.received_at,
            "alertname": r.alertname,
            "service": r.service,
            "severity": r.severity,
            "processing_status": r.processing_status,
            "confidence": r.parsed_confidence,
            "pr_opened": r.pr_opened,
            "pr_url": r.pr_url,
            "slack_notified": r.slack_notified,
            "trigger_source": r.trigger_source,
            "operator_label": r.operator_label,
        }
        for r in rows
    ]

    has_prev = offset > 0
    has_next = offset + limit < total
    prev_offset = max(0, offset - limit)
    next_offset = offset + limit

    return {
        "items": items,
        "total": total,
        "limit": limit,
        "offset": offset,
        "has_prev": has_prev,
        "has_next": has_next,
        "prev_href": f"/runs{_build_filter_qs(service=service, alertname=alertname, status=status, operator_label=operator_label, q=q, limit=limit, offset=prev_offset)}",
        "next_href": f"/runs{_build_filter_qs(service=service, alertname=alertname, status=status, operator_label=operator_label, q=q, limit=limit, offset=next_offset)}",
        "filter_qs": _build_filter_qs(service=service, alertname=alertname, status=status, operator_label=operator_label, q=q, limit=limit, offset=offset),
        "filters": {"service": service, "alertname": alertname, "status": status, "operator_label": operator_label, "q": q},
    }


@router.get("/runs", response_class=HTMLResponse, name="runs_list")
async def runs_list(
    request: Request,
    service: str | None = None,
    alertname: str | None = None,
    status: str | None = None,
    operator_label: str | None = None,
    q: str | None = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> HTMLResponse:
    from datetime import datetime

    ctx = await _runs_context(
        request, service=service, alertname=alertname, status=status,
        operator_label=operator_label, q=q, limit=limit, offset=offset,
    )

    sm = request.app.state.sessionmaker
    async with sm() as session:
        retention_raw = await repo.get_setting(session, "run_retention_days")
        last_pruned_raw = await repo.get_setting(session, "last_pruned_at")
    try:
        retention_days = int(retention_raw) if retention_raw else config.run_retention_days
    except ValueError:
        retention_days = config.run_retention_days
    last_pruned: datetime | None = None
    if last_pruned_raw:
        try:
            last_pruned = datetime.fromisoformat(last_pruned_raw)
        except ValueError:
            pass

    return templates.TemplateResponse(
        request,
        "runs_list.html",
        {
            **ctx,
            "active": "runs",
            "retention_days": retention_days,
            "last_pruned": last_pruned,
            "agent_url": config.agent_url,
        },
    )


@router.get("/runs/partial", response_class=HTMLResponse, name="runs_table_partial")
async def runs_table_partial(
    request: Request,
    service: str | None = None,
    alertname: str | None = None,
    status: str | None = None,
    operator_label: str | None = None,
    q: str | None = None,
    limit: int = Query(25, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> HTMLResponse:
    ctx = await _runs_context(
        request, service=service, alertname=alertname, status=status,
        operator_label=operator_label, q=q, limit=limit, offset=offset,
    )
    return templates.TemplateResponse(request, "partials/_runs_table.html", ctx)


@router.get("/runs/{run_id}", response_class=HTMLResponse, name="run_detail")
async def run_detail(request: Request, run_id: str) -> HTMLResponse:
    sm = request.app.state.sessionmaker
    async with sm() as session:
        run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return templates.TemplateResponse(
        request, "run_detail.html",
        {"run": run, "active": "runs", "agent_url": (config.agent_url or "").rstrip("/")},
    )


@router.post(
    "/runs/{run_id}/dispatch",
    name="run_dispatch",
    dependencies=[Depends(require_inbound_auth)],
)
async def run_dispatch(request: Request, run_id: str) -> RedirectResponse:
    """Server-side proxy of the agent's POST /trigger/dispatch/{run_id}.

    The Dispatch button on the run-detail page POSTs here instead of
    cross-origin to the agent directly, because:
      - the agent's in-cluster DNS name (``llopster-agent:8000``) isn't
        resolvable from the operator's browser, and
      - exposing the agent via Ingress just for one button would widen
        the attack surface.

    We forward via httpx over the in-cluster network, then bounce the
    operator back to the run detail page where polling picks up the
    state change. The agent always returns 303 to its own /trigger/{id}
    URL; we ignore that body and redirect to ours instead.
    """
    agent_url = (config.agent_url or "").rstrip("/")
    if not agent_url:
        raise HTTPException(
            status_code=500,
            detail="AGENT_URL is not set on the dashboard pod — cannot proxy dispatch",
        )

    # Forward the shared secret so the authenticated agent dispatch route
    # accepts this server-side proxy call (no-op header when auth is disabled).
    token = await resolve_api_token(request.app.state.sessionmaker)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    http = request.app.state.http
    try:
        resp = await http.post(
            f"{agent_url}/trigger/dispatch/{run_id}",
            headers=headers,
            timeout=10.0,
            # The agent replies with 303 → /trigger/{id}; we don't want
            # httpx to follow it (we're sending the operator to /runs/{id}
            # on the dashboard, not to the agent's trigger UI).
            follow_redirects=False,
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"could not reach agent at {agent_url}: {e}",
        )

    # 303 is the success contract from the agent dispatch endpoint.
    # 404/409 mean the run vanished or isn't actually queued anymore —
    # surface those so the operator sees what happened instead of a
    # silent redirect back to the same yellow banner.
    if resp.status_code not in (200, 303):
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"agent dispatch returned HTTP {resp.status_code}: {resp.text[:200]}",
        )

    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@router.post(
    "/runs/{run_id}/label",
    response_class=HTMLResponse,
    response_model=None,
    name="run_label",
    dependencies=[Depends(require_inbound_auth)],
)
async def run_label(
    request: Request,
    run_id: str,
    label: Annotated[str, Form()] = "",
    note: Annotated[str, Form()] = "",
) -> HTMLResponse | RedirectResponse:
    """Record an operator's ground-truth verdict on a run.

    The dashboard owns this write directly (no agent proxy): the label is
    operator-entered data, not agent-owned secrets, and it shares the DB with
    the agent. An empty `label` clears the verdict.

    Returns the swapped-in label fragment for HTMX requests; plain form POSTs
    (no JS) get a 303 back to the run-detail page so the feature degrades
    gracefully without HTMX.
    """
    sm = request.app.state.sessionmaker
    async with sm() as session:
        try:
            run = await repo.set_operator_label(session, run_id, label, note=note)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")

    if request.headers.get("HX-Request"):
        return templates.TemplateResponse(
            request, "partials/_operator_label.html", {"run": run},
        )
    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)


@router.get("/runs/{run_id}/partial", response_class=HTMLResponse, name="run_detail_partial")
async def run_detail_partial(request: Request, run_id: str) -> HTMLResponse:
    return await run_detail(request, run_id)


@router.get("/runs/{run_id}/stream", name="run_detail_stream")
async def run_detail_stream(request: Request, run_id: str) -> StreamingResponse:
    sm = request.app.state.sessionmaker
    TERMINAL = {"done", "failed", "skipped"}

    async def event_gen():
        while not await request.is_disconnected():
            async with sm() as session:
                run = await repo.get_run(session, run_id)
            if run is None:
                payload = json.dumps({"status": "not_found"})
                yield f"data: {payload}\n\n"
                return
            payload = json.dumps({
                "status": run.processing_status,
                "pr_url": run.pr_url,
                "slack_notified": run.slack_notified,
            })
            yield f"data: {payload}\n\n"
            if run.processing_status in TERMINAL:
                return
            await asyncio.sleep(2)

    return StreamingResponse(event_gen(), media_type="text/event-stream")


@router.get("/stats", response_class=HTMLResponse, name="stats_page")
async def stats_page(
    request: Request,
    days: int = Query(14, ge=1, le=90),
) -> HTMLResponse:
    from src.agent.cost import cache_savings_usd, compute_cost_usd

    sm = request.app.state.sessionmaker
    async with sm() as session:
        data = await repo.daily_stats(session, days=days)
        token_data = await repo.daily_token_stats(session, days=days)
        # Eval / ground-truth flywheel: surface the moat as a growing asset.
        latest_eval = await repo.latest_eval_run(session)
        eval_history = await repo.list_eval_runs(session, limit=20)
        operator_label_stats = await repo.operator_label_stats(session)

    # Pre-compute cost per day so the template stays markup-only.
    # Spend is dominated by output tokens; we surface the breakdown so
    # operators can spot regressions in either dimension.
    model = config.anthropic_model
    cost_rows = []
    for row in token_data:
        cost_rows.append({
            "day": row["day"],
            "input_cost": round(compute_cost_usd(
                input_tokens=row["input_tokens"],
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=0,
                model=model,
            ), 4),
            "output_cost": round(compute_cost_usd(
                input_tokens=0,
                output_tokens=row["output_tokens"],
                cache_read_tokens=0,
                cache_creation_tokens=0,
                model=model,
            ), 4),
            "cache_read_cost": round(compute_cost_usd(
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=row["cache_read_tokens"],
                cache_creation_tokens=0,
                model=model,
            ), 4),
            "cache_write_cost": round(compute_cost_usd(
                input_tokens=0,
                output_tokens=0,
                cache_read_tokens=0,
                cache_creation_tokens=row["cache_creation_tokens"],
                model=model,
            ), 4),
            "run_count": row["run_count"],
            "cache_savings": round(cache_savings_usd(
                cache_read_tokens=row["cache_read_tokens"], model=model,
            ), 4),
        })

    total_cost = sum(
        r["input_cost"] + r["output_cost"] + r["cache_read_cost"] + r["cache_write_cost"]
        for r in cost_rows
    )
    total_runs = sum(r["run_count"] for r in cost_rows)
    avg_per_run = (total_cost / total_runs) if total_runs else 0.0
    total_cache_savings = sum(r["cache_savings"] for r in cost_rows)

    # Cache hit rate (read tokens / total input). The "value of caching"
    # number above is the dollar version of this — both are useful.
    total_input = sum(r["input_tokens"] for r in token_data)
    total_cache_read = sum(r["cache_read_tokens"] for r in token_data)
    cache_hit_rate = (
        total_cache_read / (total_cache_read + total_input)
        if (total_cache_read + total_input) > 0 else 0.0
    )

    return templates.TemplateResponse(
        request, "stats.html",
        {
            "data": data,
            "cost_rows": cost_rows,
            "days": days,
            "active": "stats",
            "total_cost": round(total_cost, 2),
            "avg_per_run": round(avg_per_run, 4),
            "total_cache_savings": round(total_cache_savings, 2),
            "cache_hit_rate_pct": round(cache_hit_rate * 100, 1),
            "model": model,
            "latest_eval": latest_eval,
            # oldest → newest so the trend reads left-to-right
            "eval_history": list(reversed(eval_history)),
            "operator_label_stats": operator_label_stats,
        },
    )


@router.get("/diagnostics", response_class=HTMLResponse, name="diagnostics_page")
async def diagnostics_page(request: Request) -> HTMLResponse:
    """System diagnostics — readable even when the agent is in crash loop."""
    from datetime import datetime, timezone
    import os

    sm = request.app.state.sessionmaker
    async with sm() as session:
        settings = await repo.get_all_settings(session)
        # Pull the most recent 50 runs to give the failures table a useful
        # window without scanning the whole table.
        recent_runs = await repo.list_runs(session, limit=50)
        total_runs = await repo.count_runs(session)

    now = datetime.now(timezone.utc)
    recent_failures = [r for r in recent_runs if r.processing_status == "failed"]

    # Agent heartbeat age
    heartbeat_raw = settings.get("agent_last_heartbeat")
    heartbeat_age_s: float | None = None
    if heartbeat_raw:
        try:
            last = datetime.fromisoformat(heartbeat_raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            heartbeat_age_s = (now - last).total_seconds()
        except ValueError:
            pass

    # DB connectivity — if we got here, it works
    db_ok = True
    db_url_display = config.database_url.split("@")[-1] if "@" in config.database_url else config.database_url

    # k8s pod info (populated in-cluster via Downward API env vars)
    pod_info = {
        "pod_name": os.getenv("POD_NAME", "—"),
        "node_name": os.getenv("NODE_NAME", "—"),
        "namespace": os.getenv("POD_NAMESPACE", "—"),
    }

    return templates.TemplateResponse(
        request,
        "diagnostics.html",
        {
            "active": "diagnostics",
            "settings": settings,
            "total_runs": total_runs,
            "recent_failures": recent_failures,
            "heartbeat_raw": heartbeat_raw,
            "heartbeat_age_s": heartbeat_age_s,
            "db_ok": db_ok,
            "db_url_display": db_url_display,
            "pod_info": pod_info,
            "agent_url": config.agent_url,
        },
    )

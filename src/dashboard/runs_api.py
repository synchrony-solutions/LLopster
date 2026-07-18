"""Read-only JSON API for the dashboard.

Identical surface to src/api/runs_api.py but imported here so the dashboard
is self-contained without any dependency on src/api/.

Also exposes GET /api/agent-status — a lightweight HTMX poll target that
reads the agent heartbeat from the settings table and returns an HTML fragment
the nav bar swaps in every 30 seconds.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from src.db import repository as repo
from src.db.models import Run

router = APIRouter(prefix="/api", tags=["runs"])


# ---------------------------------------------------------------------------
# Response shapes (same as src/api/runs_api.py)
# ---------------------------------------------------------------------------

class RunSummary(BaseModel):
    id: str
    received_at: datetime
    alertname: str
    service: str | None
    severity: str | None
    status: str
    processing_status: str
    confidence: int | None
    pr_opened: bool
    pr_url: str | None
    slack_notified: bool
    trigger_source: str
    log_line_count: int
    metric_sample_count: int
    error_message: str | None
    operator_label: str | None = None


class RunDetail(BaseModel):
    id: str
    fingerprint: str | None
    received_at: datetime
    created_at: datetime
    alertname: str
    service: str | None
    severity: str | None
    status: str
    alert_payload_json: dict[str, Any]
    logql_used: str | None
    promql_used: str | None
    lookback_minutes: int | None
    log_line_count: int
    metric_sample_count: int
    collection_errors_json: list[str]
    log_lines_json: list[dict[str, Any]]
    metric_samples_json: list[dict[str, Any]]
    triage_decision: str | None = None
    triage_confidence: int | None = None
    triage_reasoning: str | None = None
    triage_model: str | None = None
    triage_input_tokens: int | None = None
    triage_output_tokens: int | None = None
    triage_cache_read_tokens: int | None = None
    triage_cache_creation_tokens: int | None = None
    investigation_root_cause: str | None = None
    investigation_affected_files_json: list[str] | None = None
    investigation_confidence: int | None = None
    investigation_reasoning: str | None = None
    investigation_response_text: str | None = None
    investigation_model: str | None = None
    investigation_input_tokens: int | None = None
    investigation_output_tokens: int | None = None
    investigation_cache_read_tokens: int | None = None
    investigation_cache_creation_tokens: int | None = None
    investigation_latency_ms: int | None = None
    model: str | None
    input_tokens: int | None
    output_tokens: int | None
    cache_read_tokens: int | None
    cache_creation_tokens: int | None
    llm_latency_ms: int | None
    llm_response_text: str | None
    parsed_root_cause: str | None
    parsed_diff: str | None
    parsed_confidence: int | None
    parsed_confidence_reason: str | None
    synthesis_used_narrowed_context: bool | None = None
    pr_opened: bool
    pr_url: str | None
    pr_number: int | None
    pr_branch: str | None
    pr_skip_reason: str | None
    slack_notified: bool
    slack_skip_reason: str | None
    trigger_source: str
    triggered_by_user_id: str | None
    processing_status: str
    error_message: str | None
    operator_label: str | None = None
    operator_label_note: str | None = None
    operator_labeled_at: datetime | None = None
    operator_labeled_by: str | None = None


class RunsList(BaseModel):
    items: list[RunSummary]
    total: int
    limit: int
    offset: int


def _to_summary(run: Run) -> RunSummary:
    return RunSummary(
        id=run.id,
        received_at=run.received_at,
        alertname=run.alertname,
        service=run.service,
        severity=run.severity,
        status=run.status,
        processing_status=run.processing_status,
        confidence=run.parsed_confidence,
        pr_opened=run.pr_opened,
        pr_url=run.pr_url,
        slack_notified=run.slack_notified,
        trigger_source=run.trigger_source,
        log_line_count=run.log_line_count,
        metric_sample_count=run.metric_sample_count,
        error_message=run.error_message,
        operator_label=run.operator_label,
    )


def _to_detail(run: Run) -> RunDetail:
    return RunDetail.model_validate(run, from_attributes=True)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/runs", response_model=RunsList)
async def list_runs_endpoint(
    request: Request,
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    service: str | None = None,
    alertname: str | None = None,
    operator_label: str | None = None,
    q: str | None = None,
) -> RunsList:
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        runs = await repo.list_runs(
            session, limit=limit, offset=offset,
            service=service, alertname=alertname, operator_label=operator_label, q=q,
        )
        total = await repo.count_runs(
            session, service=service, alertname=alertname, operator_label=operator_label, q=q,
        )
    return RunsList(
        items=[_to_summary(r) for r in runs],
        total=total,
        limit=limit,
        offset=offset,
    )


@router.get("/runs/{run_id}", response_model=RunDetail)
async def get_run_endpoint(request: Request, run_id: str) -> RunDetail:
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        run = await repo.get_run(session, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"run {run_id} not found")
    return _to_detail(run)


@router.get("/stats")
async def stats_endpoint(request: Request, days: int = Query(14, ge=1, le=90)) -> dict:
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        rows = await repo.daily_stats(session, days=days)
    return {"days": days, "data": rows}


@router.get("/eval/summary")
async def eval_summary_endpoint(request: Request, limit: int = Query(30, ge=1, le=200)) -> dict:
    """Eval / ground-truth flywheel summary — the moat made visible.

    Two compounding datasets in one payload:
      * `harness` — the latest replay of the frozen scenario corpus plus the
        pass-rate trend over the last N eval runs (the regression baseline).
      * `operator_labels` — the distribution + human pass-rate of operator
        verdicts on real runs (this is what finally *consumes*
        `Run.operator_label`).
    """
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        latest = await repo.latest_eval_run(session)
        history = await repo.list_eval_runs(session, limit=limit)
        label_stats = await repo.operator_label_stats(session)

    def _eval_run_dict(er) -> dict:  # noqa: ANN001
        return {
            "id": er.id,
            "created_at": er.created_at.isoformat() if er.created_at else None,
            "corpus_version": er.corpus_version,
            "model": er.model,
            "scenario_count": er.scenario_count,
            "correct_count": er.correct_count,
            "partial_count": er.partial_count,
            "wrong_count": er.wrong_count,
            "pass_rate": er.pass_rate,
        }

    return {
        "harness": {
            "latest": (
                {**_eval_run_dict(latest), "results": latest.results_json}
                if latest is not None else None
            ),
            # oldest → newest so a sparkline/line reads left-to-right
            "trend": [_eval_run_dict(er) for er in reversed(list(history))],
        },
        "operator_labels": label_stats,
    }


# ---------------------------------------------------------------------------
# Agent heartbeat status — HTMX poll target for the nav bar
# ---------------------------------------------------------------------------

_HEARTBEAT_KEY = "agent_last_heartbeat"
_STALE_WARNING_SECONDS = 120   # 2 min → yellow
_STALE_DOWN_SECONDS = 600      # 10 min → red


@router.get("/agent-status", response_class=HTMLResponse, include_in_schema=False)
async def agent_status(request: Request) -> HTMLResponse:
    """Returns an HTML pill fragment the nav bar refreshes every 30s.

    Green  — heartbeat < 2 min old
    Yellow — heartbeat 2–10 min old (agent degraded / slow loop)
    Red    — heartbeat > 10 min old or no heartbeat recorded
    """
    sessionmaker = request.app.state.sessionmaker
    async with sessionmaker() as session:
        raw = await repo.get_setting(session, _HEARTBEAT_KEY)

    now = datetime.now(timezone.utc)

    if raw is None:
        html = '<span class="agent-status agent-status-unknown" title="No heartbeat recorded — agent may not have started yet">⚪ Agent unknown</span>'
    else:
        try:
            last = datetime.fromisoformat(raw)
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            age_s = (now - last).total_seconds()
            if age_s < _STALE_WARNING_SECONDS:
                ago = f"{int(age_s)}s ago"
                html = f'<span class="agent-status agent-status-ok" title="Agent last heartbeat {ago}">🟢 Agent healthy</span>'
            elif age_s < _STALE_DOWN_SECONDS:
                mins = int(age_s // 60)
                html = f'<span class="agent-status agent-status-warn" title="Agent heartbeat is stale ({mins}m ago) — may be overloaded or restarting">🟡 Agent degraded ({mins}m)</span>'
            else:
                mins = int(age_s // 60)
                html = f'<span class="agent-status agent-status-down" title="Agent unreachable — last heartbeat {mins}m ago">🔴 Agent unreachable ({mins}m)</span>'
        except ValueError:
            html = '<span class="agent-status agent-status-unknown">⚪ Agent unknown</span>'

    return HTMLResponse(html)

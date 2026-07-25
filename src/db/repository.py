"""Data access for Run rows.

Each function takes an `AsyncSession` and is awaitable; commit policy is the
caller's responsibility (the processor commits at phase boundaries so a
crash mid-pipeline still leaves observable state in the row).
"""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Sequence

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.agent.alert_handler import ParsedAlert
from src.agent.context_collector import AlertContext
from src.agent.dedup import compute_dedup_key
from src.agent.investigator import Investigation
from src.agent.patch_generator import PatchProposal
from src.agent.triage import TriageDecision
from src.db.models import OPERATOR_LABELS, EvalRun, Run, Setting


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------

def _serialize(obj: Any) -> Any:
    """Convert dataclasses / datetimes to JSON-friendly values."""
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: _serialize(v) for k, v in asdict(obj).items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if hasattr(obj, "isoformat"):  # datetime
        return obj.isoformat()
    return obj


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

async def create_run_from_alert(
    session: AsyncSession,
    alert: ParsedAlert,
    raw_payload: dict[str, Any],
    trigger_source: str = "alertmanager",
    triggered_by_user_id: str | None = None,
) -> Run:
    """Insert a fresh Run in `pending` state and return it."""
    run = Run(
        fingerprint=alert.fingerprint,
        dedup_key=compute_dedup_key(alert),
        alertname=alert.alertname,
        service=alert.service,
        severity=alert.severity,
        status=alert.status,
        alert_payload_json=raw_payload,
        trigger_source=trigger_source,
        triggered_by_user_id=triggered_by_user_id,
        processing_status="pending",
    )
    session.add(run)
    await session.commit()
    await session.refresh(run)
    return run


async def update_status(
    session: AsyncSession,
    run_id: str,
    status: str,
    error: str | None = None,
) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.processing_status = status
    if error is not None:
        run.error_message = error
    await session.commit()


async def record_triage(
    session: AsyncSession,
    run_id: str,
    decision: TriageDecision,
) -> None:
    """Persist the triage gate's call + Haiku token usage.

    Called whether triage proceeded or skipped — both decisions are
    operationally interesting (the dashboard surfaces the reasoning so
    operators can spot mis-skips).
    """
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.triage_decision = decision.decision_label
    run.triage_confidence = decision.confidence
    run.triage_reasoning = decision.reasoning
    run.triage_model = decision.model
    run.triage_input_tokens = decision.input_tokens
    run.triage_output_tokens = decision.output_tokens
    run.triage_cache_read_tokens = decision.cache_read_tokens
    run.triage_cache_creation_tokens = decision.cache_creation_tokens
    await session.commit()


async def record_investigation(
    session: AsyncSession,
    run_id: str,
    investigation: Investigation,
    latency_ms: int,
) -> None:
    """Persist the Sonnet investigation output + token usage.

    In Phase B this is purely informational — the row stores it so the
    dashboard can show it and so Phase C can read `affected_files` to
    narrow Opus's prompt. Token columns let the stats page split spend
    by stage.
    """
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.investigation_root_cause = investigation.root_cause
    run.investigation_affected_files_json = list(investigation.affected_files)
    run.investigation_confidence = investigation.confidence
    run.investigation_reasoning = investigation.reasoning
    run.investigation_response_text = investigation.response_text
    run.investigation_model = investigation.model
    run.investigation_input_tokens = investigation.input_tokens
    run.investigation_output_tokens = investigation.output_tokens
    run.investigation_cache_read_tokens = investigation.cache_read_tokens
    run.investigation_cache_creation_tokens = investigation.cache_creation_tokens
    run.investigation_latency_ms = latency_ms
    await session.commit()


async def record_collected_context(
    session: AsyncSession,
    run_id: str,
    ctx: AlertContext,
    lookback_minutes: int,
) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.logql_used = ctx.queries_used.get("logql")
    run.promql_used = ctx.queries_used.get("promql")
    run.lookback_minutes = lookback_minutes
    run.log_line_count = len(ctx.log_lines)
    run.metric_sample_count = len(ctx.metric_samples)
    run.collection_errors_json = list(ctx.errors)
    run.log_lines_json = [_serialize(l) for l in ctx.log_lines]
    run.metric_samples_json = [_serialize(s) for s in ctx.metric_samples]
    await session.commit()


async def record_llm_response(
    session: AsyncSession,
    run_id: str,
    proposal: PatchProposal,
    latency_ms: int,
    parsed_root_cause: str | None = None,
    parsed_diff: str | None = None,
) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.model = proposal.model
    run.input_tokens = proposal.input_tokens
    run.output_tokens = proposal.output_tokens
    run.cache_read_tokens = proposal.cache_read_tokens
    run.cache_creation_tokens = proposal.cache_creation_tokens
    run.llm_latency_ms = latency_ms
    run.llm_response_text = proposal.text
    run.parsed_root_cause = parsed_root_cause
    run.parsed_diff = parsed_diff
    run.parsed_confidence = proposal.confidence
    run.parsed_confidence_reason = proposal.confidence_reason
    # Phase C: which mode the synthesis call actually ran in. Written
    # for every run that reaches Opus so the dashboard can distinguish
    # "Phase C narrowing fired" from "fallback to full codebase".
    run.synthesis_used_narrowed_context = proposal.used_narrowed_context
    await session.commit()


async def record_pr(
    session: AsyncSession,
    run_id: str,
    *,
    pr_url: str | None = None,
    pr_number: int | None = None,
    pr_branch: str | None = None,
    skip_reason: str | None = None,
) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.pr_opened = pr_url is not None
    run.pr_url = pr_url
    run.pr_number = pr_number
    run.pr_branch = pr_branch
    run.pr_skip_reason = skip_reason
    await session.commit()


async def record_notification(
    session: AsyncSession,
    run_id: str,
    *,
    notified: bool,
    skip_reason: str | None = None,
) -> None:
    # Stored on the slack_* columns for every provider (Slack/Teams) — the
    # column names predate multi-provider support; renaming them would be a
    # migration for no functional gain.
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.slack_notified = notified
    run.slack_skip_reason = skip_reason
    await session.commit()


async def set_operator_label(
    session: AsyncSession,
    run_id: str,
    label: str | None,
    *,
    note: str | None = None,
    labeled_by: str | None = None,
) -> Run | None:
    """Record (or clear) an operator's ground-truth verdict on a run.

    `label` must be one of `OPERATOR_LABELS`, or `None`/empty to clear the
    label. An invalid label raises `ValueError` — the caller (the dashboard
    route) is expected to surface that as a 400 rather than silently storing
    garbage into the flywheel dataset.

    Returns the updated Run, or None if `run_id` doesn't exist.
    """
    normalized = (label or "").strip().lower() or None
    if normalized is not None and normalized not in OPERATOR_LABELS:
        raise ValueError(
            f"invalid operator label {label!r}; expected one of {OPERATOR_LABELS}"
        )

    run = await session.get(Run, run_id)
    if run is None:
        return None

    run.operator_label = normalized
    if normalized is None:
        # Clearing the label clears its metadata too — a blank verdict with a
        # leftover note/timestamp would be misleading in the dataset.
        run.operator_label_note = None
        run.operator_labeled_at = None
        run.operator_labeled_by = None
    else:
        from datetime import datetime, timezone

        run.operator_label_note = (note or "").strip() or None
        run.operator_labeled_at = datetime.now(timezone.utc)
        run.operator_labeled_by = labeled_by
    await session.commit()
    return run


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def _apply_operator_label_filter(stmt, operator_label: str):
    """Filter a Run query by operator label.

    Accepts a canonical label from `OPERATOR_LABELS`, or the pseudo-values
    ``labeled`` (any verdict set) / ``unlabeled`` (no verdict yet) — the eval
    harness wants "all labeled runs" without enumerating each label.
    """
    key = operator_label.strip().lower()
    if key == "labeled":
        return stmt.where(Run.operator_label.isnot(None))
    if key == "unlabeled":
        return stmt.where(Run.operator_label.is_(None))
    return stmt.where(Run.operator_label == key)


async def list_runs(
    session: AsyncSession,
    *,
    limit: int = 50,
    offset: int = 0,
    service: str | None = None,
    alertname: str | None = None,
    status: str | None = None,
    operator_label: str | None = None,
    q: str | None = None,
) -> Sequence[Run]:
    stmt = select(Run).order_by(desc(Run.created_at))
    if service:
        stmt = stmt.where(Run.service == service)
    if alertname:
        stmt = stmt.where(Run.alertname == alertname)
    if status:
        stmt = stmt.where(Run.processing_status == status)
    if operator_label:
        stmt = _apply_operator_label_filter(stmt, operator_label)
    if q:
        from sqlalchemy import func, or_
        pat = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Run.alertname).like(pat),
                func.lower(Run.service).like(pat),
                func.lower(Run.llm_response_text).like(pat),
            )
        )
    stmt = stmt.limit(limit).offset(offset)
    result = await session.execute(stmt)
    return result.scalars().all()


async def count_runs(
    session: AsyncSession,
    *,
    service: str | None = None,
    alertname: str | None = None,
    status: str | None = None,
    operator_label: str | None = None,
    q: str | None = None,
) -> int:
    from sqlalchemy import func, or_
    stmt = select(func.count(Run.id))
    if service:
        stmt = stmt.where(Run.service == service)
    if alertname:
        stmt = stmt.where(Run.alertname == alertname)
    if status:
        stmt = stmt.where(Run.processing_status == status)
    if operator_label:
        stmt = _apply_operator_label_filter(stmt, operator_label)
    if q:
        pat = f"%{q.lower()}%"
        stmt = stmt.where(
            or_(
                func.lower(Run.alertname).like(pat),
                func.lower(Run.service).like(pat),
                func.lower(Run.llm_response_text).like(pat),
            )
        )
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def get_run(session: AsyncSession, run_id: str) -> Run | None:
    return await session.get(Run, run_id)


async def count_runs_since(session: AsyncSession, since: datetime) -> int:
    """Number of Run rows created at or after `since`. Used by the cost
    circuit breaker's runs/hour ceiling."""
    from sqlalchemy import func

    stmt = select(func.count(Run.id)).where(Run.created_at >= since)
    result = await session.execute(stmt)
    return int(result.scalar() or 0)


async def count_runs_by_status(session: AsyncSession) -> dict[str, int]:
    """Map of processing_status → run count across all runs.

    Backs the `/metrics` endpoint's per-stage series. DB-backed (not in-process
    counters) so the numbers survive an agent restart — the fire-and-forget
    pipeline would otherwise reset in-memory counters on every restart."""
    from sqlalchemy import func

    stmt = select(Run.processing_status, func.count(Run.id)).group_by(Run.processing_status)
    result = await session.execute(stmt)
    return {status: int(count) for status, count in result.all()}


async def synthesis_token_rows_since(
    session: AsyncSession, since: datetime
) -> list[tuple[str | None, int, int, int, int]]:
    """Per-run synthesis (Opus) model + token counts for runs created at or
    after `since`, restricted to runs that actually hit the LLM (non-NULL
    input_tokens). Returned as tuples the cost breaker feeds into
    cost.compute_cost_usd — synthesis is the dominant spend, matching the
    columns daily_token_stats already sums for the dashboard."""
    stmt = (
        select(
            Run.model,
            Run.input_tokens,
            Run.output_tokens,
            Run.cache_read_tokens,
            Run.cache_creation_tokens,
        )
        .where(Run.created_at >= since)
        .where(Run.input_tokens.isnot(None))
    )
    result = await session.execute(stmt)
    return [
        (row.model, row.input_tokens or 0, row.output_tokens or 0,
         row.cache_read_tokens or 0, row.cache_creation_tokens or 0)
        for row in result.all()
    ]


async def delete_runs_older_than(session: AsyncSession, days: int) -> int:
    """Delete every Run whose `created_at` is older than `days` days ago.

    Returns the number of rows removed. Caller should guard against
    `days <= 0` — this function does not interpret 0 as "disabled" because
    that would make the function impossible to use to wipe everything in a
    test scenario. The pruner background task handles the disabled check.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import delete as sql_delete

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    result = await session.execute(sql_delete(Run).where(Run.created_at < cutoff))
    await session.commit()
    return result.rowcount or 0


# ---------------------------------------------------------------------------
# Eval / ground-truth flywheel
# ---------------------------------------------------------------------------

async def record_eval_run(
    session: AsyncSession,
    *,
    scenario_count: int,
    correct_count: int,
    partial_count: int,
    wrong_count: int,
    results: list[dict[str, Any]],
    corpus_version: str | None = None,
    model: str | None = None,
    trigger_source: str = "cli",
    note: str | None = None,
) -> EvalRun:
    """Persist one execution of the scenario corpus. `pass_rate` is derived
    here (correct / scenarios) so every caller computes it the same way."""
    pass_rate = (correct_count / scenario_count) if scenario_count else 0.0
    eval_run = EvalRun(
        corpus_version=corpus_version,
        model=model,
        trigger_source=trigger_source,
        scenario_count=scenario_count,
        correct_count=correct_count,
        partial_count=partial_count,
        wrong_count=wrong_count,
        pass_rate=pass_rate,
        results_json=[_serialize(r) for r in results],
        note=note,
    )
    session.add(eval_run)
    await session.commit()
    await session.refresh(eval_run)
    return eval_run


async def list_eval_runs(
    session: AsyncSession, *, limit: int = 30
) -> Sequence[EvalRun]:
    """Most-recent eval runs first — the pass-rate trend for the stats panel."""
    stmt = select(EvalRun).order_by(desc(EvalRun.created_at)).limit(limit)
    result = await session.execute(stmt)
    return result.scalars().all()


async def latest_eval_run(session: AsyncSession) -> EvalRun | None:
    stmt = select(EvalRun).order_by(desc(EvalRun.created_at)).limit(1)
    result = await session.execute(stmt)
    return result.scalars().first()


async def operator_label_stats(session: AsyncSession) -> dict[str, Any]:
    """Aggregate operator ground-truth labels across all real runs.

    This is what finally *consumes* `Run.operator_label` for the moat
    narrative: the distribution plus a human-judged pass-rate
    (correct / labeled), surfaced as a growing dataset. `partial` counts as
    a half-pass so the rate isn't artificially binary.
    """
    from sqlalchemy import func

    stmt = (
        select(Run.operator_label, func.count(Run.id))
        .where(Run.operator_label.isnot(None))
        .group_by(Run.operator_label)
    )
    result = await session.execute(stmt)
    counts = {label: int(cnt) for label, cnt in result.all()}

    labeled = sum(counts.values())
    correct = counts.get("correct", 0)
    partial = counts.get("partial", 0)
    pass_rate = ((correct + 0.5 * partial) / labeled) if labeled else 0.0
    return {
        "counts": counts,
        "labeled_total": labeled,
        "correct": correct,
        "partial": partial,
        "wrong": counts.get("wrong", 0),
        "na": counts.get("na", 0),
        "pass_rate": pass_rate,
    }


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

async def get_setting(session: AsyncSession, key: str, default: str | None = None) -> str | None:
    row = await session.get(Setting, key)
    return row.value if row is not None else default


async def set_setting(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value
    await session.commit()


async def get_all_settings(session: AsyncSession) -> dict[str, str]:
    result = await session.execute(select(Setting))
    return {row.key: (row.value or "") for row in result.scalars().all()}


async def list_queued_runs(session: AsyncSession, *, limit: int = 50) -> Sequence[Run]:
    """Runs parked by the manual-mode kill switch, newest first."""
    stmt = (
        select(Run)
        .where(Run.processing_status == "queued")
        .order_by(desc(Run.created_at))
        .limit(limit)
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def list_open_prs(session: AsyncSession) -> Sequence[Run]:
    """Return all runs that have an open PR (pr_opened=True, pr_status not closed/merged)."""
    stmt = (
        select(Run)
        .where(Run.pr_opened.is_(True))
        .where(Run.pr_number.isnot(None))
        .where(Run.pr_status.not_in(["closed", "merged"]))
    )
    result = await session.execute(stmt)
    return result.scalars().all()


async def update_pr_status(session: AsyncSession, run_id: str, pr_status: str) -> None:
    run = await session.get(Run, run_id)
    if run is None:
        return
    run.pr_status = pr_status
    # Stamp the merge timestamp the first time we observe the merge so the
    # post-deploy grace window has something to measure from. We don't
    # overwrite it on subsequent observations — the first one is the truth.
    if pr_status == "merged" and run.pr_merged_at is None:
        from datetime import datetime, timezone
        run.pr_merged_at = datetime.now(timezone.utc)
    await session.commit()


async def daily_token_stats(session: AsyncSession, *, days: int = 14) -> list[dict]:
    """Per-day sum of input/output/cache tokens for the last N days.

    The dashboard combines this with src.agent.cost.compute_cost_usd to
    show daily spend — separated from daily_stats() so we don't pay the
    sum() cost on the existing chart and so the cost calc lives next to
    the pricing table rather than smeared across SQL.
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from src.config import config as _cfg

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    if _cfg.database_url.startswith("postgresql"):
        day_col = func.to_char(Run.created_at, "YYYY-MM-DD")
    else:
        day_col = func.strftime("%Y-%m-%d", Run.created_at)

    stmt = (
        select(
            day_col.label("day"),
            func.coalesce(func.sum(Run.input_tokens), 0).label("input_tokens"),
            func.coalesce(func.sum(Run.output_tokens), 0).label("output_tokens"),
            func.coalesce(func.sum(Run.cache_read_tokens), 0).label("cache_read_tokens"),
            func.coalesce(func.sum(Run.cache_creation_tokens), 0).label("cache_creation_tokens"),
            func.count(Run.id).label("run_count"),
        )
        .where(Run.created_at >= cutoff)
        # Only rows that actually hit the LLM — skipped / queued runs
        # have all-NULL token columns and would otherwise bloat the
        # "run_count" denominator and pull "avg cost per run" toward 0.
        .where(Run.input_tokens.isnot(None))
        .group_by("day")
        .order_by("day")
    )
    result = await session.execute(stmt)
    return [
        {
            "day": str(row.day),
            "input_tokens": int(row.input_tokens or 0),
            "output_tokens": int(row.output_tokens or 0),
            "cache_read_tokens": int(row.cache_read_tokens or 0),
            "cache_creation_tokens": int(row.cache_creation_tokens or 0),
            "run_count": int(row.run_count or 0),
        }
        for row in result.all()
    ]


async def daily_stats(session: AsyncSession, *, days: int = 14) -> list[dict]:
    """Return per-day counts of runs grouped by processing_status for the last N days.

    Uses a dialect-aware day-truncation expression so the query works on both
    SQLite (local dev / tests) and PostgreSQL (production).
    """
    from datetime import datetime, timedelta, timezone

    from sqlalchemy import func

    from src.config import config as _cfg

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    # PostgreSQL and SQLite use different date-to-string functions.
    if _cfg.database_url.startswith("postgresql"):
        day_col = func.to_char(Run.created_at, "YYYY-MM-DD")
    else:
        day_col = func.strftime("%Y-%m-%d", Run.created_at)

    stmt = (
        select(
            day_col.label("day"),
            Run.processing_status,
            func.count(Run.id).label("cnt"),
        )
        .where(Run.created_at >= cutoff)
        .group_by("day", Run.processing_status)
        .order_by("day")
    )
    result = await session.execute(stmt)
    rows = result.all()
    # Pivot into [{day, done, failed, skipped, other}]
    by_day: dict[str, dict] = {}
    for day, status, cnt in rows:
        key = str(day)  # date objects (postgres) → string; strings (sqlite) pass through
        d = by_day.setdefault(key, {"day": key, "done": 0, "failed": 0, "skipped": 0, "other": 0})
        if status in ("done", "failed", "skipped"):
            d[status] += cnt
        else:
            d["other"] += cnt
    return sorted(by_day.values(), key=lambda x: x["day"])

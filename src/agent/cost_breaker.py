"""Automatic cost circuit breaker.

``src/agent/cost.py`` *reports* spend; this module *stops* it. When the number
of runs created in the trailing hour, or the estimated synthesis spend in the
trailing day, reaches an operator-set ceiling, the breaker trips: the caller
flips ``processing_mode`` to ``manual`` so subsequent alerts park at ``queued``
for operator review instead of driving more LLM calls, and short-circuits the
current run before any LLM call.

Both ceilings are settings-backed (DB override → env) and **disabled by default**
(0 = off), mirroring ``run_retention_days``. Operators opt in with a value tuned
to their alert volume / budget.

Fail-safe contract: any error while evaluating the breaker is logged and treated
as "do not trip". A telemetry/DB hiccup must degrade to *less* interference, never
block a real incident and never crash the pipeline.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.agent import cost as cost_mod
from src.config import config
from src.db import repository as repo

log = logging.getLogger("llopster.cost_breaker")

RUNS_PER_HOUR_SETTING = "max_runs_per_hour"
USD_PER_DAY_SETTING = "max_usd_per_day"


@dataclass(frozen=True)
class BreakerDecision:
    """Result of a breaker check. ``reason`` is set only when ``tripped``."""

    tripped: bool
    reason: str | None = None


async def _resolve_runs_ceiling(session) -> int:
    raw = await repo.get_setting(session, RUNS_PER_HOUR_SETTING)
    if raw is not None and raw.strip():
        try:
            return int(raw)
        except ValueError:
            log.warning("cost breaker: bad %s=%r; using env default", RUNS_PER_HOUR_SETTING, raw)
    return config.max_runs_per_hour


async def _resolve_usd_ceiling(session) -> float:
    raw = await repo.get_setting(session, USD_PER_DAY_SETTING)
    if raw is not None and raw.strip():
        try:
            return float(raw)
        except ValueError:
            log.warning("cost breaker: bad %s=%r; using env default", USD_PER_DAY_SETTING, raw)
    return config.max_usd_per_day


def log_cost_breaker_status(service_name: str) -> None:
    """Log the active cost-breaker ceilings once at startup.

    Only the env/default values are checked here (a Setting-table override can
    still change them at runtime). The point is to make the out-of-the-box spend
    cap visible and to nudge operators to tune it — the defaults are a safety
    net, not a budget."""
    runs = config.max_runs_per_hour
    usd = config.max_usd_per_day
    if runs <= 0 and usd <= 0:
        log.warning(
            "%s: cost breaker DISABLED — no runs/hour or USD/day ceiling set. "
            "There is NO automatic spend cap. Set MAX_RUNS_PER_HOUR / "
            "MAX_USD_PER_DAY (or the matching settings) before running against "
            "real alert volume.",
            service_name,
        )
        return
    log.warning(
        "%s: cost breaker active with DEFAULT-tier ceilings "
        "(max_runs_per_hour=%s, max_usd_per_day=$%s). These are a conservative "
        "safety net, not a tuned budget — RAISE or lower them to match your "
        "alert volume and spend via MAX_RUNS_PER_HOUR / MAX_USD_PER_DAY or the "
        "dashboard Settings. When a ceiling is reached the agent flips to manual "
        "mode and parks new alerts at `queued`.",
        service_name, runs or "off", usd or "off",
    )


async def check_cost_breaker(sessionmaker, *, now: datetime | None = None) -> BreakerDecision:
    """Return whether a ceiling has been reached.

    Never raises — on any error it logs and returns ``tripped=False`` so the
    pipeline continues (fail safe: a broken breaker must not block incidents).
    """
    now = now or datetime.now(timezone.utc)
    try:
        async with sessionmaker() as session:
            max_runs = await _resolve_runs_ceiling(session)
            max_usd = await _resolve_usd_ceiling(session)

            if max_runs > 0:
                count = await repo.count_runs_since(session, now - timedelta(hours=1))
                if count >= max_runs:
                    return BreakerDecision(
                        True,
                        f"cost breaker: {count} runs in the last hour "
                        f">= ceiling {max_runs}/hour",
                    )

            if max_usd > 0:
                rows = await repo.synthesis_token_rows_since(session, now - timedelta(days=1))
                spend = sum(
                    cost_mod.compute_cost_usd(
                        input_tokens=inp,
                        output_tokens=out,
                        cache_read_tokens=cr,
                        cache_creation_tokens=cc,
                        model=model,
                    )
                    for (model, inp, out, cr, cc) in rows
                )
                if spend >= max_usd:
                    return BreakerDecision(
                        True,
                        f"cost breaker: ${spend:.2f} estimated synthesis spend in "
                        f"the last day >= ceiling ${max_usd:.2f}/day",
                    )
    except Exception as e:  # noqa: BLE001 — fail safe, never block on a breaker error
        log.warning("cost breaker check failed (%s); not tripping", e)
        return BreakerDecision(False)

    return BreakerDecision(False)

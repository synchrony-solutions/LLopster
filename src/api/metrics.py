"""Prometheus `/metrics` exposition for the agent — self-observability.

"You cannot monitor the monitor" was a launch blocker: run failure-rate,
backlog depth, daily LLM spend, and the cost-breaker state were only visible by
querying the database by hand. This renders them in the Prometheus text
exposition format so an operator can scrape the agent and alert on *it* (e.g.
"LLopster failed the last N runs" or "daily spend over budget").

The series are computed from the database at scrape time rather than from
in-process counters. That's deliberate: the pipeline is fire-and-forget and the
pod restarts lose in-memory state, so DB-backed gauges stay accurate across
restarts. No new dependency — the text format is emitted directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from src.agent import cost as cost_mod
from src.agent.processing_mode import MANUAL, get_processing_mode
from src.config import config
from src.db import repository as repo

log = logging.getLogger("llopster.metrics")

# processing_status values that mean "still in flight" — the backlog/queue depth.
_NON_TERMINAL = ("queued", "pending", "triaging", "collecting", "investigating", "generating", "posting")


def _line(name: str, value: float, labels: dict[str, str] | None = None) -> str:
    if labels:
        label_str = ",".join(f'{k}="{_escape(v)}"' for k, v in labels.items())
        return f"{name}{{{label_str}}} {value}"
    return f"{name} {value}"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


async def render_metrics(sessionmaker) -> str:
    """Return the Prometheus exposition text. Never raises — on error it emits a
    scrape-health series set to 0 so the failure itself is alertable."""
    now = datetime.now(timezone.utc)
    lines: list[str] = []
    try:
        async with sessionmaker() as session:
            by_status = await repo.count_runs_by_status(session)
            created_last_hour = await repo.count_runs_since(session, now - timedelta(hours=1))
            spend_rows = await repo.synthesis_token_rows_since(session, now - timedelta(days=1))
            mode = await get_processing_mode(session)

        spend_usd = sum(
            cost_mod.compute_cost_usd(
                input_tokens=inp, output_tokens=out,
                cache_read_tokens=cr, cache_creation_tokens=cc, model=model,
            )
            for (model, inp, out, cr, cc) in spend_rows
        )
        backlog = sum(by_status.get(s, 0) for s in _NON_TERMINAL)
        total_runs = sum(by_status.values())

        lines += [
            "# HELP llopster_runs_total Runs by terminal/in-flight processing status.",
            "# TYPE llopster_runs_total gauge",
        ]
        for status, count in sorted(by_status.items()):
            lines.append(_line("llopster_runs_total", count, {"status": status}))

        lines += [
            "# HELP llopster_runs_processed_total Total run rows ever created.",
            "# TYPE llopster_runs_processed_total gauge",
            _line("llopster_runs_processed_total", total_runs),
            "# HELP llopster_backlog Runs not yet in a terminal state (queue depth).",
            "# TYPE llopster_backlog gauge",
            _line("llopster_backlog", backlog),
            "# HELP llopster_runs_created_last_hour Runs created in the trailing hour (cost-breaker input).",
            "# TYPE llopster_runs_created_last_hour gauge",
            _line("llopster_runs_created_last_hour", created_last_hour),
            "# HELP llopster_estimated_spend_usd_last_day Estimated synthesis (Opus) spend over the trailing day.",
            "# TYPE llopster_estimated_spend_usd_last_day gauge",
            _line("llopster_estimated_spend_usd_last_day", round(spend_usd, 4)),
            "# HELP llopster_processing_mode_manual 1 when tripped to manual mode (cost breaker or operator), else 0.",
            "# TYPE llopster_processing_mode_manual gauge",
            _line("llopster_processing_mode_manual", 1 if mode == MANUAL else 0),
            "# HELP llopster_max_runs_per_hour Configured runs/hour cost-breaker ceiling (0=off).",
            "# TYPE llopster_max_runs_per_hour gauge",
            _line("llopster_max_runs_per_hour", config.max_runs_per_hour),
            "# HELP llopster_max_usd_per_day Configured USD/day cost-breaker ceiling (0=off).",
            "# TYPE llopster_max_usd_per_day gauge",
            _line("llopster_max_usd_per_day", config.max_usd_per_day),
            "# HELP llopster_scrape_ok 1 if this scrape rendered cleanly.",
            "# TYPE llopster_scrape_ok gauge",
            _line("llopster_scrape_ok", 1),
        ]
    except Exception as e:  # noqa: BLE001 — a metrics failure must not 500 the scrape
        log.warning("metrics render failed (%s); emitting scrape_ok=0", e)
        lines = [
            "# HELP llopster_scrape_ok 1 if this scrape rendered cleanly.",
            "# TYPE llopster_scrape_ok gauge",
            _line("llopster_scrape_ok", 0),
        ]
    return "\n".join(lines) + "\n"

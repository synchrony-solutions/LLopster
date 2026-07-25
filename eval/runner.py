"""Replay a frozen scenario through the *real* pipeline, deterministically.

The runner deliberately reuses `process_alert` rather than reimplementing the
stages — the whole value of the harness is that it scores the production code
path. Determinism/offline-ness come from two injections:

  * a `ContextCollector` whose Loki/Prom clients return the scenario's
    *recorded* context instead of querying anything, and
  * the LLM stages (triage / investigator / patcher), which the caller
    supplies — real Anthropic clients from scripts/run_eval.py, or AsyncMock
    in tests (same pattern as the unit suite).

No PR is ever opened (`github=None`) and no Slack message is sent
(`slack=None`); the scorer reads the patch off the persisted Run.
"""

from __future__ import annotations

import logging
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from eval.corpus import Scenario
from src.agent.context_collector import ContextCollector
from src.agent.investigator import Investigator
from src.agent.patch_generator import PatchGenerator
from src.agent.processor import process_alert
from src.agent.triage import Triage
from src.db import repository as repo
from src.db.models import Run
from src.integrations.loki_client import LogLine
from src.integrations.prometheus_client import MetricSample
from src.services_registry import ServiceRegistry

log = logging.getLogger("llopster.eval.runner")


class _RecordedLokiClient:
    """Serves the scenario's frozen log lines, ignoring the query/time range."""

    def __init__(self, lines: list[LogLine]):
        self._lines = lines

    async def query_range(self, logql, start, end, limit):  # noqa: ANN001
        return list(self._lines[:limit]) if limit else list(self._lines)


class _RecordedPrometheusClient:
    """Serves the scenario's frozen metric samples, ignoring the query."""

    def __init__(self, samples: list[MetricSample]):
        self._samples = samples

    async def query(self, promql, at):  # noqa: ANN001
        return list(self._samples)


def recorded_collector(scenario: Scenario) -> ContextCollector:
    """A ContextCollector wired to the scenario's recorded context.

    Goes through the real `collect()` (LogQL building, generatorURL PromQL
    extraction, error handling) so replay exercises that code too — the
    recorded clients just don't hit the network.
    """
    return ContextCollector(
        loki=_RecordedLokiClient(scenario.log_lines),
        prometheus=_RecordedPrometheusClient(scenario.metric_samples),
    )


async def replay_scenario(
    scenario: Scenario,
    *,
    sessionmaker: async_sessionmaker[AsyncSession],
    services: ServiceRegistry,
    patcher: PatchGenerator,
    triage: Triage | None = None,
    investigator: Investigator | None = None,
) -> Run:
    """Run one scenario through `process_alert` and return the completed Run.

    The Run is tagged `trigger_source="eval"` so eval replays never pollute
    the operator-facing run list or the cost dashboards. The cost breaker is
    disabled (this is an operator-initiated batch, not live traffic).
    """
    async with sessionmaker() as session:
        run = await repo.create_run_from_alert(
            session,
            scenario.alert,
            scenario.raw_payload,
            trigger_source="eval",
        )
        run_id = run.id

    await process_alert(
        run_id,
        scenario.alert,
        sessionmaker=sessionmaker,
        collector=recorded_collector(scenario),
        services=services,
        patcher=patcher,
        github=None,   # never open a PR from a replay
        notifier=None,    # never notify from a replay
        triage=triage,
        investigator=investigator,
        enforce_cost_breaker=False,
    )

    async with sessionmaker() as session:
        return await repo.get_run(session, run_id)


def eval_started_at() -> datetime:
    from datetime import timezone

    return datetime.now(timezone.utc)

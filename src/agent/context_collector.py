"""Given a parsed alert, fetch the surrounding logs and metric context."""

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from src.agent.alert_handler import ParsedAlert
from src.integrations.loki_client import LogLine, LokiClient
from src.integrations.prometheus_client import MetricSample, PrometheusClient

log = logging.getLogger("llopster.context")


# Loki labels we'll try, in order, to scope the log query to the alerting
# workload — the first one present on the alert wins. This is the default
# probe order; deployments override it via `LOG_SCOPE_LABELS` (config) when
# their collector (Grafana Alloy, Promtail, Vector, …) labels streams under a
# different scheme. Covers raw `app`, the `app.kubernetes.io/*` recommended
# set (dots/slashes are underscores once they're Loki stream labels), and the
# bare k8s identifiers.
LOG_SCOPE_LABELS = (
    "service",
    "app",
    "app_kubernetes_io_name",
    "app_kubernetes_io_instance",
    "container",
    "pod",
    "namespace",
    "job",
)


@dataclass
class AlertContext:
    alert: ParsedAlert
    log_lines: list[LogLine] = field(default_factory=list)
    metric_samples: list[MetricSample] = field(default_factory=list)
    queries_used: dict[str, str] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)


class ContextCollector:
    def __init__(
        self,
        loki: LokiClient,
        prometheus: PrometheusClient,
        lookback_minutes: int = 30,
        max_log_lines: int = 200,
        scope_labels: tuple[str, ...] | None = None,
    ):
        self.loki = loki
        self.prometheus = prometheus
        self.lookback_minutes = lookback_minutes
        self.max_log_lines = max_log_lines
        # Empty/None → fall back to the module default so direct construction
        # (tests, ad-hoc use) still works without wiring config through.
        self.scope_labels = tuple(scope_labels) if scope_labels else LOG_SCOPE_LABELS

    async def collect(self, alert: ParsedAlert) -> AlertContext:
        ctx = AlertContext(alert=alert)
        end = alert.starts_at or datetime.now(timezone.utc)
        start = end - timedelta(minutes=self.lookback_minutes)

        logql = _build_logql(alert, self.scope_labels)
        if logql:
            ctx.queries_used["logql"] = logql
            try:
                ctx.log_lines = await self.loki.query_range(
                    logql, start=start, end=end, limit=self.max_log_lines
                )
            except Exception as e:
                ctx.errors.append(f"loki query failed: {e}")
                log.exception("loki query failed")
        else:
            ctx.errors.append("no usable label found to build LogQL selector")

        promql = _extract_promql(alert)
        if promql:
            ctx.queries_used["promql"] = promql
            try:
                ctx.metric_samples = await self.prometheus.query(promql, at=end)
            except Exception as e:
                ctx.errors.append(f"prometheus query failed: {e}")
                log.exception("prometheus query failed")

        return ctx


def _build_logql(
    alert: ParsedAlert, scope_labels: tuple[str, ...] = LOG_SCOPE_LABELS
) -> str | None:
    """Pick the most specific label available and build a LogQL stream selector."""
    for label in scope_labels:
        value = alert.labels.get(label)
        if value:
            return f'{{{label}="{value}"}}'
    return None


def _extract_promql(alert: ParsedAlert) -> str | None:
    """Pull the original alert expression out of the generatorURL, if present.

    Prometheus encodes the expression as the `g0.expr` query parameter on the
    graph URL it generates. Falls back to None if we can't parse it.
    """
    if not alert.generator_url:
        return None
    try:
        qs = parse_qs(urlparse(alert.generator_url).query)
    except ValueError:
        return None
    expr = qs.get("g0.expr", [None])[0]
    return expr or None

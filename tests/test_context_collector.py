from datetime import datetime, timezone

import httpx
import pytest

from src.agent.alert_handler import ParsedAlert
from src.agent.context_collector import ContextCollector, _build_logql, _extract_promql
from src.integrations.loki_client import LokiClient
from src.integrations.prometheus_client import PrometheusClient


def _alert(**overrides) -> ParsedAlert:
    base = dict(
        fingerprint="fp",
        status="firing",
        alertname="X",
        severity="warning",
        service="",
        summary="",
        description="",
        starts_at=datetime(2026, 4, 18, 10, 0, tzinfo=timezone.utc),
        ends_at=None,
        labels={},
        annotations={},
        generator_url="",
    )
    base.update(overrides)
    return ParsedAlert(**base)


def test_build_logql_prefers_service_label():
    a = _alert(labels={"service": "demo-app", "namespace": "default"})
    assert _build_logql(a) == '{service="demo-app"}'


def test_build_logql_falls_back_to_namespace():
    a = _alert(labels={"namespace": "kube-system"})
    assert _build_logql(a) == '{namespace="kube-system"}'


def test_build_logql_returns_none_when_no_useful_labels():
    a = _alert(labels={"alertname": "X"})
    assert _build_logql(a) is None


def test_build_logql_matches_k8s_recommended_label():
    # No service/app, but the Alloy-shipped app_kubernetes_io_name is present.
    a = _alert(labels={"app_kubernetes_io_name": "payments", "namespace": "prod"})
    assert _build_logql(a) == '{app_kubernetes_io_name="payments"}'


def test_build_logql_respects_custom_scope_label_order():
    a = _alert(labels={"app": "demo-app", "namespace": "prod"})
    # A deployment that prefers namespace scoping overrides the probe order.
    assert _build_logql(a, ("namespace", "app")) == '{namespace="prod"}'


def test_build_logql_custom_scope_labels_can_match_nonstandard_label():
    a = _alert(labels={"workload": "billing"})
    # Default order misses it; a custom list finds it.
    assert _build_logql(a) is None
    assert _build_logql(a, ("workload",)) == '{workload="billing"}'


def test_collector_uses_injected_scope_labels():
    c = ContextCollector(
        loki=LokiClient("http://loki"),
        prometheus=PrometheusClient("http://prom"),
        scope_labels=("namespace",),
    )
    assert c.scope_labels == ("namespace",)


def test_collector_falls_back_to_default_scope_labels():
    from src.agent.context_collector import LOG_SCOPE_LABELS

    c = ContextCollector(
        loki=LokiClient("http://loki"),
        prometheus=PrometheusClient("http://prom"),
    )
    assert c.scope_labels == LOG_SCOPE_LABELS


def test_extract_promql_from_generator_url():
    a = _alert(
        generator_url="http://localhost:9090/graph?g0.expr=vector%281%29&g0.tab=1"
    )
    assert _extract_promql(a) == "vector(1)"


def test_extract_promql_returns_none_when_missing():
    assert _extract_promql(_alert(generator_url="")) is None
    assert _extract_promql(_alert(generator_url="http://x/graph?foo=bar")) is None


@pytest.mark.asyncio
async def test_collect_assembles_context_from_both_sources():
    loki_payload = {
        "data": {
            "result": [
                {
                    "stream": {"service": "demo-app"},
                    "values": [["1700000000000000000", "boom"]],
                }
            ]
        }
    }
    prom_payload = {
        "data": {
            "resultType": "vector",
            "result": [{"metric": {"job": "demo"}, "value": [1700000000.0, "1"]}],
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if "loki" in request.url.host:
            return httpx.Response(200, json=loki_payload)
        return httpx.Response(200, json=prom_payload)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        loki = LokiClient("http://loki:3100", client=client)
        prom = PrometheusClient("http://prom:9090", client=client)
        collector = ContextCollector(loki, prom, lookback_minutes=5, max_log_lines=10)

        alert = _alert(
            labels={"service": "demo-app"},
            generator_url="http://localhost:9090/graph?g0.expr=up",
        )
        ctx = await collector.collect(alert)

    assert len(ctx.log_lines) == 1
    assert ctx.log_lines[0].line == "boom"
    assert len(ctx.metric_samples) == 1
    assert ctx.queries_used == {"logql": '{service="demo-app"}', "promql": "up"}
    assert ctx.errors == []


@pytest.mark.asyncio
async def test_collect_records_error_when_loki_fails():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        collector = ContextCollector(
            LokiClient("http://loki", client=client),
            PrometheusClient("http://prom", client=client),
        )
        ctx = await collector.collect(_alert(labels={"service": "demo-app"}))

    assert ctx.log_lines == []
    assert any("loki query failed" in e for e in ctx.errors)

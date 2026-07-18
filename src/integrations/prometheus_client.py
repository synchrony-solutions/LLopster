"""Async client for the Prometheus HTTP API.

Docs: https://prometheus.io/docs/prometheus/latest/querying/api/
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx


@dataclass
class MetricSample:
    metric: dict[str, str]
    value: float
    timestamp: datetime


class PrometheusClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def query(
        self, promql: str, at: datetime | None = None
    ) -> list[MetricSample]:
        """Run an instant PromQL query. Returns one sample per matching series."""
        params: dict[str, str] = {"query": promql}
        if at is not None:
            params["time"] = _to_unix(at)

        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.get(f"{self.base_url}/api/v1/query", params=params)
            resp.raise_for_status()
            return _parse_instant(resp.json())
        finally:
            if self._client is None:
                await client.aclose()


def _to_unix(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return f"{dt.timestamp():.3f}"


def _parse_instant(payload: dict) -> list[MetricSample]:
    result = payload.get("data", {}).get("result", [])
    samples: list[MetricSample] = []
    for entry in result:
        ts, raw_value = entry.get("value", [None, None])
        if ts is None:
            continue
        try:
            value = float(raw_value)
        except (TypeError, ValueError):
            continue
        samples.append(
            MetricSample(
                metric=entry.get("metric", {}),
                value=value,
                timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc),
            )
        )
    return samples

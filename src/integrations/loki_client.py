"""Async client for the Loki HTTP API.

Docs: https://grafana.com/docs/loki/latest/reference/api/
"""

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx


@dataclass
class LogLine:
    timestamp: datetime
    line: str
    labels: dict[str, str]


class LokiClient:
    def __init__(self, base_url: str, client: httpx.AsyncClient | None = None):
        self.base_url = base_url.rstrip("/")
        self._client = client

    async def query_range(
        self,
        logql: str,
        start: datetime,
        end: datetime,
        limit: int = 200,
        direction: str = "backward",
    ) -> list[LogLine]:
        """Run a LogQL query over a time window. Returns log lines newest-first."""
        params = {
            "query": logql,
            "start": _to_ns(start),
            "end": _to_ns(end),
            "limit": str(limit),
            "direction": direction,
        }
        client = self._client or httpx.AsyncClient(timeout=10.0)
        try:
            resp = await client.get(
                f"{self.base_url}/loki/api/v1/query_range", params=params
            )
            resp.raise_for_status()
            return _parse_query_range(resp.json())
        finally:
            if self._client is None:
                await client.aclose()


def _to_ns(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return str(int(dt.timestamp() * 1_000_000_000))


def _parse_query_range(payload: dict) -> list[LogLine]:
    result = payload.get("data", {}).get("result", [])
    lines: list[LogLine] = []
    for stream in result:
        labels = stream.get("stream", {}) or {}
        for ts_ns, line in stream.get("values", []):
            lines.append(
                LogLine(
                    timestamp=datetime.fromtimestamp(int(ts_ns) / 1e9, tz=timezone.utc),
                    line=line,
                    labels=labels,
                )
            )
    lines.sort(key=lambda x: x.timestamp, reverse=True)
    return lines

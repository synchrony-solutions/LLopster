from datetime import datetime, timezone

import httpx
import pytest

from src.integrations.loki_client import LokiClient


@pytest.mark.asyncio
async def test_query_range_parses_streams():
    sample = {
        "status": "success",
        "data": {
            "resultType": "streams",
            "result": [
                {
                    "stream": {"service": "demo-app", "level": "error"},
                    "values": [
                        ["1700000000000000000", "first error"],
                        ["1700000060000000000", "second error"],
                    ],
                }
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/loki/api/v1/query_range"
        assert request.url.params["query"] == '{service="demo-app"}'
        assert request.url.params["limit"] == "50"
        return httpx.Response(200, json=sample)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        loki = LokiClient("http://loki:3100", client=client)
        lines = await loki.query_range(
            '{service="demo-app"}',
            start=datetime(2023, 1, 1, tzinfo=timezone.utc),
            end=datetime(2023, 1, 2, tzinfo=timezone.utc),
            limit=50,
        )

    assert len(lines) == 2
    assert lines[0].line == "second error"  # newest-first
    assert lines[0].labels == {"service": "demo-app", "level": "error"}

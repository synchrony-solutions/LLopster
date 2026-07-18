import httpx
import pytest

from src.integrations.prometheus_client import PrometheusClient


@pytest.mark.asyncio
async def test_query_parses_instant_vector():
    sample = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {
                    "metric": {"__name__": "up", "job": "demo"},
                    "value": [1700000000.123, "1"],
                },
                {
                    "metric": {"__name__": "up", "job": "loki"},
                    "value": [1700000000.123, "0"],
                },
            ],
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/query"
        assert request.url.params["query"] == "up"
        return httpx.Response(200, json=sample)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        prom = PrometheusClient("http://prometheus:9090", client=client)
        samples = await prom.query("up")

    assert len(samples) == 2
    assert samples[0].metric["job"] == "demo"
    assert samples[0].value == 1.0
    assert samples[1].value == 0.0


@pytest.mark.asyncio
async def test_query_skips_unparseable_values():
    sample = {
        "status": "success",
        "data": {
            "resultType": "vector",
            "result": [
                {"metric": {}, "value": [1700000000.0, "NaN-junk"]},
                {"metric": {}, "value": [1700000000.0, "42"]},
            ],
        },
    }
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=sample))
    async with httpx.AsyncClient(transport=transport) as client:
        prom = PrometheusClient("http://prom", client=client)
        samples = await prom.query("foo")

    assert len(samples) == 1
    assert samples[0].value == 42.0

"""Tests for the agent's /api/integrations/* endpoints.

These are the endpoints the dashboard calls to read connection status
and run "Test" buttons — replacing the previous arrangement where the
dashboard pod carried SLACK_WEBHOOK_URL / GITHUB_TOKEN / ANTHROPIC_API_KEY
in its environment. Keeping those secrets on the agent shrinks the blast
radius of a dashboard compromise to zero.

Critical invariants exercised below:
  - /status never leaks raw secret values, only "configured" booleans
    plus safe metadata (Slack netloc, GitHub token kind, Anthropic model)
  - test endpoints return JSON {ok, detail} so non-dashboard callers
    (CLI, monitoring) can consume them too
"""

from dataclasses import replace
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

import src.api.integrations_api as _ia
from src.api.integrations_api import router as integrations_router


def _build_app(http_mock: AsyncMock | None = None, services: list[str] | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(integrations_router)
    app.state.http = http_mock or AsyncMock()
    # The /api/integrations/services endpoint reads from app.state.services.
    # Tests that don't care about services can leave it as the stub below.
    _names = services or []
    app.state.services = type("S", (), {"names": lambda self: _names})()
    return app


# ---------------------------------------------------------------------------
# GET /api/integrations/status
# ---------------------------------------------------------------------------

async def test_status_returns_configured_flags():
    fake_cfg = replace(
        _ia.config,
        slack_webhook_url="https://hooks.slack.com/services/T1/B2/x9z",
        github_token="github_pat_11ABC_thisisfake",
        anthropic_api_key="sk-ant-abc",
        anthropic_model="claude-opus-4-7",
    )
    app = _build_app()

    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/integrations/status")

    body = r.json()
    assert body["notifier"]["configured"] is True
    assert body["notifier"]["provider"] == "slack"
    assert body["github"]["configured"] is True
    assert body["anthropic"]["configured"] is True
    assert body["anthropic"]["model"] == "claude-opus-4-7"


async def test_status_never_leaks_raw_secrets():
    """Critical: the JSON response must not contain the actual token /
    webhook URL string. The dashboard reads this on every settings page
    load — leaking the secret here defeats the entire refactor."""
    secret_url = "https://hooks.slack.com/services/T1/B2/secret-value-here"
    secret_pat = "github_pat_11A_SUPERSECRET_DONT_LEAK"
    secret_key = "sk-ant-api03-DONT_LEAK_THIS"
    fake_cfg = replace(
        _ia.config,
        slack_webhook_url=secret_url,
        github_token=secret_pat,
        anthropic_api_key=secret_key,
    )
    app = _build_app()

    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/integrations/status")

    text = r.text
    assert "secret-value-here" not in text
    assert "SUPERSECRET" not in text
    assert "DONT_LEAK" not in text


async def test_status_classifies_github_token_kind():
    """Operator should be able to tell at a glance whether they configured
    a classic PAT vs fine-grained vs OAuth — without us echoing the token."""
    cases = [
        ("github_pat_xyz", "fine-grained-pat"),
        ("ghp_xyz", "classic-pat"),
        ("ghs_xyz", "oauth"),
        ("gho_xyz", "oauth"),
        ("ghu_xyz", "oauth"),
        ("random-garbage", "unknown"),
    ]
    for token, expected in cases:
        fake_cfg = replace(_ia.config, github_token=token)
        app = _build_app()
        with patch.object(_ia, "config", fake_cfg):
            async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
                r = await c.get("/api/integrations/status")
        assert r.json()["github"]["token_kind"] == expected, f"{token} → {expected}"


async def test_status_shows_unconfigured_state():
    fake_cfg = replace(_ia.config, slack_webhook_url="", github_token="", anthropic_api_key="")
    app = _build_app()
    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/integrations/status")
    body = r.json()
    assert body["notifier"]["configured"] is False
    assert body["github"]["configured"] is False
    assert body["anthropic"]["configured"] is False


async def test_status_anthropic_provider_default():
    """Direct-API provider: status reports provider=anthropic and the
    direct-API synthesis model, no region."""
    fake_cfg = replace(
        _ia.config, llm_provider="anthropic",
        anthropic_api_key="sk-ant-abc", anthropic_model="claude-opus-4-7",
    )
    app = _build_app()
    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/integrations/status")
    a = r.json()["anthropic"]
    assert a["provider"] == "anthropic"
    assert a["model"] == "claude-opus-4-7"
    assert a["region"] == ""


async def test_status_bedrock_provider():
    """Bedrock provider: configured without an API key, reports the Bedrock
    synthesis model + region so the operator can see what's in use."""
    fake_cfg = replace(
        _ia.config, llm_provider="bedrock", anthropic_api_key="",
        aws_region="us-east-1",
        bedrock_model="us.anthropic.claude-opus-4-7-v1:0",
    )
    app = _build_app()
    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get("/api/integrations/status")
    a = r.json()["anthropic"]
    assert a["configured"] is True
    assert a["provider"] == "bedrock"
    assert a["model"] == "us.anthropic.claude-opus-4-7-v1:0"
    assert a["region"] == "us-east-1"


# ---------------------------------------------------------------------------
# POST /api/integrations/test/notifier
# ---------------------------------------------------------------------------

async def test_test_notifier_slack_success():
    fake_cfg = replace(_ia.config, notifier_provider="slack", slack_webhook_url="https://hooks.slack.com/x")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    http = AsyncMock()
    http.post = AsyncMock(return_value=mock_resp)
    app = _build_app(http)

    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/integrations/test/notifier")

    assert r.json() == {"ok": True, "detail": "Connected"}


async def test_test_notifier_teams_success_accepts_202():
    """Teams Workflows returns 202 Accepted and gets the Adaptive Card
    envelope, not the Slack text payload."""
    fake_cfg = replace(
        _ia.config, notifier_provider="teams",
        teams_webhook_url="https://prod-1.westus.logic.azure.com/workflows/x",
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 202
    http = AsyncMock()
    http.post = AsyncMock(return_value=mock_resp)
    app = _build_app(http)

    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/integrations/test/notifier")

    assert r.json() == {"ok": True, "detail": "Connected"}
    payload = http.post.call_args.kwargs["json"]
    assert payload["type"] == "message"  # Teams envelope, not {"text": ...}


async def test_test_notifier_not_configured():
    fake_cfg = replace(_ia.config, notifier_provider="slack", slack_webhook_url="")
    app = _build_app()
    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/integrations/test/notifier")
    body = r.json()
    assert body["ok"] is False
    assert "not set" in body["detail"]


async def test_test_notifier_disabled_when_none():
    fake_cfg = replace(_ia.config, notifier_provider="none")
    app = _build_app()
    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/integrations/test/notifier")
    body = r.json()
    assert body["ok"] is False
    assert "disabled" in body["detail"]


async def test_test_notifier_handles_non_2xx():
    fake_cfg = replace(_ia.config, notifier_provider="slack", slack_webhook_url="https://hooks.slack.com/bad")
    mock_resp = MagicMock()
    mock_resp.status_code = 403
    http = AsyncMock()
    http.post = AsyncMock(return_value=mock_resp)
    app = _build_app(http)

    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/integrations/test/notifier")
    body = r.json()
    assert body["ok"] is False
    assert "403" in body["detail"]


# ---------------------------------------------------------------------------
# POST /api/integrations/test/github
# ---------------------------------------------------------------------------

async def test_test_github_success():
    fake_cfg = replace(_ia.config, github_token="ghp_fake")
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"login": "octocat"})
    http = AsyncMock()
    http.get = AsyncMock(return_value=mock_resp)
    app = _build_app(http)

    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/integrations/test/github")
    body = r.json()
    assert body["ok"] is True
    assert body["user"] == "octocat"
    assert "octocat" in body["detail"]


async def test_test_github_not_configured():
    fake_cfg = replace(_ia.config, github_token="")
    app = _build_app()
    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/integrations/test/github")
    body = r.json()
    assert body["ok"] is False
    assert "not set" in body["detail"]


# ---------------------------------------------------------------------------
# GET /api/integrations/services
# ---------------------------------------------------------------------------

async def test_services_endpoint_returns_registry():
    """Dashboard's trigger form pulls the service dropdown from here so
    it doesn't need to parse services.yaml itself."""
    app = _build_app(services=["demo-app", "api-svc"])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/integrations/services")
    assert r.json() == {"services": ["demo-app", "api-svc"]}


async def test_services_endpoint_empty_registry():
    app = _build_app(services=[])
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get("/api/integrations/services")
    assert r.json() == {"services": []}


async def test_test_github_exception_surface():
    """A network failure should be reported as a non-ok JSON response,
    not propagate as a 500 — the dashboard parses the JSON either way."""
    fake_cfg = replace(_ia.config, github_token="ghp_fake")
    http = AsyncMock()
    http.get = AsyncMock(side_effect=ConnectionError("dns lookup failed"))
    app = _build_app(http)

    with patch.object(_ia, "config", fake_cfg):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.post("/api/integrations/test/github")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert "dns lookup failed" in body["detail"]

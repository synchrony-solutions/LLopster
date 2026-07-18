"""Integration status + test endpoints owned by the agent.

The dashboard pod intentionally does NOT carry the Slack webhook URL,
GitHub PAT, or Anthropic key in its environment — those are real secrets
and the dashboard doesn't need them to do its job. Instead the dashboard
calls these agent endpoints over the in-cluster network to:

  - render the Settings page (status: configured / masked values)
  - run the "Test" buttons (the agent uses its own credentials)

Returning JSON (not HTML) so non-dashboard callers (CLI, monitoring,
future Business-tier multi-tenant control plane) can reuse them too.
The dashboard wraps these into HTML fragments locally.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Request

from src.config import config

router = APIRouter(prefix="/api/integrations", tags=["integrations"])


def _mask_url(url: str) -> str:
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        return parsed.netloc or url[:40]
    except Exception:
        return url[:40]


def _classify_github_token(token: str) -> str:
    """Returns the token "kind" so the dashboard can show a hint without
    ever seeing the value itself."""
    if not token:
        return ""
    if token.startswith("github_pat_"):
        return "fine-grained-pat"
    if token.startswith("ghp_"):
        return "classic-pat"
    if token.startswith(("ghs_", "gho_", "ghu_")):
        return "oauth"
    return "unknown"


# ---------------------------------------------------------------------------
# GET /api/integrations/status
# ---------------------------------------------------------------------------

@router.get("/status")
async def integrations_status() -> dict:
    """Configured-yes/no + safe-to-display metadata for each integration.

    Never returns raw secrets. The Slack URL is collapsed to its netloc;
    the GitHub token is reduced to a kind (classic-pat / fine-grained /
    oauth) so the operator can tell what they configured.
    """
    return {
        "slack": {
            "configured": bool(config.slack_webhook_url),
            "masked": _mask_url(config.slack_webhook_url),
        },
        "github": {
            "configured": bool(config.github_token),
            "token_kind": _classify_github_token(config.github_token),
        },
        "anthropic": {
            "configured": bool(config.anthropic_api_key),
            "model": config.anthropic_model,
        },
    }


# ---------------------------------------------------------------------------
# GET /api/integrations/services
# ---------------------------------------------------------------------------
# Exposes the service registry (loaded from services.yaml on the agent) so
# the dashboard's manual-trigger form can populate its service dropdown
# without parsing services.yaml itself. Single source of truth: the agent.

@router.get("/services")
async def integrations_services(request: Request) -> dict:
    services = list(request.app.state.services.names())
    return {"services": services}


# ---------------------------------------------------------------------------
# POST /api/integrations/test/slack
# ---------------------------------------------------------------------------

@router.post("/test/slack")
async def integrations_test_slack(request: Request) -> dict:
    """Posts a test message to the configured Slack webhook.

    Returns ``{ok: bool, detail: str}``. The dashboard renders this into
    its HTML fragment; CLI/monitoring callers consume the JSON directly.
    """
    if not config.slack_webhook_url:
        return {"ok": False, "detail": "SLACK_WEBHOOK_URL not set"}
    try:
        http = request.app.state.http
        resp = await http.post(
            config.slack_webhook_url,
            json={"text": "llopster connection test ✓"},
        )
        if resp.status_code == 200:
            return {"ok": True, "detail": "Connected"}
        return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}


# ---------------------------------------------------------------------------
# POST /api/integrations/test/github
# ---------------------------------------------------------------------------

@router.post("/test/github")
async def integrations_test_github(request: Request) -> dict:
    """Calls ``GET https://api.github.com/user`` with the configured PAT.

    Returns ``{ok: bool, detail: str, user?: str}``.
    """
    if not config.github_token:
        return {"ok": False, "detail": "GITHUB_TOKEN not set"}
    try:
        http = request.app.state.http
        resp = await http.get(
            "https://api.github.com/user",
            headers={
                "Authorization": f"Bearer {config.github_token}",
                "Accept": "application/vnd.github+json",
            },
        )
        if resp.status_code == 200:
            login = resp.json().get("login", "unknown")
            return {"ok": True, "detail": f"Authenticated as {login}", "user": login}
        return {"ok": False, "detail": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "detail": str(e)}

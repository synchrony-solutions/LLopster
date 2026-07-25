"""Settings routes for the dashboard.

DB-only settings (confidence threshold, log lookback, processing mode)
are written here directly. Integration status + connection tests are
delegated to the agent's ``/api/integrations/*`` endpoints — the
dashboard pod intentionally does NOT carry SLACK_WEBHOOK_URL,
GITHUB_TOKEN, or ANTHROPIC_API_KEY in its environment.

If ``AGENT_URL`` is unset (or the agent is unreachable) the page still
renders, but the connection cards show as "agent unreachable" rather
than the previous "not configured" — distinguishing a configuration
problem from a network/availability problem.
"""

from __future__ import annotations

import logging
from typing import Annotated

import httpx
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.auth import require_inbound_auth, resolve_api_token
from src.agent.processing_mode import (
    AUTOPILOT,
    DEFAULT_MODE,
    MANUAL,
    VALID_MODES,
    get_processing_mode,
    set_processing_mode,
)
from src.config import config
from src.dashboard.templating import templates
from src.db import repository as repo

log = logging.getLogger(__name__)

router = APIRouter()


# Default integration status when the agent can't be reached. We render
# the page as if everything is unknown rather than blanking the cards.
_AGENT_DOWN_STATUS = {
    "slack": {"configured": False, "masked": "", "agent_down": True},
    "github": {"configured": False, "token_kind": "", "agent_down": True},
    "anthropic": {
        "configured": False, "model": "", "provider": "", "region": "",
        "agent_down": True,
    },
}

# Shown on the license card when the agent (source of truth for the license)
# can't be reached.
_LICENSE_DOWN_STATUS = {
    "tier": "unknown",
    "valid": False,
    "source": "agent_down",
    "features": [],
    "license_id": None,
    "expires_at": None,
    "clusters": None,
    "reason": None,
    "agent_down": True,
}


async def _fetch_integration_status(request: Request) -> dict:
    """Hit the agent's /api/integrations/status. Never raises."""
    agent_url = (getattr(config, "agent_url", "") or "").rstrip("/")
    if not agent_url:
        log.warning(
            "AGENT_URL not set on dashboard pod — settings page cannot show "
            "integration status. Set dashboard.env.AGENT_URL in values.yaml."
        )
        return _AGENT_DOWN_STATUS

    http: httpx.AsyncClient = request.app.state.http
    try:
        resp = await http.get(f"{agent_url}/api/integrations/status", timeout=5.0)
        if resp.status_code != 200:
            log.warning("agent /api/integrations/status returned %d", resp.status_code)
            return _AGENT_DOWN_STATUS
        return resp.json()
    except Exception as e:  # noqa: BLE001 — network surface, log and degrade
        log.warning("agent /api/integrations/status unreachable: %s", e)
        return _AGENT_DOWN_STATUS


async def _fetch_license_status(request: Request) -> dict:
    """Hit the agent's /api/license/status. Never raises."""
    agent_url = (getattr(config, "agent_url", "") or "").rstrip("/")
    if not agent_url:
        return _LICENSE_DOWN_STATUS

    http: httpx.AsyncClient = request.app.state.http
    try:
        resp = await http.get(f"{agent_url}/api/license/status", timeout=5.0)
        if resp.status_code != 200:
            log.warning("agent /api/license/status returned %d", resp.status_code)
            return _LICENSE_DOWN_STATUS
        return resp.json()
    except Exception as e:  # noqa: BLE001 — network surface, log and degrade
        log.warning("agent /api/license/status unreachable: %s", e)
        return _LICENSE_DOWN_STATUS


async def _settings_context(request: Request) -> dict:
    sm = request.app.state.sessionmaker
    async with sm() as session:
        stored = await repo.get_all_settings(session)
        processing_mode = await get_processing_mode(session)

    confidence_threshold = int(
        stored.get("patch_confidence_threshold", str(config.patch_confidence_threshold))
    )
    log_lookback = int(
        stored.get("log_lookback_minutes", str(config.log_lookback_minutes))
    )
    # The pruner reads this same setting on each tick — write-here-read-there.
    # 0 disables pruning entirely (runs kept forever).
    retention_days = int(
        stored.get("run_retention_days", str(config.run_retention_days))
    )
    triage_enabled_raw = stored.get("triage_enabled")
    if triage_enabled_raw is None:
        triage_enabled = config.triage_enabled
    else:
        triage_enabled = triage_enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
    triage_model = stored.get("triage_model", config.anthropic_triage_model)
    triage_min_confidence = int(
        stored.get("triage_min_confidence", str(config.triage_min_confidence))
    )
    investigation_enabled_raw = stored.get("investigation_enabled")
    if investigation_enabled_raw is None:
        investigation_enabled = config.investigation_enabled
    else:
        investigation_enabled = investigation_enabled_raw.strip().lower() in {"1", "true", "yes", "on"}
    investigation_model = stored.get(
        "investigation_model", config.anthropic_investigation_model,
    )
    open_prs_as_draft_raw = stored.get("open_prs_as_draft")
    if open_prs_as_draft_raw is None:
        open_prs_as_draft = config.open_prs_as_draft
    else:
        open_prs_as_draft = open_prs_as_draft_raw.strip().lower() in {"1", "true", "yes", "on"}
    max_runs_per_hour = int(
        stored.get("max_runs_per_hour", str(config.max_runs_per_hour))
    )
    max_usd_per_day = float(
        stored.get("max_usd_per_day", str(config.max_usd_per_day))
    )
    patch_backoff_minutes = int(
        stored.get("patch_backoff_minutes", str(config.patch_backoff_minutes))
    )
    # Whether inbound auth is active for THIS pod (DB override → env). We never
    # render the secret itself — only configured/not-configured.
    auth_configured = bool(await resolve_api_token(request.app.state.sessionmaker))

    license_status = await _fetch_license_status(request)

    integrations = await _fetch_integration_status(request)
    slack = integrations.get("slack", {})
    github = integrations.get("github", {})
    anthropic = integrations.get("anthropic", {})
    agent_down = any(v.get("agent_down") for v in (slack, github, anthropic))

    return {
        "active": "settings",
        "confidence_threshold": confidence_threshold,
        "log_lookback": log_lookback,
        "retention_days": retention_days,
        "triage_enabled": triage_enabled,
        "triage_model": triage_model,
        "triage_min_confidence": triage_min_confidence,
        "investigation_enabled": investigation_enabled,
        "investigation_model": investigation_model,
        "open_prs_as_draft": open_prs_as_draft,
        "max_runs_per_hour": max_runs_per_hour,
        "max_usd_per_day": max_usd_per_day,
        "patch_backoff_minutes": patch_backoff_minutes,
        "auth_configured": auth_configured,
        "processing_mode": processing_mode,
        "mode_autopilot": AUTOPILOT,
        "mode_manual": MANUAL,
        "slack_masked": slack.get("masked", "") or "—",
        "slack_configured": bool(slack.get("configured")),
        "github_repo": "configured" if github.get("configured") else "—",
        "github_configured": bool(github.get("configured")),
        "github_token_kind": github.get("token_kind", ""),
        "anthropic_model": anthropic.get("model", ""),
        "anthropic_configured": bool(anthropic.get("configured")),
        "anthropic_provider": anthropic.get("provider", ""),
        "anthropic_region": anthropic.get("region", ""),
        "agent_url": (getattr(config, "agent_url", "") or "").rstrip("/"),
        "agent_down": agent_down,
        "license": license_status,
    }


@router.get("/settings", response_class=HTMLResponse, name="settings_page")
async def settings_page(request: Request) -> HTMLResponse:
    ctx = await _settings_context(request)
    return templates.TemplateResponse(request, "settings.html", ctx)


@router.post("/settings", name="settings_save", dependencies=[Depends(require_inbound_auth)])
async def settings_save(
    request: Request,
    patch_confidence_threshold: Annotated[int, Form()],
    log_lookback_minutes: Annotated[int, Form()],
    processing_mode: Annotated[str, Form()] = DEFAULT_MODE,
    run_retention_days: Annotated[int, Form()] = 90,
    triage_enabled: Annotated[str | None, Form()] = None,
    triage_model: Annotated[str, Form()] = "",
    triage_min_confidence: Annotated[int, Form()] = 4,
    investigation_enabled: Annotated[str | None, Form()] = None,
    investigation_model: Annotated[str, Form()] = "",
    open_prs_as_draft: Annotated[str | None, Form()] = None,
    max_runs_per_hour: Annotated[int, Form()] = 0,
    max_usd_per_day: Annotated[float, Form()] = 0.0,
    patch_backoff_minutes: Annotated[int, Form()] = 60,
) -> RedirectResponse:
    if processing_mode not in VALID_MODES:
        processing_mode = DEFAULT_MODE
    # Clamp to sane bounds. 0 is meaningful (= disable pruning); negatives
    # are nonsense; cap at ~5 years to avoid surprise unbounded growth.
    run_retention_days = max(0, min(run_retention_days, 1825))
    triage_min_confidence = max(1, min(triage_min_confidence, 5))
    # Cost-breaker ceilings: 0 = disabled; negatives are nonsense.
    max_runs_per_hour = max(0, max_runs_per_hour)
    max_usd_per_day = max(0.0, max_usd_per_day)
    # Post-firing backoff window: 0 = disabled; negatives are nonsense.
    patch_backoff_minutes = max(0, patch_backoff_minutes)
    # An unchecked checkbox simply isn't submitted; presence of the field
    # means the box was ticked.
    triage_enabled_str = "true" if triage_enabled is not None else "false"
    investigation_enabled_str = "true" if investigation_enabled is not None else "false"
    open_prs_as_draft_str = "true" if open_prs_as_draft is not None else "false"
    sm = request.app.state.sessionmaker
    async with sm() as session:
        await repo.set_setting(session, "patch_confidence_threshold", str(patch_confidence_threshold))
        await repo.set_setting(session, "log_lookback_minutes", str(log_lookback_minutes))
        await repo.set_setting(session, "run_retention_days", str(run_retention_days))
        await repo.set_setting(session, "triage_enabled", triage_enabled_str)
        if triage_model.strip():
            await repo.set_setting(session, "triage_model", triage_model.strip())
        await repo.set_setting(session, "triage_min_confidence", str(triage_min_confidence))
        await repo.set_setting(session, "investigation_enabled", investigation_enabled_str)
        if investigation_model.strip():
            await repo.set_setting(session, "investigation_model", investigation_model.strip())
        await repo.set_setting(session, "open_prs_as_draft", open_prs_as_draft_str)
        await repo.set_setting(session, "max_runs_per_hour", str(max_runs_per_hour))
        await repo.set_setting(session, "max_usd_per_day", str(max_usd_per_day))
        await repo.set_setting(session, "patch_backoff_minutes", str(patch_backoff_minutes))
        await set_processing_mode(session, processing_mode)
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post(
    "/settings/api-token",
    name="settings_api_token_save",
    dependencies=[Depends(require_inbound_auth)],
)
async def settings_api_token_save(
    request: Request,
    api_auth_token: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Set or clear the inbound-auth shared secret (its own form so the main
    settings save can't accidentally clear it). A blank value clears the
    override, falling back to the LLOPSTER_API_TOKEN env var. Once a token is
    active, this route requires it like every other write surface — so the
    bootstrap path is: set it once while auth is still disabled, then it
    enforces. The current value is never rendered back to the page."""
    sm = request.app.state.sessionmaker
    async with sm() as session:
        await repo.set_setting(session, "api_auth_token", api_auth_token.strip())
    return RedirectResponse(url="/settings?saved=1", status_code=303)


@router.post(
    "/settings/license",
    name="settings_license_save",
    dependencies=[Depends(require_inbound_auth)],
)
async def settings_license_save(
    request: Request,
    license_key: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """Persist the license key to the Setting table (its own form so it can't
    be accidentally cleared by the main settings save). A blank value clears
    the override, falling back to the LLOPSTER_LICENSE_KEY env var. The agent
    picks the change up on its next /api/license/status fetch (which the
    redirect-to-/settings below triggers)."""
    sm = request.app.state.sessionmaker
    async with sm() as session:
        await repo.set_setting(session, "license_key", license_key.strip())
    return RedirectResponse(url="/settings?saved=1", status_code=303)


# ---------------------------------------------------------------------------
# Connection tests — pure proxies to the agent
# ---------------------------------------------------------------------------

async def _proxy_test(request: Request, path: str) -> tuple[bool, str]:
    """POST to ``{AGENT_URL}{path}`` and return ``(ok, detail)``."""
    agent_url = (getattr(config, "agent_url", "") or "").rstrip("/")
    if not agent_url:
        return False, "AGENT_URL not set on dashboard pod"
    http: httpx.AsyncClient = request.app.state.http
    try:
        resp = await http.post(f"{agent_url}{path}", timeout=10.0)
        if resp.status_code != 200:
            return False, f"agent returned HTTP {resp.status_code}"
        body = resp.json()
        return bool(body.get("ok")), str(body.get("detail", ""))
    except Exception as e:  # noqa: BLE001
        return False, f"agent unreachable: {e}"


def _render_result(ok: bool, detail: str) -> HTMLResponse:
    cls = "test-ok" if ok else "test-err"
    prefix = "✓ " if ok else ""
    return HTMLResponse(f'<span class="test-result {cls}">{prefix}{detail}</span>')


@router.post("/settings/test/slack", response_class=HTMLResponse, name="settings_test_slack")
async def settings_test_slack(request: Request) -> HTMLResponse:
    ok, detail = await _proxy_test(request, "/api/integrations/test/slack")
    return _render_result(ok, detail)


@router.post("/settings/test/github", response_class=HTMLResponse, name="settings_test_github")
async def settings_test_github(request: Request) -> HTMLResponse:
    ok, detail = await _proxy_test(request, "/api/integrations/test/github")
    return _render_result(ok, detail)

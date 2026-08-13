"""FastAPI webhook receiver for AlertManager — agent service.

This process handles the inbound webhook, runs the alert pipeline as a
background task, and manages the long-lived background tasks (pr_poller,
run_pruner, heartbeat).  It has NO UI routes — the dashboard lives in
src/dashboard/main.py and connects to the same database independently.

If this process crashes, the dashboard continues serving run history and
diagnostics from the shared database.

Entrypoint (Helm / Docker):
    SERVICE=agent uvicorn src.api.main:app --host 0.0.0.0 --port 8000
"""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from fastapi import Depends, FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from src.agent.alert_handler import parse_alertmanager_payload
from src.agent.cost_breaker import log_cost_breaker_status
from src.api.auth import log_auth_status, require_inbound_auth
from src.api.metrics import render_metrics
from src.agent.context_collector import ContextCollector
from src.agent.dedup import compute_dedup_key, find_open_run_by_dedup_key
from src.agent.packs import load_packs_into
from src.agent.patch_generator import SYSTEM_PROMPT as SYNTHESIS_PROMPT, PatchGenerator
from src.agent.investigator import SYSTEM_PROMPT as INVESTIGATION_PROMPT, Investigator
from src.agent.llm_provider import (
    build_message_client,
    effective_extended_cache_ttl,
    resolve_models,
)
from src.agent import license
from src.agent.processing_mode import MANUAL, get_processing_mode
from src.agent.processor import process_alert
from src.agent.prompts import (
    STAGE_INVESTIGATION,
    STAGE_SYNTHESIS,
    STAGE_TRIAGE,
    PromptResolver,
)
from src.agent.triage import SYSTEM_PROMPT as TRIAGE_PROMPT, Triage
from src.api.integrations_api import router as integrations_router
from src.api.license_api import refresh_active_license
from src.api.license_api import router as license_router
from src.api.trigger_routes import router as trigger_router
from src.config import config
from src.db import create_engine, get_sessionmaker, init_schema
from src.db import repository as repo
from src.integrations.github_client import GitHubClient
from src.integrations.loki_client import LokiClient
from src.integrations.prometheus_client import PrometheusClient
from src.integrations.slack_client import SlackClient
from src.services_registry import ServiceRegistry

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("llopster")

_HEARTBEAT_KEY = "agent_last_heartbeat"
_HEARTBEAT_INTERVAL = 30  # seconds


async def _heartbeat_loop(sessionmaker) -> None:
    """Write a timestamp to the settings table every 30 s.

    The dashboard reads this to show the agent-status pill.  If this process
    dies, the heartbeat stops updating and the dashboard shows "Agent
    unreachable" after 2 minutes.
    """
    while True:
        try:
            async with sessionmaker() as session:
                await repo.set_setting(
                    session,
                    _HEARTBEAT_KEY,
                    datetime.now(timezone.utc).isoformat(),
                )
        except Exception:
            log.warning("heartbeat write failed", exc_info=True)
        await asyncio.sleep(_HEARTBEAT_INTERVAL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Shared HTTP client.
    app.state.http = httpx.AsyncClient(timeout=10.0)

    # Database engine + session factory + schema bootstrap.
    app.state.db_engine = create_engine(config.database_url)
    app.state.sessionmaker = get_sessionmaker(app.state.db_engine)
    await init_schema(app.state.db_engine)

    # Apply the license_key Setting override (DB → in-memory) so paid-feature
    # gating reflects a UI-pasted key, not just the env var. Re-applied on every
    # /api/license/status fetch; env var is the fallback when no Setting exists.
    await refresh_active_license(app.state.sessionmaker)
    _active_license = license.get_active_license()
    log.info(
        "license: active tier=%s source=%s features=%d",
        _active_license.tier, _active_license.source, len(_active_license.features),
    )

    # Announce the inbound-auth posture loudly so an operator who ships without
    # a shared secret sees the warning in the agent logs.
    log_auth_status("agent")

    # Announce the cost-breaker ceilings so the out-of-the-box spend cap is
    # visible and operators are nudged to tune it to their volume/budget.
    log_cost_breaker_status("agent")

    # Agent components.
    app.state.collector = ContextCollector(
        loki=LokiClient(config.loki_url, client=app.state.http),
        prometheus=PrometheusClient(config.prometheus_url, client=app.state.http),
        lookback_minutes=config.log_lookback_minutes,
        max_log_lines=config.max_log_lines,
        scope_labels=config.log_scope_labels,
    )
    app.state.services = ServiceRegistry(config.services_config_path)

    # Prompt-resolution seam: Community defaults overlaid by entitled premium
    # packs mounted at config.packs_dir. Built unconditionally (cheap; useful
    # for diagnostics even when no API key is set) and injected into each LLM
    # stage. Pack loading fails open — an absent/broken pack leaves the
    # baked-in Community prompts in place.
    prompt_resolver = PromptResolver(
        {
            STAGE_TRIAGE: TRIAGE_PROMPT,
            STAGE_INVESTIGATION: INVESTIGATION_PROMPT,
            STAGE_SYNTHESIS: SYNTHESIS_PROMPT,
        }
    )
    load_packs_into(prompt_resolver, config.packs_dir)
    app.state.prompt_resolver = prompt_resolver

    # Provider seam (Anthropic API vs AWS Bedrock). One decision point:
    # resolve the per-stage model IDs and whether the extended (1h) cache
    # TTL applies (Bedrock forces it off), then build a provider-specific
    # client per client kind. Triage never used the cache beta header;
    # investigator + patch share the extended-TTL decision. All three are
    # gated on `llm_configured` (API key present, or provider=bedrock).
    models = resolve_models(config)
    extended_ttl = effective_extended_cache_ttl(config)
    log.info(
        "LLM provider=%s models=(triage=%s, investigation=%s, synthesis=%s) "
        "extended_cache_ttl=%s",
        config.llm_provider, models.triage, models.investigation,
        models.synthesis, extended_ttl,
    )

    app.state.patcher = (
        PatchGenerator(
            api_key=config.anthropic_api_key,
            model=models.synthesis,
            extended_cache_ttl=extended_ttl,
            client=build_message_client(config, extended_cache_ttl=extended_ttl),
            prompt_resolver=prompt_resolver,
        )
        if config.llm_configured
        else None
    )
    if app.state.patcher is None:
        log.warning(
            "LLM provider not configured (provider=%s) — patch generation disabled",
            config.llm_provider,
        )
    # Triage shares the provider with the patch generator — there's no
    # separate Haiku key. If the provider is unconfigured the gate is
    # disabled entirely; the processor then falls through to the existing
    # pipeline (which is a no-op since patcher is also None). Triage never
    # sends the cache beta header, so its client is built with it off.
    app.state.triage = (
        Triage(
            api_key=config.anthropic_api_key,
            model=models.triage,
            client=build_message_client(config, extended_cache_ttl=False),
            prompt_resolver=prompt_resolver,
        )
        if config.llm_configured
        else None
    )
    # Investigator shares the provider + the same extended-cache-ttl
    # decision as the patch generator (the outline blob is the cache prefix
    # here, same shape as the codebase blob is for Opus today).
    app.state.investigator = (
        Investigator(
            api_key=config.anthropic_api_key,
            model=models.investigation,
            extended_cache_ttl=extended_ttl,
            client=build_message_client(config, extended_cache_ttl=extended_ttl),
            prompt_resolver=prompt_resolver,
        )
        if config.llm_configured
        else None
    )
    app.state.slack = (
        SlackClient(webhook_url=config.slack_webhook_url, client=app.state.http)
        if config.slack_webhook_url
        else None
    )
    if app.state.slack is None:
        log.warning("SLACK_WEBHOOK_URL not set — Slack notifications disabled")
    app.state.github = (
        GitHubClient(token=config.github_token, client=app.state.http)
        if config.github_token
        else None
    )
    if app.state.github is None:
        log.warning("GITHUB_TOKEN not set — PR creation disabled")

    # Strong references to in-flight background tasks (asyncio GC trap).
    app.state.background_tasks: set[asyncio.Task] = set()

    # PR poller — only launch if GitHub is configured.
    app.state.pr_poller_task: asyncio.Task | None = None
    if app.state.github is not None:
        from src.agent.pr_poller import pr_poller
        poller_task = asyncio.create_task(
            pr_poller(app.state.sessionmaker, app.state.github, app.state.services),
            name="pr_poller",
        )
        app.state.pr_poller_task = poller_task

    # Run pruner — always launched; self-disables when retention <= 0.
    from src.agent.run_pruner import run_pruner
    app.state.run_pruner_task: asyncio.Task = asyncio.create_task(
        run_pruner(
            app.state.sessionmaker,
            interval_seconds=config.run_prune_interval_seconds,
        ),
        name="run_pruner",
    )

    # Heartbeat — writes agent_last_heartbeat to settings every 30 s so the
    # dashboard can show whether the agent is alive.
    app.state.heartbeat_task: asyncio.Task = asyncio.create_task(
        _heartbeat_loop(app.state.sessionmaker),
        name="heartbeat",
    )

    try:
        yield
    finally:
        for task_name, task in [
            ("pr_poller", app.state.pr_poller_task),
            ("run_pruner", app.state.run_pruner_task),
            ("heartbeat", app.state.heartbeat_task),
        ]:
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if app.state.background_tasks:
            log.info("waiting on %d in-flight task(s)", len(app.state.background_tasks))
            await asyncio.gather(*app.state.background_tasks, return_exceptions=True)
        await app.state.http.aclose()
        await app.state.db_engine.dispose()


app = FastAPI(title="llopster agent", version="0.6.0", lifespan=lifespan)

# Static files for the trigger UI (just the CSS — the dashboard owns the
# main UI surface).  Mounted with name="static" so the trigger templates'
# url_for('static', path='app.css') resolves correctly.
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)

# Trigger UI lives on the agent — it needs collector/patcher/github/slack.
app.include_router(trigger_router)
app.include_router(integrations_router)
app.include_router(license_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus scrape target — self-observability for the agent.

    Left unauthenticated on purpose: it exposes only aggregate counts/gauges
    (no log content, diffs, or secrets), and Prometheus scrape configs can't
    easily present the shared bearer. Restrict at the network layer if needed.
    """
    body = await render_metrics(request.app.state.sessionmaker)
    return Response(content=body, media_type="text/plain; version=0.0.4")


@app.post("/webhook", dependencies=[Depends(require_inbound_auth)])
async def alertmanager_webhook(request: Request) -> dict[str, Any]:
    payload = await request.json()
    alerts = parse_alertmanager_payload(payload)
    log.info("received %d alert(s) from alertmanager", len(alerts))

    state = request.app.state
    run_summaries: list[dict[str, Any]] = []

    # Read the kill-switch once per webhook firing — applies to every alert
    # in this batch. Doing it inside the loop would let a flip mid-batch
    # split a single AlertManager group across modes.
    async with state.sessionmaker() as session:
        mode = await get_processing_mode(session)

    for alert in alerts:
        # Pre-create dedup: if there's already an open Run (queued or in-flight)
        # for the same dedup_key, skip creating another row entirely. This is
        # what stops AlertManager's 5min repeat_interval from accumulating
        # thousands of queued rows in manual mode — and prevents in-flight
        # races in autopilot. Open-PR dedup still runs inside process_alert
        # for the case where the PR exists but the previous Run is terminal.
        dedup_key = compute_dedup_key(alert)
        async with state.sessionmaker() as session:
            existing = await find_open_run_by_dedup_key(session, dedup_key)
        if existing is not None:
            log.info(
                "  [skip] duplicate fire: alert=%s service=%s already open as run %s (status=%s)",
                alert.alertname, alert.service, existing.id, existing.processing_status,
            )
            run_summaries.append(
                {
                    "run_id": existing.id,
                    "alertname": alert.alertname,
                    "service": alert.service,
                    "status": existing.processing_status,
                    "deduped": True,
                }
            )
            continue

        async with state.sessionmaker() as session:
            run = await repo.create_run_from_alert(
                session, alert, raw_payload=payload, trigger_source="alertmanager",
            )

        if mode == MANUAL:
            # Park the Run at `queued` and skip dispatch. The dashboard
            # surfaces queued runs; the operator processes them via the
            # trigger UI when ready.
            async with state.sessionmaker() as session:
                await repo.update_status(
                    session, run.id, "queued",
                    error="processing_mode=manual — awaiting operator dispatch",
                )
            log.info(
                "  [%s] queued (manual mode): %s service=%s severity=%s",
                run.id, alert.alertname, alert.service, alert.severity,
            )
            run_summaries.append(
                {
                    "run_id": run.id,
                    "alertname": alert.alertname,
                    "service": alert.service,
                    "status": "queued",
                }
            )
            continue

        log.info(
            "  [%s] dispatched: %s service=%s severity=%s",
            run.id, alert.alertname, alert.service, alert.severity,
        )

        task = asyncio.create_task(
            process_alert(
                run.id,
                alert,
                sessionmaker=state.sessionmaker,
                collector=state.collector,
                services=state.services,
                patcher=state.patcher,
                github=state.github,
                slack=state.slack,
                triage=getattr(state, "triage", None),
                investigator=getattr(state, "investigator", None),
            )
        )
        state.background_tasks.add(task)
        task.add_done_callback(state.background_tasks.discard)

        run_summaries.append(
            {
                "run_id": run.id,
                "alertname": alert.alertname,
                "service": alert.service,
                "status": "pending",
            }
        )

    return {"received": len(alerts), "alerts": run_summaries, "mode": mode}

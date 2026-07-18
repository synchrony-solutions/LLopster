"""Dashboard FastAPI app — read-only view over the shared database.

This is a **separate process** from the agent (src/api/main.py). It connects
to the same PostgreSQL database but carries no agent components: no Loki
client, no Prometheus client, no Anthropic API key, no Slack/GitHub clients.

If the agent pod crashes, this service continues running and serves the full
run history, last-known agent heartbeat, and diagnostics — exactly the
information operators need to diagnose what went wrong.

Entrypoint (in the Helm chart):
    SERVICE=dashboard uvicorn src.dashboard.main:app --host 0.0.0.0 --port 8000

For local dev run both together:
    uvicorn src.api.main:app --port 8000 --reload &
    uvicorn src.dashboard.main:app --port 8001 --reload
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from fastapi import Depends

from src.api.auth import log_auth_status, require_inbound_auth
from src.config import config
from src.dashboard.runs_api import router as runs_router
from src.dashboard.settings_routes import router as settings_router
from src.dashboard.trigger_routes import router as trigger_router
from src.dashboard.web_routes import router as web_router
from src.db import create_engine, get_sessionmaker, init_schema

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    force=True,
)
log = logging.getLogger("llopster.dashboard")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Shared HTTP client (used by settings connection-test endpoints).
    app.state.http = httpx.AsyncClient(timeout=10.0)

    # Connect to the same DB as the agent. The agent owns Alembic migrations;
    # the dashboard only ensures tables exist (idempotent). This avoids the
    # race condition where both pods would try to insert the initial
    # alembic_version row on a fresh PostgreSQL.
    app.state.db_engine = create_engine(config.database_url)
    app.state.sessionmaker = get_sessionmaker(app.state.db_engine)
    await init_schema(app.state.db_engine, migrate=False)

    log_auth_status("dashboard")
    log.info("dashboard started (db=%s)", config.database_url.split("@")[-1])
    try:
        yield
    finally:
        await app.state.http.aclose()
        await app.state.db_engine.dispose()


app = FastAPI(title="llopster dashboard", version="0.6.0", lifespan=lifespan)
app.mount(
    "/static",
    StaticFiles(directory=str(Path(__file__).parent / "static")),
    name="static",
)
# The dashboard's read surface exposes raw prod log lines, proposed diffs, full
# LLM output, and system diagnostics. Gate every router behind the shared-secret
# check so a configured LLOPSTER_API_TOKEN protects reads too — not just the
# write mutations. require_inbound_auth is a no-op when no token is configured
# (fail-safe: local eval keeps working), so this only enforces once a secret is
# set. Settings/trigger routes keep their per-route deps as well (harmless).
_dashboard_auth = [Depends(require_inbound_auth)]
app.include_router(runs_router, dependencies=_dashboard_auth)
app.include_router(settings_router, dependencies=_dashboard_auth)
app.include_router(trigger_router, dependencies=_dashboard_auth)
app.include_router(web_router, dependencies=_dashboard_auth)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}

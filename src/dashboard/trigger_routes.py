"""Manual-trigger UI hosted on the dashboard.

History: the trigger form used to live on the agent (src/api/trigger_routes.py)
and the dashboard linked to it with `target="_blank"`. That worked locally
but broke as soon as the agent stopped being directly browser-reachable —
e.g. in production the agent only has in-cluster DNS, the dashboard ingress
is the public surface. Operators clicking the link landed on a host their
browser couldn't resolve.

This module exposes the same form on the dashboard and POSTs server-side
to the agent via httpx over in-cluster DNS. Same pattern as the run-dispatch
proxy in src/dashboard/web_routes.py.

The agent's POST /trigger still exists — it's now an internal API. We
parse the run-id from the agent's 303 Location header (`/trigger/{run_id}`)
and redirect the operator to `/runs/{run_id}` on the dashboard so they
stay on the host their browser can reach.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from src.api.auth import require_inbound_auth, resolve_api_token
from src.config import config
from src.dashboard.templating import templates
from src.db import repository as repo

log = logging.getLogger(__name__)

router = APIRouter()


# ---------------------------------------------------------------------------
# GET /trigger — form
# ---------------------------------------------------------------------------

@router.get("/trigger", response_class=HTMLResponse, name="trigger_form")
async def trigger_form(request: Request) -> HTMLResponse:
    sm = request.app.state.sessionmaker
    async with sm() as session:
        recent_runs = await repo.list_runs(session, limit=20)

    # We render the synthesize form's service dropdown from agent-known
    # services. Since this dashboard doesn't load services.yaml itself,
    # we just leave the list empty when the agent isn't reachable —
    # operators can still use Replay, which doesn't need the list.
    services: list[str] = []
    agent_url = (config.agent_url or "").rstrip("/")
    if agent_url:
        http = request.app.state.http
        try:
            resp = await http.get(f"{agent_url}/api/integrations/services", timeout=3.0)
            if resp.status_code == 200:
                services = resp.json().get("services", []) or []
        except Exception as e:  # noqa: BLE001
            log.warning("could not fetch service list from agent: %s", e)

    return templates.TemplateResponse(
        request,
        "trigger.html",
        {
            "active": "trigger",
            "recent_runs": recent_runs,
            "services": services,
            "agent_url": agent_url,
        },
    )


# ---------------------------------------------------------------------------
# POST /trigger — proxy to agent
# ---------------------------------------------------------------------------

@router.post(
    "/trigger",
    name="trigger_submit",
    dependencies=[Depends(require_inbound_auth)],
)
async def trigger_submit(
    request: Request,
    mode: Annotated[str, Form()],
    replay_run_id: Annotated[str | None, Form()] = None,
    service: Annotated[str | None, Form()] = None,
    alertname: Annotated[str | None, Form()] = None,
    severity: Annotated[str | None, Form()] = "warning",
    summary: Annotated[str | None, Form()] = "",
    description: Annotated[str | None, Form()] = "",
    lookback_minutes: Annotated[str | None, Form()] = None,
) -> RedirectResponse:
    agent_url = (config.agent_url or "").rstrip("/")
    if not agent_url:
        raise HTTPException(
            status_code=500,
            detail="AGENT_URL is not set on the dashboard pod — cannot proxy trigger submit",
        )

    # HTML forms submit empty fields as "" — normalise to None so we don't
    # forward a blank string to the agent, which would cause its own 422.
    _lb_raw = (lookback_minutes or "").strip()

    # Carry the form fields verbatim. None values are dropped so the
    # agent's defaults / required-field validation behave the same as
    # if the form posted directly to it.
    form_data = {
        "mode": mode,
        "replay_run_id": replay_run_id or "",
        "service": service or "",
        "alertname": alertname or "",
        "severity": severity or "warning",
        "summary": summary or "",
        "description": description or "",
    }
    if _lb_raw:
        form_data["lookback_minutes"] = _lb_raw

    # Forward the shared secret so the (now-authenticated) agent /trigger
    # accepts this server-side proxy call. When no secret is configured the
    # token is "" and we send no header — agent auth is disabled too.
    token = await resolve_api_token(request.app.state.sessionmaker)
    headers = {"Authorization": f"Bearer {token}"} if token else {}

    http = request.app.state.http
    try:
        resp = await http.post(
            f"{agent_url}/trigger",
            data=form_data,
            headers=headers,
            timeout=10.0,
            follow_redirects=False,  # we extract the new run_id from Location
        )
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=502,
            detail=f"could not reach agent at {agent_url}: {e}",
        )

    if resp.status_code != 303:
        # The agent uses HTTPException for validation problems (422) —
        # pass those through so the operator sees what was wrong instead
        # of a generic 500 from us.
        raise HTTPException(
            status_code=resp.status_code,
            detail=f"agent returned HTTP {resp.status_code}: {resp.text[:200]}",
        )

    # Agent always 303s to /trigger/{run_id}. We pluck the id off the
    # end and redirect to /runs/{run_id} on the dashboard so the
    # operator stays on the host their browser can reach.
    location = resp.headers.get("location", "")
    run_id = location.rsplit("/", 1)[-1] if location else ""
    if not run_id:
        raise HTTPException(
            status_code=502,
            detail=f"agent 303 had no usable Location header: {location!r}",
        )

    return RedirectResponse(url=f"/runs/{run_id}", status_code=303)

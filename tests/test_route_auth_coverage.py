"""Route-coverage guard for inbound auth.

Every state-changing route (POST/PUT/PATCH/DELETE) on BOTH the agent and the
dashboard app must either carry the ``require_inbound_auth`` dependency or be
explicitly listed in ``AUTH_EXEMPT`` below. This is the backstop that stops a
future new write route from silently shipping unguarded — exactly the gap that
left ``POST /trigger`` and ``POST /runs/{id}/label`` open on the dashboard while
the agent's own auth was added (a confused-deputy bypass, since the dashboard
forwards a valid bearer to the authenticated agent).

If you add a write route and this test fails: add ``dependencies=[Depends(
require_inbound_auth)]`` to it. Only add it to ``AUTH_EXEMPT`` if it genuinely
spends no LLM money, opens no PR, discloses no secret, and writes no
operator/eval data — and say why in the comment.
"""

from __future__ import annotations

from fastapi.routing import APIRoute

from src.api.auth import require_inbound_auth
from src.api.main import app as agent_app
from src.dashboard.main import app as dashboard_app

_WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}

# (method, path) pairs that are intentionally unauthenticated. Keep this small
# and justified — every entry is an explicit decision to leave a write surface
# open, reviewed here rather than forgotten in a decorator.
AUTH_EXEMPT: set[tuple[str, str]] = {
    # Connection-test endpoints. They spend no LLM money, open no PR, and never
    # return a secret (only a masked ok/detail). The agent pair is documented
    # as deliberately reusable by CLI/monitoring; the dashboard pair just
    # proxies to them. The /webhook NetworkPolicy + their low blast radius
    # cover the residual (a fixed Slack test message / a read-only GET /user).
    ("POST", "/api/integrations/test/notifier"),
    ("POST", "/api/integrations/test/github"),
    ("POST", "/settings/test/notifier"),
    ("POST", "/settings/test/github"),
}


def _has_inbound_auth(route: APIRoute) -> bool:
    """True if ``require_inbound_auth`` is anywhere in the route's dependant tree."""

    def walk(dependant) -> bool:
        for dep in dependant.dependencies:
            if dep.call is require_inbound_auth:
                return True
            if walk(dep):
                return True
        return False

    return walk(route.dependant)


def _write_routes(app):
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        methods = route.methods & _WRITE_METHODS
        if not methods:
            continue
        for method in sorted(methods):
            yield method, route


def test_every_state_changing_route_is_guarded_or_explicitly_exempt():
    unguarded: list[tuple[str, str]] = []
    for app in (agent_app, dashboard_app):
        for method, route in _write_routes(app):
            key = (method, route.path)
            if key in AUTH_EXEMPT:
                continue
            if not _has_inbound_auth(route):
                unguarded.append(key)

    assert not unguarded, (
        "state-changing route(s) missing require_inbound_auth and not in "
        f"AUTH_EXEMPT: {sorted(unguarded)}"
    )


def test_dashboard_read_surface_is_guarded():
    """The dashboard's READ routes disclose raw prod log lines, proposed diffs,
    full LLM output, and diagnostics — so they must carry require_inbound_auth
    too (it's a no-op until a token is configured). Regression guard for the gap
    where a set LLOPSTER_API_TOKEN protected writes but left reads wide open."""
    must_be_guarded = {
        ("GET", "/runs"),
        ("GET", "/runs/{run_id}"),
        ("GET", "/diagnostics"),
        ("GET", "/stats"),
        ("GET", "/api/runs"),
        ("GET", "/api/runs/{run_id}"),
    }
    guarded: set[tuple[str, str]] = set()
    for route in dashboard_app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not _has_inbound_auth(route):
            continue
        for method in route.methods:
            guarded.add((method, route.path))

    missing = must_be_guarded - guarded
    assert not missing, f"dashboard read route(s) not behind inbound auth: {sorted(missing)}"


def test_auth_exempt_entries_still_exist():
    """Guard against the allow-list rotting: every AUTH_EXEMPT entry must match
    a real route, so a renamed/removed route forces the exemption to be revisited
    rather than silently masking a typo."""
    live: set[tuple[str, str]] = set()
    for app in (agent_app, dashboard_app):
        for method, route in _write_routes(app):
            live.add((method, route.path))
    stale = AUTH_EXEMPT - live
    assert not stale, f"AUTH_EXEMPT entries match no live route: {sorted(stale)}"

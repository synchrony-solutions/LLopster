"""Pre-pipeline alert filtering.

Decides whether an incoming alert is worth running through the full
context-collection + LLM pipeline. Alerts that no patch could ever fix —
heartbeat alerts, infra-availability alerts, info-severity noise, alerts
for services we don't know about — are short-circuited before any Loki or
Prometheus queries run, so they cost zero LLM tokens and zero outbound
HTTP roundtrips.

The check still creates the `Run` row (so the dashboard surfaces what came
in) but advances `processing_status` straight to `skipped` with a clear
`skip_reason`. Visibility preserved, cost saved.

Configuration:
  - DEFAULT_IGNORE_ALERTNAMES is the built-in list (intentionally small —
    only alerts that are *definitionally* not patchable).
  - Operators can extend it via the `ignore_alertnames` setting (comma-
    separated string; lives in the settings table, edited from /settings).
  - Severity-level skip is fixed to `info` — anything below warning is
    informational by definition.
"""

from __future__ import annotations

from dataclasses import dataclass

from src.agent.alert_handler import ParsedAlert
from src.services_registry import ServiceRegistry


# Alerts whose presence is informational, not actionable. These are
# patterns we are confident never warrant a code patch.
DEFAULT_IGNORE_ALERTNAMES: frozenset[str] = frozenset({
    "AlwaysFiringDemoAlert",   # demo / canary; intentionally always firing
    "PrometheusTargetDown",    # infra availability — agent can't fix scrape failures
    "Watchdog",                # kube-prometheus-stack heartbeat convention
    "InfoInhibitor",           # kube-prometheus-stack noise
})

IGNORE_SEVERITIES: frozenset[str] = frozenset({"info", "informational", "none"})


@dataclass(frozen=True)
class SkipDecision:
    """Result of the pre-pipeline filter check.

    `skip` is True when the alert should NOT proceed to context collection /
    LLM. `reason` is a short human-readable string written into
    `Run.processing_status='skipped'` and `Run.error_message` so it shows up
    in the dashboard.
    """

    skip: bool
    reason: str | None = None


def should_skip(
    alert: ParsedAlert,
    *,
    services: ServiceRegistry,
    extra_ignore_alertnames: frozenset[str] | set[str] | None = None,
) -> SkipDecision:
    """Decide whether to short-circuit the pipeline for this alert.

    Order matters — we check cheapest / most-definitive reasons first so the
    skip_reason a user sees in the dashboard is the most informative one.
    """
    # Status check is the cheapest and the most decisive: we don't generate
    # patches for resolutions, just for active firing alerts.
    if alert.status != "firing":
        return SkipDecision(True, f"alert is not firing (status={alert.status})")

    # Severity-level rejection.
    if alert.severity and alert.severity.lower() in IGNORE_SEVERITIES:
        return SkipDecision(True, f"severity={alert.severity!r} is informational")

    # Alertname rejection. Built-in list + operator extension.
    ignore = set(DEFAULT_IGNORE_ALERTNAMES)
    if extra_ignore_alertnames:
        ignore |= {n.strip() for n in extra_ignore_alertnames if n and n.strip()}
    if alert.alertname in ignore:
        return SkipDecision(True, f"alertname {alert.alertname!r} is in the ignore list")

    # Service-registry rejection. The agent can't generate a patch for a
    # codebase it doesn't know about; surface this clearly rather than
    # paying for a Loki query first.
    if services.get(alert.service) is None:
        known = ", ".join(services.names()) or "(none)"
        return SkipDecision(
            True,
            f"service {alert.service!r} is not in services.yaml (known: {known})",
        )

    return SkipDecision(False)


def parse_extra_ignore_setting(raw: str | None) -> frozenset[str]:
    """Parse the comma-separated `ignore_alertnames` setting value into a set
    of alertnames. Empty / missing settings yield an empty frozenset."""
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())

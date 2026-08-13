"""Notification provider seam — one place that knows *which* messaging
channel to build, so the pipeline stays channel-agnostic.

Providers (``NOTIFIER_PROVIDER``):

* ``slack`` (default) — Slack incoming webhook, keyed by ``SLACK_WEBHOOK_URL``.
* ``teams`` — Microsoft Teams via a Power Automate Workflows webhook, keyed by
  ``TEAMS_WEBHOOK_URL``.
* ``none`` — notifications explicitly disabled (no channel, no warning).

Both concrete clients (``SlackClient``, ``TeamsClient``) expose the same
``post_patch(alert, proposal, pr_url)`` surface and a ``provider`` attribute,
so the processor calls one without branching. ``build_notifier`` returns the
active client, or ``None`` when notifications are off / unconfigured (the
processor records that as a skip reason, exactly as an unset webhook does
today).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Protocol, runtime_checkable

from src.integrations.slack_client import SlackClient
from src.integrations.teams_client import TeamsClient

if TYPE_CHECKING:
    import httpx

    from src.agent.alert_handler import ParsedAlert
    from src.agent.patch_generator import PatchProposal
    from src.config import Config

log = logging.getLogger("llopster.notifier")

PROVIDER_SLACK = "slack"
PROVIDER_TEAMS = "teams"
PROVIDER_NONE = "none"
VALID_PROVIDERS = (PROVIDER_SLACK, PROVIDER_TEAMS, PROVIDER_NONE)


@runtime_checkable
class Notifier(Protocol):
    """The interface the processor depends on. Implemented by SlackClient
    and TeamsClient."""

    provider: str

    async def post_patch(
        self, alert: "ParsedAlert", proposal: "PatchProposal", pr_url: str | None = ...
    ) -> None: ...


def normalize_provider(raw: str | None) -> str:
    """Coerce ``NOTIFIER_PROVIDER`` to a known provider. Unknown / empty →
    ``slack`` (the historical default) with a warning — never a crash."""
    provider = (raw or PROVIDER_SLACK).strip().lower()
    if provider not in VALID_PROVIDERS:
        log.warning(
            "NOTIFIER_PROVIDER=%r is not one of %s — falling back to %r",
            raw, VALID_PROVIDERS, PROVIDER_SLACK,
        )
        return PROVIDER_SLACK
    return provider


def active_webhook_url(config: "Config") -> str:
    """The webhook URL for the active provider ("" for none / unconfigured)."""
    provider = config.notifier_provider
    if provider == PROVIDER_SLACK:
        return config.slack_webhook_url
    if provider == PROVIDER_TEAMS:
        return config.teams_webhook_url
    return ""


def build_notifier(config: "Config", http_client: "httpx.AsyncClient") -> Notifier | None:
    """Build the notifier for the active provider, or None when disabled.

    Returns None (notifications skipped) when the provider is ``none`` or the
    active provider's webhook URL is unset — the processor treats both the
    same way it treats an unset SLACK_WEBHOOK_URL today.
    """
    provider = config.notifier_provider
    if provider == PROVIDER_NONE:
        log.info("NOTIFIER_PROVIDER=none — notifications disabled")
        return None
    if provider == PROVIDER_TEAMS:
        if not config.teams_webhook_url:
            log.warning("NOTIFIER_PROVIDER=teams but TEAMS_WEBHOOK_URL not set — notifications disabled")
            return None
        return TeamsClient(webhook_url=config.teams_webhook_url, client=http_client)
    # Default / slack
    if not config.slack_webhook_url:
        log.warning("SLACK_WEBHOOK_URL not set — Slack notifications disabled")
        return None
    return SlackClient(webhook_url=config.slack_webhook_url, client=http_client)

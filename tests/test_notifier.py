"""Tests for the notifier provider seam (src/integrations/notifier.py) and
the notifier-aware bits of Config it drives."""

from dataclasses import replace

import pytest

from src.config import Config
from src.integrations.notifier import (
    PROVIDER_NONE,
    PROVIDER_SLACK,
    PROVIDER_TEAMS,
    active_webhook_url,
    build_notifier,
    normalize_provider,
)
from src.integrations.slack_client import SlackClient
from src.integrations.teams_client import TeamsClient

SLACK_URL = "https://hooks.slack.com/services/T/B/x"
TEAMS_URL = "https://prod-1.westus.logic.azure.com/workflows/abc/triggers/manual/paths/invoke"


def _cfg(**overrides) -> Config:
    return replace(Config(), **overrides)


class _DummyHTTP:
    """Stand-in for httpx.AsyncClient — build_notifier only stores it."""


# ---------------------------------------------------------------------------
# normalize_provider
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("slack", PROVIDER_SLACK),
    ("teams", PROVIDER_TEAMS),
    ("TEAMS", PROVIDER_TEAMS),
    ("  none  ", PROVIDER_NONE),
    ("", PROVIDER_SLACK),
    (None, PROVIDER_SLACK),
    ("discord", PROVIDER_SLACK),  # unknown → safe fallback
])
def test_normalize_provider(raw, expected):
    assert normalize_provider(raw) == expected


# ---------------------------------------------------------------------------
# active_webhook_url
# ---------------------------------------------------------------------------

def test_active_webhook_url():
    assert active_webhook_url(_cfg(notifier_provider="slack", slack_webhook_url=SLACK_URL)) == SLACK_URL
    assert active_webhook_url(_cfg(notifier_provider="teams", teams_webhook_url=TEAMS_URL)) == TEAMS_URL
    assert active_webhook_url(_cfg(notifier_provider="none", slack_webhook_url=SLACK_URL)) == ""


# ---------------------------------------------------------------------------
# build_notifier
# ---------------------------------------------------------------------------

def test_build_slack_when_configured():
    n = build_notifier(_cfg(notifier_provider="slack", slack_webhook_url=SLACK_URL), _DummyHTTP())
    assert isinstance(n, SlackClient)
    assert n.provider == "slack"


def test_build_teams_when_configured():
    n = build_notifier(_cfg(notifier_provider="teams", teams_webhook_url=TEAMS_URL), _DummyHTTP())
    assert isinstance(n, TeamsClient)
    assert n.provider == "teams"


def test_build_none_provider_disables():
    n = build_notifier(_cfg(notifier_provider="none", slack_webhook_url=SLACK_URL, teams_webhook_url=TEAMS_URL), _DummyHTTP())
    assert n is None


def test_build_slack_without_url_disables():
    assert build_notifier(_cfg(notifier_provider="slack", slack_webhook_url=""), _DummyHTTP()) is None


def test_build_teams_without_url_disables():
    assert build_notifier(_cfg(notifier_provider="teams", teams_webhook_url=""), _DummyHTTP()) is None


def test_default_provider_is_slack_backcompat():
    # An existing SLACK_WEBHOOK_URL-only deployment (no NOTIFIER_PROVIDER) still
    # gets a Slack client — the pre-multi-provider behavior.
    cfg = replace(Config(), slack_webhook_url=SLACK_URL)  # notifier_provider default
    assert cfg.notifier_provider == "slack"
    assert isinstance(build_notifier(cfg, _DummyHTTP()), SlackClient)


# ---------------------------------------------------------------------------
# Config.notifier_configured
# ---------------------------------------------------------------------------

def test_notifier_configured():
    assert _cfg(notifier_provider="slack", slack_webhook_url=SLACK_URL).notifier_configured is True
    assert _cfg(notifier_provider="slack", slack_webhook_url="").notifier_configured is False
    assert _cfg(notifier_provider="teams", teams_webhook_url=TEAMS_URL).notifier_configured is True
    assert _cfg(notifier_provider="teams", teams_webhook_url="").notifier_configured is False
    assert _cfg(notifier_provider="none", slack_webhook_url=SLACK_URL).notifier_configured is False

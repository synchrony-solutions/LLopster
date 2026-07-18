"""Tests for the pre-pipeline alert filter.

The filter runs before any context collection, so a `True` skip decision
means zero Loki / Prometheus / LLM cost. These tests pin the exact rules
so accidental loosening of the filter (e.g. someone removing
`PrometheusTargetDown` from the default ignore list) is caught immediately.
"""

import textwrap
from datetime import datetime, timezone

import pytest

from src.agent.alert_filter import (
    DEFAULT_IGNORE_ALERTNAMES,
    parse_extra_ignore_setting,
    should_skip,
)
from src.agent.alert_handler import ParsedAlert
from src.services_registry import ServiceRegistry


def _alert(
    alertname: str = "TestAlert",
    service: str = "demo-app",
    severity: str = "warning",
    status: str = "firing",
) -> ParsedAlert:
    return ParsedAlert(
        fingerprint="fp",
        status=status,
        alertname=alertname,
        severity=severity,
        service=service,
        summary="x",
        description="y",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": service},
        annotations={},
        generator_url="",
    )


@pytest.fixture
def services(tmp_path):
    cfg = tmp_path / "services.yaml"
    cfg.write_text(textwrap.dedent("""
        demo-app:
          codebase_path: ./demo-app
          github_repo: org/demo-app
    """))
    return ServiceRegistry(str(cfg))


# ---------------------------------------------------------------------------
# Status check (cheapest)
# ---------------------------------------------------------------------------

def test_resolved_alerts_are_skipped(services):
    decision = should_skip(_alert(status="resolved"), services=services)
    assert decision.skip is True
    assert "not firing" in decision.reason


def test_firing_alerts_proceed(services):
    decision = should_skip(_alert(status="firing"), services=services)
    assert decision.skip is False
    assert decision.reason is None


# ---------------------------------------------------------------------------
# Severity check
# ---------------------------------------------------------------------------

def test_info_severity_is_skipped(services):
    decision = should_skip(_alert(severity="info"), services=services)
    assert decision.skip is True
    assert "info" in decision.reason


def test_info_severity_case_insensitive(services):
    decision = should_skip(_alert(severity="INFO"), services=services)
    assert decision.skip is True


def test_informational_severity_is_skipped(services):
    decision = should_skip(_alert(severity="informational"), services=services)
    assert decision.skip is True


def test_warning_severity_proceeds(services):
    decision = should_skip(_alert(severity="warning"), services=services)
    assert decision.skip is False


def test_critical_severity_proceeds(services):
    decision = should_skip(_alert(severity="critical"), services=services)
    assert decision.skip is False


# ---------------------------------------------------------------------------
# Alertname ignore-list
# ---------------------------------------------------------------------------

def test_default_ignore_alertnames(services):
    for name in ["AlwaysFiringDemoAlert", "PrometheusTargetDown", "Watchdog"]:
        decision = should_skip(_alert(alertname=name), services=services)
        assert decision.skip is True, f"{name} should be in default ignore list"
        assert name in decision.reason


def test_extra_ignore_alertnames_extends_default(services):
    decision = should_skip(
        _alert(alertname="MyCustomNoiseAlert"),
        services=services,
        extra_ignore_alertnames={"MyCustomNoiseAlert"},
    )
    assert decision.skip is True
    assert "MyCustomNoiseAlert" in decision.reason


def test_default_ignore_set_is_intentionally_small():
    # If this fails, someone added something — make sure the addition is
    # actually never-actionable, not just "rarely actionable."
    assert len(DEFAULT_IGNORE_ALERTNAMES) <= 10


# ---------------------------------------------------------------------------
# Service-registry check
# ---------------------------------------------------------------------------

def test_unmapped_service_is_skipped(services):
    decision = should_skip(_alert(service="payments"), services=services)
    assert decision.skip is True
    assert "payments" in decision.reason
    assert "services.yaml" in decision.reason
    # The reason should hint at known services so operators know what to add
    assert "demo-app" in decision.reason


def test_unmapped_service_with_empty_registry(tmp_path):
    empty_cfg = tmp_path / "empty.yaml"
    empty_cfg.write_text("")
    empty = ServiceRegistry(str(empty_cfg))
    decision = should_skip(_alert(), services=empty)
    assert decision.skip is True
    assert "(none)" in decision.reason


# ---------------------------------------------------------------------------
# Order of checks
# ---------------------------------------------------------------------------

def test_status_check_takes_precedence(services):
    """A resolved info-severity alert for an unknown service should report
    the most-decisive reason: not firing. Other checks shouldn't even run."""
    decision = should_skip(
        _alert(status="resolved", severity="info", service="unknown"),
        services=services,
    )
    assert decision.skip is True
    assert "not firing" in decision.reason


# ---------------------------------------------------------------------------
# Setting parser
# ---------------------------------------------------------------------------

def test_parse_extra_ignore_setting_handles_empty():
    assert parse_extra_ignore_setting(None) == frozenset()
    assert parse_extra_ignore_setting("") == frozenset()


def test_parse_extra_ignore_setting_strips_whitespace():
    parsed = parse_extra_ignore_setting("  Foo,Bar ,, Baz  ")
    assert parsed == frozenset({"Foo", "Bar", "Baz"})


def test_parse_extra_ignore_setting_dedups():
    parsed = parse_extra_ignore_setting("Foo,Foo,Bar")
    assert parsed == frozenset({"Foo", "Bar"})

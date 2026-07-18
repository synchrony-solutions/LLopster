"""Unit tests for the Haiku triage gate.

Mocks the Anthropic client so no live API calls run. Focus areas:
  - Decision/confidence parser including malformed responses (must
    fail-safe to PROCEED so a bad Haiku response can't silently drop
    a real incident).
  - Token usage round-trip from `usage` → TriageDecision.
  - Prompt assembly puts service-registered hint + key fields in the
    user blob (those drive Haiku's accuracy).
"""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.agent.alert_handler import ParsedAlert
from src.agent.triage import (
    SYSTEM_PROMPT,
    Triage,
    TriageDecision,
    _format_alert,
    _parse_decision,
)


def _alert(**overrides) -> ParsedAlert:
    base = dict(
        fingerprint="fp1",
        status="firing",
        alertname="DatabasePoolExhausted",
        severity="warning",
        service="demo-app",
        summary="Pool exhausted",
        description="Too many concurrent queries",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": "demo-app", "severity": "warning"},
        annotations={"runbook": "https://example.com/rb"},
        generator_url="",
    )
    base.update(overrides)
    return ParsedAlert(**base)


# ---------------------------------------------------------------------------
# _parse_decision
# ---------------------------------------------------------------------------

def test_parse_decision_proceed_standard():
    text = (
        "## Decision\nproceed\n\n"
        "## Confidence\n5/5 — Clear actionable error.\n\n"
        "## Reasoning\nPool exhaustion is a real outage symptom.\n"
    )
    proceed, confidence, reasoning = _parse_decision(text)
    assert proceed is True
    assert confidence == 5
    assert "real outage" in reasoning


def test_parse_decision_skip_standard():
    text = (
        "## Decision\nskip\n\n"
        "## Confidence\n4/5 — Watchdog heartbeat.\n\n"
        "## Reasoning\nThis is the synthetic AlwaysFiringDemoAlert.\n"
    )
    proceed, confidence, reasoning = _parse_decision(text)
    assert proceed is False
    assert confidence == 4
    assert "synthetic" in reasoning


def test_parse_decision_case_insensitive():
    text = "## Decision\nPROCEED\n## Confidence\n3/5 — meh.\n"
    proceed, _, _ = _parse_decision(text)
    assert proceed is True


def test_parse_decision_missing_section_fails_safe_to_proceed():
    """No `## Decision` header → fail-safe to PROCEED. The whole point
    of the gate is to skip noise; a malformed Haiku response that
    accidentally drops a real alert would be much worse than running
    the pipeline."""
    proceed, confidence, reasoning = _parse_decision("garbage response from haiku")
    assert proceed is True
    assert confidence == 0
    assert "missing" in reasoning


def test_parse_decision_missing_confidence_defaults_to_zero():
    text = "## Decision\nskip\n\n## Reasoning\nLooks noisy.\n"
    proceed, confidence, reasoning = _parse_decision(text)
    assert proceed is False
    assert confidence == 0
    assert "Looks noisy" in reasoning


def test_parse_decision_reasoning_falls_back_to_confidence_tail():
    """When there's no `## Reasoning` block, the tail of the confidence
    line still gives useful operator-visible context."""
    text = "## Decision\nskip\n## Confidence\n4/5 — Pure noise, no code path can fix."
    _, _, reasoning = _parse_decision(text)
    assert "Pure noise" in reasoning


# ---------------------------------------------------------------------------
# _format_alert
# ---------------------------------------------------------------------------

def test_format_alert_includes_service_known_hint():
    blob = _format_alert(_alert(), service_known=True)
    assert "demo-app" in blob
    assert "registered in services.yaml" in blob
    assert "DatabasePoolExhausted" in blob
    assert "runbook" in blob


def test_format_alert_marks_unknown_service():
    blob = _format_alert(_alert(service="ghost-service"), service_known=False)
    assert "ghost-service" in blob
    assert "NOT in services.yaml" in blob


# ---------------------------------------------------------------------------
# System prompt sanity check
# ---------------------------------------------------------------------------

def test_system_prompt_biases_to_proceed_on_uncertainty():
    """The fail-safe philosophy lives in the system prompt — operators
    must be able to trust that a confused Haiku errs on the side of
    running the pipeline."""
    assert "When in doubt" in SYSTEM_PROMPT
    assert "PROCEED" in SYSTEM_PROMPT
    # Output format the parser depends on must be in the prompt
    assert "## Decision" in SYSTEM_PROMPT
    assert "## Confidence" in SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Triage.evaluate
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_evaluate_round_trips_skip_decision():
    triage = Triage(api_key="sk-test", model="claude-haiku-4-5")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(
            type="text",
            text=(
                "## Decision\nskip\n"
                "## Confidence\n5/5 — Watchdog.\n"
                "## Reasoning\nMonitoring liveness signal, not an incident.\n"
            ),
        )],
        model="claude-haiku-4-5",
        usage=SimpleNamespace(
            input_tokens=400,
            output_tokens=80,
            cache_read_input_tokens=0,
            cache_creation_input_tokens=0,
        ),
    )
    triage.client.messages.create = AsyncMock(return_value=fake_response)

    decision = await triage.evaluate(_alert(), service_known=True)
    assert isinstance(decision, TriageDecision)
    assert decision.proceed is False
    assert decision.confidence == 5
    assert decision.decision_label == "skip"
    assert decision.model == "claude-haiku-4-5"
    assert decision.input_tokens == 400
    assert decision.output_tokens == 80

    kwargs = triage.client.messages.create.await_args.kwargs
    assert kwargs["model"] == "claude-haiku-4-5"
    # Haiku 4.5 rejects adaptive thinking — the triage call deliberately
    # omits the `thinking` arg so a docker-compose smoke test doesn't 400
    # and silently fail-open on every alert.
    assert "thinking" not in kwargs
    # The user block must carry the registered-service hint Haiku needs.
    user_text = kwargs["messages"][0]["content"][0]["text"]
    assert "registered in services.yaml" in user_text


@pytest.mark.asyncio
async def test_evaluate_handles_missing_usage_cache_fields():
    triage = Triage(api_key="sk-test", model="claude-haiku-4-5")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(
            type="text",
            text="## Decision\nproceed\n## Confidence\n4/5 — actionable.\n",
        )],
        model="claude-haiku-4-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    triage.client.messages.create = AsyncMock(return_value=fake_response)
    decision = await triage.evaluate(_alert(), service_known=False)
    assert decision.cache_read_tokens == 0
    assert decision.cache_creation_tokens == 0


@pytest.mark.asyncio
async def test_evaluate_fails_safe_on_garbage_response():
    triage = Triage(api_key="sk-test", model="claude-haiku-4-5")
    fake_response = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="hello i am a confused haiku")],
        model="claude-haiku-4-5",
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )
    triage.client.messages.create = AsyncMock(return_value=fake_response)
    decision = await triage.evaluate(_alert(), service_known=True)
    # Fail-safe: garbage means proceed with confidence 0.
    assert decision.proceed is True
    assert decision.confidence == 0

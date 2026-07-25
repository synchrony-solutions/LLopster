"""Tests for the run-history repository against an in-memory SQLite."""

from datetime import datetime, timezone

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.agent.context_collector import AlertContext
from src.agent.patch_generator import PatchProposal
from src.db import repository as repo
from src.db.models import Base
from src.integrations.loki_client import LogLine
from src.integrations.prometheus_client import MetricSample


@pytest.fixture
async def session():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    async with sm() as s:
        yield s
    await engine.dispose()


def _alert(alertname: str = "TestAlert", service: str = "demo-app") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="fp123",
        status="firing",
        alertname=alertname,
        severity="warning",
        service=service,
        summary="x",
        description="y",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": service},
        annotations={},
        generator_url="http://localhost:9090/graph?g0.expr=vector%281%29",
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

async def test_create_run_starts_in_pending(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={"x": 1})
    assert run.id
    assert run.processing_status == "pending"
    assert run.alertname == "TestAlert"
    assert run.service == "demo-app"
    assert run.alert_payload_json == {"x": 1}
    assert run.trigger_source == "alertmanager"


async def test_update_status_persists(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    await repo.update_status(session, run.id, "collecting")
    fetched = await repo.get_run(session, run.id)
    assert fetched.processing_status == "collecting"


async def test_update_status_with_error(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    await repo.update_status(session, run.id, "failed", error="boom")
    fetched = await repo.get_run(session, run.id)
    assert fetched.processing_status == "failed"
    assert fetched.error_message == "boom"


# ---------------------------------------------------------------------------
# Context recording
# ---------------------------------------------------------------------------

async def test_record_collected_context(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    ctx = AlertContext(
        alert=_alert(),
        log_lines=[
            LogLine(
                timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
                line="ERROR boom",
                labels={"service": "demo-app"},
            )
        ],
        metric_samples=[
            MetricSample(
                metric={"__name__": "x"},
                value=1.0,
                timestamp=datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc),
            )
        ],
        queries_used={"logql": '{service="demo-app"}', "promql": "vector(1)"},
        errors=["loki query timed out once"],
    )
    await repo.record_collected_context(session, run.id, ctx, lookback_minutes=30)
    fetched = await repo.get_run(session, run.id)
    assert fetched.logql_used == '{service="demo-app"}'
    assert fetched.promql_used == "vector(1)"
    assert fetched.lookback_minutes == 30
    assert fetched.log_line_count == 1
    assert fetched.metric_sample_count == 1
    assert fetched.collection_errors_json == ["loki query timed out once"]
    assert fetched.log_lines_json[0]["line"] == "ERROR boom"
    assert fetched.metric_samples_json[0]["value"] == 1.0


# ---------------------------------------------------------------------------
# LLM recording
# ---------------------------------------------------------------------------

async def test_record_llm_response(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    proposal = PatchProposal(
        text="## Root Cause\nbug\n## Confidence\n4/5 — clear\n",
        model="claude-opus-4-7",
        input_tokens=1000,
        output_tokens=200,
        cache_read_tokens=800,
        cache_creation_tokens=0,
        confidence=4,
        confidence_reason="clear",
    )
    await repo.record_llm_response(
        session, run.id, proposal, latency_ms=12345,
        parsed_root_cause="bug", parsed_diff="--- a/x\n+++ b/x\n",
    )
    fetched = await repo.get_run(session, run.id)
    assert fetched.model == "claude-opus-4-7"
    assert fetched.input_tokens == 1000
    assert fetched.cache_read_tokens == 800
    assert fetched.llm_latency_ms == 12345
    assert fetched.parsed_confidence == 4
    assert fetched.parsed_root_cause == "bug"
    assert "--- a/x" in fetched.parsed_diff
    assert fetched.llm_response_text.startswith("## Root Cause")


# ---------------------------------------------------------------------------
# Outcome recording
# ---------------------------------------------------------------------------

async def test_record_pr_opened(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    await repo.record_pr(
        session, run.id, pr_url="https://gh/x/y/pull/1",
        pr_number=1, pr_branch="llopster/x",
    )
    fetched = await repo.get_run(session, run.id)
    assert fetched.pr_opened is True
    assert fetched.pr_number == 1
    assert fetched.pr_skip_reason is None


async def test_record_pr_skipped(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    await repo.record_pr(session, run.id, skip_reason="confidence too low")
    fetched = await repo.get_run(session, run.id)
    assert fetched.pr_opened is False
    assert fetched.pr_skip_reason == "confidence too low"


async def test_record_notification(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    await repo.record_notification(session, run.id, notified=True)
    fetched = await repo.get_run(session, run.id)
    assert fetched.slack_notified is True
    assert fetched.slack_skip_reason is None


async def test_record_investigation_round_trip(session):
    """Sonnet investigation output + token usage land in the dedicated
    columns. Affected files survive as a JSON list."""
    from src.agent.investigator import Investigation

    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    investigation = Investigation(
        root_cause="The memory unit is invalid",
        affected_files=["helm-values.yaml", "check_helm_values.py"],
        confidence=4,
        reasoning="Logs name the file",
        response_text="## Root Cause Hypothesis\n...",
        model="claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=300,
        cache_read_tokens=1500,
        cache_creation_tokens=0,
    )
    await repo.record_investigation(session, run.id, investigation, latency_ms=1234)
    fetched = await repo.get_run(session, run.id)
    assert fetched.investigation_model == "claude-sonnet-4-6"
    assert fetched.investigation_confidence == 4
    assert fetched.investigation_affected_files_json == [
        "helm-values.yaml", "check_helm_values.py",
    ]
    assert fetched.investigation_latency_ms == 1234


async def test_record_triage_round_trip(session):
    """Triage decision + Haiku token usage land in the dedicated columns so
    the dashboard can show per-stage cost without parsing JSON blobs."""
    from src.agent.triage import TriageDecision

    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    decision = TriageDecision(
        proceed=False,
        confidence=4,
        reasoning="Watchdog heartbeat — no code path can fix.",
        model="claude-haiku-4-5",
        input_tokens=420,
        output_tokens=85,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )
    await repo.record_triage(session, run.id, decision)
    fetched = await repo.get_run(session, run.id)
    assert fetched.triage_decision == "skip"
    assert fetched.triage_confidence == 4
    assert fetched.triage_model == "claude-haiku-4-5"
    assert fetched.triage_input_tokens == 420
    assert fetched.triage_output_tokens == 85
    assert "Watchdog" in fetched.triage_reasoning


# ---------------------------------------------------------------------------
# List + count
# ---------------------------------------------------------------------------

async def test_list_runs_orders_newest_first(session):
    await repo.create_run_from_alert(session, _alert("First"), raw_payload={})
    await repo.create_run_from_alert(session, _alert("Second"), raw_payload={})
    rows = await repo.list_runs(session)
    assert [r.alertname for r in rows] == ["Second", "First"]


async def test_list_runs_filters_by_service(session):
    await repo.create_run_from_alert(session, _alert(service="payments"), raw_payload={})
    await repo.create_run_from_alert(session, _alert(service="demo-app"), raw_payload={})
    rows = await repo.list_runs(session, service="payments")
    assert len(rows) == 1
    assert rows[0].service == "payments"


async def test_count_runs_respects_filter(session):
    await repo.create_run_from_alert(session, _alert("A"), raw_payload={})
    await repo.create_run_from_alert(session, _alert("B"), raw_payload={})
    await repo.create_run_from_alert(session, _alert("A"), raw_payload={})
    assert await repo.count_runs(session) == 3
    assert await repo.count_runs(session, alertname="A") == 2


# ---------------------------------------------------------------------------
# Setting CRUD
# ---------------------------------------------------------------------------

async def test_get_setting_returns_default_when_missing(session):
    val = await repo.get_setting(session, "nonexistent_key", default="fallback")
    assert val == "fallback"


async def test_get_setting_returns_none_when_no_default(session):
    val = await repo.get_setting(session, "nonexistent_key")
    assert val is None


async def test_set_setting_creates_and_retrieves(session):
    await repo.set_setting(session, "my_key", "my_value")
    val = await repo.get_setting(session, "my_key")
    assert val == "my_value"


async def test_set_setting_updates_existing(session):
    await repo.set_setting(session, "key", "first")
    await repo.set_setting(session, "key", "second")
    val = await repo.get_setting(session, "key")
    assert val == "second"


async def test_get_all_settings_empty(session):
    result = await repo.get_all_settings(session)
    assert result == {}


async def test_get_all_settings_returns_all(session):
    await repo.set_setting(session, "a", "1")
    await repo.set_setting(session, "b", "2")
    result = await repo.get_all_settings(session)
    assert result == {"a": "1", "b": "2"}


# ---------------------------------------------------------------------------
# Operator ground-truth labels (eval flywheel)
# ---------------------------------------------------------------------------

async def test_new_run_has_no_operator_label(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    assert run.operator_label is None
    assert run.operator_label_note is None
    assert run.operator_labeled_at is None
    assert run.operator_labeled_by is None


async def test_set_operator_label_persists(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    updated = await repo.set_operator_label(
        session, run.id, "correct", note="fixed the units bug", labeled_by="alice",
    )
    assert updated is not None
    assert updated.operator_label == "correct"
    assert updated.operator_label_note == "fixed the units bug"
    assert updated.operator_labeled_at is not None
    assert updated.operator_labeled_by == "alice"

    fetched = await repo.get_run(session, run.id)
    assert fetched.operator_label == "correct"


async def test_set_operator_label_normalizes_case_and_whitespace(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    updated = await repo.set_operator_label(session, run.id, "  Partial  ")
    assert updated.operator_label == "partial"
    # Empty note normalizes to None rather than "".
    assert updated.operator_label_note is None


async def test_set_operator_label_rejects_invalid(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    with pytest.raises(ValueError):
        await repo.set_operator_label(session, run.id, "definitely-not-a-label")
    # Nothing was written.
    fetched = await repo.get_run(session, run.id)
    assert fetched.operator_label is None


async def test_clearing_operator_label_wipes_metadata(session):
    run = await repo.create_run_from_alert(session, _alert(), raw_payload={})
    await repo.set_operator_label(session, run.id, "wrong", note="re-broke it")
    cleared = await repo.set_operator_label(session, run.id, "")
    assert cleared.operator_label is None
    assert cleared.operator_label_note is None
    assert cleared.operator_labeled_at is None
    assert cleared.operator_labeled_by is None


async def test_set_operator_label_missing_run_returns_none(session):
    result = await repo.set_operator_label(session, "no-such-run", "correct")
    assert result is None


async def test_list_runs_filters_by_operator_label(session):
    a = await repo.create_run_from_alert(session, _alert("A"), raw_payload={})
    b = await repo.create_run_from_alert(session, _alert("B"), raw_payload={})
    await repo.create_run_from_alert(session, _alert("C"), raw_payload={})
    await repo.set_operator_label(session, a.id, "correct")
    await repo.set_operator_label(session, b.id, "wrong")

    correct = await repo.list_runs(session, operator_label="correct")
    assert {r.alertname for r in correct} == {"A"}
    assert await repo.count_runs(session, operator_label="correct") == 1


async def test_list_runs_labeled_and_unlabeled_pseudofilters(session):
    a = await repo.create_run_from_alert(session, _alert("A"), raw_payload={})
    await repo.create_run_from_alert(session, _alert("B"), raw_payload={})
    await repo.set_operator_label(session, a.id, "partial")

    labeled = await repo.list_runs(session, operator_label="labeled")
    assert {r.alertname for r in labeled} == {"A"}
    unlabeled = await repo.list_runs(session, operator_label="unlabeled")
    assert {r.alertname for r in unlabeled} == {"B"}
    assert await repo.count_runs(session, operator_label="unlabeled") == 1


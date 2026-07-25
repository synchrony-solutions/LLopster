"""Integration tests for the processor pipeline.

Verifies that `process_alert` walks the Run row through the expected status
transitions and persists context, LLM output, and outcome decisions —
without touching any live external service.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.agent.context_collector import AlertContext
from src.agent.cost_breaker import BreakerDecision
from src.agent.investigator import Investigation
from src.agent.patch_generator import PatchProposal
from src.agent.processing_mode import MANUAL, get_processing_mode
from src.agent.processor import process_alert
from src.agent.triage import TriageDecision
from src.db import repository as repo
from src.db.models import Base
from src.integrations.github_client import PatchApplyError, PullRequest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
async def sessionmaker_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _alert(status: str = "firing", service: str = "demo-app") -> ParsedAlert:
    return ParsedAlert(
        fingerprint="fp1",
        status=status,
        alertname="HelmValuesMisconfigured",
        severity="warning",
        service=service,
        summary="bad",
        description="bad",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": service},
        annotations={},
        generator_url="",
    )


def _ctx(alert: ParsedAlert) -> AlertContext:
    return AlertContext(
        alert=alert,
        log_lines=[],
        metric_samples=[],
        queries_used={"logql": '{service="demo-app"}', "promql": "vector(1)"},
        errors=[],
    )


def _proposal(confidence: int = 5, with_diff: bool = True) -> PatchProposal:
    diff_block = (
        "\n## Proposed Patch\n```diff\n--- a/x.yaml\n+++ b/x.yaml\n@@ -1 +1 @@\n-old\n+new\n```\n"
        if with_diff else ""
    )
    return PatchProposal(
        text=f"## Root Cause\nbug{diff_block}\n## Confidence\n{confidence}/5 — clear\n",
        model="claude-opus-4-7",
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=80,
        cache_creation_tokens=0,
        confidence=confidence,
        confidence_reason="clear",
    )


def _triage_decision(proceed: bool = True, confidence: int = 5, reasoning: str = "ok") -> TriageDecision:
    return TriageDecision(
        proceed=proceed,
        confidence=confidence,
        reasoning=reasoning,
        model="claude-haiku-4-5",
        input_tokens=300,
        output_tokens=50,
        cache_read_tokens=0,
        cache_creation_tokens=0,
    )


def _triage_mock(proceed: bool = True, confidence: int = 5) -> MagicMock:
    triage = MagicMock()
    triage.evaluate = AsyncMock(return_value=_triage_decision(proceed, confidence))
    return triage


def _investigation(
    *,
    affected_files: list[str] | None = None,
    confidence: int = 4,
    root_cause: str = "the helm memory unit is invalid",
) -> Investigation:
    return Investigation(
        root_cause=root_cause,
        affected_files=affected_files if affected_files is not None else ["helm-values.yaml"],
        confidence=confidence,
        reasoning="logs name the file",
        response_text="## Root Cause Hypothesis\n...",
        model="claude-sonnet-4-6",
        input_tokens=2000,
        output_tokens=300,
        cache_read_tokens=1500,
        cache_creation_tokens=0,
    )


def _investigator_mock(**kwargs) -> MagicMock:
    inv = MagicMock()
    inv.investigate = AsyncMock(return_value=_investigation(**kwargs))
    return inv


def _services(returns_config: bool = True) -> MagicMock:
    services = MagicMock()
    if returns_config:
        cfg = MagicMock()
        cfg.codebase_path = "./demo-app"
        cfg.github_repo = "owner/repo"
        services.get.return_value = cfg
    else:
        services.get.return_value = None
    services.names.return_value = ["demo-app"]
    return services


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

async def test_full_pipeline_records_each_phase(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))

    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))

    github = MagicMock()
    github.open_pr = AsyncMock(
        return_value=PullRequest(
            number=42, url="https://gh/owner/repo/pull/42", branch="llopster/x",
        )
    )

    slack = MagicMock()
    slack.post_patch = AsyncMock()

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=github,
        notifier=slack,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)

    assert fetched.processing_status == "done"
    assert fetched.logql_used == '{service="demo-app"}'
    assert fetched.model == "claude-opus-4-7"
    assert fetched.parsed_confidence == 5
    assert fetched.pr_opened is True
    assert fetched.pr_url == "https://gh/owner/repo/pull/42"
    assert fetched.slack_notified is True
    assert fetched.error_message is None


# ---------------------------------------------------------------------------
# Skip / gate paths
# ---------------------------------------------------------------------------

async def test_unmapped_service_marks_skipped(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock()  # should not be called

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(returns_config=False),
        patcher=patcher,
        github=None,
        notifier=None,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "skipped"
    patcher.generate.assert_not_awaited()
    # Pre-filter must short-circuit BEFORE Loki/Prom collection — otherwise
    # we burn cost on every unknown-service alert.
    collector.collect.assert_not_awaited()


async def test_pre_filter_skips_known_noise_alertname(sessionmaker_fixture):
    """`AlwaysFiringDemoAlert` is in the default ignore list. The filter
    must short-circuit it before any phase begins, even when the service IS
    registered."""
    sm = sessionmaker_fixture
    alert = _alert()
    alert = ParsedAlert(
        fingerprint=alert.fingerprint, status=alert.status,
        alertname="AlwaysFiringDemoAlert", severity=alert.severity,
        service=alert.service, summary=alert.summary, description=alert.description,
        starts_at=alert.starts_at, ends_at=alert.ends_at,
        labels=alert.labels, annotations=alert.annotations,
        generator_url=alert.generator_url,
    )
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock()
    patcher = MagicMock()
    patcher.generate = AsyncMock()

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),  # service IS registered; filter still rejects on alertname
        patcher=patcher,
        github=None,
        notifier=None,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "skipped"
    assert "ignore list" in fetched.error_message
    collector.collect.assert_not_awaited()
    patcher.generate.assert_not_awaited()


async def test_low_confidence_skips_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=2))
    github = MagicMock()
    github.open_pr = AsyncMock()  # should not be called
    slack = MagicMock()
    slack.post_patch = AsyncMock()

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=github,
        notifier=slack,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    assert fetched.pr_opened is False
    assert "below threshold" in fetched.pr_skip_reason
    github.open_pr.assert_not_awaited()
    # Slack still gets notified even when PR is skipped
    assert fetched.slack_notified is True


async def test_patch_apply_error_fails_the_run(sessionmaker_fixture):
    """A PatchApplyError from open_pr (the diff didn't match the file) must
    fail the run, not silently report `done` — patch corruption is a
    correctness failure, not a benign PR skip."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    github = MagicMock()
    github.open_pr = AsyncMock(
        side_effect=PatchApplyError("x.yaml: context mismatch at line 1")
    )
    slack = MagicMock()
    slack.post_patch = AsyncMock()

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=github,
        notifier=slack,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "failed"
    assert fetched.pr_opened is False
    assert "patch verification failed" in fetched.error_message
    # Fail closed: no Slack notification went out for a corrupt patch.
    slack.post_patch.assert_not_awaited()


async def test_cost_breaker_trip_parks_run_and_flips_to_manual(sessionmaker_fixture):
    """When the breaker trips, the run parks at `queued`, the agent flips to
    manual mode, and no LLM call happens (short-circuit before synthesis)."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock()
    patcher = MagicMock()
    patcher.generate = AsyncMock()  # must NOT be called

    with patch(
        "src.agent.processor.check_cost_breaker",
        new=AsyncMock(return_value=BreakerDecision(True, "cost breaker: 99 runs in the last hour")),
    ):
        await process_alert(
            run.id, alert,
            sessionmaker=sm,
            collector=collector,
            services=_services(),
            patcher=patcher,
            github=None,
            notifier=None,
        )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
        mode = await get_processing_mode(s)
    assert fetched.processing_status == "queued"
    assert "cost breaker" in fetched.error_message
    assert mode == MANUAL
    collector.collect.assert_not_awaited()
    patcher.generate.assert_not_awaited()


async def test_cost_breaker_bypassed_for_operator_runs(sessionmaker_fixture):
    """Operator-initiated runs (enforce_cost_breaker=False) ignore the breaker
    so the operator can still drain the queue after a trip."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    slack = MagicMock()
    slack.post_patch = AsyncMock()

    breaker = AsyncMock(return_value=BreakerDecision(True, "would trip"))
    with patch("src.agent.processor.check_cost_breaker", new=breaker):
        await process_alert(
            run.id, alert,
            sessionmaker=sm,
            collector=collector,
            services=_services(),
            patcher=patcher,
            github=None,
            notifier=slack,
            enforce_cost_breaker=False,
        )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    # Breaker never consulted; pipeline ran to completion.
    breaker.assert_not_awaited()
    patcher.generate.assert_awaited()
    assert fetched.processing_status == "done"


async def test_no_actionable_patch_skips_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5, with_diff=False))
    github = MagicMock()
    github.open_pr = AsyncMock()

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=github,
        notifier=None,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.pr_opened is False
    assert "no actionable patch" in fetched.pr_skip_reason
    github.open_pr.assert_not_awaited()


async def test_triage_skip_short_circuits_pipeline(sessionmaker_fixture):
    """A high-confidence triage skip must record the decision AND prevent
    any Loki/Prom/Anthropic work. This is the entire cost-saving point of
    Phase A — if the test only checks the status, a refactor could leave
    collector calls in place and the gate would burn money silently."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock()
    patcher = MagicMock()
    patcher.generate = AsyncMock()
    triage = _triage_mock(proceed=False, confidence=5)

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        triage=triage,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "skipped"
    assert fetched.triage_decision == "skip"
    assert fetched.triage_confidence == 5
    assert "triage-skip" in fetched.error_message
    # Triage skip path MUST NOT pay for context collection or synthesis.
    collector.collect.assert_not_awaited()
    patcher.generate.assert_not_awaited()


async def test_triage_low_confidence_skip_falls_through_to_pipeline(sessionmaker_fixture):
    """Confidence 2 with skip=true → still proceed. Better to overspend on
    Opus than to miss a real incident on a wobbly Haiku call."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    triage = _triage_mock(proceed=False, confidence=2)

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        triage=triage,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    assert fetched.triage_decision == "skip"  # decision recorded ...
    collector.collect.assert_awaited_once()    # ... but pipeline still ran
    patcher.generate.assert_awaited_once()


async def test_triage_proceed_runs_full_pipeline(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    triage = _triage_mock(proceed=True, confidence=4)

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        triage=triage,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    assert fetched.triage_decision == "proceed"
    assert fetched.triage_input_tokens == 300


async def test_critical_severity_bypasses_triage_entirely(sessionmaker_fixture):
    """Critical alerts skip the Haiku call so the highest-stakes path has
    zero triage token cost AND no failure mode where Haiku mis-skips a P1."""
    sm = sessionmaker_fixture
    alert = ParsedAlert(
        fingerprint="fp1", status="firing", alertname="HelmValuesMisconfigured",
        severity="critical", service="demo-app",
        summary="bad", description="bad",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc), ends_at=None,
        labels={"service": "demo-app"}, annotations={}, generator_url="",
    )
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    triage = _triage_mock(proceed=False, confidence=5)  # would skip if called

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        triage=triage,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    assert fetched.triage_decision is None  # bypassed → no row written
    triage.evaluate.assert_not_awaited()
    patcher.generate.assert_awaited_once()


async def test_triage_disabled_setting_bypasses_gate(sessionmaker_fixture):
    """`triage_enabled=false` in the settings table must disable the gate
    even when a Triage instance is wired in — that's the operator kill
    switch when Haiku starts dropping legit alerts."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        await repo.set_setting(s, "triage_enabled", "false")
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    triage = _triage_mock(proceed=False, confidence=5)

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        triage=triage,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    triage.evaluate.assert_not_awaited()


async def test_triage_failure_falls_through_to_pipeline(sessionmaker_fixture):
    """If the Haiku API call raises, the gate must fail-open. A Haiku
    outage must NOT block real incidents from reaching the Opus pipeline."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    triage = MagicMock()
    triage.evaluate = AsyncMock(side_effect=RuntimeError("anthropic 503"))

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        triage=triage,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    patcher.generate.assert_awaited_once()


# ---------------------------------------------------------------------------
# Investigation stage (Sonnet). Phase B records the investigation on
# the Run row; Phase C also forwards it into the patcher so Opus can
# narrow its codebase blob. PatchGenerator owns the narrow-vs-full
# fallback ladder — the processor's only job is "pass it through".
# ---------------------------------------------------------------------------

async def test_investigation_runs_after_collection_and_records_output(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    investigator = _investigator_mock(
        affected_files=["helm-values.yaml", "check_helm_values.py"],
        confidence=4,
    )

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        triage=None,
        investigator=investigator,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    assert fetched.investigation_model == "claude-sonnet-4-6"
    assert fetched.investigation_confidence == 4
    assert fetched.investigation_affected_files_json == [
        "helm-values.yaml", "check_helm_values.py",
    ]
    assert fetched.investigation_input_tokens == 2000
    assert fetched.investigation_latency_ms is not None
    # Investigation runs after collection
    collector.collect.assert_awaited_once()
    patcher.generate.assert_awaited_once()
    # Phase C: the investigation must flow into the patcher kwargs so
    # Opus can build the narrowed codebase blob.
    patcher_kwargs = patcher.generate.await_args.kwargs
    assert patcher_kwargs.get("investigation") is not None
    assert patcher_kwargs["investigation"].affected_files == [
        "helm-values.yaml", "check_helm_values.py",
    ]


async def test_synthesis_records_narrowed_context_flag(sessionmaker_fixture):
    """When PatchGenerator returns `used_narrowed_context=True`, the
    repository must persist that on the Run row so the dashboard badge
    and future per-mode spend stats can read it."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    # Hand-craft a proposal whose narrowed flag is True (the default
    # in _proposal is False to keep the older tests stable).
    narrowed_proposal = PatchProposal(
        text=_proposal(confidence=5).text,
        model="claude-opus-4-7",
        input_tokens=100, output_tokens=50,
        cache_read_tokens=80, cache_creation_tokens=0,
        confidence=5, confidence_reason="clear",
        used_narrowed_context=True, file_count=2,
    )
    patcher.generate = AsyncMock(return_value=narrowed_proposal)
    investigator = _investigator_mock(
        affected_files=["helm-values.yaml", "check_helm_values.py"],
    )

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        investigator=investigator,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.synthesis_used_narrowed_context is True


async def test_investigation_failure_leaves_no_investigation_for_synthesis(sessionmaker_fixture):
    """Fail-open path: investigation errored → patcher receives
    investigation=None and Opus runs against the full codebase
    (PatchGenerator's first fallback rung)."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    investigator = MagicMock()
    investigator.investigate = AsyncMock(side_effect=RuntimeError("anthropic 503"))

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        investigator=investigator,
    )

    patcher_kwargs = patcher.generate.await_args.kwargs
    # No investigation flowed into synthesis → Opus sees the full tree.
    assert patcher_kwargs.get("investigation") is None


async def test_investigation_receives_triage_reasoning(sessionmaker_fixture):
    """Sonnet builds on Haiku's framing rather than starting cold. The
    triage decision's reasoning must reach the investigator call."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    triage = MagicMock()
    triage.evaluate = AsyncMock(return_value=TriageDecision(
        proceed=True, confidence=4,
        reasoning="actionable — helm values look misconfigured",
        model="claude-haiku-4-5",
        input_tokens=300, output_tokens=50,
        cache_read_tokens=0, cache_creation_tokens=0,
    ))
    investigator = _investigator_mock()

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        triage=triage,
        investigator=investigator,
    )

    call_kwargs = investigator.investigate.await_args.kwargs
    assert call_kwargs["triage_reasoning"] == "actionable — helm values look misconfigured"


async def test_investigation_failure_falls_through_to_synthesis(sessionmaker_fixture):
    """Sonnet outage MUST NOT block Opus. Fail-open: log warning, leave
    investigation columns NULL, continue to patch generation."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    investigator = MagicMock()
    investigator.investigate = AsyncMock(side_effect=RuntimeError("anthropic 503"))

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        investigator=investigator,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    assert fetched.investigation_model is None  # nothing recorded
    patcher.generate.assert_awaited_once()


async def test_investigation_disabled_setting_skips_stage(sessionmaker_fixture):
    """`investigation_enabled=false` skips the stage even with a wired
    Investigator. Opus still runs against the full codebase (Phase B
    contract: synthesis is unchanged)."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        await repo.set_setting(s, "investigation_enabled", "false")
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(confidence=5))
    investigator = _investigator_mock()

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
        investigator=investigator,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "done"
    assert fetched.investigation_model is None
    investigator.investigate.assert_not_awaited()
    patcher.generate.assert_awaited_once()


async def test_failure_in_patch_generation_marks_failed(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=_ctx(alert))
    patcher = MagicMock()
    patcher.generate = AsyncMock(side_effect=RuntimeError("anthropic 500"))

    await process_alert(
        run.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "failed"
    assert "anthropic 500" in fetched.error_message

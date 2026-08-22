"""Tests for the deterministic replay runner.

Exercises the *real* process_alert pipeline against a frozen scenario, with
the LLM stages injected as AsyncMocks (same pattern as test_processor) so the
replay is offline and deterministic. Confirms the recorded context reaches the
pipeline and the fail-safe contract holds.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from eval.corpus import load_corpus
from eval.runner import recorded_collector, replay_scenario
from eval.scoring import score_run
from src.agent.investigator import Investigation
from src.agent.patch_generator import PatchProposal
from src.db.models import Base
from src.services_registry import ServiceConfig, ServiceRegistry


@pytest.fixture
async def sessionmaker_fixture():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sm = async_sessionmaker(engine, expire_on_commit=False)
    yield sm
    await engine.dispose()


def _services() -> ServiceRegistry:
    return ServiceRegistry.from_mapping(
        {
            "demo-app": ServiceConfig(
                name="demo-app",
                codebase_path="demo-app",
                github_repo="owner/repo",
            )
        }
    )


def _db_pool_scenario():
    return next(s for s in load_corpus() if s.id == "db-pool-exhausted")


def _patcher_mock(diff_file: str = "check_db_pool.py", confidence: int = 5):
    diff = (
        f"## Root Cause\nThe connection pool is too small.\n"
        f"## Proposed Patch\n```diff\n--- a/{diff_file}\n+++ b/{diff_file}\n"
        f"@@ -1 +1 @@\n-MAX_CONNECTIONS = 2\n+MAX_CONNECTIONS = 10\n```\n"
        f"## Confidence\n{confidence}/5 — clear\n"
    )
    proposal = PatchProposal(
        text=diff,
        model="claude-opus-4-7",
        input_tokens=100, output_tokens=50,
        cache_read_tokens=80, cache_creation_tokens=0,
        confidence=confidence, confidence_reason="clear",
        used_narrowed_context=True, file_count=1,
    )
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=proposal)
    return patcher


def _investigator_mock(affected):
    inv = MagicMock()
    inv.investigate = AsyncMock(return_value=Investigation(
        root_cause="pool too small", affected_files=affected, confidence=4,
        reasoning="logs name the file", response_text="...",
        model="claude-sonnet-4-6", input_tokens=200, output_tokens=30,
        cache_read_tokens=150, cache_creation_tokens=0,
    ))
    return inv


async def test_recorded_collector_serves_frozen_context_offline():
    scenario = _db_pool_scenario()
    collector = recorded_collector(scenario)
    # The collector's clients must not need a network — they echo the recording.
    ctx = await collector.collect(scenario.alert)
    assert len(ctx.log_lines) == len(scenario.log_lines)
    assert len(ctx.metric_samples) == len(scenario.metric_samples)
    assert ctx.errors == []  # LogQL + PromQL both built from the alert


async def test_replay_produces_scorable_run(sessionmaker_fixture):
    scenario = _db_pool_scenario()
    run = await replay_scenario(
        scenario,
        sessionmaker=sessionmaker_fixture,
        services=_services(),
        patcher=_patcher_mock(),
        triage=None,
        investigator=_investigator_mock(["check_db_pool.py"]),
    )
    assert run is not None
    assert run.trigger_source == "eval"
    assert run.processing_status == "done"
    # Recorded context reached the pipeline.
    assert run.log_line_count == len(scenario.log_lines)
    # Scores correct: patch targets the ground-truth file.
    score = score_run(scenario, run)
    assert score.label == "correct"


async def test_replay_never_opens_pr_or_slack(sessionmaker_fixture):
    # github/slack are hard-wired to None in the runner; the run still completes.
    scenario = _db_pool_scenario()
    run = await replay_scenario(
        scenario,
        sessionmaker=sessionmaker_fixture,
        services=_services(),
        patcher=_patcher_mock(),
        triage=None,
        investigator=None,
    )
    assert run.pr_opened is False
    assert run.pr_url is None
    assert run.slack_notified is False


async def test_replay_failsafe_on_llm_error_scores_wrong(sessionmaker_fixture):
    # A synthesis blow-up must not crash the harness — the run is recorded
    # `failed` and the scorer grades it `wrong`.
    scenario = _db_pool_scenario()
    patcher = MagicMock()
    patcher.generate = AsyncMock(side_effect=RuntimeError("opus exploded"))
    run = await replay_scenario(
        scenario,
        sessionmaker=sessionmaker_fixture,
        services=_services(),
        patcher=patcher,
        triage=None,
        investigator=None,
    )
    assert run.processing_status == "failed"
    assert score_run(scenario, run).label == "wrong"


async def test_replay_partial_when_wrong_file(sessionmaker_fixture):
    scenario = _db_pool_scenario()
    run = await replay_scenario(
        scenario,
        sessionmaker=sessionmaker_fixture,
        services=_services(),
        patcher=_patcher_mock(diff_file="check_cache.py"),
        triage=None,
        investigator=_investigator_mock([]),
    )
    assert score_run(scenario, run).label == "partial"


# ---------------------------------------------------------------------------
# Scenario-declared service configs (#24 part B, #25 option C).
#
# These scenarios carry their own service block because the declaration is the
# thing under test. The replay must use it, the declaration must reach the
# synthesis prompt, and the grader must catch a confident answer.
# ---------------------------------------------------------------------------

def _scenario_by_id(scenario_id: str):
    return next(s for s in load_corpus() if s.id == scenario_id)


def _proposal(text: str, confidence: int) -> PatchProposal:
    return PatchProposal(
        text=text, model="claude-opus-4-7", input_tokens=10, output_tokens=10,
        cache_read_tokens=0, cache_creation_tokens=0, confidence=confidence,
        confidence_reason="r", used_narrowed_context=False, file_count=1,
    )


@pytest.mark.asyncio
async def test_oci_scenario_declaration_reaches_the_synthesis_prompt(
    sessionmaker_fixture,
):
    """The scenario's own service config must override the caller's registry,
    and the delivery constraint must land in the prompt Opus actually sees."""
    scenario = _scenario_by_id("oci-chart-undeliverable-patch")

    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(
        "## Root Cause\nProbe timeout too low; needs a chart repackage.\n"
        "\n## Confidence\n2/5 — cannot reconcile from this repo\n", 2,
    ))

    await replay_scenario(
        scenario,
        sessionmaker=sessionmaker_fixture,
        services=_services(),          # deliberately WRONG registry
        patcher=patcher,
        triage=None,
        investigator=None,
    )

    kwargs = patcher.generate.call_args.kwargs
    assert kwargs["github_repo"] == "example-org/platform-charts"
    assert kwargs["delivery"].mode == "oci-chart"
    assert kwargs["delivery"].is_indirect is True
    # Cross-repo version ref — the prompt must ask for an explanation, not a
    # patch the pipeline could never open.
    assert kwargs["delivery"].version_ref.repo == "example-org/platform-deployment"
    assert [layer.visible for layer in kwargs["chart_lineage"]] == [False, True, False]
    assert scenario.service.codebase_path.endswith("codebase")


@pytest.mark.asyncio
async def test_confident_patch_on_the_oci_scenario_is_graded_wrong(
    sessionmaker_fixture,
):
    """The regression this corpus entry exists to catch: a clean, confident
    diff against a chart that cannot reconcile from this repo."""
    scenario = _scenario_by_id("oci-chart-undeliverable-patch")

    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(
        "## Root Cause\nLiveness probe timeout is too tight.\n\n"
        "## Proposed Patch\n```diff\n"
        "--- a/values.yaml\n+++ b/values.yaml\n@@ -4,1 +4,1 @@\n"
        "-    livenessProbeTimeoutSeconds: 1\n+    livenessProbeTimeoutSeconds: 10\n"
        "```\n\n## Confidence\n5/5 — unambiguous\n", 5,
    ))

    run = await replay_scenario(
        scenario, sessionmaker=sessionmaker_fixture, services=_services(),
        patcher=patcher, triage=None, investigator=None,
    )
    score = score_run(scenario, run)
    assert score.label == "wrong"
    assert "exceeds the 2/5 ceiling" in score.reason


@pytest.mark.asyncio
async def test_honest_low_confidence_on_the_oci_scenario_passes(
    sessionmaker_fixture,
):
    scenario = _scenario_by_id("oci-chart-undeliverable-patch")

    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(
        "## Root Cause\nThe probe timeout in values.yaml is too tight, but this "
        "chart is consumed as an OCI package pinned in platform-deployment — a "
        "source edit here needs a repackage and a version bump to take effect.\n"
        "\n## Confidence\n2/5 — cannot be delivered from this repository\n", 2,
    ))

    run = await replay_scenario(
        scenario, sessionmaker=sessionmaker_fixture, services=_services(),
        patcher=patcher, triage=None, investigator=None,
    )
    assert score_run(scenario, run).label == "correct"


@pytest.mark.asyncio
async def test_lineage_scenario_forwards_invisible_layers_to_both_stages(
    sessionmaker_fixture,
):
    """The lineage has to reach the investigator too — wrong-FILE selection
    happens there, before synthesis ever sees the code."""
    scenario = _scenario_by_id("invisible-chart-layer-override")

    investigator = MagicMock()
    investigator.investigate = AsyncMock(return_value=Investigation(
        root_cause="memory limit is overridden by the parent chart resrv",
        affected_files=[], confidence=2, reasoning="r", response_text="t",
        model="claude-sonnet-4-5", input_tokens=1, output_tokens=1,
        cache_read_tokens=0, cache_creation_tokens=0,
    ))
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=_proposal(
        "## Root Cause\nOverridden by parent chart resrv, which is not visible.\n"
        "\n## Confidence\n1/5 — the causal layer was not provided\n", 1,
    ))

    run = await replay_scenario(
        scenario, sessionmaker=sessionmaker_fixture, services=_services(),
        patcher=patcher, triage=None, investigator=investigator,
    )

    inv_lineage = investigator.investigate.call_args.kwargs["chart_lineage"]
    assert [(l.name, l.visible) for l in inv_lineage] == [
        ("resrv", False), ("airflow-tool", True),
    ]
    # Delivery is direct here, so a pass cannot be credited to that feature.
    assert patcher.generate.call_args.kwargs["delivery"].is_indirect is False
    assert score_run(scenario, run).label == "correct"

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

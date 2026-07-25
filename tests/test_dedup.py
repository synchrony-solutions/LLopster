"""Tests for duplicate-alert suppression.

Covers the dedup_key shape, the repository-level "is there a blocking open
PR?" query, and the processor integration that short-circuits the pipeline
on a dedup hit.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.agent.dedup import (
    compute_dedup_key,
    find_blocking_open_pr_run,
    find_previous_merged_attempt,
    find_recent_unproductive_run,
    is_within_grace_window,
    previous_attempt_from_run,
)
from src.agent.processor import process_alert
from src.db import repository as repo
from src.db.models import Base


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


def _alert(
    alertname: str = "HelmValuesMisconfigured",
    service: str = "demo-app",
    severity: str = "warning",
) -> ParsedAlert:
    return ParsedAlert(
        fingerprint="fp1",
        status="firing",
        alertname=alertname,
        severity=severity,
        service=service,
        summary="bad",
        description="bad",
        starts_at=datetime(2026, 5, 1, tzinfo=timezone.utc),
        ends_at=None,
        labels={"service": service},
        annotations={},
        generator_url="",
    )


def _services() -> MagicMock:
    services = MagicMock()
    cfg = MagicMock()
    cfg.codebase_path = "./demo-app"
    cfg.github_repo = "owner/repo"
    services.get.return_value = cfg
    services.names.return_value = ["demo-app"]
    return services


# ---------------------------------------------------------------------------
# Key shape
# ---------------------------------------------------------------------------

def test_dedup_key_is_alertname_service_severity():
    key = compute_dedup_key(_alert("MyAlert", "payments", "critical"))
    assert key == "myalert:payments:critical"


def test_dedup_key_is_lowercased():
    a = compute_dedup_key(_alert("MyAlert", "Payments", "Critical"))
    b = compute_dedup_key(_alert("myalert", "payments", "critical"))
    assert a == b


def test_dedup_key_ignores_pod_and_instance_labels():
    """Two firings of the same alert from different pods must share a key —
    operators want one investigation per alertname, not one per pod."""
    a = _alert()
    a.labels = {"service": "demo-app", "pod": "demo-app-abc"}
    b = _alert()
    b.labels = {"service": "demo-app", "pod": "demo-app-xyz"}
    assert compute_dedup_key(a) == compute_dedup_key(b)


def test_dedup_key_differs_on_severity():
    a = compute_dedup_key(_alert(severity="warning"))
    b = compute_dedup_key(_alert(severity="critical"))
    assert a != b


# ---------------------------------------------------------------------------
# Repository lookup
# ---------------------------------------------------------------------------

async def test_find_blocking_returns_none_when_no_prior_run(sessionmaker_fixture):
    async with sessionmaker_fixture() as s:
        result = await find_blocking_open_pr_run(s, "anything")
    assert result is None


async def test_find_blocking_returns_run_with_open_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )

    async with sm() as s:
        blocker = await find_blocking_open_pr_run(s, compute_dedup_key(alert))
    assert blocker is not None
    assert blocker.id == prior.id


async def test_find_blocking_ignores_closed_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )
        await repo.update_pr_status(s, prior.id, "closed")

    async with sm() as s:
        blocker = await find_blocking_open_pr_run(s, compute_dedup_key(alert))
    assert blocker is None


async def test_find_blocking_ignores_merged_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )
        await repo.update_pr_status(s, prior.id, "merged")

    async with sm() as s:
        blocker = await find_blocking_open_pr_run(s, compute_dedup_key(alert))
    assert blocker is None


async def test_find_blocking_excludes_self(sessionmaker_fixture):
    """The Run-row-for-this-firing already exists when dedup runs; it must
    not match itself even if it somehow already had pr_opened set."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        current = await repo.create_run_from_alert(s, alert, raw_payload={})
        # Pretend (paranoid) this Run already had a PR — shouldn't dedup
        # against itself.
        await repo.record_pr(
            s, current.id, pr_url="https://gh/x/y/pull/9",
            pr_number=9, pr_branch="b",
        )

    async with sm() as s:
        blocker = await find_blocking_open_pr_run(
            s, compute_dedup_key(alert), exclude_run_id=current.id,
        )
    assert blocker is None


async def test_find_blocking_different_alert_does_not_match(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, _alert("OtherAlert"), raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )

    async with sm() as s:
        blocker = await find_blocking_open_pr_run(s, compute_dedup_key(_alert()))
    assert blocker is None


# ---------------------------------------------------------------------------
# Processor integration
# ---------------------------------------------------------------------------

async def test_processor_skips_when_prior_run_has_open_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()

    # Prior Run with an open PR for the same alert
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/7",
            pr_number=7, pr_branch="b",
        )

    # Now a new firing comes in
    async with sm() as s:
        current = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock()
    patcher = MagicMock()
    patcher.generate = AsyncMock()
    github = MagicMock()
    github.open_pr = AsyncMock()
    slack = MagicMock()
    slack.post_patch = AsyncMock()

    await process_alert(
        current.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=github,
        notifier=slack,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, current.id)
    assert fetched.processing_status == "skipped"
    assert "duplicate-pending-pr" in fetched.error_message
    assert "pull/7" in fetched.error_message
    # No outbound work happened — that's the whole point.
    collector.collect.assert_not_awaited()
    patcher.generate.assert_not_awaited()
    github.open_pr.assert_not_awaited()
    slack.post_patch.assert_not_awaited()


# ---------------------------------------------------------------------------
# Post-firing backoff (repeated firing with no PR)
# ---------------------------------------------------------------------------

async def _make_done_no_pr_run(sm, alert, *, status: str = "done", age_minutes: int = 0):
    """Create a Run for `alert` in a terminal state without a PR, optionally
    backdated `age_minutes` into the past."""
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.update_status(s, run.id, status)
        if age_minutes:
            fetched = await repo.get_run(s, run.id)
            fetched.created_at = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
            await s.commit()
    return run


async def test_find_unproductive_matches_done_without_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    prior = await _make_done_no_pr_run(sm, alert)
    since = datetime.now(timezone.utc) - timedelta(minutes=60)
    async with sm() as s:
        found = await find_recent_unproductive_run(s, compute_dedup_key(alert), since=since)
    assert found is not None and found.id == prior.id


async def test_find_unproductive_matches_failed_without_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    prior = await _make_done_no_pr_run(sm, alert, status="failed")
    since = datetime.now(timezone.utc) - timedelta(minutes=60)
    async with sm() as s:
        found = await find_recent_unproductive_run(s, compute_dedup_key(alert), since=since)
    assert found is not None and found.id == prior.id


async def test_find_unproductive_ignores_run_with_pr(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(s, run.id, pr_url="https://gh/x/y/pull/1", pr_number=1, pr_branch="b")
        await repo.update_status(s, run.id, "done")
    since = datetime.now(timezone.utc) - timedelta(minutes=60)
    async with sm() as s:
        found = await find_recent_unproductive_run(s, compute_dedup_key(alert), since=since)
    assert found is None


async def test_find_unproductive_ignores_skipped(sessionmaker_fixture):
    """A prior backoff/triage `skipped` row is not an anchor — otherwise the
    window would roll forward on every firing and suppress forever."""
    sm = sessionmaker_fixture
    alert = _alert()
    await _make_done_no_pr_run(sm, alert, status="skipped")
    since = datetime.now(timezone.utc) - timedelta(minutes=60)
    async with sm() as s:
        found = await find_recent_unproductive_run(s, compute_dedup_key(alert), since=since)
    assert found is None


async def test_find_unproductive_respects_window(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    await _make_done_no_pr_run(sm, alert, age_minutes=120)  # older than the window
    since = datetime.now(timezone.utc) - timedelta(minutes=60)
    async with sm() as s:
        found = await find_recent_unproductive_run(s, compute_dedup_key(alert), since=since)
    assert found is None


async def test_find_unproductive_excludes_self(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    current = await _make_done_no_pr_run(sm, alert)
    since = datetime.now(timezone.utc) - timedelta(minutes=60)
    async with sm() as s:
        found = await find_recent_unproductive_run(
            s, compute_dedup_key(alert), since=since, exclude_run_id=current.id,
        )
    assert found is None


async def test_processor_backoff_skips_after_unproductive_run(sessionmaker_fixture):
    """A re-firing of an alert whose last real run finished without a PR is
    suppressed before any collection/LLM call — closing the uncapped cost loop."""
    sm = sessionmaker_fixture
    alert = _alert()
    await _make_done_no_pr_run(sm, alert)  # prior: done, no PR, recent

    async with sm() as s:
        current = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock()
    patcher = MagicMock()
    patcher.generate = AsyncMock()
    github = MagicMock()
    github.open_pr = AsyncMock()
    slack = MagicMock()
    slack.post_patch = AsyncMock()

    await process_alert(
        current.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=github,
        notifier=slack,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, current.id)
    assert fetched.processing_status == "skipped"
    assert "backoff" in fetched.error_message
    collector.collect.assert_not_awaited()
    patcher.generate.assert_not_awaited()
    github.open_pr.assert_not_awaited()


async def test_processor_backoff_disabled_when_zero(sessionmaker_fixture):
    """patch_backoff_minutes=0 disables the backoff — the re-firing proceeds."""
    sm = sessionmaker_fixture
    alert = _alert()
    await _make_done_no_pr_run(sm, alert)

    async with sm() as s:
        await repo.set_setting(s, "patch_backoff_minutes", "0")
        current = await repo.create_run_from_alert(s, alert, raw_payload={})

    # collect() raises so the pipeline stops right after the backoff gate; we
    # only care that it was REACHED (backoff didn't short-circuit before it).
    collector = MagicMock()
    collector.collect = AsyncMock(side_effect=RuntimeError("stop here"))
    patcher = MagicMock()
    patcher.generate = AsyncMock()
    github = MagicMock()
    github.open_pr = AsyncMock()
    slack = MagicMock()
    slack.post_patch = AsyncMock()

    await process_alert(
        current.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=github,
        notifier=slack,
    )

    # Backoff off → collection ran (the pipeline was not short-circuited).
    collector.collect.assert_awaited()


async def test_create_run_populates_dedup_key(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert("HelmValuesMisconfigured", "demo-app", "warning")
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})
    assert run.dedup_key == "helmvaluesmisconfigured:demo-app:warning"


# ---------------------------------------------------------------------------
# pr_merged_at backfill
# ---------------------------------------------------------------------------

async def test_update_pr_status_backfills_pr_merged_at(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.record_pr(
            s, run.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )
        before = datetime.now(timezone.utc)
        await repo.update_pr_status(s, run.id, "merged")
        after = datetime.now(timezone.utc)

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.pr_merged_at is not None
    # SQLite strips tzinfo on round-trip — treat as UTC for comparison.
    merged_at = fetched.pr_merged_at
    if merged_at.tzinfo is None:
        merged_at = merged_at.replace(tzinfo=timezone.utc)
    assert before <= merged_at <= after


async def test_update_pr_status_does_not_overwrite_existing_pr_merged_at(sessionmaker_fixture):
    """First-observed merge time wins — don't overwrite on subsequent polls."""
    sm = sessionmaker_fixture
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.record_pr(
            s, run.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )
        await repo.update_pr_status(s, run.id, "merged")
        first = (await repo.get_run(s, run.id)).pr_merged_at
        await repo.update_pr_status(s, run.id, "merged")  # second poll
        second = (await repo.get_run(s, run.id)).pr_merged_at
    assert first == second


async def test_update_pr_status_to_closed_does_not_stamp(sessionmaker_fixture):
    sm = sessionmaker_fixture
    async with sm() as s:
        run = await repo.create_run_from_alert(s, _alert(), raw_payload={})
        await repo.record_pr(
            s, run.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )
        await repo.update_pr_status(s, run.id, "closed")
        fetched = await repo.get_run(s, run.id)
    assert fetched.pr_merged_at is None


# ---------------------------------------------------------------------------
# find_previous_merged_attempt
# ---------------------------------------------------------------------------

async def test_find_previous_merged_returns_none_when_only_open(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )
    async with sm() as s:
        result = await find_previous_merged_attempt(s, compute_dedup_key(alert))
    assert result is None


async def test_find_previous_merged_returns_merged_run(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )
        await repo.update_pr_status(s, prior.id, "merged")
    async with sm() as s:
        result = await find_previous_merged_attempt(s, compute_dedup_key(alert))
    assert result is not None
    assert result.id == prior.id


async def test_find_previous_merged_ignores_closed_run(sessionmaker_fixture):
    """A closed-not-merged PR means the maintainer rejected the fix — we
    should not feed it back to the model as a 'previous attempt'."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/1",
            pr_number=1, pr_branch="b",
        )
        await repo.update_pr_status(s, prior.id, "closed")
    async with sm() as s:
        result = await find_previous_merged_attempt(s, compute_dedup_key(alert))
    assert result is None


# ---------------------------------------------------------------------------
# is_within_grace_window
# ---------------------------------------------------------------------------

def test_is_within_grace_window_true_when_recent():
    now = datetime(2026, 5, 23, 12, 30, tzinfo=timezone.utc)
    merged = now - timedelta(minutes=10)
    assert is_within_grace_window(merged, 30, now=now) is True


def test_is_within_grace_window_false_when_past():
    now = datetime(2026, 5, 23, 12, 30, tzinfo=timezone.utc)
    merged = now - timedelta(minutes=45)
    assert is_within_grace_window(merged, 30, now=now) is False


def test_is_within_grace_window_naive_datetime_treated_as_utc():
    """SQLite returns datetimes without tzinfo; the function must not crash."""
    now = datetime(2026, 5, 23, 12, 30, tzinfo=timezone.utc)
    merged_naive = (now - timedelta(minutes=10)).replace(tzinfo=None)
    assert is_within_grace_window(merged_naive, 30, now=now) is True


# ---------------------------------------------------------------------------
# Processor integration — grace window
# ---------------------------------------------------------------------------

async def test_processor_skips_within_grace_window(sessionmaker_fixture):
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/7",
            pr_number=7, pr_branch="b",
        )
        # Backfilled to "just now" by update_pr_status — well within grace.
        await repo.update_pr_status(s, prior.id, "merged")
        current = await repo.create_run_from_alert(s, alert, raw_payload={})

    collector = MagicMock()
    collector.collect = AsyncMock()
    patcher = MagicMock()
    patcher.generate = AsyncMock()

    await process_alert(
        current.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
    )

    async with sm() as s:
        fetched = await repo.get_run(s, current.id)
    assert fetched.processing_status == "skipped"
    assert "within-deploy-grace" in fetched.error_message
    assert "pull/7" in fetched.error_message
    collector.collect.assert_not_awaited()
    patcher.generate.assert_not_awaited()


async def test_processor_passes_previous_attempt_past_grace(sessionmaker_fixture):
    """Once the grace window has elapsed, the merged PR's diagnosis + diff
    must be attached to the next investigation so the model knows the
    obvious fix didn't work."""
    sm = sessionmaker_fixture
    alert = _alert()
    old_merge = datetime.now(timezone.utc) - timedelta(hours=4)

    async with sm() as s:
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/7",
            pr_number=7, pr_branch="b",
        )
        prior.pr_status = "merged"
        prior.pr_merged_at = old_merge
        prior.parsed_root_cause = "memory limit too low"
        prior.parsed_diff = "--- a/values.yaml\n+++ b/values.yaml\n@@ -1 +1 @@\n-old\n+new\n"
        await s.commit()
        current = await repo.create_run_from_alert(s, alert, raw_payload={})

    from src.agent.context_collector import AlertContext
    from src.agent.patch_generator import PatchProposal

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=AlertContext(
        alert=alert, log_lines=[], metric_samples=[],
        queries_used={"logql": "", "promql": ""}, errors=[],
    ))
    proposal = PatchProposal(
        text="## Root Cause\nx\n## Confidence\n2/5 — y\n",
        model="m", input_tokens=1, output_tokens=1,
        cache_read_tokens=0, cache_creation_tokens=0,
        confidence=2, confidence_reason="y",
    )
    patcher = MagicMock()
    patcher.generate = AsyncMock(return_value=proposal)

    await process_alert(
        current.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
    )

    # patcher.generate was called with previous_attempt set
    assert patcher.generate.await_count == 1
    kwargs = patcher.generate.await_args.kwargs
    prev = kwargs.get("previous_attempt")
    assert prev is not None
    assert prev.run_id == prior.id
    assert prev.pr_url == "https://gh/x/y/pull/7"
    assert prev.parsed_root_cause == "memory limit too low"
    assert "values.yaml" in prev.parsed_diff


async def test_processor_respects_grace_setting_override(sessionmaker_fixture):
    """A short grace setting must allow recently-merged PRs to count as
    'past grace' immediately."""
    sm = sessionmaker_fixture
    alert = _alert()
    async with sm() as s:
        await repo.set_setting(s, "post_merge_grace_minutes", "0")
        prior = await repo.create_run_from_alert(s, alert, raw_payload={})
        await repo.record_pr(
            s, prior.id, pr_url="https://gh/x/y/pull/7",
            pr_number=7, pr_branch="b",
        )
        await repo.update_pr_status(s, prior.id, "merged")
        current = await repo.create_run_from_alert(s, alert, raw_payload={})

    from src.agent.context_collector import AlertContext

    collector = MagicMock()
    collector.collect = AsyncMock(return_value=AlertContext(
        alert=alert, log_lines=[], metric_samples=[],
        queries_used={"logql": "", "promql": ""}, errors=[],
    ))
    patcher = MagicMock()
    patcher.generate = AsyncMock(side_effect=RuntimeError("stop"))

    await process_alert(
        current.id, alert,
        sessionmaker=sm,
        collector=collector,
        services=_services(),
        patcher=patcher,
        github=None,
        notifier=None,
    )

    # Should have proceeded past dedup and reached the patcher.
    patcher.generate.assert_awaited_once()


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------

def test_alert_context_includes_previous_attempt_block():
    """The patch generator's formatter must surface the previous-attempt
    warning at the top of the prompt so the model can't miss it."""
    from src.agent.context_collector import AlertContext
    from src.agent.dedup import PreviousAttempt
    from src.agent.patch_generator import _format_alert_context

    prev = PreviousAttempt(
        run_id="abc-123",
        pr_url="https://gh/x/y/pull/7",
        pr_merged_at=datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc),
        parsed_root_cause="cache TTL miscalculated",
        parsed_diff="--- a/cache.py\n+++ b/cache.py\n@@ -1 +1 @@\n-old\n+new\n",
    )
    ctx = AlertContext(
        alert=_alert(), log_lines=[], metric_samples=[],
        queries_used={"logql": "", "promql": ""}, errors=[],
    )
    blob = _format_alert_context(ctx, previous_attempt=prev)
    assert "Previous fix attempt did not resolve" in blob
    assert "https://gh/x/y/pull/7" in blob
    assert "cache TTL miscalculated" in blob
    assert "cache.py" in blob
    assert "Do not re-propose the same diff" in blob
    # And the regular incident context still follows.
    assert "# Incident context" in blob


def test_alert_context_omits_previous_attempt_block_by_default():
    from src.agent.context_collector import AlertContext
    from src.agent.patch_generator import _format_alert_context

    ctx = AlertContext(
        alert=_alert(), log_lines=[], metric_samples=[],
        queries_used={"logql": "", "promql": ""}, errors=[],
    )
    blob = _format_alert_context(ctx)
    assert "Previous fix attempt" not in blob

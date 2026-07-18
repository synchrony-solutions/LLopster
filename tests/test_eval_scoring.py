"""Tests for the eval scorer — the ground-truth grading logic."""

from datetime import datetime, timezone

import pytest

from eval.corpus import GroundTruth, Scenario
from eval.scoring import aggregate, score_run
from src.agent.alert_handler import ParsedAlert
from src.db.models import Run


def _scenario(
    *,
    expect_patch: bool = True,
    expected_files=("check_db_pool.py",),
    keywords=("pool",),
) -> Scenario:
    alert = ParsedAlert(
        fingerprint="f", status="firing", alertname="DatabasePoolExhausted",
        severity="critical", service="demo-app", summary="x", description="y",
        starts_at=datetime(2026, 4, 18, tzinfo=timezone.utc), ends_at=None,
        labels={"service": "demo-app"}, annotations={}, generator_url="",
    )
    return Scenario(
        id="db-pool-exhausted",
        description="",
        alert=alert,
        raw_payload={},
        log_lines=[],
        metric_samples=[],
        ground_truth=GroundTruth(
            expect_patch=expect_patch,
            expected_files=tuple(expected_files),
            root_cause_keywords=tuple(keywords),
        ),
    )


def _run(
    *,
    status="done",
    parsed_diff=None,
    affected=None,
    root_cause=None,
    confidence=5,
    error=None,
) -> Run:
    return Run(
        id="r1",
        alertname="DatabasePoolExhausted",
        service="demo-app",
        processing_status=status,
        parsed_diff=parsed_diff,
        investigation_affected_files_json=affected,
        parsed_root_cause=root_cause,
        parsed_confidence=confidence,
        error_message=error,
    )


def test_correct_when_patch_targets_expected_file():
    diff = "--- a/check_db_pool.py\n+++ b/check_db_pool.py\n@@ -1 +1 @@\n-x\n+y\n"
    score = score_run(_scenario(), _run(parsed_diff=diff))
    assert score.label == "correct"
    assert score.patch_proposed and score.targets_expected_file


def test_partial_when_patch_targets_wrong_file():
    diff = "--- a/check_cache.py\n+++ b/check_cache.py\n@@ -1 +1 @@\n-x\n+y\n"
    score = score_run(_scenario(), _run(parsed_diff=diff))
    assert score.label == "partial"
    assert score.patch_proposed and not score.targets_expected_file


def test_partial_when_file_located_but_no_patch():
    # Investigation flagged the right file but synthesis produced no diff.
    score = score_run(_scenario(), _run(parsed_diff=None, affected=["check_db_pool.py"]))
    assert score.label == "partial"
    assert score.targets_expected_file and not score.patch_proposed


def test_wrong_when_no_patch_and_no_file():
    score = score_run(_scenario(), _run(parsed_diff=None, affected=[]))
    assert score.label == "wrong"


def test_failed_run_scores_wrong():
    score = score_run(_scenario(), _run(status="failed", error="boom"))
    assert score.label == "wrong"
    assert "boom" in score.reason


def test_missing_run_scores_wrong_without_crashing():
    score = score_run(_scenario(), None)
    assert score.label == "wrong"
    assert score.run_id is None


def test_root_cause_keyword_match_is_reported():
    diff = "--- a/check_db_pool.py\n+++ b/check_db_pool.py\n@@ -1 +1 @@\n-x\n+y\n"
    score = score_run(
        _scenario(keywords=("pool", "connection")),
        _run(parsed_diff=diff, root_cause="the connection pool is too small"),
    )
    assert score.root_cause_match is True


def test_noise_suppression_scenario_inverts():
    # expect_patch=False → correct answer is to NOT produce a patch.
    sc = _scenario(expect_patch=False, expected_files=())
    assert score_run(sc, _run(parsed_diff=None)).label == "correct"
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    assert score_run(sc, _run(parsed_diff=diff)).label == "wrong"


def test_aggregate_computes_pass_rate():
    diff_ok = "--- a/check_db_pool.py\n+++ b/check_db_pool.py\n@@ -1 +1 @@\n-x\n+y\n"
    diff_wrong = "--- a/check_cache.py\n+++ b/check_cache.py\n@@ -1 +1 @@\n-x\n+y\n"
    scores = [
        score_run(_scenario(), _run(parsed_diff=diff_ok)),       # correct
        score_run(_scenario(), _run(parsed_diff=diff_wrong)),    # partial
        score_run(_scenario(), _run(parsed_diff=None, affected=[])),  # wrong
    ]
    corpus = aggregate(scores)
    assert corpus.scenario_count == 3
    assert corpus.correct_count == 1
    assert corpus.partial_count == 1
    assert corpus.wrong_count == 1
    assert corpus.pass_rate == pytest.approx(1 / 3)

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
    max_confidence=None,
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
            max_confidence=max_confidence,
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


# ---------------------------------------------------------------------------
# max_confidence: the undeliverable-fix grade (#24 part B, #25 option C).
#
# These scenarios fail differently from "wrong file". The diff can be perfect
# and the run still worthless, because what the cluster runs is a packaged
# artifact or a value set in a layer the agent never saw. Every gate in the
# pipeline passes such a patch — so the thing being graded is the confidence.
# ---------------------------------------------------------------------------

UNDELIVERABLE = dict(expect_patch=False, max_confidence=2,
                     expected_files=("values.yaml",), keywords=("repackage",))


def test_confident_patch_on_an_undeliverable_fix_is_wrong():
    """The worst outcome, and the one every other gate lets through."""
    score = score_run(
        _scenario(**UNDELIVERABLE),
        _run(parsed_diff="--- a/values.yaml\n+++ b/values.yaml\n", confidence=5,
             root_cause="bump the memory limit"),
    )
    assert score.label == "wrong"
    assert "exceeds the 2/5 ceiling" in score.reason


def test_low_confidence_patch_with_an_explanation_is_correct():
    """A patch is acceptable if offered tentatively — the criterion is honesty
    about deliverability, not silence."""
    score = score_run(
        _scenario(**UNDELIVERABLE),
        _run(parsed_diff="--- a/values.yaml\n+++ b/values.yaml\n", confidence=2,
             root_cause="needs a chart repackage before this takes effect"),
    )
    assert score.label == "correct"
    assert score.patch_proposed is True


def test_low_confidence_without_an_explanation_is_partial():
    score = score_run(
        _scenario(**UNDELIVERABLE),
        _run(parsed_diff="--- a/values.yaml\n", confidence=1,
             root_cause="the memory limit is too low"),
    )
    assert score.label == "partial"
    assert "did not explain" in score.reason


def test_withholding_the_patch_entirely_is_correct():
    score = score_run(
        _scenario(**UNDELIVERABLE),
        _run(parsed_diff=None, confidence=1,
             root_cause="cannot fix from this repo; needs a repackage"),
    )
    assert score.label == "correct"
    assert "withheld" in score.reason


def test_confidence_at_the_ceiling_passes():
    """The ceiling is inclusive — `max_confidence: 2` means 2 is acceptable."""
    score = score_run(
        _scenario(**UNDELIVERABLE),
        _run(parsed_diff="--- a/values.yaml\n", confidence=2,
             root_cause="repackage required"),
    )
    assert score.label == "correct"


def test_no_ceiling_keeps_the_old_noise_suppression_grade():
    """Regression guard: scenarios without max_confidence are unaffected."""
    score = score_run(
        _scenario(expect_patch=False),
        _run(parsed_diff="--- a/x.py\n", confidence=5),
    )
    assert score.label == "wrong"
    assert "should have been skipped" in score.reason

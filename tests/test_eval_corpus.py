"""Tests for the frozen eval scenario corpus + loader."""

from pathlib import Path

import pytest

from eval.corpus import (
    DEFAULT_SCENARIOS_DIR,
    GroundTruth,
    Scenario,
    corpus_version,
    load_corpus,
    load_scenario,
)

# The five seeded demo-app bugs we froze as the regression baseline.
EXPECTED_IDS = {
    "db-pool-exhausted",
    "helm-values-misconfigured",
    "cache-hit-rate-low",
    "upstream-timeout-spike",
    "heartbeat-stale",
}


def test_corpus_loads_all_seeded_scenarios():
    scenarios = load_corpus()
    assert {s.id for s in scenarios} == EXPECTED_IDS


def test_each_scenario_is_well_formed():
    for s in load_corpus():
        assert isinstance(s, Scenario)
        # The alert parsed out of the AlertManager payload.
        assert s.alert.alertname
        assert s.alert.service == "demo-app"
        # Recorded context is present so replay is offline.
        assert s.log_lines, f"{s.id} has no recorded log lines"
        assert s.metric_samples, f"{s.id} has no recorded metric samples"
        # Ground truth names at least one expected file + keywords.
        assert isinstance(s.ground_truth, GroundTruth)
        assert s.ground_truth.expect_patch is True
        assert s.ground_truth.expected_files


def test_recorded_context_timestamps_match_alert_start():
    # The loader stamps recorded samples with the alert's start time so replay
    # is fully deterministic (no datetime.now() anywhere).
    s = next(s for s in load_corpus() if s.id == "db-pool-exhausted")
    assert all(l.timestamp == s.alert.starts_at for l in s.log_lines)
    assert all(m.timestamp == s.alert.starts_at for m in s.metric_samples)


def test_corpus_version_is_stable_and_content_addressed():
    scenarios = load_corpus()
    v1 = corpus_version(scenarios)
    v2 = corpus_version(scenarios)
    assert v1 == v2
    assert v1.startswith(f"{len(scenarios)}:")
    # Dropping a scenario changes the version.
    assert corpus_version(scenarios[:-1]) != v1


def test_missing_dir_yields_empty_corpus(tmp_path):
    assert load_corpus(tmp_path / "does-not-exist") == []


def test_malformed_scenario_raises(tmp_path):
    d = tmp_path / "bad"
    d.mkdir()
    (d / "scenario.yaml").write_text("id: bad\n")  # no alert payload
    with pytest.raises(ValueError):
        load_scenario(d / "scenario.yaml")


def test_default_scenarios_dir_exists():
    assert DEFAULT_SCENARIOS_DIR.exists()
    assert (DEFAULT_SCENARIOS_DIR).is_dir()

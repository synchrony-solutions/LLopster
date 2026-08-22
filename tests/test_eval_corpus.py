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
# Seeded demo-app bugs: the agent is expected to produce a patch.
EXPECTED_PATCH_IDS = {
    "db-pool-exhausted",
    "helm-values-misconfigured",
    "cache-hit-rate-low",
    "upstream-timeout-spike",
    "heartbeat-stale",
}

# Undeliverable-fix scenarios: the correct answer is an honest low confidence,
# because what the cluster runs is a packaged artifact or a value set in a
# chart layer the agent was never shown. These carry their own `service` block
# with the operator declaration under test.
EXPECTED_UNDELIVERABLE_IDS = {
    "oci-chart-undeliverable-patch",
    "invisible-chart-layer-override",
}

EXPECTED_IDS = EXPECTED_PATCH_IDS | EXPECTED_UNDELIVERABLE_IDS


def test_corpus_loads_all_seeded_scenarios():
    scenarios = load_corpus()
    assert {s.id for s in scenarios} == EXPECTED_IDS


def test_each_scenario_is_well_formed():
    for s in load_corpus():
        assert isinstance(s, Scenario)
        # The alert parsed out of the AlertManager payload.
        assert s.alert.alertname
        # The alert's `service` label has to resolve to the registry entry the
        # replay will use, or the run is skipped as an unmapped service. This
        # is the invariant the old `== "demo-app"` check was really protecting.
        expected_service = s.service.name if s.service else "demo-app"
        assert s.alert.service == expected_service, (
            f"{s.id}: alert service {s.alert.service!r} does not match the "
            f"registry entry {expected_service!r} the replay will look up"
        )
        # Recorded context is present so replay is offline.
        assert s.log_lines, f"{s.id} has no recorded log lines"
        assert s.metric_samples, f"{s.id} has no recorded metric samples"
        # Ground truth names at least one expected file + keywords.
        assert isinstance(s.ground_truth, GroundTruth)
        assert s.ground_truth.expected_files
        assert s.ground_truth.expect_patch is (s.id in EXPECTED_PATCH_IDS)


def test_undeliverable_scenarios_declare_a_confidence_ceiling():
    """These scenarios grade confidence, not file selection. Without a ceiling
    they would silently fall back to the noise-suppression grade, where any
    patch is wrong — the opposite of what they are testing."""
    for s in load_corpus():
        if s.id not in EXPECTED_UNDELIVERABLE_IDS:
            assert s.ground_truth.max_confidence is None
            continue
        assert s.ground_truth.max_confidence == 2, s.id
        assert s.ground_truth.root_cause_keywords, s.id


def test_undeliverable_scenarios_carry_a_usable_service_declaration():
    """The declaration is the thing under test, so it has to be present, point
    at a real fixture tree, and be reachable by the replay."""
    for s in load_corpus():
        if s.id not in EXPECTED_UNDELIVERABLE_IDS:
            continue
        assert s.service is not None, s.id
        codebase = Path(s.service.codebase_path)
        assert codebase.is_dir(), f"{s.id}: fixture codebase {codebase} missing"
        assert any(codebase.rglob("*.yaml")), f"{s.id}: fixture codebase is empty"
        # Every one of these declares at least one layer the agent cannot see,
        # or a delivery mode that does not reconcile directly — otherwise the
        # scenario is not exercising anything.
        has_hidden_layer = any(not l.visible for l in s.service.chart_lineage)
        indirect = s.service.delivery is not None and s.service.delivery.is_indirect
        assert has_hidden_layer or indirect, s.id


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

"""Tests for the frozen eval scenario corpus + loader."""

from pathlib import Path

import pytest

from src.agent.alert_handler import alert_label
from src.agent.cluster_state import WORKLOAD_LABELS
from src.agent.gitops import gitops_ref_from_alert
from eval.corpus import (
    DEFAULT_SCENARIOS_DIR,
    GroundTruth,
    Scenario,
    corpus_version,
    load_corpus,
    load_scenario,
    select_scenarios,
    UnknownScenarioError,
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

# Scenarios whose evidence lives on a Kubernetes object rather than in logs
# or metrics (issue #23). They carry a `recorded_context.cluster_state` block
# and, by design, may record no log lines at all — a container OOM-killed
# mid-batch flushes nothing.
EXPECTED_CLUSTER_STATE_IDS = {
    "crashloop-oomkilled-no-logs",
    # Flux delivery state (issue #24): the HelmRelease condition message is
    # the only evidence that the cluster lacks the ServiceMonitor CRD.
    "flux-helmrelease-missing-crd",
}

EXPECTED_IDS = (
    EXPECTED_PATCH_IDS | EXPECTED_UNDELIVERABLE_IDS | EXPECTED_CLUSTER_STATE_IDS
)


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
        # Recorded context is present so replay is offline. Log lines are NOT
        # required: a scenario whose whole point is that the workload logged
        # nothing before dying has to be allowed to record nothing.
        assert s.metric_samples, f"{s.id} has no recorded metric samples"
        assert s.log_lines or s.cluster_state is not None, (
            f"{s.id} records no log lines and no cluster state — an alert with "
            f"neither has nothing for the pipeline to diagnose from"
        )
        # Ground truth names at least one expected file + keywords.
        assert isinstance(s.ground_truth, GroundTruth)
        assert s.ground_truth.expected_files
        assert s.ground_truth.expect_patch is (
            s.id in EXPECTED_PATCH_IDS | EXPECTED_CLUSTER_STATE_IDS
        )


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


# ---------------------------------------------------------------------------
# select_scenarios — the --scenario-id filter.
#
# Every replay costs live tokens, so narrowing the run matters while iterating.
# The failure to avoid is a typo'd id quietly selecting nothing: a clean run
# over zero scenarios reads exactly like a pass.
# ---------------------------------------------------------------------------

def test_no_ids_returns_the_whole_corpus():
    corpus = load_corpus()
    assert select_scenarios(corpus, None) == corpus
    assert select_scenarios(corpus, []) == corpus


def test_selects_a_single_scenario():
    selected = select_scenarios(load_corpus(), ["oci-chart-undeliverable-patch"])
    assert [s.id for s in selected] == ["oci-chart-undeliverable-patch"]


def test_repeated_flags_and_comma_separated_are_equivalent():
    corpus = load_corpus()
    ids = ["oci-chart-undeliverable-patch", "invisible-chart-layer-override"]
    assert (
        [s.id for s in select_scenarios(corpus, ids)]
        == [s.id for s in select_scenarios(corpus, [",".join(ids)])]
    )


def test_selection_preserves_corpus_order_not_argument_order():
    """Stable ordering keeps two runs of the same set comparable."""
    corpus = load_corpus()
    selected = select_scenarios(
        corpus, ["oci-chart-undeliverable-patch,db-pool-exhausted"],
    )
    assert [s.id for s in selected] == [
        s.id for s in corpus if s.id in {
            "oci-chart-undeliverable-patch", "db-pool-exhausted",
        }
    ]


def test_unknown_id_raises_and_names_the_alternatives():
    with pytest.raises(UnknownScenarioError) as excinfo:
        select_scenarios(load_corpus(), ["db-pool-exhuasted"])   # typo
    message = str(excinfo.value)
    assert "db-pool-exhuasted" in message
    assert "db-pool-exhausted" in message   # the real id is offered


def test_one_bad_id_among_good_ones_still_raises():
    with pytest.raises(UnknownScenarioError):
        select_scenarios(load_corpus(), ["db-pool-exhausted", "nope"])


def test_filtered_corpus_version_differs_from_the_full_one():
    """corpus_version is derived from the ids present, so a filtered run cannot
    masquerade as a full-corpus result if one is ever recorded."""
    corpus = load_corpus()
    subset = select_scenarios(corpus, ["oci-chart-undeliverable-patch"])
    assert corpus_version(subset) != corpus_version(corpus)
    assert corpus_version(subset).startswith("1:")


# ---------------------------------------------------------------------------
# recorded_context fidelity.
#
# A scenario is only a valid regression case if it gives the pipeline what a
# real run would have had. ContextCollector queries exactly one PromQL
# expression — the alert's own `g0.expr` — so a recorded sample for any other
# series is evidence the agent could never have collected.
#
# This is enforced here rather than in `load_scenario` on purpose: it is a
# fidelity heuristic, not a parse error, and raising at load time would also
# block ad-hoc `--scenarios` experiments. CI is the right place to guard the
# corpus that ships.
# ---------------------------------------------------------------------------

def _alert_promql(scenario) -> str:
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(scenario.alert.generator_url or "").query)
    return qs.get("g0.expr", [""])[0]


@pytest.mark.parametrize("scenario", load_corpus(), ids=lambda s: s.id)
def test_recorded_metrics_are_reachable_from_the_alert_expression(scenario):
    """Every recorded metric must be a series the alert's own query returns.

    A sample outside it cannot reach a real run, and silently makes the
    scenario easier — which is exactly how `invisible-chart-layer-override`
    came to grade the same with and without the feature it was testing.
    """
    expr = _alert_promql(scenario)
    assert expr, f"{scenario.id}: generatorURL carries no g0.expr to collect from"

    for sample in scenario.metric_samples:
        name = sample.metric.get("__name__")
        assert name, f"{scenario.id}: metric sample has no __name__"
        assert name in expr, (
            f"{scenario.id}: recorded metric {name!r} does not appear in the "
            f"alert's own expression ({expr!r}). ContextCollector only ever "
            f"runs that one query, so a real run could not have this sample. "
            f"Either drop it or widen the alert expression to match."
        )


@pytest.mark.parametrize("scenario", load_corpus(), ids=lambda s: s.id)
def test_recorded_cluster_state_matches_the_object_the_alert_names(scenario):
    """Cluster objects are collectable only for the object the alert points at.

    `ClusterStateCollector` reads the alert's own `namespace` and `pod` labels
    (or resolves pods through the named workload's selector). A scenario that
    recorded a neighbouring pod, or a namespace the alert never mentions, is
    handing the model something no real run could produce — the same class of
    fabricated clue the metric rule above exists to catch.
    """
    state = scenario.cluster_state
    if state is None:
        return

    alert_ns = alert_label(scenario.alert, "namespace")
    assert alert_ns, (
        f"{scenario.id}: recorded cluster_state but the alert carries no "
        f"namespace label — the collector would have collected nothing"
    )
    if state.namespace:
        assert state.namespace == alert_ns, (
            f"{scenario.id}: cluster_state namespace {state.namespace!r} is not "
            f"the alert's namespace {alert_ns!r}"
        )

    alert_pod = alert_label(scenario.alert, "pod")
    if alert_pod:
        for pod in state.pods:
            assert pod.name == alert_pod, (
                f"{scenario.id}: recorded pod {pod.name!r} is not the pod the "
                f"alert names ({alert_pod!r}). With a `pod` label the collector "
                f"fetches exactly that one object."
            )
    else:
        # Without a `pod` label the collector needs a workload label to find
        # any pod at all.
        assert not state.pods or any(
            alert_label(scenario.alert, k) for k in WORKLOAD_LABELS
        ), (
            f"{scenario.id}: recorded pods but the alert names neither a pod nor "
            f"a workload — the collector had nothing to look up"
        )

    # Flux state, like pods, must be the object the alert names: for a
    # gotk_resource_info alert that is the labelled object, exactly.
    ref = gitops_ref_from_alert(scenario.alert)
    if state.gitops is not None and state.gitops.objects and ref is not None:
        owner = state.gitops.owner
        assert (owner.kind, owner.namespace, owner.name) == (ref.kind, ref.namespace, ref.name), (
            f"{scenario.id}: recorded Flux owner {owner.kind} {owner.namespace}/{owner.name} "
            f"is not the object the alert names ({ref.display})"
        )
        assert state.gitops.resolved_from == "alert labels", (
            f"{scenario.id}: a Flux alert resolves from its own labels"
        )

    for event in state.events:
        assert "/" in event.involved_object, (
            f"{scenario.id}: event involved_object {event.involved_object!r} "
            f"should be 'Kind/name'"
        )

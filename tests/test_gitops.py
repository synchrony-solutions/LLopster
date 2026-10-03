"""Flux delivery objects named by an alert's own labels (issue #24 part C).

Fixtures use the label set Flux's reference kube-state-metrics config actually
emits (fluxcd/flux2-monitoring-example), including the KSM target labels that
sit beside it on every series — the case the issue's shorthand
(`kind`/`namespace`) would have gotten wrong.
"""

import textwrap
from datetime import datetime, timezone

import pytest

from src.agent.alert_filter import should_skip
from src.agent.alert_handler import ParsedAlert, strip_url_credentials
from src.agent.cluster_state import ClusterStateCollector
from src.agent.context_collector import AlertContext
from src.agent.gitops import format_gitops_ref, gitops_ref_from_alert
from src.agent.investigator import _format_user_blob
from src.agent.patch_generator import _format_alert_context
from src.services_registry import ServiceRegistry

# What a FluxHelmReleaseNotReady alert built on gotk_resource_info carries.
KSM_TARGET = {
    "namespace": "monitoring",
    "pod": "kube-prometheus-stack-kube-state-metrics-7c9d",
    "service": "kube-prometheus-stack-kube-state-metrics",
    "job": "kube-state-metrics",
    "instance": "10.0.3.17:8080",
}
HELMRELEASE = {
    "customresource_group": "helm.toolkit.fluxcd.io",
    "customresource_kind": "HelmRelease",
    "customresource_version": "v2",
    "exported_namespace": "prod",
    "name": "api",
    "ready": "False",
    "revision": "1.4.2",
    "chart_name": "api",
    "chart_app_version": "2.7.0",
    "chart_source_name": "platform-charts",
}


def flux_alert(**overrides) -> ParsedAlert:
    labels = {"alertname": "FluxHelmReleaseNotReady", **KSM_TARGET, **HELMRELEASE}
    labels.update({k: v for k, v in overrides.items() if v is not None})
    for k, v in overrides.items():
        if v is None:
            labels.pop(k, None)
    return ParsedAlert(
        fingerprint="fp",
        status="firing",
        alertname=labels["alertname"],
        severity="warning",
        service=labels.get("service", "unknown"),
        summary="HelmRelease prod/api is not ready",
        description="",
        starts_at=datetime(2026, 10, 3, tzinfo=timezone.utc),
        ends_at=None,
        labels=labels,
    )


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_reads_the_object_identity_off_the_labels():
    ref = gitops_ref_from_alert(flux_alert())
    assert ref is not None
    assert (ref.controller, ref.kind, ref.group, ref.version) == (
        "flux", "HelmRelease", "helm.toolkit.fluxcd.io", "v2"
    )
    assert (ref.name, ref.namespace) == ("api", "prod")
    assert ref.ready is False
    assert ref.suspended is False
    assert ref.revision == "1.4.2"
    assert ref.details == {
        "chart_name": "api", "chart_app_version": "2.7.0",
        "chart_source_name": "platform-charts",
    }
    assert ref.display == "Flux HelmRelease prod/api"


def test_namespace_is_the_objects_not_kube_state_metrics():
    # `namespace` is the KSM pod's; the HelmRelease's is `exported_namespace`.
    assert gitops_ref_from_alert(flux_alert()).namespace == "prod"


@pytest.mark.parametrize("raw,expected", [("True", True), ("False", False),
                                          ("Unknown", None), (None, None)])
def test_ready_maps_the_condition_status(raw, expected):
    assert gitops_ref_from_alert(flux_alert(ready=raw)).ready is expected


def test_suspended_only_when_the_label_says_true():
    assert gitops_ref_from_alert(flux_alert(suspended="true")).suspended is True
    assert gitops_ref_from_alert(flux_alert(suspended="false")).suspended is False
    assert gitops_ref_from_alert(flux_alert()).suspended is False


@pytest.mark.parametrize("labels", [
    {"alertname": "KubePodCrashLooping", "namespace": "prod", "pod": "api-1", "name": "api"},
    {"customresource_group": "monitoring.coreos.com",
     "customresource_kind": "Prometheus", "name": "k8s"},
    {"customresource_group": "helm.toolkit.fluxcd.io", "name": "api"},
    {"customresource_group": "helm.toolkit.fluxcd.io", "customresource_kind": "HelmRelease"},
])
def test_non_flux_or_incomplete_series_are_not_gitops_alerts(labels):
    alert = flux_alert()
    alert.labels = labels
    assert gitops_ref_from_alert(alert) is None


def test_source_url_credentials_never_reach_the_ref():
    alert = flux_alert(
        customresource_group="source.toolkit.fluxcd.io",
        customresource_kind="GitRepository",
        url="https://deploy:ghp_s3cr3t@github.com/org/platform.git",
    )
    assert gitops_ref_from_alert(alert).details["url"] == "https://github.com/org/platform.git"


@pytest.mark.parametrize("value,expected", [
    ("https://user:tok@example.com:8443/r.git", "https://example.com:8443/r.git"),
    ("ssh://git@github.com/org/r.git", "ssh://github.com/org/r.git"),
    ("https://github.com/org/r.git", "https://github.com/org/r.git"),
    ("ops@example.com", "ops@example.com"),       # not a URL
    ("prod", "prod"),
    ("http://[::1", "http://[::1"),               # unparsable: left alone
])
def test_strip_url_credentials(value, expected):
    assert strip_url_credentials(value) == expected


# --------------------------------------------------------------------------
# Pre-filter: a suspended object is skipped before anything is spent
# --------------------------------------------------------------------------


@pytest.fixture
def services(tmp_path):
    cfg = tmp_path / "services.yaml"
    cfg.write_text(textwrap.dedent("""
        api:
          codebase_path: ./api
          github_repo: org/api
    """))
    return ServiceRegistry(str(cfg))


def test_suspended_release_is_skipped_with_a_distinct_reason(services):
    alert = flux_alert(suspended="true", service="api")
    decision = should_skip(alert, services=services)
    assert decision.skip
    assert "Flux HelmRelease prod/api is suspended" in decision.reason
    assert "spec.suspend: true" in decision.reason


def test_suspension_is_reported_ahead_of_an_unmapped_service(services):
    # The KSM `service` label is not in services.yaml; "suspended" is still
    # the reason an operator needs to read.
    decision = should_skip(flux_alert(suspended="true"), services=services)
    assert "is suspended" in decision.reason


def test_a_not_ready_release_that_is_not_suspended_proceeds(services):
    assert should_skip(flux_alert(service="api"), services=services).skip is False


def test_resolved_beats_suspended(services):
    alert = flux_alert(suspended="true", service="api")
    alert.status = "resolved"
    assert "not firing" in should_skip(alert, services=services).reason


# --------------------------------------------------------------------------
# Prompts
# --------------------------------------------------------------------------


RENDERERS = [
    pytest.param(lambda ctx: _format_alert_context(ctx), id="synthesis"),
    pytest.param(lambda ctx: _format_user_blob(ctx, triage_reasoning=None), id="investigation"),
]


@pytest.mark.parametrize("render", RENDERERS)
def test_both_prompts_carry_the_block_for_a_flux_alert(render):
    text = render(AlertContext(alert=flux_alert()))
    assert "## GitOps resource (from alert labels)" in text
    assert "HelmRelease `api` in namespace `prod`" in text
    assert "- Ready: False" in text
    # Cluster access is off here: the block does not depend on it.
    assert "## Cluster state" not in text


@pytest.mark.parametrize("render", RENDERERS)
def test_ordinary_alerts_get_no_gitops_block(render):
    alert = flux_alert()
    alert.labels = {"alertname": "KubePodCrashLooping", "namespace": "prod", "pod": "api-1"}
    assert "## GitOps resource" not in render(AlertContext(alert=alert))


@pytest.mark.parametrize("render", RENDERERS)
def test_raw_labels_section_strips_url_credentials_too(render):
    alert = flux_alert(url="https://deploy:ghp_s3cr3t@github.com/org/platform.git")
    text = render(AlertContext(alert=alert))
    assert "ghp_s3cr3t" not in text
    assert "- url: https://github.com/org/platform.git" in text


def test_block_says_the_target_labels_belong_to_kube_state_metrics():
    text = "\n".join(format_gitops_ref(gitops_ref_from_alert(flux_alert())))
    assert "kube-state-metrics" in text


# --------------------------------------------------------------------------
# Cluster state: a Flux alert is not a workload alert with something missing
# --------------------------------------------------------------------------


class _NoCalls:
    async def get_pod(self, *a, **k):  # pragma: no cover - must not be called
        raise AssertionError("no pod lookup for a Flux object alert")


@pytest.mark.asyncio
async def test_cluster_state_names_the_flux_object_instead_of_a_missing_pod():
    collector = ClusterStateCollector(_NoCalls(), namespaces=("prod",))
    state = await collector.collect(flux_alert())
    assert state.namespace == "prod"
    assert state.pods == []
    assert any("Flux HelmRelease prod/api, not a workload" in n for n in state.notes)
    assert not any("names no pod or workload" in n for n in state.notes)

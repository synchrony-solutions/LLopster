"""Flux object reads: version discovery, projection, and what never leaves.

Object fixtures follow the current Flux APIs (helm-controller api/v2,
kustomize-controller api/v1, source-controller api/v1): HelmRelease v2 has
`status.history`, not `lastAppliedRevision`.
"""

import json

import httpx
import pytest

from src.db.repository import _serialize
from src.integrations.flux_client import (
    DISCOVERY_TTL_SECONDS,
    MAX_CONDITION_MESSAGE,
    FluxClient,
    FluxNotInstalled,
    FluxRef,
    parse_flux_object,
)
from src.integrations.kubernetes_client import KubernetesAPIError, KubernetesClient

SECRET = "hunter2-db-password"

HELMRELEASE = {
    "apiVersion": "helm.toolkit.fluxcd.io/v2",
    "kind": "HelmRelease",
    "metadata": {"name": "api", "namespace": "prod"},
    "spec": {
        "interval": "10m",
        "releaseName": "api",
        "chart": {"spec": {
            "chart": "api", "version": "1.4.x",
            "sourceRef": {"kind": "HelmRepository", "name": "platform-charts", "namespace": "flux-system"},
        }},
        "driftDetection": {"mode": "warn"},
        "values": {"database": {"password": SECRET}, "replicas": 3},
        "valuesFrom": [{"kind": "Secret", "name": "api-values", "valuesKey": "values.yaml"}],
    },
    "status": {
        "helmChart": "flux-system/prod-api",
        "conditions": [
            {"type": "Ready", "status": "False", "reason": "UpgradeFailed",
             "message": "Helm upgrade failed for release prod/api with chart api@1.4.3: "
                        "cannot patch \"api-migrate\" with kind Job: Job.batch \"api-migrate\" "
                        "is invalid: spec.template: field is immutable"},
            {"type": "Released", "status": "False", "reason": "UpgradeFailed", "message": "x"},
        ],
        "history": [
            {"chartName": "api", "chartVersion": "1.4.3", "appVersion": "2.8.0",
             "status": "failed", "lastDeployed": "2026-10-02T09:00:00Z"},
            {"chartName": "api", "chartVersion": "1.4.2", "appVersion": "2.7.0",
             "status": "superseded", "lastDeployed": "2026-09-20T09:00:00Z"},
            {"chartName": "api", "chartVersion": "1.4.1", "appVersion": "2.7.0",
             "status": "deployed", "lastDeployed": "2026-09-01T09:00:00Z"},
            {"chartName": "api", "chartVersion": "1.4.0", "status": "superseded"},
        ],
        "lastAttemptedRevision": "1.4.3",
        "lastAttemptedReleaseAction": "upgrade",
        "upgradeFailures": 3,
        "failures": 3,
    },
}

KUSTOMIZATION = {
    "apiVersion": "kustomize.toolkit.fluxcd.io/v1",
    "kind": "Kustomization",
    "metadata": {"name": "apps", "namespace": "flux-system"},
    "spec": {
        "path": "./clusters/prod/apps", "prune": True, "targetNamespace": "prod",
        "sourceRef": {"kind": "GitRepository", "name": "platform"},
        "postBuild": {
            "substitute": {"DB_PASSWORD": SECRET, "CLUSTER": "prod"},
            "substituteFrom": [{"kind": "Secret", "name": "cluster-vars", "optional": True}],
        },
    },
    "status": {
        "conditions": [{"type": "Ready", "status": "True", "reason": "ReconciliationSucceeded",
                        "message": "Applied revision: main@sha1:abc123"}],
        "lastAppliedRevision": "main@sha1:abc123",
        "lastAttemptedRevision": "main@sha1:def456",
    },
}

GITREPO = {
    "apiVersion": "source.toolkit.fluxcd.io/v1",
    "kind": "GitRepository",
    "metadata": {"name": "platform", "namespace": "flux-system"},
    "spec": {"url": f"https://deploy:{SECRET}@github.com/org/platform.git",
             "ref": {"branch": "main"}, "suspend": True},
    "status": {
        "conditions": [{"type": "Ready", "status": "False", "reason": "GitOperationFailed",
                        "message": "failed to checkout and determine revision: authentication required"}],
        "artifact": {"revision": "main@sha1:abc123"},
    },
}


# --------------------------------------------------------------------------
# Projection
# --------------------------------------------------------------------------


def test_helmrelease_reads_applied_vs_attempted_from_v2_history():
    hr = parse_flux_object("HelmRelease", HELMRELEASE)
    # Deployed = newest `deployed` snapshot, not history[0] (which failed).
    assert hr.applied_revision == "1.4.1"
    assert hr.attempted_revision == "1.4.3"
    assert [r.chart_version for r in hr.releases] == ["1.4.3", "1.4.2", "1.4.1"]
    assert hr.releases[0].status == "failed"


def test_helmrelease_keeps_the_full_condition_message():
    hr = parse_flux_object("HelmRelease", HELMRELEASE)
    assert hr.ready is False
    ready = next(c for c in hr.conditions if c.type == "Ready")
    assert ready.reason == "UpgradeFailed"
    assert "field is immutable" in ready.message


def test_helmrelease_source_is_the_materialised_helmchart():
    hr = parse_flux_object("HelmRelease", HELMRELEASE)
    assert hr.source == FluxRef("HelmChart", "flux-system", "prod-api")
    assert hr.facts["chart"] == "api (version constraint 1.4.x)"
    assert hr.facts["chart source"] == "HelmRepository/platform-charts"
    assert hr.facts["driftDetection"] == "warn"
    assert hr.facts["upgradeFailures"] == "3"


def test_helmrelease_chartref_is_followed_directly():
    obj = json.loads(json.dumps(HELMRELEASE))
    del obj["spec"]["chart"]
    obj["status"].pop("helmChart")
    obj["spec"]["chartRef"] = {"kind": "OCIRepository", "name": "api-chart"}
    hr = parse_flux_object("HelmRelease", obj)
    assert hr.source == FluxRef("OCIRepository", "prod", "api-chart")


def test_v2beta1_lastappliedrevision_is_the_fallback():
    obj = json.loads(json.dumps(HELMRELEASE))
    obj["status"].pop("history")
    obj["status"]["lastAppliedRevision"] = "1.3.9"
    assert parse_flux_object("HelmRelease", obj).applied_revision == "1.3.9"


def test_drift_detection_defaults_to_disabled_when_unset():
    obj = json.loads(json.dumps(HELMRELEASE))
    obj["spec"].pop("driftDetection")
    assert parse_flux_object("HelmRelease", obj).facts["driftDetection"] == "disabled"


def test_kustomization_revisions_source_and_facts():
    ks = parse_flux_object("Kustomization", KUSTOMIZATION)
    assert (ks.applied_revision, ks.attempted_revision) == ("main@sha1:abc123", "main@sha1:def456")
    assert ks.source == FluxRef("GitRepository", "flux-system", "platform")
    assert ks.facts["path"] == "./clusters/prod/apps"
    assert ks.facts["prune"] == "true"
    assert ks.value_refs == ["Secret/cluster-vars [optional]"]
    assert ks.facts["inline substitutions"] == "2 variable(s) (values withheld)"


def test_gitrepository_strips_url_credentials_and_reads_suspension():
    repo = parse_flux_object("GitRepository", GITREPO)
    assert repo.facts["url"] == "https://github.com/org/platform.git"
    assert repo.facts["ref.branch"] == "main"
    assert repo.applied_revision == "main@sha1:abc123"
    assert repo.suspended is True


def test_helmchart_points_at_its_repository():
    chart = parse_flux_object("HelmChart", {
        "apiVersion": "source.toolkit.fluxcd.io/v1",
        "metadata": {"name": "prod-api", "namespace": "flux-system"},
        "spec": {"chart": "api", "version": "1.4.x",
                 "sourceRef": {"kind": "HelmRepository", "name": "platform-charts"}},
        "status": {"artifact": {"revision": "1.4.3"}, "observedChartName": "api"},
    })
    assert chart.source == FluxRef("HelmRepository", "flux-system", "platform-charts")
    assert chart.applied_revision == "1.4.3"
    assert chart.facts["version constraint"] == "1.4.x"


def test_overlong_condition_messages_are_capped():
    obj = json.loads(json.dumps(GITREPO))
    obj["status"]["conditions"][0]["message"] = "x" * (MAX_CONDITION_MESSAGE * 3)
    message = parse_flux_object("GitRepository", obj).conditions[0].message
    assert len(message) < MAX_CONDITION_MESSAGE + 50
    assert message.endswith("[truncated]")


@pytest.mark.parametrize("kind,obj", [
    ("HelmRelease", HELMRELEASE), ("Kustomization", KUSTOMIZATION), ("GitRepository", GITREPO),
])
def test_inline_secrets_never_survive_projection(kind, obj):
    """The security boundary: values, substitutions and URL userinfo."""
    blob = json.dumps(_serialize(parse_flux_object(kind, obj)))
    assert SECRET not in blob


def test_valuesfrom_names_survive_but_inline_values_are_only_acknowledged():
    hr = parse_flux_object("HelmRelease", HELMRELEASE)
    assert hr.value_refs == ["Secret/api-values (key values.yaml)"]
    assert hr.facts["inline values"] == "present (withheld)"
    assert "replicas" not in json.dumps(_serialize(hr))


# --------------------------------------------------------------------------
# Client: discovery and fetch
# --------------------------------------------------------------------------


def _flux(routes: dict, clock=None):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"kind": "Status", "reason": "NotFound"})
        if isinstance(body, int):
            return httpx.Response(body, json={"message": "forbidden"})
        return httpx.Response(200, json=body)

    k8s = KubernetesClient("https://k8s", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), token_path="/nonexistent")
    client = FluxClient(k8s, clock=clock) if clock else FluxClient(k8s)
    return client, seen


def _group(version: str) -> dict:
    return {"kind": "APIGroup", "preferredVersion": {"version": version}}


@pytest.mark.asyncio
async def test_get_discovers_the_served_version():
    client, seen = _flux({
        "/apis/helm.toolkit.fluxcd.io": _group("v2beta2"),
        "/apis/helm.toolkit.fluxcd.io/v2beta2/namespaces/prod/helmreleases/api": HELMRELEASE,
    })
    hr = await client.get("HelmRelease", "prod", "api")
    assert hr.name == "api" and hr.attempted_revision == "1.4.3"
    assert seen[0] == "/apis/helm.toolkit.fluxcd.io"


@pytest.mark.asyncio
async def test_discovery_is_cached_then_refreshed_after_the_ttl():
    now = [1000.0]
    client, seen = _flux({"/apis/helm.toolkit.fluxcd.io": _group("v2")}, clock=lambda: now[0])
    for _ in range(3):
        assert await client.preferred_version("helm.toolkit.fluxcd.io") == "v2"
    assert seen.count("/apis/helm.toolkit.fluxcd.io") == 1
    now[0] += DISCOVERY_TTL_SECONDS + 1
    await client.preferred_version("helm.toolkit.fluxcd.io")
    assert seen.count("/apis/helm.toolkit.fluxcd.io") == 2


@pytest.mark.asyncio
async def test_unserved_group_raises_flux_not_installed():
    client, _ = _flux({})
    with pytest.raises(FluxNotInstalled) as exc:
        await client.get("Kustomization", "flux-system", "apps")
    assert exc.value.group == "kustomize.toolkit.fluxcd.io"


@pytest.mark.asyncio
async def test_a_label_supplied_version_skips_discovery():
    client, seen = _flux({
        "/apis/helm.toolkit.fluxcd.io/v2/namespaces/prod/helmreleases/api": HELMRELEASE,
    })
    assert (await client.get("HelmRelease", "prod", "api", version="v2")) is not None
    assert seen == ["/apis/helm.toolkit.fluxcd.io/v2/namespaces/prod/helmreleases/api"]


@pytest.mark.parametrize("bad", ["../v1", "v2/../../api", "latest", "v2?watch=1", ""])
@pytest.mark.asyncio
async def test_a_malformed_label_version_is_ignored_not_put_in_a_url(bad):
    client, seen = _flux({
        "/apis/helm.toolkit.fluxcd.io": _group("v2"),
        "/apis/helm.toolkit.fluxcd.io/v2/namespaces/prod/helmreleases/api": HELMRELEASE,
    })
    assert (await client.get("HelmRelease", "prod", "api", version=bad)) is not None
    assert all(bad not in p for p in seen if bad)


@pytest.mark.asyncio
async def test_missing_object_is_none_and_denial_is_an_error():
    client, _ = _flux({
        "/apis/source.toolkit.fluxcd.io": _group("v1"),
        "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/gitrepositories/denied": 403,
    })
    assert await client.get("GitRepository", "flux-system", "gone") is None
    with pytest.raises(KubernetesAPIError, match="403"):
        await client.get("GitRepository", "flux-system", "denied")


@pytest.mark.asyncio
async def test_only_kinds_the_role_grants_are_requested():
    client, seen = _flux({})
    with pytest.raises(ValueError):
        await client.get("Alert", "flux-system", "slack")
    assert seen == []


@pytest.mark.asyncio
async def test_object_names_are_validated_by_the_underlying_client():
    client, seen = _flux({"/apis/helm.toolkit.fluxcd.io": _group("v2")})
    with pytest.raises(KubernetesAPIError, match="invalid Kubernetes object name"):
        await client.get("HelmRelease", "prod", "../../kube-system/secrets/x")
    assert all("secrets" not in p for p in seen)

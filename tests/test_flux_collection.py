"""Flux objects in collection, prompts and the pipeline (issue #24, phase 3)."""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from src.agent.alert_handler import ParsedAlert
from src.agent.cluster_state import ClusterStateCollector, format_cluster_state
from src.agent.context_collector import AlertContext, ContextCollector
from src.agent.investigator import _format_user_blob
from src.agent.patch_generator import _format_alert_context
from src.agent.processor import process_alert
from src.db import repository as repo
from src.db.models import Base
from src.integrations.flux_client import FluxClient
from src.integrations.kubernetes_client import KubernetesClient

from tests.test_flux_client import HELMRELEASE, SECRET

HELM_LABELS = {"helm.toolkit.fluxcd.io/name": "api", "helm.toolkit.fluxcd.io/namespace": "prod"}

HELMCHART = {
    "apiVersion": "source.toolkit.fluxcd.io/v1",
    "metadata": {"name": "prod-api", "namespace": "flux-system"},
    "spec": {"chart": "api", "version": "1.4.x",
             "sourceRef": {"kind": "HelmRepository", "name": "platform-charts"}},
    "status": {"conditions": [{"type": "Ready", "status": "True"}],
               "artifact": {"revision": "1.4.3"}},
}
HELMREPO = {
    "apiVersion": "source.toolkit.fluxcd.io/v1",
    "metadata": {"name": "platform-charts", "namespace": "flux-system"},
    "spec": {"url": "oci://ghcr.io/org/charts", "type": "oci"},
    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
}
HR_EVENT = {
    "involvedObject": {"kind": "HelmRelease", "name": "api"},
    "type": "Warning", "reason": "UpgradeFailed",
    "message": "Helm upgrade failed: field is immutable",
    "lastTimestamp": "2026-10-02T09:00:05Z",
}
SAME_NAME_DEPLOYMENT_EVENT = {
    "involvedObject": {"kind": "Deployment", "name": "api"},
    "type": "Normal", "reason": "ScalingReplicaSet", "message": "noise",
    "lastTimestamp": "2026-10-02T09:00:06Z",
}
POD = {
    "metadata": {"name": "api-7d9f-abc", "namespace": "prod", "ownerReferences": [
        {"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "api-7d9f", "controller": True}]},
    "spec": {"containers": [{"name": "api"}]},
    "status": {"phase": "Running"},
}
REPLICASET = {"metadata": {"name": "api-7d9f", "ownerReferences": [
    {"apiVersion": "apps/v1", "kind": "Deployment", "name": "api", "controller": True}]}}
DEPLOYMENT = {"metadata": {"name": "api", "labels": {**HELM_LABELS, "app": "api"}},
              "spec": {"selector": {"matchLabels": {"app": "api"}}}}

BASE_ROUTES = {
    "/apis/helm.toolkit.fluxcd.io": {"preferredVersion": {"version": "v2"}},
    "/apis/source.toolkit.fluxcd.io": {"preferredVersion": {"version": "v1"}},
    "/apis/helm.toolkit.fluxcd.io/v2/namespaces/prod/helmreleases/api": HELMRELEASE,
    "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/helmcharts/prod-api": HELMCHART,
    "/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/helmrepositories/platform-charts": HELMREPO,
    "/api/v1/namespaces/prod/pods/api-7d9f-abc": POD,
    "/apis/apps/v1/namespaces/prod/replicasets/api-7d9f": REPLICASET,
    "/apis/apps/v1/namespaces/prod/deployments/api": DEPLOYMENT,
    "/api/v1/namespaces/prod/pods": {"items": [POD]},
}


def _collector(routes=None, *, flux=True, **kwargs):
    routes = {**BASE_ROUTES, **(routes or {})}
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path.endswith("/events"):
            sel = request.url.params.get("fieldSelector", "")
            items = [HR_EVENT, SAME_NAME_DEPLOYMENT_EVENT] if sel.endswith("=api") else []
            return httpx.Response(200, json={"items": items})
        body = routes.get(request.url.path)
        if body is None:
            return httpx.Response(404, json={"reason": "NotFound"})
        if isinstance(body, int):
            return httpx.Response(body, json={"message": "denied"})
        return httpx.Response(200, json=body)

    k8s = KubernetesClient("https://k8s", client=httpx.AsyncClient(
        transport=httpx.MockTransport(handler)), token_path="/nonexistent")
    kwargs.setdefault("namespaces", ("prod",))
    kwargs.setdefault("flux_namespaces", ("flux-system",))
    c = ClusterStateCollector(k8s, flux=FluxClient(k8s) if flux else None, **kwargs)
    c.seen = seen
    return c


def _alert(**labels) -> ParsedAlert:
    return ParsedAlert(
        fingerprint="fp", status="firing", alertname=labels.pop("alertname", "X"),
        severity="warning", service=labels.pop("service", "api"), summary="", description="",
        starts_at=datetime(2026, 10, 3, tzinfo=timezone.utc), ends_at=None, labels=labels,
    )


def _flux_alert(**extra) -> ParsedAlert:
    return _alert(
        alertname="FluxHelmReleaseNotReady", namespace="monitoring", pod="ksm-0",
        customresource_group="helm.toolkit.fluxcd.io", customresource_kind="HelmRelease",
        customresource_version="v2", exported_namespace="prod", name="api", ready="False",
        **extra,
    )


# --------------------------------------------------------------------------
# Resolution
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flux_alert_reads_the_named_release_and_its_source_chain():
    c = _collector()
    state = await c.collect(_flux_alert())
    gs = state.gitops
    assert gs.resolved_from == "alert labels"
    assert [o.kind for o in gs.objects] == ["HelmRelease", "HelmChart", "HelmRepository"]
    assert gs.owner.attempted_revision == "1.4.3"
    # The label's API version was used: no discovery for the release's group.
    assert "/apis/helm.toolkit.fluxcd.io" not in c.seen
    # The KSM pod named by the alert's `pod` label was never requested.
    assert not any("ksm-0" in p for p in c.seen)


@pytest.mark.asyncio
async def test_flux_events_are_filtered_to_the_flux_kind():
    state = await _collector().collect(_flux_alert())
    reasons = [e.reason for e in state.gitops.events]
    assert reasons == ["UpgradeFailed"]          # not the same-name Deployment's


@pytest.mark.asyncio
async def test_workload_alert_resolves_the_release_from_deployment_labels():
    state = await _collector().collect(_alert(namespace="prod", pod="api-7d9f-abc"))
    assert state.gitops.resolved_from == "Deployment/api labels"
    assert state.gitops.owner.name == "api"
    assert state.pods[0].owner_chain[-1].gitops_labels == HELM_LABELS


@pytest.mark.asyncio
async def test_workload_path_reads_labels_off_the_named_deployment():
    state = await _collector().collect(_alert(namespace="prod", deployment="api"))
    assert state.gitops.resolved_from == "Deployment/api labels"


@pytest.mark.asyncio
async def test_kustomize_labels_resolve_a_kustomization():
    ks = {"apiVersion": "kustomize.toolkit.fluxcd.io/v1",
          "metadata": {"name": "apps", "namespace": "flux-system"},
          "spec": {"path": "./apps"}, "status": {}}
    dep = {"metadata": {"name": "api", "labels": {
        "kustomize.toolkit.fluxcd.io/name": "apps",
        "kustomize.toolkit.fluxcd.io/namespace": "flux-system"}}}
    c = _collector({
        "/apis/apps/v1/namespaces/prod/deployments/api": dep,
        "/apis/kustomize.toolkit.fluxcd.io": {"preferredVersion": {"version": "v1"}},
        "/apis/kustomize.toolkit.fluxcd.io/v1/namespaces/flux-system/kustomizations/apps": ks,
    })
    state = await c.collect(_alert(namespace="prod", pod="api-7d9f-abc"))
    assert state.gitops.owner.kind == "Kustomization"


@pytest.mark.asyncio
async def test_unlabelled_workload_says_no_flux_owner():
    c = _collector({"/apis/apps/v1/namespaces/prod/deployments/api": {"metadata": {"name": "api"}}})
    state = await c.collect(_alert(namespace="prod", pod="api-7d9f-abc"))
    assert state.gitops is None
    assert any("no Flux owner found" in n for n in state.notes)


@pytest.mark.asyncio
async def test_flux_not_installed_is_a_note_not_an_error():
    c = _collector({"/apis/helm.toolkit.fluxcd.io": None,
                    "/apis/helm.toolkit.fluxcd.io/v2/namespaces/prod/helmreleases/api": None})
    state = await c.collect(_alert(namespace="prod", pod="api-7d9f-abc"))
    assert state.gitops is None
    assert any("not served" in n for n in state.notes)
    assert not any("flux" in e.lower() for e in state.errors)


@pytest.mark.asyncio
async def test_release_in_an_unbound_namespace_is_noted_with_the_fix():
    c = _collector(flux_namespaces=())
    state = await c.collect(_flux_alert())
    # prod is a core namespace, so the release is readable; its chart in
    # flux-system is not.
    assert [o.kind for o in state.gitops.objects] == ["HelmRelease"]
    assert any("flux.namespaces" in n and "flux-system/prod-api" in n for n in state.notes)


@pytest.mark.asyncio
async def test_flux_alert_in_a_flux_only_namespace_skips_workload_reads():
    alert = _alert(alertname="FluxSourceNotReady", namespace="monitoring",
                   customresource_group="source.toolkit.fluxcd.io",
                   customresource_kind="HelmRepository", exported_namespace="flux-system",
                   name="platform-charts", ready="False")
    c = _collector()
    state = await c.collect(alert)
    assert state.gitops.owner.kind == "HelmRepository"
    assert not any("/pods" in p for p in c.seen)


@pytest.mark.asyncio
async def test_rbac_denial_on_the_release_is_an_error():
    c = _collector({"/apis/helm.toolkit.fluxcd.io/v2/namespaces/prod/helmreleases/api": 403})
    state = await c.collect(_flux_alert())
    assert state.gitops is None
    assert any("flux lookup failed" in e for e in state.errors)


@pytest.mark.asyncio
async def test_flux_off_keeps_the_phase_one_note():
    state = await _collector(flux=False).collect(_flux_alert())
    assert state.gitops is None
    assert any("Flux objects are not read" in n for n in state.notes)


@pytest.mark.asyncio
async def test_suspended_source_is_reported_on_the_chain():
    chart = json.loads(json.dumps(HELMCHART))
    chart["spec"]["suspend"] = True
    c = _collector({"/apis/source.toolkit.fluxcd.io/v1/namespaces/flux-system/helmcharts/prod-api": chart})
    state = await c.collect(_flux_alert())
    assert [o.kind for o in state.gitops.suspended] == ["HelmChart"]


@pytest.mark.asyncio
async def test_concurrent_collections_never_cross_owners():
    """Regression: owner labels are per-collection, not collector state."""
    other_dep = {"metadata": {"name": "web", "labels": {
        "helm.toolkit.fluxcd.io/name": "web", "helm.toolkit.fluxcd.io/namespace": "prod"}},
        "spec": {"selector": {"matchLabels": {"app": "web"}}}}
    web_hr = json.loads(json.dumps(HELMRELEASE))
    web_hr["metadata"]["name"] = "web"
    c = _collector({
        "/apis/apps/v1/namespaces/prod/deployments/web": other_dep,
        "/apis/helm.toolkit.fluxcd.io/v2/namespaces/prod/helmreleases/web": web_hr,
    })
    a, b = await asyncio.gather(*(
        c.collect(_alert(namespace="prod", deployment=name)) for name in ("api", "web")
    ))
    assert (a.gitops.owner.name, b.gitops.owner.name) == ("api", "web")


# --------------------------------------------------------------------------
# Prompts and the run record
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_gitops_block_carries_the_diagnosis_and_revision_reading():
    state = await _collector().collect(_flux_alert())
    text = "\n".join(format_cluster_state(state))
    assert "## GitOps state" in text
    assert "field is immutable" in text
    assert "applied=1.4.1 attempted=1.4.3" in text
    assert "the attempted revision has not been applied" in text
    assert "Delivery chain: HelmRelease/prod/api -> HelmChart/flux-system/prod-api" in text.replace("**", "")
    assert SECRET not in text


@pytest.mark.parametrize("render", [
    lambda ctx: _format_alert_context(ctx),
    lambda ctx: _format_user_blob(ctx, triage_reasoning=None),
])
@pytest.mark.asyncio
async def test_label_block_gives_way_to_collected_state(render):
    alert = _flux_alert()
    with_state = render(AlertContext(alert=alert, cluster_state=await _collector().collect(alert)))
    assert "## GitOps state" in with_state
    assert "## GitOps resource (from alert labels)" not in with_state
    without = render(AlertContext(alert=alert))
    assert "## GitOps resource (from alert labels)" in without


@pytest.mark.asyncio
async def test_gitops_state_round_trips_through_the_run_record():
    state = await _collector().collect(_flux_alert())
    blob = repo._serialize(state)
    assert blob["gitops"]["objects"][0]["kind"] == "HelmRelease"
    assert SECRET not in json.dumps(blob)


# --------------------------------------------------------------------------
# Pipeline
# --------------------------------------------------------------------------


@pytest.fixture
async def sm():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", future=True)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


def _services():
    services = MagicMock()
    cfg = MagicMock(codebase_path="./demo-app", github_repo="owner/repo", delivery=None)
    cfg.chart_lineage = ()
    services.get.return_value = cfg
    services.names.return_value = ["api"]
    return services


@pytest.mark.asyncio
async def test_suspended_release_found_during_collection_skips_before_llm(sm):
    hr = json.loads(json.dumps(HELMRELEASE))
    hr["spec"]["suspend"] = True
    alert = _alert(namespace="prod", pod="api-7d9f-abc")
    cluster = _collector({"/apis/helm.toolkit.fluxcd.io/v2/namespaces/prod/helmreleases/api": hr})
    collector = ContextCollector(loki=MagicMock(query_range=AsyncMock(return_value=[])),
                                 prometheus=MagicMock(query=AsyncMock(return_value=[])),
                                 cluster=cluster)
    patcher = MagicMock(generate=AsyncMock())
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})

    await process_alert(run.id, alert, sessionmaker=sm, collector=collector,
                        services=_services(), patcher=patcher, github=None, notifier=None)

    async with sm() as s:
        fetched = await repo.get_run(s, run.id)
    assert fetched.processing_status == "skipped"
    assert "Flux HelmRelease prod/api is suspended" in fetched.error_message
    assert fetched.cluster_state_json["gitops"]["objects"][0]["suspended"] is True
    patcher.generate.assert_not_called()


@pytest.mark.asyncio
async def test_lookback_override_keeps_cluster_collection(sm):
    """Regression: the override collector used to drop `cluster=`."""
    alert = _alert(namespace="prod", pod="api-7d9f-abc")
    cluster = MagicMock()
    cluster.collect = AsyncMock(side_effect=AssertionError("stop here"))
    collector = ContextCollector(loki=MagicMock(query_range=AsyncMock(return_value=[])),
                                 prometheus=MagicMock(query=AsyncMock(return_value=[])),
                                 cluster=cluster)
    async with sm() as s:
        run = await repo.create_run_from_alert(s, alert, raw_payload={})
    await process_alert(run.id, alert, sessionmaker=sm, collector=collector,
                        services=_services(), patcher=MagicMock(generate=AsyncMock()),
                        github=None, notifier=None, lookback_minutes=5)
    cluster.collect.assert_awaited_once()

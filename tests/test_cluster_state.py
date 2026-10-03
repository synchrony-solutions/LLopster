"""Alert -> cluster objects selection, and its degradation paths."""

import httpx
import pytest

from src.agent.alert_handler import ParsedAlert
from src.agent.cluster_state import ClusterStateCollector, _match_labels
from src.integrations.kubernetes_client import KubernetesClient


def make_alert(**labels) -> ParsedAlert:
    return ParsedAlert(
        fingerprint="fp",
        status="firing",
        alertname=labels.pop("alertname", "KubePodCrashLooping"),
        severity="warning",
        service="api",
        summary="",
        description="",
        starts_at=None,
        ends_at=None,
        labels=labels,
    )


def collector(routes: dict, **kwargs) -> ClusterStateCollector:
    """`routes` maps a path suffix to a status/json pair or a callable."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        for suffix, response in routes.items():
            if request.url.path.endswith(suffix):
                if callable(response):
                    return response(request)
                return httpx.Response(200, json=response)
        return httpx.Response(404, json={"message": "not found"})

    client = KubernetesClient(
        "https://k8s",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        token_path="/nonexistent",
    )
    kwargs.setdefault("namespaces", ("prod",))
    c = ClusterStateCollector(client, **kwargs)
    c.seen = seen  # type: ignore[attr-defined]
    return c


POD = {
    "metadata": {
        "name": "api-7d9f-abc",
        "namespace": "prod",
        "ownerReferences": [
            {"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "api-7d9f", "controller": True}
        ],
    },
    "spec": {"nodeName": "node-1", "containers": [{"name": "api"}]},
    "status": {
        "phase": "Running",
        "containerStatuses": [
            {
                "name": "api",
                "restartCount": 9,
                "state": {"waiting": {"reason": "CrashLoopBackOff"}},
                "lastState": {"terminated": {"reason": "OOMKilled", "exitCode": 137}},
            }
        ],
    },
}

REPLICASET = {
    "metadata": {
        "name": "api-7d9f",
        "ownerReferences": [
            {"apiVersion": "apps/v1", "kind": "Deployment", "name": "api", "controller": True}
        ],
    }
}

def events_for(by_name: dict[str, list[dict]]):
    """Mock the API server's own fieldSelector filtering: each call returns
    only the named object's events, never every event in the namespace."""

    def handler(request: httpx.Request) -> httpx.Response:
        selector = request.url.params.get("fieldSelector", "")
        _, _, name = selector.partition("involvedObject.name=")
        return httpx.Response(200, json={"items": by_name.get(name, [])})

    return handler


def event(name: str, reason: str, ts: str = "2026-08-29T10:00:00Z", **extra) -> dict:
    return {
        "involvedObject": {"kind": "Pod", "name": name},
        "type": "Warning",
        "reason": reason,
        "lastTimestamp": ts,
        **extra,
    }


EVENTS = events_for(
    {
        "api-7d9f-abc": [
            event("api-7d9f-abc", "BackOff", message="Back-off restarting failed container")
        ]
    }
)


# --------------------------------------------------------------------------
# The acceptance criterion: a CrashLoopBackOff run records exit code + reason
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_crashloop_alert_collects_exit_code_owner_chain_and_events():
    c = collector(
        {
            "/pods/api-7d9f-abc": POD,
            "/replicasets/api-7d9f": REPLICASET,
            "/deployments/api": {"metadata": {}},
            "/events": EVENTS,
        }
    )
    state = await c.collect(make_alert(namespace="prod", pod="api-7d9f-abc"))

    assert state.namespace == "prod"
    (pod,) = state.pods
    (container,) = pod.containers
    assert container.state_reason == "CrashLoopBackOff"
    assert container.last_state_reason == "OOMKilled"
    assert container.last_state_exit_code == 137
    assert container.restart_count == 9
    assert [(o.kind, o.name) for o in pod.owner_chain] == [
        ("ReplicaSet", "api-7d9f"),
        ("Deployment", "api"),
    ]
    assert [e.reason for e in state.events] == ["BackOff"]
    assert "Pod/api-7d9f-abc" in state.objects_queried
    assert state.errors == []


@pytest.mark.asyncio
async def test_events_are_gathered_for_the_owner_chain_too():
    """A pod that was never admitted has no events of its own; the admission
    rejection is recorded against the ReplicaSet trying to create it."""
    c = collector(
        {
            "/pods/api-7d9f-abc": POD,
            "/replicasets/api-7d9f": REPLICASET,
            "/deployments/api": {"metadata": {}},
            "/events": events_for(
                {
                    "api-7d9f-abc": [event("api-7d9f-abc", "BackOff", "2026-08-29T10:00:00Z")],
                    "api-7d9f": [
                        event(
                            "api-7d9f",
                            "FailedCreate",
                            "2026-08-29T10:05:00Z",
                            message="admission webhook denied the request",
                        )
                    ],
                }
            ),
        }
    )
    state = await c.collect(make_alert(namespace="prod", pod="api-7d9f-abc"))
    assert len([p for p in c.seen if p.endswith("/events")]) == 3  # pod + RS + Deploy
    assert [e.reason for e in state.events] == ["FailedCreate", "BackOff"]


# --------------------------------------------------------------------------
# Namespace scoping
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_scope_namespace_is_never_queried():
    c = collector({"/pods/api-1": POD}, namespaces=("prod",))
    state = await c.collect(make_alert(namespace="kube-system", pod="api-1"))
    assert state.pods == []
    assert c.seen == []
    assert "outside the namespaces" in state.notes[0]


@pytest.mark.asyncio
async def test_all_namespaces_bypasses_the_allowlist():
    c = collector({"/pods/api-7d9f-abc": POD, "/events": {"items": []}},
                  namespaces=(), all_namespaces=True)
    state = await c.collect(make_alert(namespace="anything", pod="api-7d9f-abc"))
    assert len(state.pods) == 1


@pytest.mark.asyncio
async def test_alert_without_a_namespace_label_collects_nothing():
    c = collector({})
    state = await c.collect(make_alert(pod="api-1"))
    assert state.is_empty
    assert c.seen == []
    assert "no namespace label" in state.notes[0]


# --------------------------------------------------------------------------
# Workload path (no `pod` label)
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_deployment_alert_resolves_pods_via_the_workload_selector():
    deployment = {
        "metadata": {"name": "api"},
        "spec": {"selector": {"matchLabels": {"app": "api", "tier": "web"}}},
    }

    def pods(request: httpx.Request) -> httpx.Response:
        assert request.url.params["labelSelector"] == "app=api,tier=web"
        return httpx.Response(200, json={"items": [POD]})

    c = collector(
        {
            "/deployments/api": deployment,
            "/namespaces/prod/pods": pods,
            "/replicasets/api-7d9f": REPLICASET,
            "/deployments/api/": {"metadata": {}},
            "/events": {"items": []},
        }
    )
    state = await c.collect(make_alert(namespace="prod", deployment="api"))
    assert [p.name for p in state.pods] == ["api-7d9f-abc"]
    assert "Deployment/api" in state.objects_queried


@pytest.mark.asyncio
async def test_workload_without_a_selector_is_reported_not_guessed():
    c = collector({"/statefulsets/db": {"metadata": {"name": "db"}, "spec": {}}})
    state = await c.collect(make_alert(namespace="prod", statefulset="db"))
    assert state.pods == []
    assert any("no matchLabels selector" in n for n in state.notes)


@pytest.mark.asyncio
async def test_alert_naming_no_pod_or_workload_says_so():
    c = collector({})
    state = await c.collect(make_alert(namespace="prod"))
    assert any("names no pod or workload" in n for n in state.notes)


def test_match_labels_is_deterministic_and_rejects_empty_selectors():
    assert _match_labels({"spec": {"selector": {"matchLabels": {"b": "2", "a": "1"}}}}) == "a=1,b=2"
    assert _match_labels({"spec": {"selector": {"matchLabels": {}}}}) is None
    assert _match_labels({}) is None


# --------------------------------------------------------------------------
# Nodes -- cluster-scoped, so only reachable with a ClusterRoleBinding
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_node_is_not_requested_when_bound_per_namespace():
    """A per-namespace RoleBinding cannot grant `get nodes`, so asking would be
    a guaranteed 403 on every node-scoped alert."""
    c = collector({"/pods/api-7d9f-abc": POD, "/events": {"items": []}},
                  all_namespaces=False)
    state = await c.collect(make_alert(namespace="prod", pod="api-7d9f-abc"))
    assert state.nodes == []
    assert not [p for p in c.seen if "/nodes/" in p]
    assert any("cluster-scoped" in n for n in state.notes)


@pytest.mark.asyncio
async def test_node_conditions_are_read_when_cluster_scoped():
    node = {
        "metadata": {"name": "node-1"},
        "status": {"conditions": [{"type": "MemoryPressure", "status": "True"}]},
    }
    c = collector(
        {"/pods/api-7d9f-abc": POD, "/events": {"items": []}, "/nodes/node-1": node},
        all_namespaces=True,
    )
    state = await c.collect(make_alert(namespace="prod", pod="api-7d9f-abc"))
    assert state.nodes[0].name == "node-1"
    assert state.nodes[0].conditions[0].type == "MemoryPressure"


@pytest.mark.asyncio
async def test_node_scoped_alert_without_a_namespace_still_reads_the_node():
    node = {"metadata": {"name": "node-9"}, "status": {"conditions": []}}
    c = collector({"/nodes/node-9": node}, all_namespaces=True)
    state = await c.collect(make_alert(alertname="KubeNodeNotReady", node="node-9"))
    assert [n.name for n in state.nodes] == ["node-9"]


# --------------------------------------------------------------------------
# PVCs
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_pvc_collects_the_claim_and_its_binding_events():
    pvc = {
        "metadata": {"name": "data-db-0", "namespace": "prod"},
        "spec": {"storageClassName": "gp3", "resources": {"requests": {"storage": "20Gi"}}},
        "status": {"phase": "Pending"},
    }
    events = events_for(
        {
            "data-db-0": [
                event(
                    "data-db-0",
                    "FailedBinding",
                    message="no persistent volumes available for this claim",
                )
            ]
        }
    )
    c = collector({"/persistentvolumeclaims/data-db-0": pvc, "/events": events})
    state = await c.collect(
        make_alert(namespace="prod", persistentvolumeclaim="data-db-0")
    )
    assert state.pvcs[0].phase == "Pending"
    assert state.pvcs[0].requested_storage == "20Gi"
    assert [e.reason for e in state.events] == ["FailedBinding"]


# --------------------------------------------------------------------------
# Caps
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_events_are_capped_and_the_cap_is_recorded():
    many = events_for(
        {
            "api-7d9f-abc": [
                event("api-7d9f-abc", f"E{i}", f"2026-08-29T10:{i:02d}:00Z")
                for i in range(10)
            ]
        }
    )
    c = collector(
        {"/pods/api-7d9f-abc": POD, "/replicasets/api-7d9f": REPLICASET,
         "/deployments/api": {"metadata": {}}, "/events": many},
        max_events=3,
    )
    state = await c.collect(make_alert(namespace="prod", pod="api-7d9f-abc"))
    assert len(state.events) == 3
    assert [e.reason for e in state.events] == ["E9", "E8", "E7"]  # newest first
    assert any("most recent" in n for n in state.notes)


@pytest.mark.asyncio
async def test_include_pod_spec_false_omits_the_spec_without_a_note():
    c = collector({"/pods/api-7d9f-abc": POD, "/replicasets/api-7d9f": REPLICASET,
                   "/deployments/api": {"metadata": {}}, "/events": {"items": []}},
                  include_pod_spec=False)
    state = await c.collect(make_alert(namespace="prod", pod="api-7d9f-abc"))
    assert state.pods[0].spec is None
    assert state.pods[0].spec_truncated is False
    # "not requested" must not be reported as "dropped for size".
    assert not any("size ceiling" in n for n in state.notes)


@pytest.mark.asyncio
async def test_oversized_pod_spec_is_noted_so_the_omission_is_visible():
    big = {**POD, "spec": {**POD["spec"], "containers": [{"name": "api", "image": "x" * 20000}]}}
    c = collector({"/pods/api-7d9f-abc": big, "/replicasets/api-7d9f": REPLICASET,
                   "/deployments/api": {"metadata": {}}, "/events": {"items": []}},
                  max_pod_spec_bytes=500)
    state = await c.collect(make_alert(namespace="prod", pod="api-7d9f-abc"))
    assert state.pods[0].spec is None
    assert any("size ceiling" in n for n in state.notes)


# --------------------------------------------------------------------------
# Degradation
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rbac_denial_records_an_error_and_returns_empty_state():
    c = collector({"/pods/api-1": lambda r: httpx.Response(403, json={"message": "forbidden"})})
    state = await c.collect(make_alert(namespace="prod", pod="api-1"))
    assert state.is_empty
    assert len(state.errors) == 1
    assert "403" in state.errors[0]


@pytest.mark.asyncio
async def test_unreachable_api_server_records_an_error_rather_than_raising():
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    c = collector({"/pods/api-1": down})
    state = await c.collect(make_alert(namespace="prod", pod="api-1"))
    assert state.errors and "failed" in state.errors[0]


@pytest.mark.asyncio
async def test_a_pod_deleted_between_firing_and_collection_is_a_note_not_an_error():
    c = collector({})
    state = await c.collect(make_alert(namespace="prod", pod="gone-abc"))
    assert state.errors == []
    assert any("no longer exists" in n for n in state.notes)


@pytest.mark.asyncio
async def test_event_lookup_failure_does_not_lose_the_pod():
    c = collector(
        {
            "/pods/api-7d9f-abc": POD,
            "/replicasets/api-7d9f": REPLICASET,
            "/deployments/api": {"metadata": {}},
            "/events": lambda r: httpx.Response(403, json={"message": "forbidden"}),
        }
    )
    state = await c.collect(make_alert(namespace="prod", pod="api-7d9f-abc"))
    assert len(state.pods) == 1
    assert state.events == []
    assert state.errors


# --------------------------------------------------------------------------
# Label resolution: scrape collisions and the `job` label
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exported_labels_win_over_the_scrape_targets_own():
    # KSM scraped with honor_labels off: `namespace`/`pod` are KSM's own.
    c = collector({"/pods/api-7d9f-abc": POD, "/replicasets/api-7d9f": REPLICASET,
                   "/deployments/api": {"metadata": {}}, "/events": EVENTS})
    state = await c.collect(make_alert(
        namespace="monitoring", exported_namespace="prod",
        pod="kube-state-metrics-xyz", exported_pod="api-7d9f-abc",
    ))
    assert state.namespace == "prod"
    assert [p.name for p in state.pods] == ["api-7d9f-abc"]
    assert not any("kube-state-metrics" in path for path in c.seen)


@pytest.mark.asyncio
async def test_after_a_collision_a_bare_pod_label_is_the_target_not_the_object():
    deployment = {"metadata": {"name": "api"},
                  "spec": {"selector": {"matchLabels": {"app": "api"}}}}
    c = collector({"/deployments/api": deployment,
                   "/namespaces/prod/pods": {"items": [POD]},
                   "/replicasets/api-7d9f": REPLICASET, "/events": {"items": []}})
    state = await c.collect(make_alert(
        namespace="monitoring", exported_namespace="prod",
        pod="kube-state-metrics-xyz", deployment="api",
    ))
    assert [p.name for p in state.pods] == ["api-7d9f-abc"]
    assert not any("kube-state-metrics" in path for path in c.seen)


@pytest.mark.asyncio
async def test_prometheus_job_label_is_not_read_as_a_batch_job():
    c = collector({})
    state = await c.collect(make_alert(namespace="prod", job="demo-app"))
    assert not any("/jobs/" in path for path in c.seen)
    assert not any("no longer exists" in n for n in state.notes)
    assert any("names no pod or workload" in n for n in state.notes)


@pytest.mark.asyncio
async def test_traversal_in_a_label_is_recorded_as_an_error_not_requested():
    c = collector({})
    state = await c.collect(make_alert(namespace="prod", pod="../../kube-system/pods/x"))
    assert c.seen == []
    assert state.pods == []
    assert any("invalid Kubernetes object name" in e for e in state.errors)


# --------------------------------------------------------------------------
# Caps are stated, never silent
# --------------------------------------------------------------------------


def _replica(name: str, *, ready: bool, restarts: int = 0) -> dict:
    return {
        "metadata": {"name": name, "namespace": "prod"},
        "spec": {"containers": [{"name": "api"}]},
        "status": {"phase": "Running", "containerStatuses": [
            {"name": "api", "ready": ready, "restartCount": restarts}
        ]},
    }


@pytest.mark.asyncio
async def test_workload_path_keeps_the_least_healthy_pods_and_notes_the_rest():
    deployment = {"metadata": {"name": "api"},
                  "spec": {"selector": {"matchLabels": {"app": "api"}}}}
    replicas = [_replica(f"api-{i}", ready=True) for i in range(9)]
    replicas.insert(6, _replica("api-crashing", ready=False, restarts=12))

    def pods(request: httpx.Request) -> httpx.Response:
        assert int(request.url.params["limit"]) > 3  # scanned, not sliced
        return httpx.Response(200, json={"items": replicas})

    c = collector({"/deployments/api": deployment, "/namespaces/prod/pods": pods,
                   "/events": {"items": []}}, max_pods=3)
    state = await c.collect(make_alert(namespace="prod", deployment="api"))
    assert len(state.pods) == 3
    assert state.pods[0].name == "api-crashing"
    assert any("10 pods" in n and "7 omitted" in n for n in state.notes)


@pytest.mark.asyncio
async def test_event_sources_past_the_cap_are_named_in_a_note():
    deployment = {"metadata": {"name": "api"},
                  "spec": {"selector": {"matchLabels": {"app": "api"}}}}
    replicas = [_replica(f"api-{i}", ready=False) for i in range(3)]
    for r in replicas:
        r["metadata"]["ownerReferences"] = REPLICASET_REF
    c = collector({"/deployments/api": deployment,
                   "/namespaces/prod/pods": {"items": replicas},
                   "/replicasets/api-7d9f": REPLICASET, "/events": {"items": []}})
    state = await c.collect(make_alert(namespace="prod", deployment="api"))
    # pod, RS, Deployment, pod = 4 sources; the third pod is past the cap.
    assert any("events not fetched for api-2" in n for n in state.notes)


@pytest.mark.asyncio
async def test_pvc_event_merge_notes_its_truncation():
    pvc = {"metadata": {"name": "data", "namespace": "prod"}, "status": {"phase": "Pending"}}
    many = events_for({"data": [
        event("data", f"E{i}", f"2026-08-29T10:{i:02d}:00Z") for i in range(5)
    ]})
    c = collector({"/persistentvolumeclaims/data": pvc, "/events": many}, max_events=2)
    state = await c.collect(make_alert(namespace="prod", persistentvolumeclaim="data"))
    assert len(state.events) == 2
    assert any("including the claim's" in n for n in state.notes)


REPLICASET_REF = [
    {"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "api-7d9f", "controller": True}
]

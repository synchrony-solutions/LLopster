from datetime import datetime, timezone

import httpx
import pytest

from src.integrations.kubernetes_client import (
    REDACTED,
    ClusterEvent,
    KubernetesAPIError,
    KubernetesClient,
    _api_root,
    _controller_ref,
    in_cluster_base_url,
    parse_events,
    parse_node,
    parse_pod,
    parse_pvc,
    redact_object,
    redact_pod_spec,
    sort_events,
)


# --------------------------------------------------------------------------
# Redaction -- the acceptance criterion "env-var values are redacted"
# --------------------------------------------------------------------------


def test_redact_pod_spec_drops_env_values_but_keeps_keys():
    spec = {
        "containers": [
            {
                "name": "api",
                "env": [
                    {"name": "DATABASE_URL", "value": "postgres://u:hunter2@db/app"},
                    {"name": "LOG_LEVEL", "value": "debug"},
                ],
            }
        ]
    }
    out = redact_pod_spec(spec)
    env = out["containers"][0]["env"]
    assert [e["name"] for e in env] == ["DATABASE_URL", "LOG_LEVEL"]
    assert all(e["value"] == REDACTED for e in env)
    assert "hunter2" not in str(out)


def test_redact_pod_spec_covers_init_and_ephemeral_containers():
    spec = {
        "initContainers": [{"name": "migrate", "env": [{"name": "PW", "value": "s3cr3t"}]}],
        "ephemeralContainers": [{"name": "debug", "env": [{"name": "TOK", "value": "abc"}]}],
    }
    out = redact_pod_spec(spec)
    assert "s3cr3t" not in str(out)
    assert "abc" not in str(out)
    assert out["initContainers"][0]["env"][0]["name"] == "PW"


def test_redact_pod_spec_keeps_valuefrom_and_envfrom_references():
    """A Secret/ConfigMap *name* is a reference, not a value -- and "this env
    var comes from Secret X" is usually the whole answer to a
    CreateContainerConfigError."""
    spec = {
        "containers": [
            {
                "name": "api",
                "env": [
                    {"name": "PW", "valueFrom": {"secretKeyRef": {"name": "db", "key": "pw"}}}
                ],
                "envFrom": [{"configMapRef": {"name": "app-config"}}],
            }
        ]
    }
    out = redact_pod_spec(spec)
    env = out["containers"][0]["env"][0]
    assert env["valueFrom"]["secretKeyRef"] == {"name": "db", "key": "pw"}
    assert "value" not in env
    assert out["containers"][0]["envFrom"][0]["configMapRef"]["name"] == "app-config"


def test_redact_pod_spec_does_not_mutate_the_input():
    spec = {"containers": [{"name": "api", "env": [{"name": "PW", "value": "secret"}]}]}
    redact_pod_spec(spec)
    assert spec["containers"][0]["env"][0]["value"] == "secret"


def test_redact_object_elides_data_and_string_data_values():
    obj = {"data": {"password": "aHVudGVyMg=="}, "stringData": {"token": "plain"}}
    out = redact_object(obj)
    assert out["data"] == {"password": REDACTED}
    assert out["stringData"] == {"token": REDACTED}


def test_redact_object_elides_last_applied_configuration_annotation():
    """The annotation is a serialized copy of the whole object, env values
    included -- redacting the spec while shipping this would leak anyway."""
    obj = {
        "annotations": {
            "kubectl.kubernetes.io/last-applied-configuration": '{"env":[{"value":"hunter2"}]}',
            "prometheus.io/scrape": "true",
        }
    }
    out = redact_object(obj)
    assert out["annotations"]["kubectl.kubernetes.io/last-applied-configuration"] == REDACTED
    assert out["annotations"]["prometheus.io/scrape"] == "true"


def test_redact_object_recurses_into_nested_structures():
    obj = {"spec": {"volumes": [{"secret": {"secretName": "db"}, "data": {"k": "v"}}]}}
    out = redact_object(obj)
    assert out["spec"]["volumes"][0]["data"] == {"k": REDACTED}
    assert out["spec"]["volumes"][0]["secret"]["secretName"] == "db"


# --------------------------------------------------------------------------
# Pod parsing
# --------------------------------------------------------------------------


def _crashloop_pod() -> dict:
    return {
        "metadata": {
            "name": "api-7d9f-abc",
            "namespace": "prod",
            "ownerReferences": [
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "api-7d9f",
                    "controller": True,
                }
            ],
        },
        "spec": {
            "nodeName": "node-1",
            "containers": [
                {
                    "name": "api",
                    "resources": {
                        "requests": {"memory": "256Mi", "cpu": "100m"},
                        "limits": {"memory": "512Mi"},
                    },
                    "env": [{"name": "SECRET", "value": "leak-me"}],
                }
            ],
        },
        "status": {
            "phase": "Running",
            "conditions": [
                {"type": "Ready", "status": "False", "reason": "ContainersNotReady"}
            ],
            "containerStatuses": [
                {
                    "name": "api",
                    "image": "ghcr.io/acme/api:1.2.3",
                    "ready": False,
                    "restartCount": 7,
                    "state": {"waiting": {"reason": "CrashLoopBackOff", "message": "back-off 5m"}},
                    "lastState": {
                        "terminated": {
                            "reason": "OOMKilled",
                            "exitCode": 137,
                            "signal": 9,
                            "finishedAt": "2026-08-29T10:00:00Z",
                        }
                    },
                }
            ],
        },
    }


def test_parse_pod_extracts_crashloop_exit_code_and_oomkilled_reason():
    pod = parse_pod(_crashloop_pod())
    assert pod.name == "api-7d9f-abc"
    assert pod.namespace == "prod"
    assert pod.node_name == "node-1"
    assert pod.phase == "Running"
    assert [c.type for c in pod.conditions] == ["Ready"]

    (container,) = pod.containers
    assert container.state == "waiting"
    assert container.state_reason == "CrashLoopBackOff"
    assert container.restart_count == 7
    assert container.last_state == "terminated"
    assert container.last_state_reason == "OOMKilled"
    assert container.last_state_exit_code == 137
    assert container.last_state_signal == 9
    assert container.requests == {"memory": "256Mi", "cpu": "100m"}
    assert container.limits == {"memory": "512Mi"}


def test_parse_pod_redacts_the_spec_it_carries():
    pod = parse_pod(_crashloop_pod(), include_spec=True)
    assert pod.spec is not None
    assert "leak-me" not in str(pod.spec)
    assert pod.spec["containers"][0]["env"][0]["name"] == "SECRET"


def test_parse_pod_omits_spec_when_not_requested():
    pod = parse_pod(_crashloop_pod(), include_spec=False)
    assert pod.spec is None
    assert pod.spec_truncated is False
    # The extracted facts are independent of the spec blob.
    assert pod.containers[0].limits == {"memory": "512Mi"}


def test_parse_pod_drops_oversized_spec_and_flags_it():
    obj = _crashloop_pod()
    obj["spec"]["containers"][0]["args"] = ["x" * 20_000]
    pod = parse_pod(obj, include_spec=True, max_spec_bytes=1000)
    assert pod.spec is None
    assert pod.spec_truncated is True
    assert pod.containers[0].last_state_exit_code == 137


def test_parse_pod_reports_containers_that_never_started():
    obj = _crashloop_pod()
    obj["spec"]["containers"].append(
        {"name": "sidecar", "resources": {"limits": {"memory": "64Mi"}}}
    )
    pod = parse_pod(obj)
    sidecar = next(c for c in pod.containers if c.name == "sidecar")
    assert sidecar.state is None
    assert sidecar.limits == {"memory": "64Mi"}


def test_parse_pod_tolerates_an_empty_object():
    pod = parse_pod({})
    assert pod.name == ""
    assert pod.containers == []
    assert pod.owner_chain == []


def test_parse_pod_marks_unfollowable_owner_kinds():
    obj = _crashloop_pod()
    obj["metadata"]["ownerReferences"] = [
        {"apiVersion": "acme.io/v1", "kind": "Widget", "name": "w1", "controller": True}
    ]
    pod = parse_pod(obj)
    assert pod.owner_chain[0].kind == "Widget"
    assert pod.owner_chain[0].resolved is False


# --------------------------------------------------------------------------
# Node / PVC / event parsing
# --------------------------------------------------------------------------


def test_parse_node_reads_conditions_and_unschedulable():
    node = parse_node(
        {
            "metadata": {"name": "node-1"},
            "spec": {"unschedulable": True},
            "status": {
                "conditions": [
                    {"type": "MemoryPressure", "status": "True", "reason": "KubeletHasInsufficientMemory"}
                ]
            },
        }
    )
    assert node.name == "node-1"
    assert node.unschedulable is True
    assert node.conditions[0].reason == "KubeletHasInsufficientMemory"


def test_parse_pvc_reads_phase_and_request():
    pvc = parse_pvc(
        {
            "metadata": {"name": "data", "namespace": "prod"},
            "spec": {
                "storageClassName": "gp3",
                "resources": {"requests": {"storage": "20Gi"}},
            },
            "status": {"phase": "Pending"},
        }
    )
    assert pvc.phase == "Pending"
    assert pvc.storage_class == "gp3"
    assert pvc.requested_storage == "20Gi"


def test_parse_events_sorts_newest_first_and_falls_back_for_timestamps():
    payload = {
        "items": [
            {
                "involvedObject": {"kind": "Pod", "name": "api-1"},
                "type": "Warning",
                "reason": "BackOff",
                "message": "Back-off restarting failed container",
                "count": 12,
                "lastTimestamp": "2026-08-29T10:00:00Z",
            },
            {
                # events.k8s.io writes eventTime and leaves lastTimestamp null
                "involvedObject": {"kind": "Pod", "name": "api-1"},
                "type": "Warning",
                "reason": "Unhealthy",
                "lastTimestamp": None,
                "eventTime": "2026-08-29T11:00:00.123456Z",
            },
        ]
    }
    events = parse_events(payload)
    assert [e.reason for e in events] == ["Unhealthy", "BackOff"]
    assert events[1].count == 12
    assert events[1].involved_object == "Pod/api-1"
    assert events[0].last_timestamp == datetime(
        2026, 8, 29, 11, 0, 0, 123456, tzinfo=timezone.utc
    )


def test_sort_events_places_undated_events_last():
    dated = ClusterEvent(
        involved_object="Pod/a", last_timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc)
    )
    undated = ClusterEvent(involved_object="Pod/b")
    assert sort_events([undated, dated]) == [dated, undated]


# --------------------------------------------------------------------------
# Transport
# --------------------------------------------------------------------------


def _client(handler, tmp_path=None, token: str | None = "tok") -> KubernetesClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    token_path = "/nonexistent/token"
    if token is not None and tmp_path is not None:
        p = tmp_path / "token"
        p.write_text(token + "\n")
        token_path = str(p)
    return KubernetesClient(
        "https://kubernetes.default.svc:443", client=http, token_path=token_path
    )


@pytest.mark.asyncio
async def test_get_pod_sends_bearer_token_from_the_token_file(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"metadata": {"name": "api-1"}})

    k8s = _client(handler, tmp_path)
    obj = await k8s.get_pod("prod", "api-1")
    assert obj == {"metadata": {"name": "api-1"}}
    assert seen["path"] == "/api/v1/namespaces/prod/pods/api-1"
    assert seen["auth"] == "Bearer tok"


@pytest.mark.asyncio
async def test_token_is_re_read_per_request_so_rotation_is_picked_up(tmp_path):
    """Projected service-account tokens are rotated by kubelet; a token cached
    at construction starts 401ing for the rest of the process's life."""
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={})

    k8s = _client(handler, tmp_path, token="first")
    await k8s.get_pod("prod", "api-1")
    (tmp_path / "token").write_text("second\n")
    await k8s.get_pod("prod", "api-1")
    assert seen == ["Bearer first", "Bearer second"]


@pytest.mark.asyncio
async def test_missing_token_file_still_issues_an_unauthenticated_request(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert "authorization" not in request.headers
        return httpx.Response(200, json={})

    k8s = _client(handler, tmp_path, token=None)
    assert await k8s.get_pod("prod", "api-1") == {}


@pytest.mark.asyncio
async def test_404_returns_none_rather_than_raising(tmp_path):
    k8s = _client(lambda r: httpx.Response(404, json={"message": "not found"}), tmp_path)
    assert await k8s.get_pod("prod", "gone") is None


@pytest.mark.asyncio
async def test_rbac_denial_raises_with_the_api_server_message(tmp_path):
    body = {
        "kind": "Status",
        "message": 'pods "api-1" is forbidden: User "system:serviceaccount:llopster:llopster" cannot get resource "pods"',
    }
    k8s = _client(lambda r: httpx.Response(403, json=body), tmp_path)
    with pytest.raises(KubernetesAPIError) as exc:
        await k8s.get_pod("prod", "api-1")
    assert "403" in str(exc.value)
    assert "is forbidden" in str(exc.value)


@pytest.mark.asyncio
async def test_transport_failure_raises_kubernetes_api_error(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    k8s = _client(handler, tmp_path)
    with pytest.raises(KubernetesAPIError):
        await k8s.get_pod("prod", "api-1")


@pytest.mark.asyncio
async def test_list_events_uses_a_field_selector_and_returns_parsed_events(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["selector"] = request.url.params["fieldSelector"]
        seen["limit"] = request.url.params["limit"]
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "involvedObject": {"kind": "Pod", "name": "api-1"},
                        "reason": "BackOff",
                        "lastTimestamp": "2026-08-29T10:00:00Z",
                    }
                ]
            },
        )

    k8s = _client(handler, tmp_path)
    events = await k8s.list_events("prod", "api-1", limit=5)
    assert seen["selector"] == "involvedObject.name=api-1"
    assert seen["limit"] == "5"
    assert events[0].reason == "BackOff"


@pytest.mark.asyncio
async def test_list_pods_passes_the_label_selector(tmp_path):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["selector"] = request.url.params["labelSelector"]
        return httpx.Response(200, json={"items": [{"metadata": {"name": "api-1"}}]})

    k8s = _client(handler, tmp_path)
    pods = await k8s.list_pods("prod", "app=api", limit=3)
    assert seen["selector"] == "app=api"
    assert pods[0]["metadata"]["name"] == "api-1"


# --------------------------------------------------------------------------
# Owner-chain resolution
# --------------------------------------------------------------------------


def _owned(kind: str, name: str, api_version: str = "apps/v1") -> dict:
    return {
        "metadata": {
            "ownerReferences": [
                {
                    "apiVersion": api_version,
                    "kind": kind,
                    "name": name,
                    "controller": True,
                }
            ]
        }
    }


@pytest.mark.asyncio
async def test_owner_chain_walks_pod_to_replicaset_to_deployment(tmp_path):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path.endswith("/replicasets/api-7d9f"):
            return httpx.Response(200, json=_owned("Deployment", "api"))
        if request.url.path.endswith("/deployments/api"):
            return httpx.Response(200, json={"metadata": {}})
        return httpx.Response(404, json={})

    k8s = _client(handler, tmp_path)
    chain = await k8s.resolve_owner_chain("prod", _owned("ReplicaSet", "api-7d9f"))
    assert [(o.kind, o.name) for o in chain] == [
        ("ReplicaSet", "api-7d9f"),
        ("Deployment", "api"),
    ]
    assert all(o.resolved for o in chain)
    assert paths == [
        "/apis/apps/v1/namespaces/prod/replicasets/api-7d9f",
        "/apis/apps/v1/namespaces/prod/deployments/api",
    ]


@pytest.mark.asyncio
async def test_owner_chain_stops_at_a_kind_outside_the_granted_rbac(tmp_path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, json={"metadata": {}})

    k8s = _client(handler, tmp_path)
    chain = await k8s.resolve_owner_chain(
        "prod", _owned("Widget", "w1", api_version="acme.io/v1")
    )
    assert [(o.kind, o.resolved) for o in chain] == [("Widget", False)]
    assert calls == []  # never asked for something we know we can't read


@pytest.mark.asyncio
async def test_owner_chain_survives_a_denied_parent_lookup(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"message": "forbidden"})

    k8s = _client(handler, tmp_path)
    chain = await k8s.resolve_owner_chain("prod", _owned("ReplicaSet", "api-7d9f"))
    # A partial chain beats no chain: the ReplicaSet name is still identity.
    assert [(o.kind, o.name, o.resolved) for o in chain] == [
        ("ReplicaSet", "api-7d9f", False)
    ]


@pytest.mark.asyncio
async def test_owner_chain_terminates_on_a_cycle(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/replicasets/a"):
            return httpx.Response(200, json=_owned("ReplicaSet", "b"))
        return httpx.Response(200, json=_owned("ReplicaSet", "a"))

    k8s = _client(handler, tmp_path)
    chain = await k8s.resolve_owner_chain("prod", _owned("ReplicaSet", "a"))
    assert [o.name for o in chain] == ["a", "b"]


@pytest.mark.asyncio
async def test_owner_chain_respects_max_depth(tmp_path):
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        return httpx.Response(200, json=_owned("ReplicaSet", f"rs-{counter['n']}"))

    k8s = _client(handler, tmp_path)
    chain = await k8s.resolve_owner_chain(
        "prod", _owned("ReplicaSet", "rs-0"), max_depth=2
    )
    assert len(chain) == 2


@pytest.mark.asyncio
async def test_owner_chain_prefers_the_controller_reference(tmp_path):
    """An object can carry several ownerReferences; only the controller one
    describes the workload chain."""
    obj = {
        "metadata": {
            "ownerReferences": [
                {"apiVersion": "apps/v1", "kind": "ReplicaSet", "name": "not-it"},
                {
                    "apiVersion": "apps/v1",
                    "kind": "ReplicaSet",
                    "name": "the-controller",
                    "controller": True,
                },
            ]
        }
    }
    k8s = _client(lambda r: httpx.Response(200, json={"metadata": {}}), tmp_path)
    chain = await k8s.resolve_owner_chain("prod", obj)
    assert [o.name for o in chain] == ["the-controller"]


def test_controller_ref_falls_back_to_the_first_reference():
    obj = {"metadata": {"ownerReferences": [{"kind": "Job", "name": "j1"}]}}
    assert _controller_ref(obj)["name"] == "j1"
    assert _controller_ref({"metadata": {}}) is None


def test_api_root_splits_core_group_from_the_rest():
    assert _api_root("v1") == "/api/v1"
    assert _api_root("apps/v1") == "/apis/apps/v1"
    assert _api_root("batch/v1") == "/apis/batch/v1"


def test_in_cluster_base_url_is_none_outside_a_pod(monkeypatch):
    monkeypatch.delenv("KUBERNETES_SERVICE_HOST", raising=False)
    assert in_cluster_base_url() is None


def test_in_cluster_base_url_brackets_ipv6_service_addresses(monkeypatch):
    monkeypatch.setenv("KUBERNETES_SERVICE_HOST", "fd00::1")
    monkeypatch.setenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
    assert in_cluster_base_url() == "https://[fd00::1]:443"

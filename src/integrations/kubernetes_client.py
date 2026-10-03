"""Read-only client for the Kubernetes API server.

Deliberately *not* backed by a Kubernetes SDK. Everything the agent needs is a
handful of authenticated GETs, so this follows the same shape as
``loki_client`` / ``prometheus_client``: a thin ``httpx`` wrapper plus pure
parse functions. Adding ``kubernetes_asyncio`` would pull a large dependency
tree into an image that already carries boto3 for the Bedrock provider.

Three properties this module exists to guarantee:

* **Read-only.** Only ``GET`` is ever issued. There is no write path to
  disable, so a misconfigured RoleBinding cannot become a mutation.
* **Redacted.** Anything returned here is destined for an LLM prompt *and* a
  run record on disk *and* the dashboard. PodSpecs carry ``env`` values
  inline, so :func:`redact_pod_spec` runs before a spec is ever handed out.
* **Never fatal.** Callers are expected to treat failures as missing context.
  The client raises :class:`KubernetesAPIError` for genuine transport/RBAC
  failures and returns ``None`` for a 404, so "the object is gone" and "we
  were denied" stay distinguishable.
"""

from __future__ import annotations

import copy
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("llopster.kubernetes")

# Standard in-cluster service-account projection. The token is a *projected*
# (bound) token on any modern cluster, which means kubelet rotates it roughly
# hourly — so it is re-read per request rather than cached at construction.
SERVICE_ACCOUNT_DIR = "/var/run/secrets/kubernetes.io/serviceaccount"
TOKEN_PATH = f"{SERVICE_ACCOUNT_DIR}/token"
CA_CERT_PATH = f"{SERVICE_ACCOUNT_DIR}/ca.crt"
NAMESPACE_PATH = f"{SERVICE_ACCOUNT_DIR}/namespace"

REDACTED = "[redacted]"

# Kinds we follow when walking ownerReferences. This is an allowlist, not a
# pluralization rule: it is exactly the set the chart's ClusterRole grants, so
# an owner outside it is *reported* (kind + name are useful on their own) but
# never fetched — no RBAC-denied noise for something we knowingly can't read.
OWNER_KINDS: dict[tuple[str, str], str] = {
    ("apps/v1", "ReplicaSet"): "replicasets",
    ("apps/v1", "Deployment"): "deployments",
    ("apps/v1", "StatefulSet"): "statefulsets",
    ("apps/v1", "DaemonSet"): "daemonsets",
    ("batch/v1", "Job"): "jobs",
    ("batch/v1", "CronJob"): "cronjobs",
}

GITOPS_LABEL_PREFIXES = ("helm.toolkit.fluxcd.io/", "kustomize.toolkit.fluxcd.io/")


def gitops_labels(obj: dict[str, Any]) -> dict[str, str]:
    """An object's Flux ownership labels, and nothing else from its metadata."""
    labels = (obj.get("metadata") or {}).get("labels") or {}
    return {
        k: str(v) for k, v in labels.items()
        if isinstance(k, str) and k.startswith(GITOPS_LABEL_PREFIXES)
    }


# pod -> ReplicaSet -> Deployment is 2 hops; CronJob -> Job -> Pod is 2. Four
# bounds any real chain and makes an ownerReference cycle terminate.
MAX_OWNER_DEPTH = 4


class KubernetesAPIError(Exception):
    """API server unreachable, TLS failure, or a non-404 HTTP status."""


# RFC 1123 subdomain: what Kubernetes itself enforces for namespaces, pods,
# nodes, PVCs and workloads. Every name reaching a URL path here comes from
# alert labels, and the webhook can be unauthenticated -- a value like
# "../../other-ns/pods/x" would otherwise be normalized by httpx into a
# request outside the namespace the scope check just approved, and "x?watch=1"
# would inject a query. Refusing anything that is not a legal object name
# closes both, leaving RBAC as the second boundary rather than the only one.
_OBJECT_NAME = re.compile(r"^[a-z0-9]([-a-z0-9.]{0,251}[a-z0-9])?$")


def _segment(name: str) -> str:
    """Validate an alert-supplied name before it becomes a path segment."""
    if not isinstance(name, str) or not _OBJECT_NAME.match(name) or ".." in name:
        raise KubernetesAPIError(f"refusing invalid Kubernetes object name {name!r}")
    return name


# --------------------------------------------------------------------------
# Parsed shapes
#
# These are the objects that reach the prompt, the run record and the
# dashboard. Everything here is a projection of an API object down to the
# fields that actually change a diagnosis -- not a faithful model of the
# resource.
# --------------------------------------------------------------------------


@dataclass
class OwnerRef:
    kind: str
    name: str
    # False when the reference was read off an object but the kind is outside
    # OWNER_KINDS, so the chain stops here. Recorded rather than dropped: the
    # name of an unreadable owner is still a diagnosis.
    resolved: bool = True
    # The owner's Flux ownership labels (helm.toolkit.fluxcd.io/*,
    # kustomize.toolkit.fluxcd.io/*), when it carries any. helm-controller
    # stamps them on the Deployment it renders, not on the pods, so this is
    # how a workload alert finds its HelmRelease. Filtered to those prefixes:
    # nothing else from the owner's metadata is kept.
    gitops_labels: dict[str, str] = field(default_factory=dict)


@dataclass
class PodCondition:
    type: str
    status: str
    reason: str | None = None
    message: str | None = None


@dataclass
class ContainerState:
    name: str
    image: str | None = None
    ready: bool = False
    restart_count: int = 0
    # Current state: "running" | "waiting" | "terminated" (None if unreported).
    state: str | None = None
    state_reason: str | None = None
    state_message: str | None = None
    # Previous state -- for a CrashLoopBackOff that produces no logs at all,
    # `last_state_exit_code` / `last_state_reason` (e.g. OOMKilled) are the
    # only signal that exists anywhere.
    last_state: str | None = None
    last_state_reason: str | None = None
    last_state_exit_code: int | None = None
    last_state_signal: int | None = None
    last_state_finished_at: str | None = None
    requests: dict[str, str] = field(default_factory=dict)
    limits: dict[str, str] = field(default_factory=dict)


@dataclass
class PodState:
    name: str
    namespace: str
    phase: str | None = None
    node_name: str | None = None
    conditions: list[PodCondition] = field(default_factory=list)
    containers: list[ContainerState] = field(default_factory=list)
    owner_chain: list[OwnerRef] = field(default_factory=list)
    # Redacted pod spec, present only when the caller asked for it and it fit
    # under the size ceiling. `spec_truncated` distinguishes "not requested"
    # from "too large to send", which matters when reading a run record.
    spec: dict[str, Any] | None = None
    spec_truncated: bool = False


@dataclass
class NodeState:
    name: str
    conditions: list[PodCondition] = field(default_factory=list)
    unschedulable: bool = False


@dataclass
class PVCState:
    name: str
    namespace: str
    phase: str | None = None
    storage_class: str | None = None
    requested_storage: str | None = None
    volume_name: str | None = None


@dataclass
class ClusterEvent:
    involved_object: str
    type: str | None = None
    reason: str | None = None
    message: str | None = None
    count: int = 1
    last_timestamp: datetime | None = None


# --------------------------------------------------------------------------
# Redaction
#
# This is the path where a plaintext credential sitting in someone's PodSpec
# would otherwise be shipped to an LLM provider and then written to disk in a
# run record. Treat it as a security boundary, not hygiene.
# --------------------------------------------------------------------------

_CONTAINER_LISTS = ("containers", "initContainers", "ephemeralContainers")

# Holds a serialized copy of the entire object, env values included, which is
# exactly how a redacted spec leaks anyway if metadata ever gets included.
_LAST_APPLIED = "kubectl.kubernetes.io/last-applied-configuration"


def redact_pod_spec(spec: dict[str, Any]) -> dict[str, Any]:
    """Strip credential-bearing values out of a pod spec.

    Keys survive, values do not: knowing a container reads ``DATABASE_URL`` is
    diagnostic, knowing its value is a breach. ``valueFrom`` and ``envFrom``
    entries are references (a ConfigMap/Secret *name*), not values, so they are
    kept -- "this env var comes from Secret X" is often the whole answer to a
    ``CreateContainerConfigError``.
    """
    out = copy.deepcopy(spec)
    for key in _CONTAINER_LISTS:
        containers = out.get(key)
        if not isinstance(containers, list):
            continue
        for container in containers:
            if isinstance(container, dict):
                _redact_env(container)
                _redact_inline_values(container)
    return redact_object(out)


def _redact_env(container: dict[str, Any]) -> None:
    env = container.get("env")
    if not isinstance(env, list):
        return
    for entry in env:
        if isinstance(entry, dict) and "value" in entry:
            entry["value"] = REDACTED


# Container fields that carry inline literals with no key/value split to hide
# behind. `--db-password=hunter2` and `mysql -phunter2` are common enough that
# argv is treated as a value, not a key: element count survives (the shape is
# still visible) but every element is blanked. Covers probe/lifecycle
# `exec.command` too, since the walk below recurses.
_ARGV_KEYS = ("command", "args")


def _redact_inline_values(node: Any) -> None:
    """Blank argv lists and HTTP header values anywhere inside a container.

    Probe and lifecycle ``httpGet.httpHeaders`` entries are ``{name, value}``
    pairs, and ``Authorization: Bearer ...`` on a liveness probe is exactly the
    kind of literal this boundary exists for. Header *names* are kept.
    """
    if isinstance(node, dict):
        for key, value in node.items():
            if key in _ARGV_KEYS and isinstance(value, list):
                node[key] = [REDACTED for _ in value]
            elif key == "httpHeaders" and isinstance(value, list):
                for header in value:
                    if isinstance(header, dict) and "value" in header:
                        header["value"] = REDACTED
            elif key != "env":
                _redact_inline_values(value)
    elif isinstance(node, list):
        for item in node:
            _redact_inline_values(item)


def redact_object(node: Any) -> Any:
    """Recursively blank inline secret material anywhere in an API object.

    Applied to everything on its way out of this module, not just pod specs:
    ``data`` / ``stringData`` show up on ConfigMaps and Secrets, and the
    last-applied-configuration annotation is a full serialized copy of an
    object (env values and all) hiding in a string.
    """
    if isinstance(node, dict):
        out: dict[str, Any] = {}
        for key, value in node.items():
            if key in ("data", "stringData") and isinstance(value, dict):
                out[key] = {k: REDACTED for k in value}
            elif key == "annotations" and isinstance(value, dict):
                out[key] = {
                    k: (REDACTED if k == _LAST_APPLIED else v) for k, v in value.items()
                }
            else:
                out[key] = redact_object(value)
        return out
    if isinstance(node, list):
        return [redact_object(item) for item in node]
    return node


# --------------------------------------------------------------------------
# Parsers
# --------------------------------------------------------------------------


def parse_pod(
    obj: dict[str, Any],
    *,
    include_spec: bool = True,
    max_spec_bytes: int = 8000,
) -> PodState:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}

    pod = PodState(
        name=meta.get("name") or "",
        namespace=meta.get("namespace") or "",
        phase=status.get("phase"),
        node_name=spec.get("nodeName"),
        conditions=_parse_conditions(status.get("conditions")),
        owner_chain=[_parse_owner_ref(r) for r in (meta.get("ownerReferences") or [])],
    )

    resources = _resources_by_container(spec)
    seen: set[str] = set()
    for raw in _container_statuses(status):
        cs = _parse_container_status(raw, resources)
        seen.add(cs.name)
        pod.containers.append(cs)
    # A container that never started has no status entry at all; its
    # configured limits are still the point of a resource alert.
    for name, (requests, limits) in resources.items():
        if name not in seen:
            pod.containers.append(
                ContainerState(name=name, requests=requests, limits=limits)
            )

    if include_spec and spec:
        redacted = redact_pod_spec(spec)
        if len(json.dumps(redacted, default=str)) <= max_spec_bytes:
            pod.spec = redacted
        else:
            # Drop the blob rather than truncate it into invalid JSON. The
            # facts that matter (states, exit codes, resources) are already
            # extracted above, and this feeds a metered synthesis prompt.
            pod.spec_truncated = True
    return pod


def _container_statuses(status: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for key in ("initContainerStatuses", "containerStatuses", "ephemeralContainerStatuses"):
        value = status.get(key)
        if isinstance(value, list):
            out.extend(s for s in value if isinstance(s, dict))
    return out


def _resources_by_container(spec: dict[str, Any]) -> dict[str, tuple[dict, dict]]:
    out: dict[str, tuple[dict, dict]] = {}
    for key in _CONTAINER_LISTS:
        for container in spec.get(key) or []:
            if not isinstance(container, dict):
                continue
            name = container.get("name")
            if not name:
                continue
            resources = container.get("resources") or {}
            out[name] = (
                dict(resources.get("requests") or {}),
                dict(resources.get("limits") or {}),
            )
    return out


def _parse_container_status(
    raw: dict[str, Any], resources: dict[str, tuple[dict, dict]]
) -> ContainerState:
    name = raw.get("name") or ""
    requests, limits = resources.get(name, ({}, {}))
    cs = ContainerState(
        name=name,
        image=raw.get("image"),
        ready=bool(raw.get("ready")),
        restart_count=int(raw.get("restartCount") or 0),
        requests=requests,
        limits=limits,
    )

    state_kind, state_body = _first_state(raw.get("state"))
    cs.state = state_kind
    if state_body:
        cs.state_reason = state_body.get("reason")
        cs.state_message = state_body.get("message")

    last_kind, last_body = _first_state(raw.get("lastState"))
    cs.last_state = last_kind
    if last_body:
        cs.last_state_reason = last_body.get("reason")
        exit_code = last_body.get("exitCode")
        cs.last_state_exit_code = int(exit_code) if exit_code is not None else None
        signal = last_body.get("signal")
        cs.last_state_signal = int(signal) if signal is not None else None
        cs.last_state_finished_at = last_body.get("finishedAt")
    return cs


def _first_state(state: Any) -> tuple[str | None, dict[str, Any] | None]:
    """A ContainerState is a union rendered as a one-key map."""
    if not isinstance(state, dict):
        return None, None
    for kind in ("waiting", "terminated", "running"):
        body = state.get(kind)
        if isinstance(body, dict):
            return kind, body
    return None, None


def _parse_conditions(raw: Any) -> list[PodCondition]:
    out: list[PodCondition] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        out.append(
            PodCondition(
                type=entry.get("type") or "",
                status=entry.get("status") or "",
                reason=entry.get("reason"),
                message=entry.get("message"),
            )
        )
    return out


def _parse_owner_ref(raw: dict[str, Any]) -> OwnerRef:
    kind = raw.get("kind") or ""
    api_version = raw.get("apiVersion") or ""
    return OwnerRef(
        kind=kind,
        name=raw.get("name") or "",
        resolved=(api_version, kind) in OWNER_KINDS,
    )


def parse_node(obj: dict[str, Any]) -> NodeState:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    return NodeState(
        name=meta.get("name") or "",
        conditions=_parse_conditions(status.get("conditions")),
        unschedulable=bool(spec.get("unschedulable")),
    )


def parse_pvc(obj: dict[str, Any]) -> PVCState:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    requested = ((spec.get("resources") or {}).get("requests") or {}).get("storage")
    return PVCState(
        name=meta.get("name") or "",
        namespace=meta.get("namespace") or "",
        phase=status.get("phase"),
        storage_class=spec.get("storageClassName"),
        requested_storage=requested,
        volume_name=spec.get("volumeName"),
    )


def parse_events(payload: dict[str, Any]) -> list[ClusterEvent]:
    """Parse an EventList. Newest first; undated events sort last."""
    out: list[ClusterEvent] = []
    for item in payload.get("items") or []:
        if not isinstance(item, dict):
            continue
        involved = item.get("involvedObject") or {}
        out.append(
            ClusterEvent(
                involved_object=f"{involved.get('kind', '?')}/{involved.get('name', '?')}",
                type=item.get("type"),
                reason=item.get("reason"),
                message=item.get("message"),
                count=int(item.get("count") or 1),
                last_timestamp=_event_timestamp(item),
            )
        )
    return sort_events(out)


def sort_events(events: list[ClusterEvent]) -> list[ClusterEvent]:
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    return sorted(events, key=lambda e: e.last_timestamp or epoch, reverse=True)


def _event_timestamp(item: dict[str, Any]) -> datetime | None:
    """`lastTimestamp` is empty on events written through the events.k8s.io
    path, which is most of them on a modern cluster -- fall back rather than
    render every event as undated."""
    series = item.get("series") or {}
    for raw in (
        item.get("lastTimestamp"),
        series.get("lastObservedTime"),
        item.get("eventTime"),
        item.get("firstTimestamp"),
    ):
        parsed = _parse_ts(raw)
        if parsed is not None:
            return parsed
    return None


def _parse_ts(raw: Any) -> datetime | None:
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # RFC3339; microsecond-precision variants appear on eventTime.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def in_cluster_base_url() -> str | None:
    """The API server URL from the ambient in-cluster environment, or None
    when the process is not running in a pod."""
    import os

    host = os.getenv("KUBERNETES_SERVICE_HOST")
    port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS") or os.getenv(
        "KUBERNETES_SERVICE_PORT", "443"
    )
    if not host:
        return None
    # IPv6 service IPs must be bracketed in a URL.
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    return f"https://{host}:{port}"


class KubernetesClient:
    """Authenticated read-only GETs against the API server.

    ``client`` follows the loki/prometheus convention (inject one for tests,
    otherwise a short-lived one is built per call) with one difference: the
    API server presents a cert from the cluster CA, so a client built here is
    given ``verify=ca_cert_path``.
    """

    def __init__(
        self,
        base_url: str,
        *,
        token_path: str = TOKEN_PATH,
        ca_cert_path: str | None = CA_CERT_PATH,
        client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.token_path = token_path
        self.ca_cert_path = ca_cert_path
        self._client = client
        self.timeout = timeout

    # -- transport ---------------------------------------------------------

    def _token(self) -> str | None:
        """Read the service-account token fresh on every call.

        Projected tokens are rotated by kubelet well inside the lifetime of a
        long-running pod, so a token cached at construction eventually starts
        returning 401 for the rest of the process's life.
        """
        try:
            return Path(self.token_path).read_text().strip()
        except OSError:
            return None

    async def get_json(
        self, path: str, params: dict[str, str] | None = None
    ) -> dict[str, Any] | None:
        """GET a resource. ``None`` for 404 (object genuinely absent);
        :class:`KubernetesAPIError` for anything else, RBAC denials included."""
        headers = {"Accept": "application/json"}
        token = self._token()
        if token:
            headers["Authorization"] = f"Bearer {token}"

        client = self._client
        owned = client is None
        if owned:
            verify: Any = True
            if self.ca_cert_path and Path(self.ca_cert_path).exists():
                verify = self.ca_cert_path
            client = httpx.AsyncClient(timeout=self.timeout, verify=verify)
        try:
            resp = await client.get(
                f"{self.base_url}{path}", params=params, headers=headers
            )
            if resp.status_code == 404:
                return None
            if resp.status_code >= 400:
                raise KubernetesAPIError(
                    f"GET {path} -> {resp.status_code} {_api_message(resp)}"
                )
            return resp.json()
        except httpx.HTTPError as e:
            raise KubernetesAPIError(f"GET {path} failed: {e}") from e
        finally:
            if owned:
                await client.aclose()

    # -- resources ---------------------------------------------------------

    async def get_pod(self, namespace: str, name: str) -> dict[str, Any] | None:
        return await self.get_json(
            f"/api/v1/namespaces/{_segment(namespace)}/pods/{_segment(name)}"
        )

    async def list_pods(
        self, namespace: str, label_selector: str, limit: int = 10
    ) -> list[dict[str, Any]]:
        payload = await self.get_json(
            f"/api/v1/namespaces/{_segment(namespace)}/pods",
            params={"labelSelector": label_selector, "limit": str(limit)},
        )
        return list((payload or {}).get("items") or [])

    async def get_namespaced(
        self, api_version: str, plural: str, namespace: str, name: str
    ) -> dict[str, Any] | None:
        return await self.get_json(
            f"{_api_root(api_version)}/namespaces/{_segment(namespace)}"
            f"/{plural}/{_segment(name)}"
        )

    async def get_node(self, name: str) -> dict[str, Any] | None:
        return await self.get_json(f"/api/v1/nodes/{_segment(name)}")

    async def get_pvc(self, namespace: str, name: str) -> dict[str, Any] | None:
        return await self.get_json(
            f"/api/v1/namespaces/{_segment(namespace)}"
            f"/persistentvolumeclaims/{_segment(name)}"
        )

    async def list_events(
        self, namespace: str, involved_object_name: str, limit: int = 50
    ) -> list[ClusterEvent]:
        payload = await self.get_json(
            f"/api/v1/namespaces/{_segment(namespace)}/events",
            params={
                "fieldSelector": f"involvedObject.name={_segment(involved_object_name)}",
                "limit": str(limit),
            },
        )
        return parse_events(payload or {})

    async def resolve_owner_chain(
        self, namespace: str, obj: dict[str, Any], max_depth: int = MAX_OWNER_DEPTH
    ) -> list[OwnerRef]:
        """Walk ownerReferences upward: pod -> ReplicaSet -> Deployment.

        Only the *controller* reference is followed -- an object can carry
        several ownerReferences and only one of them describes the workload
        chain. Kubernetes does not guarantee acyclicity here, so `seen` plus
        `max_depth` bound the walk regardless of what the cluster reports.
        """
        chain: list[OwnerRef] = []
        seen: set[tuple[str, str]] = set()
        current = obj
        for _ in range(max_depth):
            raw = _controller_ref(current)
            if raw is None:
                break
            ref = _parse_owner_ref(raw)
            key = (ref.kind, ref.name)
            if key in seen:
                break
            seen.add(key)
            chain.append(ref)
            if not ref.resolved:
                break
            plural = OWNER_KINDS[(raw.get("apiVersion") or "", ref.kind)]
            try:
                parent = await self.get_namespaced(
                    raw.get("apiVersion") or "", plural, namespace, ref.name
                )
            except KubernetesAPIError as e:
                # A partial chain beats no chain: the names collected so far
                # are still the workload identity the prompt needs.
                log.warning("owner lookup failed for %s/%s: %s", ref.kind, ref.name, e)
                ref.resolved = False
                break
            if parent is None:
                ref.resolved = False
                break
            ref.gitops_labels = gitops_labels(parent)
            current = parent
        return chain


def _controller_ref(obj: dict[str, Any]) -> dict[str, Any] | None:
    refs = (obj.get("metadata") or {}).get("ownerReferences") or []
    fallback: dict[str, Any] | None = None
    for raw in refs:
        if not isinstance(raw, dict):
            continue
        if raw.get("controller"):
            return raw
        if fallback is None:
            fallback = raw
    return fallback


def _api_root(api_version: str) -> str:
    """Core group ("v1") lives under /api; everything else under /apis."""
    return "/api/v1" if api_version == "v1" else f"/apis/{api_version}"


def _api_message(resp: httpx.Response) -> str:
    """Pull the Status.message out of an API error body -- an RBAC denial says
    exactly which verb on which resource was refused, which is the entire
    diagnosis when an operator scoped the Role too tightly."""
    try:
        body = resp.json()
    except ValueError:
        return resp.text[:200]
    if isinstance(body, dict) and body.get("message"):
        return str(body["message"])[:300]
    return resp.text[:200]

"""Turn a firing alert into the cluster objects that explain it.

The seam mirrors logs and metrics: :mod:`src.integrations.kubernetes_client`
speaks the protocol, this module decides *what to ask for* — the same split as
``loki_client`` vs. ``ContextCollector._build_logql``, just with more selection
logic because "the object the alert is about" is not one query.

Everything here is best-effort. A denied read, a missing object or an
unreachable API server degrades to a note or an error string on the returned
:class:`ClusterState`; the caller still gets its logs and metrics.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from src.agent.alert_handler import ParsedAlert
from src.integrations.kubernetes_client import (
    ClusterEvent,
    KubernetesAPIError,
    KubernetesClient,
    NodeState,
    PodState,
    PVCState,
    parse_node,
    parse_pod,
    parse_pvc,
    sort_events,
)

log = logging.getLogger("llopster.cluster")

# Alert labels naming the namespace, in priority order. `exported_namespace`
# appears when a scrape relabeling collision renames the original.
NAMESPACE_LABELS = ("namespace", "exported_namespace")

# Workload labels kube-state-metrics puts on the alerts that need this most,
# mapped to the API group/plural needed to fetch the object. Used only when
# the alert carries no `pod` label — then the workload's own selector is what
# finds the pods.
WORKLOAD_LABELS: dict[str, tuple[str, str, str]] = {
    # label            -> (kind, apiVersion, plural)
    "deployment": ("Deployment", "apps/v1", "deployments"),
    "statefulset": ("StatefulSet", "apps/v1", "statefulsets"),
    "daemonset": ("DaemonSet", "apps/v1", "daemonsets"),
    "job_name": ("Job", "batch/v1", "jobs"),
    "job": ("Job", "batch/v1", "jobs"),
}

# Objects whose events are worth a round trip. Admission rejections
# (Gatekeeper, validating webhooks) never reach the workload's logs and land
# on the owning ReplicaSet, so the owner chain is included — but bounded,
# since each entry is one more API call per alert.
MAX_EVENT_SOURCES = 4


@dataclass
class ClusterState:
    """The `## Cluster state` payload: what was found, and what wasn't."""

    namespace: str | None = None
    pods: list[PodState] = field(default_factory=list)
    events: list[ClusterEvent] = field(default_factory=list)
    nodes: list[NodeState] = field(default_factory=list)
    pvcs: list[PVCState] = field(default_factory=list)
    # Objects actually fetched, e.g. ["Pod/api-1", "Deployment/api"]. Recorded
    # so a run record answers "what did the agent look at" without inference.
    objects_queried: list[str] = field(default_factory=list)
    # Why something is absent: out-of-scope namespace, a cap that fired, an
    # alert with no object labels. Rendered into the prompt so the model reads
    # "not collected" rather than treating silence as "nothing wrong".
    notes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        return not (self.pods or self.events or self.nodes or self.pvcs)


class ClusterStateCollector:
    def __init__(
        self,
        client: KubernetesClient,
        *,
        namespaces: tuple[str, ...] = (),
        all_namespaces: bool = False,
        max_events: int = 20,
        include_pod_spec: bool = True,
        max_pod_spec_bytes: int = 8000,
        max_pods: int = 3,
    ):
        self.client = client
        self.namespaces = tuple(namespaces)
        self.all_namespaces = all_namespaces
        self.max_events = max_events
        self.include_pod_spec = include_pod_spec
        self.max_pod_spec_bytes = max_pod_spec_bytes
        self.max_pods = max_pods

    # `all_namespaces` does double duty on purpose: it is the single bit that
    # says whether the chart bound a ClusterRoleBinding. Nodes are
    # cluster-scoped, so a per-namespace RoleBinding cannot grant `get nodes`
    # no matter which namespaces it lists — attempting one would be a
    # guaranteed 403 on every node-scoped alert.
    @property
    def cluster_scoped_reads(self) -> bool:
        return self.all_namespaces

    async def collect(self, alert: ParsedAlert) -> ClusterState:
        state = ClusterState()
        namespace = _first_label(alert, NAMESPACE_LABELS)

        if namespace is None:
            # A node-scoped alert (KubeNodeNotReady and friends) carries no
            # namespace at all, but the node itself is still readable.
            if alert.labels.get("node"):
                await self._fetch_nodes(alert, state)
            else:
                state.notes.append(
                    "alert carries no namespace label; no cluster objects to look up"
                )
            return state

        if not self._in_scope(namespace):
            state.notes.append(
                f"namespace {namespace!r} is outside the namespaces this agent "
                "is permitted to read; no cluster objects collected"
            )
            return state

        state.namespace = namespace
        pod_objects = await self._fetch_pods(alert, namespace, state)
        await self._fetch_events(namespace, state, pod_objects)
        await self._fetch_pvc(alert, namespace, state)
        await self._fetch_nodes(alert, state)
        return state

    # -- scope -------------------------------------------------------------

    def _in_scope(self, namespace: str) -> bool:
        return self.all_namespaces or namespace in self.namespaces

    # -- pods --------------------------------------------------------------

    async def _fetch_pods(
        self, alert: ParsedAlert, namespace: str, state: ClusterState
    ) -> list[dict]:
        pod_name = alert.labels.get("pod")
        raw_pods: list[dict] = []

        if pod_name:
            try:
                obj = await self.client.get_pod(namespace, pod_name)
            except KubernetesAPIError as e:
                state.errors.append(f"pod lookup failed: {e}")
                log.warning("pod lookup failed for %s/%s: %s", namespace, pod_name, e)
                return []
            if obj is None:
                # Routine, not an error: a CrashLoopBackOff pod can be replaced
                # between the alert firing and the agent reaching this line.
                state.notes.append(
                    f"pod {namespace}/{pod_name} no longer exists (replaced or deleted)"
                )
                return []
            raw_pods = [obj]
        else:
            raw_pods = await self._fetch_pods_via_workload(alert, namespace, state)

        for obj in raw_pods:
            pod = parse_pod(
                obj,
                include_spec=self.include_pod_spec,
                max_spec_bytes=self.max_pod_spec_bytes,
            )
            state.objects_queried.append(f"Pod/{pod.name}")
            if pod.spec_truncated:
                state.notes.append(
                    f"pod spec for {pod.name} exceeded the size ceiling and was omitted; "
                    "container states, restart counts and resources are still below"
                )
            try:
                pod.owner_chain = await self.client.resolve_owner_chain(namespace, obj)
            except KubernetesAPIError as e:  # pragma: no cover - defensive
                state.errors.append(f"owner chain lookup failed: {e}")
            state.pods.append(pod)
        return raw_pods

    async def _fetch_pods_via_workload(
        self, alert: ParsedAlert, namespace: str, state: ClusterState
    ) -> list[dict]:
        """No `pod` label: find the workload, then use its own selector.

        Two calls instead of listing the namespace and guessing, and it is
        exact — the workload's `spec.selector.matchLabels` is by definition
        what its pods carry.
        """
        for label, (kind, api_version, plural) in WORKLOAD_LABELS.items():
            name = alert.labels.get(label)
            if not name:
                continue
            try:
                workload = await self.client.get_namespaced(
                    api_version, plural, namespace, name
                )
            except KubernetesAPIError as e:
                state.errors.append(f"{kind.lower()} lookup failed: {e}")
                log.warning("%s lookup failed for %s/%s: %s", kind, namespace, name, e)
                return []
            if workload is None:
                state.notes.append(f"{kind} {namespace}/{name} no longer exists")
                return []
            state.objects_queried.append(f"{kind}/{name}")

            selector = _match_labels(workload)
            if not selector:
                state.notes.append(
                    f"{kind} {name} has no matchLabels selector; pods not resolved"
                )
                return []
            try:
                return await self.client.list_pods(
                    namespace, selector, limit=self.max_pods
                )
            except KubernetesAPIError as e:
                state.errors.append(f"pod list failed: {e}")
                log.warning("pod list failed for %s/%s: %s", namespace, selector, e)
                return []

        state.notes.append(
            "alert names no pod or workload; only namespace-level context collected"
        )
        return []

    # -- events ------------------------------------------------------------

    async def _fetch_events(
        self, namespace: str, state: ClusterState, raw_pods: list[dict]
    ) -> None:
        sources = _event_sources(state, raw_pods)[:MAX_EVENT_SOURCES]
        collected: list[ClusterEvent] = []
        for name in sources:
            try:
                collected.extend(
                    await self.client.list_events(
                        namespace, name, limit=self.max_events
                    )
                )
            except KubernetesAPIError as e:
                state.errors.append(f"event lookup failed for {name}: {e}")
                log.warning("event lookup failed for %s/%s: %s", namespace, name, e)

        ordered = sort_events(collected)
        if len(ordered) > self.max_events:
            state.notes.append(
                f"{len(ordered)} events matched; showing the {self.max_events} most recent"
            )
        state.events = ordered[: self.max_events]

    # -- pvc / nodes -------------------------------------------------------

    async def _fetch_pvc(
        self, alert: ParsedAlert, namespace: str, state: ClusterState
    ) -> None:
        """A pending PVC's answer is in its events (`FailedBinding`, and why),
        which no workload's logs ever carry."""
        name = alert.labels.get("persistentvolumeclaim")
        if not name:
            return
        try:
            obj = await self.client.get_pvc(namespace, name)
        except KubernetesAPIError as e:
            state.errors.append(f"pvc lookup failed: {e}")
            log.warning("pvc lookup failed for %s/%s: %s", namespace, name, e)
            return
        if obj is None:
            state.notes.append(f"PersistentVolumeClaim {namespace}/{name} not found")
            return
        state.pvcs.append(parse_pvc(obj))
        state.objects_queried.append(f"PersistentVolumeClaim/{name}")
        try:
            extra = await self.client.list_events(namespace, name, limit=self.max_events)
        except KubernetesAPIError as e:
            state.errors.append(f"event lookup failed for {name}: {e}")
            return
        state.events = sort_events(state.events + extra)[: self.max_events]

    async def _fetch_nodes(self, alert: ParsedAlert, state: ClusterState) -> None:
        name = alert.labels.get("node") or next(
            (p.node_name for p in state.pods if p.node_name), None
        )
        if not name:
            return
        if not self.cluster_scoped_reads:
            state.notes.append(
                f"node {name} not read: nodes are cluster-scoped and this agent is "
                "bound to individual namespaces"
            )
            return
        try:
            obj = await self.client.get_node(name)
        except KubernetesAPIError as e:
            state.errors.append(f"node lookup failed: {e}")
            log.warning("node lookup failed for %s: %s", name, e)
            return
        if obj is None:
            state.notes.append(f"node {name} not found")
            return
        state.nodes.append(parse_node(obj))
        state.objects_queried.append(f"Node/{name}")


def _first_label(alert: ParsedAlert, names: tuple[str, ...]) -> str | None:
    for name in names:
        value = alert.labels.get(name)
        if value:
            return value
    return None


def _match_labels(workload: dict) -> str | None:
    match = ((workload.get("spec") or {}).get("selector") or {}).get("matchLabels")
    if not isinstance(match, dict) or not match:
        return None
    return ",".join(f"{k}={v}" for k, v in sorted(match.items()))


def _event_sources(state: ClusterState, raw_pods: list[dict]) -> list[str]:
    """Pod names first, then their owners — deduplicated, order preserved.

    Owners matter because a pod that was never admitted has no events of its
    own; the rejection is recorded against the ReplicaSet trying to create it.
    """
    names: list[str] = []
    for pod in state.pods:
        if pod.name and pod.name not in names:
            names.append(pod.name)
        for owner in pod.owner_chain:
            if owner.name and owner.name not in names:
                names.append(owner.name)
    if not names:
        # Workload-only path: no pods came back, but the workload object we did
        # fetch still has events.
        for entry in state.objects_queried:
            _, _, name = entry.partition("/")
            if name and name not in names:
                names.append(name)
    return names

"""Read-only access to Flux objects (issue #24 part A).

Flux objects are ordinary custom resources, so this rides on
:class:`~src.integrations.kubernetes_client.KubernetesClient` -- same token,
same CA, same GET-only transport, same name validation. What it adds is the
Flux-specific part:

* **Version discovery.** HelmRelease is ``v2`` on current Flux and
  ``v2beta2`` on older installs; OCIRepository moved from ``v1beta2`` to
  ``v1``. Hard-coding either breaks half the clusters, so the served version
  is asked of the API server (``GET /apis/<group>``). A 404 there is also the
  clean answer to "is Flux installed at all", which the caller reports as a
  note rather than an error.
* **Projection, never pass-through.** Every object is reduced to the fields
  that carry a diagnosis. Nothing returned here holds a raw ``spec``: a
  HelmRelease's ``spec.values`` and a Kustomization's
  ``spec.postBuild.substitute`` are inline configuration where credentials
  routinely live, and the only way to guarantee they never reach a prompt, a
  run record or the dashboard is to never copy them out. References
  (``valuesFrom``, ``substituteFrom``) are kept as names -- "values come from
  Secret X" is diagnostic; X's contents are not, and are never read.

Status fields follow the current APIs: HelmRelease v2 has no
``lastAppliedRevision`` -- what is deployed is ``status.history`` (newest
first) -- while Kustomization v1 still has both applied and attempted
revisions. Drift detection has no status field at all; it is a
``spec.driftDetection.mode`` setting whose findings arrive as events.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

from src.agent.alert_handler import strip_url_credentials
from src.integrations.kubernetes_client import KubernetesClient, PodCondition

log = logging.getLogger("llopster.flux")

# kind -> (API group, plural). Exactly the set the chart's Flux ClusterRole
# grants; a kind outside it is never requested.
FLUX_KINDS: dict[str, tuple[str, str]] = {
    "HelmRelease": ("helm.toolkit.fluxcd.io", "helmreleases"),
    "Kustomization": ("kustomize.toolkit.fluxcd.io", "kustomizations"),
    "GitRepository": ("source.toolkit.fluxcd.io", "gitrepositories"),
    "OCIRepository": ("source.toolkit.fluxcd.io", "ocirepositories"),
    "HelmRepository": ("source.toolkit.fluxcd.io", "helmrepositories"),
    "HelmChart": ("source.toolkit.fluxcd.io", "helmcharts"),
    "Bucket": ("source.toolkit.fluxcd.io", "buckets"),
}

# Condition messages are written by the controller and are the diagnosis
# ("upgrade retries exhausted", "chart pull error: 403"), so they are kept in
# full -- up to a ceiling, because a Helm template error can quote a large
# rendered manifest and this feeds a metered prompt.
MAX_CONDITION_MESSAGE = 2000

# How many release snapshots to keep. Three is enough to read "worked until
# this version" without shipping the whole history.
MAX_RELEASES = 3

# Discovery results are stable for the life of a Flux install; re-asked
# periodically so installing Flux after the agent starts is picked up.
DISCOVERY_TTL_SECONDS = 600.0

# An API version taken from alert labels is only trusted if it looks like
# one; otherwise it would become a path segment chosen by whoever can post to
# the webhook.
_API_VERSION = re.compile(r"^v\d+((alpha|beta)\d+)?$")


class FluxNotInstalled(Exception):
    """The API server does not serve this Flux API group."""

    def __init__(self, group: str):
        super().__init__(f"API group {group} is not served (Flux not installed?)")
        self.group = group


@dataclass(frozen=True)
class FluxRef:
    kind: str
    namespace: str
    name: str

    def __str__(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"


@dataclass
class FluxRelease:
    """One HelmRelease history snapshot (status.history[n])."""

    chart_version: str | None = None
    app_version: str | None = None
    status: str | None = None       # deployed | failed | superseded | ...
    last_deployed: str | None = None


@dataclass
class FluxObjectState:
    kind: str
    name: str
    namespace: str
    api_version: str
    suspended: bool = False
    conditions: list[PodCondition] = field(default_factory=list)
    # What is actually running vs. what the controller last tried. Equal
    # means healthy or never retried; different means "worked until the
    # attempted revision" -- the distinction #24 asks for.
    applied_revision: str | None = None
    attempted_revision: str | None = None
    # Where this object's content comes from, for walking to its source.
    source: FluxRef | None = None
    # Human-readable facts that differ by kind (chart, url, ref, path, ...).
    facts: dict[str, str] = field(default_factory=dict)
    releases: list[FluxRelease] = field(default_factory=list)
    # valuesFrom / substituteFrom references -- names only.
    value_refs: list[str] = field(default_factory=list)

    @property
    def ref(self) -> FluxRef:
        return FluxRef(self.kind, self.namespace, self.name)

    @property
    def ready(self) -> bool | None:
        for c in self.conditions:
            if c.type == "Ready":
                return {"True": True, "False": False}.get(c.status)
        return None


# --------------------------------------------------------------------------
# Parsers -- pure, so the projection rules are testable without a server
# --------------------------------------------------------------------------


def parse_flux_object(kind: str, obj: dict[str, Any]) -> FluxObjectState:
    meta = obj.get("metadata") or {}
    spec = obj.get("spec") or {}
    status = obj.get("status") or {}
    namespace = meta.get("namespace") or ""
    state = FluxObjectState(
        kind=kind,
        name=meta.get("name") or "",
        namespace=namespace,
        api_version=obj.get("apiVersion") or "",
        suspended=bool(spec.get("suspend")),
        conditions=_conditions(status.get("conditions")),
    )
    parser = _PARSERS.get(kind, _parse_source)
    parser(state, spec, status, namespace)
    return state


def _parse_helmrelease(state: FluxObjectState, spec: dict, status: dict, ns: str) -> None:
    chart_ref = spec.get("chartRef") or {}
    chart_spec = (spec.get("chart") or {}).get("spec") or {}
    if chart_ref:
        state.source = _ref(chart_ref, ns)
        state.facts["chart"] = f"chartRef {chart_ref.get('kind')}/{chart_ref.get('name')}"
    elif chart_spec:
        # With an inline chart template, Flux materialises a HelmChart object
        # (status.helmChart = "<ns>/<name>"); that is the object whose
        # conditions say whether the chart could be fetched.
        helm_chart = status.get("helmChart") or ""
        if "/" in helm_chart:
            chart_ns, _, chart_name = helm_chart.partition("/")
            state.source = FluxRef("HelmChart", chart_ns, chart_name)
        elif chart_spec.get("sourceRef"):
            state.source = _ref(chart_spec["sourceRef"], ns)
        version = chart_spec.get("version") or "*"
        state.facts["chart"] = f"{chart_spec.get('chart')} (version constraint {version})"
        if chart_spec.get("sourceRef"):
            src = chart_spec["sourceRef"]
            state.facts["chart source"] = f"{src.get('kind')}/{src.get('name')}"

    history = [h for h in (status.get("history") or []) if isinstance(h, dict)]
    state.releases = [
        FluxRelease(
            chart_version=h.get("chartVersion"),
            app_version=h.get("appVersion"),
            status=h.get("status"),
            last_deployed=h.get("lastDeployed"),
        )
        for h in history[:MAX_RELEASES]
    ]
    deployed = next((h for h in history if h.get("status") == "deployed"), None)
    # v2beta1 carried lastAppliedRevision; v2 replaced it with history.
    state.applied_revision = (
        (deployed or {}).get("chartVersion") or status.get("lastAppliedRevision")
    )
    state.attempted_revision = status.get("lastAttemptedRevision")

    for key in ("releaseName", "targetNamespace", "storageNamespace"):
        if spec.get(key):
            state.facts[key] = str(spec[key])
    drift = (spec.get("driftDetection") or {}).get("mode")
    state.facts["driftDetection"] = drift or "disabled"
    for key in ("failures", "installFailures", "upgradeFailures"):
        if status.get(key):
            state.facts[key] = str(status[key])
    if status.get("lastAttemptedReleaseAction"):
        state.facts["lastAttemptedReleaseAction"] = str(status["lastAttemptedReleaseAction"])
    state.value_refs = [_value_ref(v) for v in spec.get("valuesFrom") or [] if isinstance(v, dict)]
    if spec.get("values"):
        # Say the inline values exist without saying what they are.
        state.facts["inline values"] = "present (withheld)"


def _parse_kustomization(state: FluxObjectState, spec: dict, status: dict, ns: str) -> None:
    if spec.get("sourceRef"):
        state.source = _ref(spec["sourceRef"], ns)
    state.applied_revision = status.get("lastAppliedRevision")
    state.attempted_revision = status.get("lastAttemptedRevision")
    for key in ("path", "targetNamespace"):
        if spec.get(key):
            state.facts[key] = str(spec[key])
    if "prune" in spec:
        state.facts["prune"] = str(bool(spec["prune"])).lower()
    post_build = spec.get("postBuild") or {}
    state.value_refs = [
        _value_ref(v) for v in post_build.get("substituteFrom") or [] if isinstance(v, dict)
    ]
    if post_build.get("substitute"):
        state.facts["inline substitutions"] = (
            f"{len(post_build['substitute'])} variable(s) (values withheld)"
        )


def _parse_source(state: FluxObjectState, spec: dict, status: dict, ns: str) -> None:
    """GitRepository, OCIRepository, HelmRepository, Bucket, HelmChart."""
    artifact = status.get("artifact") or {}
    state.applied_revision = artifact.get("revision")
    if spec.get("url"):
        state.facts["url"] = strip_url_credentials(str(spec["url"]))
    ref = spec.get("ref") or {}
    for key in ("branch", "tag", "semver", "commit", "name", "digest"):
        if ref.get(key):
            state.facts[f"ref.{key}"] = str(ref[key])
    for key in ("bucketName", "provider", "type"):
        if spec.get(key):
            state.facts[key] = str(spec[key])
    if state.kind == "HelmChart":
        if spec.get("sourceRef"):
            state.source = _ref(spec["sourceRef"], ns)
        if spec.get("chart"):
            state.facts["chart"] = str(spec["chart"])
        if spec.get("version"):
            state.facts["version constraint"] = str(spec["version"])
        if status.get("observedChartName"):
            state.facts["observedChartName"] = str(status["observedChartName"])


_PARSERS = {
    "HelmRelease": _parse_helmrelease,
    "Kustomization": _parse_kustomization,
}


def _conditions(raw: Any) -> list[PodCondition]:
    out: list[PodCondition] = []
    for entry in raw or []:
        if not isinstance(entry, dict):
            continue
        message = entry.get("message")
        if isinstance(message, str) and len(message) > MAX_CONDITION_MESSAGE:
            message = message[:MAX_CONDITION_MESSAGE] + " … [truncated]"
        out.append(
            PodCondition(
                type=entry.get("type") or "",
                status=entry.get("status") or "",
                reason=entry.get("reason"),
                message=message,
            )
        )
    return out


def _ref(raw: dict, default_namespace: str) -> FluxRef | None:
    kind, name = raw.get("kind"), raw.get("name")
    if not kind or not name:
        return None
    return FluxRef(kind, raw.get("namespace") or default_namespace, name)


def _value_ref(raw: dict) -> str:
    out = f"{raw.get('kind', '?')}/{raw.get('name', '?')}"
    if raw.get("valuesKey"):
        out += f" (key {raw['valuesKey']})"
    if raw.get("optional"):
        out += " [optional]"
    return out


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


class FluxClient:
    """Fetch Flux objects through an existing :class:`KubernetesClient`."""

    def __init__(self, k8s: KubernetesClient, *, clock=time.monotonic):
        self.k8s = k8s
        self._clock = clock
        # group -> (preferred version or None if not served, fetched_at)
        self._versions: dict[str, tuple[str | None, float]] = {}

    async def preferred_version(self, group: str) -> str:
        """The served version for a Flux group; FluxNotInstalled if none."""
        cached = self._versions.get(group)
        if cached is not None and self._clock() - cached[1] < DISCOVERY_TTL_SECONDS:
            version = cached[0]
        else:
            payload = await self.k8s.get_json(f"/apis/{group}")
            version = ((payload or {}).get("preferredVersion") or {}).get("version")
            self._versions[group] = (version, self._clock())
        if not version:
            raise FluxNotInstalled(group)
        return version

    async def get(
        self,
        kind: str,
        namespace: str,
        name: str,
        *,
        version: str | None = None,
    ) -> FluxObjectState | None:
        """One Flux object, projected. None when it does not exist.

        ``version`` comes from an alert's ``customresource_version`` label
        when there is one, which saves the discovery round trip; anything
        that does not look like an API version is ignored rather than put in
        a URL.
        """
        if kind not in FLUX_KINDS:
            raise ValueError(f"not a Flux kind this agent reads: {kind!r}")
        group, plural = FLUX_KINDS[kind]
        if not (version and _API_VERSION.match(version)):
            version = await self.preferred_version(group)
        obj = await self.k8s.get_namespaced(f"{group}/{version}", plural, namespace, name)
        if obj is None:
            return None
        return parse_flux_object(kind, obj)


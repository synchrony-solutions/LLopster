"""Recognise alerts that are about a GitOps delivery object, not a workload.

On a Flux cluster a large share of alerts are the delivery system reporting on
itself — a HelmRelease that will not become Ready, a GitRepository that cannot
fetch. Those arrive as `gotk_resource_info` series from kube-state-metrics'
custom-resource-state config, and the series *already names the object*:

    gotk_resource_info{customresource_group="helm.toolkit.fluxcd.io",
                       customresource_kind="HelmRelease",
                       customresource_version="v2",
                       exported_namespace="prod", name="api",
                       ready="False", suspended="true", revision="1.4.2"}

Reading that identity off the labels is exact, and it works when the release
produced no pods at all — the case where working back from a workload finds
nothing. (Issue #24 part C.)

The label names come from Flux's reference config
(fluxcd/flux2-monitoring-example, kube-state-metrics-config.yaml), not from
the shorthand in the issue: the namespace arrives as `exported_namespace`
because kube-state-metrics' own target labels claim `namespace`, and the kind
is KSM's automatic `customresource_kind`.

Shaped around a `controller` field so an Argo CD `Application` reference can
slot in beside Flux without a second seam.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from src.agent.alert_handler import ParsedAlert, alert_label, strip_url_credentials

FLUX_GROUP_SUFFIX = ".toolkit.fluxcd.io"

# Kind-specific labels from the reference config that carry diagnosis, in a
# stable render order. Anything else an operator adds is still visible in the
# prompt's raw `## Labels` section; this block is the interpreted view.
_DETAIL_LABELS = (
    "chart_name",
    "chart_app_version",
    "chart_ref_name",
    "chart_source_name",
    "source_name",
    "url",
)


@dataclass(frozen=True)
class GitOpsResourceRef:
    """The delivery object an alert is about, as read from its labels."""

    controller: str            # "flux" today; "argocd" is the planned sibling
    kind: str                  # HelmRelease, Kustomization, GitRepository, ...
    group: str                 # helm.toolkit.fluxcd.io, ...
    name: str
    namespace: str | None
    version: str | None = None  # API version, when the series carries it
    ready: bool | None = None   # None = condition absent or Unknown
    suspended: bool = False
    revision: str | None = None
    details: dict[str, str] = field(default_factory=dict)

    @property
    def display(self) -> str:
        where = f"{self.namespace}/{self.name}" if self.namespace else self.name
        return f"Flux {self.kind} {where}"


def gitops_ref_from_alert(alert: ParsedAlert) -> GitOpsResourceRef | None:
    """The Flux object a `gotk_*` alert names, or None for any other alert.

    Keyed on `customresource_group`, which only a custom-resource-state series
    carries, so an ordinary workload alert that happens to have a `name` label
    is never mistaken for one.
    """
    labels = alert.labels
    group = labels.get("customresource_group") or ""
    kind = labels.get("customresource_kind") or ""
    name = labels.get("name") or ""
    if not group.endswith(FLUX_GROUP_SUFFIX) or not kind or not name:
        return None

    details = {}
    for key in _DETAIL_LABELS:
        value = labels.get(key)
        if value:
            details[key] = strip_url_credentials(value)

    return GitOpsResourceRef(
        controller="flux",
        kind=kind,
        group=group,
        name=name,
        namespace=alert_label(alert, "namespace"),
        version=labels.get("customresource_version") or None,
        ready=_condition(labels.get("ready")),
        # `[spec, suspend]` renders only when set; absent means not suspended.
        suspended=(labels.get("suspended") or "").lower() == "true",
        revision=labels.get("revision") or None,
        details=details,
    )


def suspended_skip_reason(ref: GitOpsResourceRef) -> str:
    """The skip reason for a suspended object, worded for the dashboard."""
    return (
        f"{ref.display} is suspended (spec.suspend: true) — reconciliation has "
        "been paused deliberately, so no patch can take effect until an "
        "operator resumes it"
    )


def format_gitops_ref(ref: GitOpsResourceRef) -> list[str]:
    """Render `## GitOps resource` for the volatile half of an LLM prompt.

    The raw labels are already in the prompt. What this adds is the reading of
    them that the model would otherwise have to get right on its own: which
    object the alert is about (not the kube-state-metrics pod whose `pod` and
    `namespace` labels sit beside it), and what the reconciliation state means
    for where a fix can live.
    """
    ready = {True: "True", False: "False", None: "Unknown"}[ref.ready]
    lines = [
        "## GitOps resource (from alert labels)",
        "",
        f"This alert is about a {ref.kind} reconciled by Flux, not about a "
        "workload. Its `pod` and `namespace` labels belong to the "
        "kube-state-metrics instance that exported the series, not to the "
        "object.",
        "",
        f"- Object: {ref.kind} `{ref.name}`"
        + (f" in namespace `{ref.namespace}`" if ref.namespace else ""),
        f"- API group: {ref.group}" + (f"/{ref.version}" if ref.version else ""),
        f"- Ready: {ready}",
        f"- Suspended: {'yes' if ref.suspended else 'no'}",
    ]
    if ref.revision:
        lines.append(f"- Revision: {ref.revision}")
    lines += [f"- {key}: {value}" for key, value in ref.details.items()]
    lines += [
        "",
        "A not-Ready delivery object usually means the cluster never received "
        "the desired state, so the fix belongs in what Flux reconciles (the "
        "chart, values or manifests it points at) rather than in a running "
        "workload. Its status conditions carry the controller's own diagnosis "
        "and are not included here.",
        "",
    ]
    return lines


def _condition(raw: str | None) -> bool | None:
    value = (raw or "").lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


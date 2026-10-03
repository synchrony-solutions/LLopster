"""Parse AlertManager webhook payloads into a normalized internal shape."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit, urlunsplit


@dataclass
class ParsedAlert:
    fingerprint: str
    status: str
    alertname: str
    severity: str
    service: str
    summary: str
    description: str
    starts_at: datetime | None
    ends_at: datetime | None
    labels: dict[str, str] = field(default_factory=dict)
    annotations: dict[str, str] = field(default_factory=dict)
    generator_url: str = ""


# When a series carries a label the scrape target also sets (honor_labels
# off, the Prometheus default), the series' own value is renamed
# `exported_<name>` and `<name>` becomes the *target's* -- e.g. namespace
# "monitoring" and pod "kube-state-metrics-xyz" on every KSM alert. The
# exported value is the object the alert is about, so it always wins.
#
# A collision is detectable by `exported_namespace` being present, and once it
# has happened a bare `pod` with no `exported_pod` names the scrape target, not
# the alerted object (a pod-less KSM series like kube_deployment_* still gets
# the KSM pod's `pod` label stamped on). Those labels are discarded rather
# than trusted. `node` is not in the set: pod targets do not carry a node
# label by default, so a bare `node` beside a collision is still the series'.
_TARGET_IDENTITY_LABELS = frozenset({"pod"})


def alert_label(alert: ParsedAlert, name: str) -> str | None:
    """The alerted object's value for `name`, resolving scrape collisions.

    Every consumer that resolves an alert to an object goes through this --
    the cluster-state collector, the GitOps reference parser and the eval
    corpus fidelity test -- so none of them can disagree about which object
    an alert is about.
    """
    labels = alert.labels
    exported = labels.get(f"exported_{name}")
    if exported:
        return exported
    if name in _TARGET_IDENTITY_LABELS and labels.get("exported_namespace"):
        return None
    return labels.get(name) or None


def strip_url_credentials(value: str) -> str:
    """Drop `user:token@` from a URL-shaped label value; other values pass.

    Alert labels are rendered into LLM prompts verbatim, and some carry URLs
    an operator controls -- Flux's `gotk_resource_info` exports a
    GitRepository's `spec.url`, which can embed credentials. Flux discourages
    that, but nothing prevents it, and prompt text goes to the LLM provider.
    """
    if "://" not in value or "@" not in value:
        return value
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return value
    if not parts.username and not parts.password:
        return value
    host = parts.hostname or ""
    if port:
        host = f"{host}:{port}"
    return urlunsplit((parts.scheme, host, parts.path, parts.query, parts.fragment))


def _parse_ts(value: str | None) -> datetime | None:
    if not value or value.startswith("0001-01-01"):
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def parse_alertmanager_payload(payload: dict[str, Any]) -> list[ParsedAlert]:
    """Convert an AlertManager webhook payload into a list of ParsedAlert objects.

    See https://prometheus.io/docs/alerting/latest/configuration/#webhook_config
    for the payload schema.
    """
    alerts = []
    for raw in payload.get("alerts", []):
        labels = raw.get("labels", {}) or {}
        annotations = raw.get("annotations", {}) or {}
        alerts.append(
            ParsedAlert(
                fingerprint=raw.get("fingerprint", ""),
                status=raw.get("status", "firing"),
                alertname=labels.get("alertname", "unknown"),
                severity=labels.get("severity", "unknown"),
                service=labels.get("service", "unknown"),
                summary=annotations.get("summary", ""),
                description=annotations.get("description", ""),
                starts_at=_parse_ts(raw.get("startsAt")),
                ends_at=_parse_ts(raw.get("endsAt")),
                labels=labels,
                annotations=annotations,
                generator_url=raw.get("generatorURL", ""),
            )
        )
    return alerts

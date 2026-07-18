"""Parse AlertManager webhook payloads into a normalized internal shape."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


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

"""Load the frozen scenario corpus from disk.

A *scenario* is a single seeded bug frozen as a regression case:
  - the AlertManager webhook payload that fires for it,
  - the Loki log lines and Prometheus metric samples recorded at the time
    (so replay needs no live observability stack), and
  - a ground-truth block the scorer grades against.

Keeping all of that inline in `scenario.yaml` makes the corpus self-contained
and stable — the whole point of a regression baseline. The on-disk schema:

    id: db-pool-exhausted
    description: ...
    alert:                       # AlertManager webhook payload (>= 1 alert)
      alerts:
        - labels: {alertname: ..., service: demo-app, ...}
          annotations: {...}
          startsAt: "..."
          generatorURL: "...g0.expr=..."
    recorded_context:
      log_lines:
        - line: "ERROR db_pool: ..."
          labels: {service: demo-app, component: db_pool}
      metric_samples:
        - metric: {__name__: demo_app_db_pool_exhausted_total}
          value: 12
    ground_truth:
      expect_patch: true
      expected_files: [check_db_pool.py]
      root_cause_keywords: [pool, connection]
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.agent.alert_handler import ParsedAlert, parse_alertmanager_payload
from src.integrations.loki_client import LogLine
from src.integrations.prometheus_client import MetricSample

log = logging.getLogger("llopster.eval.corpus")

# Default corpus location (relative to repo root). Override in callers/tests.
DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


@dataclass(frozen=True)
class GroundTruth:
    """The human-authored answer key for a scenario."""

    expect_patch: bool
    expected_files: tuple[str, ...]
    root_cause_keywords: tuple[str, ...] = ()


@dataclass
class Scenario:
    """One frozen regression case, ready to replay through the pipeline."""

    id: str
    description: str
    alert: ParsedAlert
    raw_payload: dict
    log_lines: list[LogLine]
    metric_samples: list[MetricSample]
    ground_truth: GroundTruth


def _parse_log_lines(raw: list[dict], at) -> list[LogLine]:
    out: list[LogLine] = []
    for entry in raw or []:
        out.append(
            LogLine(
                timestamp=at,
                line=str(entry.get("line", "")),
                labels={str(k): str(v) for k, v in (entry.get("labels") or {}).items()},
            )
        )
    return out


def _parse_metric_samples(raw: list[dict], at) -> list[MetricSample]:
    out: list[MetricSample] = []
    for entry in raw or []:
        out.append(
            MetricSample(
                metric={str(k): str(v) for k, v in (entry.get("metric") or {}).items()},
                value=float(entry.get("value", 0.0)),
                timestamp=at,
            )
        )
    return out


def load_scenario(path: Path) -> Scenario:
    """Parse a single `scenario.yaml` into a `Scenario`.

    Raises on malformed scenarios — a corpus that won't parse is a bug in the
    regression baseline, not a runtime condition to swallow. (The *replay* of a
    scenario fails safe; loading the corpus is build-time and should be loud.)
    """
    data = yaml.safe_load(path.read_text()) or {}

    alert_payload = data.get("alert")
    if not isinstance(alert_payload, dict):
        raise ValueError(f"{path}: missing or invalid 'alert' payload")
    parsed = parse_alertmanager_payload(alert_payload)
    if not parsed:
        raise ValueError(f"{path}: alert payload produced no parsed alerts")
    alert = parsed[0]

    at = alert.starts_at
    rc = data.get("recorded_context") or {}
    log_lines = _parse_log_lines(rc.get("log_lines", []), at)
    metric_samples = _parse_metric_samples(rc.get("metric_samples", []), at)

    gt = data.get("ground_truth") or {}
    # YAML coerces bare tokens like 512 / 0.001 to numbers — keep the answer
    # key as strings so substring matching against diff/root-cause text works.
    ground_truth = GroundTruth(
        expect_patch=bool(gt.get("expect_patch", True)),
        expected_files=tuple(str(f) for f in (gt.get("expected_files", []) or ())),
        root_cause_keywords=tuple(str(k) for k in (gt.get("root_cause_keywords", []) or ())),
    )

    return Scenario(
        id=str(data.get("id") or path.parent.name),
        description=str(data.get("description", "")),
        alert=alert,
        raw_payload=alert_payload,
        log_lines=log_lines,
        metric_samples=metric_samples,
        ground_truth=ground_truth,
    )


def load_corpus(scenarios_dir: Path | str | None = None) -> list[Scenario]:
    """Load every `<dir>/scenario.yaml`, sorted by id for a stable order."""
    root = Path(scenarios_dir) if scenarios_dir else DEFAULT_SCENARIOS_DIR
    if not root.exists():
        log.warning("scenarios dir %s not found — empty corpus", root)
        return []
    scenarios = [
        load_scenario(p) for p in sorted(root.glob("*/scenario.yaml"))
    ]
    log.info("loaded %d eval scenario(s) from %s", len(scenarios), root)
    return scenarios


def corpus_version(scenarios: list[Scenario]) -> str:
    """A short, stable identifier for the corpus contents — `<count>:<ids-hash>`.

    Lets the dashboard tell "pass-rate dropped" (regression) apart from
    "pass-rate changed because the corpus changed" (new scenarios added).
    """
    import hashlib

    ids = ",".join(sorted(s.id for s in scenarios))
    digest = hashlib.sha256(ids.encode()).hexdigest()[:8]
    return f"{len(scenarios)}:{digest}"

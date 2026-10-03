"""Load the frozen scenario corpus from disk.

A *scenario* is a single seeded bug frozen as a regression case:
  - the AlertManager webhook payload that fires for it,
  - the Loki log lines and Prometheus metric samples recorded at the time
    (so replay needs no live observability stack), and
  - a ground-truth block the scorer grades against.

Keeping all of that inline in `scenario.yaml` makes the corpus self-contained
and stable — the whole point of a regression baseline.

**`recorded_context` must contain only what a real run could have collected.**
`ContextCollector` extracts exactly ONE PromQL expression — the alert's own
`g0.expr` from its `generatorURL` — and queries only that, so every entry in
`metric_samples` has to be a series that expression could return. Log lines are
similarly limited to the single LogQL stream selector the collector builds.

This is not pedantry. A scenario carrying an extra series (say a
`kube_pod_container_resource_limits` sample next to an OOMKill alert) hands the
model evidence it would never have in production, and a feature that looks like
it passes may only be passing on the fabricated clue. That happened here: the
`invisible-chart-layer-override` scenario graded identically with and without
the declaration it was written to test, because two out-of-band samples gave the
answer away. `tests/test_eval_corpus.py` now enforces the metric half of this.

The same rule governs `cluster_state`, which is only collectable when the agent
has read-only cluster access enabled AND the alert names the object: every pod
in a recorded `cluster_state` must be the pod the alert's own labels point at,
in the alert's own namespace. A scenario that recorded a neighbouring pod, or a
namespace the alert never mentions, is testing something the collector could
never hand the model. `tests/test_eval_corpus.py` enforces this too.

The on-disk schema:

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
      cluster_state:             # optional; omit unless the scenario is ABOUT
        namespace: prod          # cluster state (read-only access is off by
        pods:                    # default, so most runs have none)
          - name: api-7d9f-abc
            phase: Running
            containers:
              - name: api
                restart_count: 9
                state: waiting
                state_reason: CrashLoopBackOff
                last_state: terminated
                last_state_reason: OOMKilled
                last_state_exit_code: 137
                limits: {memory: 256Mi}
        events:
          - involved_object: Pod/api-7d9f-abc
            reason: BackOff
    service:                     # optional; overrides the default registry
      name: subscription-airflow
      codebase_path: codebase    # relative paths resolve against this dir
      github_repo: example-org/platform-charts
      delivery: {mode: oci-chart, version_ref: {...}}
      chart_lineage: [{name: ..., visible: false}, ...]
    ground_truth:
      expect_patch: true
      expected_files: [check_db_pool.py]
      root_cause_keywords: [pool, connection]
      max_confidence: 2          # optional ceiling; see eval/scoring.py
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from src.agent.alert_handler import ParsedAlert, parse_alertmanager_payload
from src.agent.cluster_state import ClusterState, GitOpsState
from src.integrations.flux_client import FluxObjectState, FluxRef, FluxRelease
from src.integrations.kubernetes_client import (
    ClusterEvent,
    ContainerState,
    NodeState,
    OwnerRef,
    PodCondition,
    PodState,
    PVCState,
)
from src.integrations.loki_client import LogLine
from src.integrations.prometheus_client import MetricSample
from src.services_registry import (
    ServiceConfig,
    _parse_chart_lineage,
    _parse_delivery,
)

log = logging.getLogger("llopster.eval.corpus")

# Default corpus location (relative to repo root). Override in callers/tests.
DEFAULT_SCENARIOS_DIR = Path(__file__).resolve().parent / "scenarios"


@dataclass(frozen=True)
class GroundTruth:
    """The human-authored answer key for a scenario."""

    expect_patch: bool
    expected_files: tuple[str, ...]
    root_cause_keywords: tuple[str, ...] = ()
    # Optional ceiling on the synthesis confidence. For scenarios where the
    # patch cannot take effect from this repo (an OCI-packaged chart, a cause
    # in an invisible chart layer), the failure mode is not "patched" — it is
    # "patched CONFIDENTLY". A high score on an undeliverable fix is what burns
    # reviewer trust, so it is graded as wrong even when the diff looks right.
    # None = no ceiling asserted (every pre-existing scenario).
    max_confidence: int | None = None


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
    # Optional per-scenario service config. The default corpus is all demo-app
    # bugs served by one registry entry, but a scenario that exercises an
    # operator declaration (`delivery`, `chart_lineage`) has to carry its own —
    # the declaration IS the thing under test.
    service: ServiceConfig | None = None
    # Frozen read-only cluster objects, or None for the (usual) scenario where
    # cluster access is off. None and an empty ClusterState replay differently:
    # the first omits the `## Cluster state` prompt block entirely, the second
    # tells the model the agent looked and found nothing.
    cluster_state: ClusterState | None = None


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


def _parse_cluster_state(raw: object) -> ClusterState | None:
    """Parse a recorded `cluster_state` block into a real ClusterState.

    Deliberately builds the same dataclasses the live collector produces, so
    a scenario cannot encode a shape the prompt renderer never sees. Absent
    block -> None, matching a run with cluster access disabled.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("recorded_context.cluster_state must be a mapping")
    return ClusterState(
        namespace=raw.get("namespace"),
        pods=[_parse_recorded_pod(p) for p in raw.get("pods") or []],
        events=[
            ClusterEvent(
                involved_object=str(e.get("involved_object", "")),
                type=e.get("type", "Warning"),
                reason=e.get("reason"),
                message=e.get("message"),
                count=int(e.get("count", 1)),
                last_timestamp=None,
            )
            for e in raw.get("events") or []
        ],
        nodes=[
            NodeState(
                name=str(n.get("name", "")),
                conditions=[_parse_recorded_condition(c) for c in n.get("conditions") or []],
                unschedulable=bool(n.get("unschedulable", False)),
            )
            for n in raw.get("nodes") or []
        ],
        pvcs=[
            PVCState(
                name=str(v.get("name", "")),
                namespace=str(v.get("namespace", raw.get("namespace") or "")),
                phase=v.get("phase"),
                storage_class=v.get("storage_class"),
                requested_storage=v.get("requested_storage"),
                volume_name=v.get("volume_name"),
            )
            for v in raw.get("pvcs") or []
        ],
        objects_queried=[str(o) for o in raw.get("objects_queried") or []],
        notes=[str(n) for n in raw.get("notes") or []],
        gitops=_parse_recorded_gitops(raw.get("gitops")),
    )


def _parse_recorded_gitops(raw: object) -> GitOpsState | None:
    """A recorded `gitops` block, built into the collector's own dataclasses.

    Objects are recorded as the *projection* (FluxObjectState fields), never
    as raw Flux objects -- the same boundary the live client enforces, so a
    scenario cannot hand the model a field the collector would have withheld.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("recorded_context.cluster_state.gitops must be a mapping")
    objects = []
    for o in raw.get("objects") or []:
        src = o.get("source")
        objects.append(FluxObjectState(
            kind=str(o["kind"]),
            name=str(o["name"]),
            namespace=str(o["namespace"]),
            api_version=str(o.get("api_version", "")),
            suspended=bool(o.get("suspended", False)),
            conditions=[_parse_recorded_condition(c) for c in o.get("conditions") or []],
            applied_revision=o.get("applied_revision"),
            attempted_revision=o.get("attempted_revision"),
            source=FluxRef(str(src["kind"]), str(src["namespace"]), str(src["name"])) if src else None,
            facts={str(k): str(v) for k, v in (o.get("facts") or {}).items()},
            releases=[FluxRelease(**r) for r in o.get("releases") or []],
            value_refs=[str(v) for v in o.get("value_refs") or []],
        ))
    return GitOpsState(
        resolved_from=str(raw.get("resolved_from", "alert labels")),
        objects=objects,
        events=[
            ClusterEvent(
                involved_object=str(e.get("involved_object", "")),
                type=e.get("type", "Warning"),
                reason=e.get("reason"),
                message=e.get("message"),
                count=int(e.get("count", 1)),
                last_timestamp=None,
            )
            for e in raw.get("events") or []
        ],
    )


def _parse_recorded_pod(raw: dict) -> PodState:
    return PodState(
        name=str(raw.get("name", "")),
        namespace=str(raw.get("namespace", "")),
        phase=raw.get("phase"),
        node_name=raw.get("node_name"),
        conditions=[_parse_recorded_condition(c) for c in raw.get("conditions") or []],
        containers=[
            ContainerState(
                name=str(c.get("name", "")),
                image=c.get("image"),
                ready=bool(c.get("ready", False)),
                restart_count=int(c.get("restart_count", 0)),
                state=c.get("state"),
                state_reason=c.get("state_reason"),
                state_message=c.get("state_message"),
                last_state=c.get("last_state"),
                last_state_reason=c.get("last_state_reason"),
                last_state_exit_code=c.get("last_state_exit_code"),
                last_state_signal=c.get("last_state_signal"),
                last_state_finished_at=c.get("last_state_finished_at"),
                requests={str(k): str(v) for k, v in (c.get("requests") or {}).items()},
                limits={str(k): str(v) for k, v in (c.get("limits") or {}).items()},
            )
            for c in raw.get("containers") or []
        ],
        owner_chain=[
            OwnerRef(kind=str(o.get("kind", "")), name=str(o.get("name", "")),
                     resolved=bool(o.get("resolved", True)))
            for o in raw.get("owner_chain") or []
        ],
        spec=raw.get("spec"),
    )


def _parse_recorded_condition(raw: dict) -> PodCondition:
    return PodCondition(
        type=str(raw.get("type", "")),
        status=str(raw.get("status", "")),
        reason=raw.get("reason"),
        message=raw.get("message"),
    )


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
    cluster_state = _parse_cluster_state(rc.get("cluster_state"))

    gt = data.get("ground_truth") or {}
    # YAML coerces bare tokens like 512 / 0.001 to numbers — keep the answer
    # key as strings so substring matching against diff/root-cause text works.
    max_confidence = gt.get("max_confidence")
    ground_truth = GroundTruth(
        expect_patch=bool(gt.get("expect_patch", True)),
        expected_files=tuple(str(f) for f in (gt.get("expected_files", []) or ())),
        root_cause_keywords=tuple(str(k) for k in (gt.get("root_cause_keywords", []) or ())),
        max_confidence=int(max_confidence) if max_confidence is not None else None,
    )

    service = _parse_service(data.get("service"), scenario_dir=path.parent)

    return Scenario(
        id=str(data.get("id") or path.parent.name),
        description=str(data.get("description", "")),
        alert=alert,
        raw_payload=alert_payload,
        log_lines=log_lines,
        metric_samples=metric_samples,
        cluster_state=cluster_state,
        ground_truth=ground_truth,
        service=service,
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


def _parse_service(raw: object, *, scenario_dir: Path) -> ServiceConfig | None:
    """Parse a scenario's optional `service` block into a ServiceConfig.

    Reuses the registry's own `delivery` / `chart_lineage` parsers rather than
    reimplementing them, so the corpus cannot drift from what production
    accepts — a scenario that parses here parses identically in services.yaml.

    `codebase_path` resolves relative to the scenario directory, keeping each
    scenario self-contained (fixture tree next to the scenario.yaml that
    describes it). Absolute paths pass through untouched.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{scenario_dir}: 'service' must be a mapping")

    name = raw.get("name")
    codebase_path = raw.get("codebase_path")
    github_repo = raw.get("github_repo")
    if not name or not codebase_path or not github_repo:
        raise ValueError(
            f"{scenario_dir}: 'service' needs name, codebase_path and github_repo"
        )

    resolved = Path(str(codebase_path))
    if not resolved.is_absolute():
        resolved = (scenario_dir / resolved).resolve()

    return ServiceConfig(
        name=str(name),
        codebase_path=str(resolved),
        github_repo=str(github_repo),
        pack=str(raw["pack"]) if raw.get("pack") else None,
        delivery=_parse_delivery(str(name), raw.get("delivery")),
        chart_lineage=_parse_chart_lineage(str(name), raw.get("chart_lineage")),
    )


class UnknownScenarioError(ValueError):
    """A requested scenario id is not in the corpus.

    Raised rather than silently skipping: a typo'd id that quietly selected
    nothing would report a clean run over zero scenarios, which reads exactly
    like a pass.
    """


def select_scenarios(
    scenarios: list[Scenario], ids: list[str] | None,
) -> list[Scenario]:
    """Filter a loaded corpus down to the requested ids, preserving order.

    `ids` entries may be comma-separated, so both of these select the same two:

        --scenario-id a --scenario-id b
        --scenario-id a,b

    `None` or an empty selection returns the corpus untouched.
    """
    if not ids:
        return scenarios

    wanted: list[str] = []
    for entry in ids:
        wanted.extend(part.strip() for part in str(entry).split(",") if part.strip())
    if not wanted:
        return scenarios

    available = {s.id for s in scenarios}
    unknown = [w for w in wanted if w not in available]
    if unknown:
        raise UnknownScenarioError(
            f"unknown scenario id(s): {', '.join(sorted(unknown))}. "
            f"Available: {', '.join(sorted(available))}"
        )

    selected = set(wanted)
    return [s for s in scenarios if s.id in selected]

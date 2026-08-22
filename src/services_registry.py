"""Lookup per-service config (codebase path, GitHub repo) from a YAML file.

Each incoming alert carries a `service` label; the registry maps that label to
the local codebase directory the patch generator should send to Claude and the
GitHub repo the PR creator should target. Services not in the registry are
skipped with a warning rather than falling back to a global default.

Two optional blocks per service describe things the agent cannot observe from
the cluster or the codebase, and that materially change whether a patch will
actually take effect:

  * ``delivery`` — how a merged change in this repo reaches the cluster. On a
    GitOps cluster a source-only patch to a chart packaged into an OCI registry
    never reconciles, so the synthesis stage has to know which regime it is in
    (issue #24).
  * ``chart_lineage`` — the chart-of-charts this service is delivered by, and
    which layers the agent can actually see. Layers it cannot see routinely
    hold the key that overrides the one it is about to patch (issue #25).

Both are optional and additive: an entry without them loads and behaves exactly
as it did before they existed.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("llopster.services")


# How a merged change in a service's repo reaches the running cluster. The
# distinction the agent cannot infer on its own, and the reason it matters:
#
#   git-manifest  Flux/Argo applies these files directly — a merged PR
#                 reconciles. This is what LLopster has always assumed.
#   oci-chart     the chart is packaged and pushed to a registry; editing the
#                 chart source changes NOTHING in the cluster until the package
#                 is rebuilt and the version reference bumped.
#   image-build   CI builds an image; the tag has to be bumped to take effect.
#
# The last two are the dangerous ones: a patch under them can be correct,
# validate cleanly, merge, and still leave the alert firing forever.
DELIVERY_MODES = ("git-manifest", "oci-chart", "image-build")

# Modes where a source-only patch does not reach the cluster by itself.
INDIRECT_DELIVERY_MODES = ("oci-chart", "image-build")


@dataclass(frozen=True)
class VersionRef:
    """Where the version/tag that the cluster actually consumes is declared.

    Only meaningful for the indirect delivery modes. Every field is optional —
    a half-specified ref still tells the model more than nothing, and "there is
    a version reference somewhere I cannot see" is itself useful context.
    """
    repo: str | None = None
    path: str | None = None
    key: str | None = None


@dataclass(frozen=True)
class Delivery:
    mode: str
    version_ref: VersionRef | None = None

    @property
    def is_indirect(self) -> bool:
        """True when a source-only patch will not reconcile on its own."""
        return self.mode in INDIRECT_DELIVERY_MODES


@dataclass(frozen=True)
class ChartLayer:
    """One chart in the delivery lineage, outermost (most specific) first.

    `visible` records whether this layer is inside the codebase the agent was
    given. Declaring the invisible layers is the entire point: it lets the
    model say "the cause is probably in a layer I cannot see" instead of naming
    the closest-looking file in the one tree it has.
    """
    name: str
    version: str | None = None
    repo: str | None = None
    visible: bool = False


@dataclass(frozen=True)
class ServiceConfig:
    name: str
    codebase_path: str
    github_repo: str
    # Optional premium-pack selector. When set, names the pack "stack"
    # (e.g. "jvm", "postgres") whose prompt overlays apply to this service —
    # see src/agent/prompts.py. Absent/None = Community prompts (default).
    # Backward compatible: services.yaml entries without a `pack:` field load
    # exactly as before.
    pack: str | None = None
    # Optional; None/() mean "not declared" and reproduce pre-existing
    # behaviour exactly.
    delivery: Delivery | None = None
    chart_lineage: tuple[ChartLayer, ...] = ()


class ServiceRegistry:
    def __init__(self, config_path: str):
        self._services: dict[str, ServiceConfig] = {}
        path = Path(config_path)
        if not path.exists():
            log.warning("services config %s not found — registry is empty", path)
            return
        try:
            data = yaml.safe_load(path.read_text()) or {}
        except yaml.YAMLError:
            log.exception("failed to parse %s", path)
            return
        if not isinstance(data, dict):
            log.warning("services config %s is not a mapping — registry is empty", path)
            return

        for name, entry in data.items():
            if not isinstance(entry, dict):
                log.warning("service %r in %s is not a mapping — skipping", name, path)
                continue
            codebase_path = entry.get("codebase_path")
            github_repo = entry.get("github_repo")
            if not codebase_path or not github_repo:
                log.warning(
                    "service %r missing codebase_path or github_repo — skipping", name,
                )
                continue
            pack = entry.get("pack")
            self._services[name] = ServiceConfig(
                name=name,
                codebase_path=codebase_path,
                github_repo=github_repo,
                pack=str(pack) if pack else None,
                delivery=_parse_delivery(name, entry.get("delivery")),
                chart_lineage=_parse_chart_lineage(name, entry.get("chart_lineage")),
            )
        log.info("loaded %d service config(s) from %s", len(self._services), path)

    @classmethod
    def from_mapping(cls, services: dict[str, ServiceConfig]) -> "ServiceRegistry":
        """Build a registry directly from ServiceConfig objects, bypassing the
        YAML file. Used by the eval harness and tests, which wire services
        in-process rather than from disk."""
        registry = cls.__new__(cls)
        registry._services = dict(services)
        return registry

    def get(self, name: str) -> ServiceConfig | None:
        return self._services.get(name)

    def __len__(self) -> int:
        return len(self._services)

    def names(self) -> list[str]:
        return sorted(self._services.keys())


# ---------------------------------------------------------------------------
# Optional block parsers
#
# Both follow the registry's existing contract: a malformed block logs a
# warning and is DROPPED, leaving the service loaded without it. The service
# itself is never skipped over an optional field — a typo in an advisory block
# should not silently pull a service out of monitoring, which would be a much
# larger operational regression than losing the advisory.
#
# The tradeoff runs the other way for `delivery`, so the warnings say so out
# loud: dropping a delivery block returns the service to the behaviour these
# declarations exist to prevent (a confident patch that never reconciles), and
# an operator who typo'd `oci_chart` would otherwise believe they were covered.
# ---------------------------------------------------------------------------

def _parse_version_ref(service: str, raw: object) -> VersionRef | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning(
            "service %r: delivery.version_ref is not a mapping — ignoring it", service,
        )
        return None
    ref = VersionRef(
        repo=_opt_str(raw.get("repo")),
        path=_opt_str(raw.get("path")),
        key=_opt_str(raw.get("key")),
    )
    if ref == VersionRef():
        return None  # present but entirely empty — same as absent
    return ref


def _parse_delivery(service: str, raw: object) -> Delivery | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        log.warning(
            "service %r: `delivery` is not a mapping — NOT applying any delivery "
            "constraint; patches will be treated as directly reconciling", service,
        )
        return None

    mode = _opt_str(raw.get("mode"))
    if not mode:
        log.warning(
            "service %r: `delivery` has no `mode` — NOT applying any delivery "
            "constraint; expected one of %s", service, ", ".join(DELIVERY_MODES),
        )
        return None
    if mode not in DELIVERY_MODES:
        log.warning(
            "service %r: unknown delivery.mode %r — NOT applying any delivery "
            "constraint; expected one of %s. A source-only patch for this service "
            "will be generated as if it reconciles directly.",
            service, mode, ", ".join(DELIVERY_MODES),
        )
        return None

    delivery = Delivery(
        mode=mode, version_ref=_parse_version_ref(service, raw.get("version_ref")),
    )
    if delivery.is_indirect and delivery.version_ref is None:
        # Not an error: knowing the mode is most of the value. Worth a note so
        # an operator can see why the prompt could not name the file to bump.
        log.info(
            "service %r: delivery.mode=%s with no version_ref — the synthesis "
            "prompt will flag that a repackage/bump is required but cannot say "
            "where the version lives", service, mode,
        )
    return delivery


def _parse_chart_lineage(service: str, raw: object) -> tuple[ChartLayer, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        log.warning(
            "service %r: `chart_lineage` is not a list — ignoring it", service,
        )
        return ()

    layers: list[ChartLayer] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            log.warning(
                "service %r: chart_lineage[%d] is not a mapping — skipping it",
                service, i,
            )
            continue
        layer_name = _opt_str(item.get("name"))
        if not layer_name:
            log.warning(
                "service %r: chart_lineage[%d] has no `name` — skipping it",
                service, i,
            )
            continue
        layers.append(ChartLayer(
            name=layer_name,
            version=_opt_str(item.get("version")),
            repo=_opt_str(item.get("repo")),
            visible=bool(item.get("visible", False)),
        ))
    return tuple(layers)


def _opt_str(value: object) -> str | None:
    """Coerce a scalar YAML value to a non-empty string, or None.

    YAML happily yields floats for `version: 2.5` and bools for `key: yes`, so
    everything scalar is stringified rather than type-checked.
    """
    if value is None or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    return text or None

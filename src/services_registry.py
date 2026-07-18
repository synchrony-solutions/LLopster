"""Lookup per-service config (codebase path, GitHub repo) from a YAML file.

Each incoming alert carries a `service` label; the registry maps that label to
the local codebase directory the patch generator should send to Claude and the
GitHub repo the PR creator should target. Services not in the registry are
skipped with a warning rather than falling back to a global default.
"""

import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

log = logging.getLogger("llopster.services")


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

#!/usr/bin/env python3
"""Assert the cluster-context RBAC the chart generates stays read-only.

Issue #23 grants an LLM-driven agent the ability to read live cluster objects.
Two properties make that acceptable, and neither is self-enforcing in a Helm
template — a one-word edit to a verb list is easy to make and easy to miss in
review, so this runs in chart CI:

  1. No verb outside get/list/watch, in any rule.
  2. No `secrets` rule, ever. The agent already holds a write-scoped GitHub
     token; secrets read would turn incident context into an exfiltration
     surface.

Also checks the negative case: with `clusterContext.enabled=false` (the
default) the chart must render no RBAC for the agent at all, and no
ServiceAccount token into the agent pod.

Usage: scripts/check_cluster_rbac.py [chart-dir]

Requires `helm` on PATH *and* the chart's subchart tarballs fetched
(`helm dependency build`) — helm resolves dependencies before it evaluates
their conditions, so even a `--show-only` render of one template fails without
them. Exit codes: 0 pass, 1 violation, 2 cannot run (see EXIT_CANNOT_RUN).
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import yaml

# Distinguished from a violation (1) so callers can tell "this check found a
# problem" from "this check could not run". CI must treat both as failure —
# there the dependencies ARE built, so 2 means the workflow is broken — but the
# pytest wrapper skips on 2, because a developer's working tree legitimately
# has no tarballs (they are gitignored and fetched by bootstrap-helm.sh).
EXIT_CANNOT_RUN = 2

ALLOWED_VERBS = {"get", "list", "watch"}
FORBIDDEN_RESOURCES = {"secrets"}

RBAC_KINDS = {"ClusterRole", "Role", "ClusterRoleBinding", "RoleBinding"}


def missing_dependencies(chart_dir: str) -> list[str]:
    """Declared subcharts with no tarball in charts/.

    Checked structurally rather than by matching helm's error text: the message
    is not an API, and a silent skip on a changed wording would turn this gate
    off without anyone noticing.
    """
    chart_yaml = yaml.safe_load(Path(chart_dir, "Chart.yaml").read_text()) or {}
    declared = [
        d.get("name")
        for d in (chart_yaml.get("dependencies") or [])
        if isinstance(d, dict) and d.get("name")
    ]
    present = {p.name for p in Path(chart_dir, "charts").glob("*.tgz")}
    return [
        name
        for name in declared
        if not any(p.startswith(f"{name}-") for p in present)
    ]


def render(chart_dir: str, *sets: str, show_only: str | None = None) -> list[dict]:
    cmd = ["helm", "template", "llopster", chart_dir, "-n", "llopster"]
    for s in sets:
        cmd += ["--set", s]
    if show_only:
        cmd += ["--show-only", show_only]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        # `--show-only` on a template that renders nothing is not a failure
        # here; every other non-zero exit is.
        if show_only and "could not find template" in proc.stderr:
            return []
        print(proc.stderr, file=sys.stderr)
        raise SystemExit(f"helm template failed: {' '.join(cmd)}")
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def fail(msg: str, failures: list[str]) -> None:
    failures.append(msg)


def check_read_only(docs: list[dict], failures: list[str]) -> None:
    roles = [d for d in docs if d.get("kind") in ("ClusterRole", "Role")]
    if not roles:
        fail("expected a ClusterRole when clusterContext.enabled=true", failures)
    for role in roles:
        name = role.get("metadata", {}).get("name", "?")
        for rule in role.get("rules") or []:
            verbs = set(rule.get("verbs") or [])
            extra = verbs - ALLOWED_VERBS
            if extra:
                fail(
                    f"{role['kind']}/{name} grants verb(s) {sorted(extra)} — "
                    "read-only means get/list/watch and nothing else",
                    failures,
                )
            resources = set(rule.get("resources") or [])
            banned = resources & FORBIDDEN_RESOURCES
            if banned:
                fail(
                    f"{role['kind']}/{name} grants {sorted(banned)} — the agent "
                    "holds a write-scoped GitHub token; this must never be added",
                    failures,
                )
            if "*" in resources or "*" in verbs or "*" in (rule.get("apiGroups") or []):
                fail(f"{role['kind']}/{name} uses a wildcard rule", failures)


def main() -> int:
    chart_dir = sys.argv[1] if len(sys.argv) > 1 else "helm-chart"
    if not Path(chart_dir, "Chart.yaml").exists():
        raise SystemExit(f"no chart at {chart_dir}")

    if shutil.which("helm") is None:
        print("cannot run: helm is not on PATH", file=sys.stderr)
        return EXIT_CANNOT_RUN
    missing = missing_dependencies(chart_dir)
    if missing:
        print(
            f"cannot run: subchart tarball(s) missing for {', '.join(missing)}. "
            f"helm resolves dependencies before evaluating their conditions, so "
            f"even a --show-only render needs them. Run: "
            f"helm dependency build {chart_dir}",
            file=sys.stderr,
        )
        return EXIT_CANNOT_RUN

    failures: list[str] = []

    # 1. Namespace-scoped: ClusterRole + one RoleBinding per namespace.
    scoped = render(
        chart_dir,
        "agent.clusterContext.enabled=true",
        "agent.clusterContext.namespaces={demo-app,order-service}",
        show_only="templates/cluster-rbac.yaml",
    )
    check_read_only(scoped, failures)
    bindings = [d for d in scoped if d.get("kind") == "RoleBinding"]
    bound = sorted(b["metadata"]["namespace"] for b in bindings)
    if bound != ["demo-app", "order-service"]:
        fail(f"expected a RoleBinding per namespace, got {bound}", failures)
    if any(d.get("kind") == "ClusterRoleBinding" for d in scoped):
        fail(
            "namespace-scoped config rendered a ClusterRoleBinding — that grants "
            "every namespace, which is the thing scoping exists to prevent",
            failures,
        )

    # 2. Cluster-wide: exactly one ClusterRoleBinding, no RoleBindings.
    wide = render(
        chart_dir,
        "agent.clusterContext.enabled=true",
        "agent.clusterContext.allNamespaces=true",
        show_only="templates/cluster-rbac.yaml",
    )
    check_read_only(wide, failures)
    if len([d for d in wide if d.get("kind") == "ClusterRoleBinding"]) != 1:
        fail("allNamespaces=true should render exactly one ClusterRoleBinding", failures)
    if any(d.get("kind") == "RoleBinding" for d in wide):
        fail("allNamespaces=true should render no RoleBindings", failures)

    # 3. Default (disabled): no agent RBAC, and no token in the pod.
    default_rbac = render(chart_dir, show_only="templates/cluster-rbac.yaml")
    if default_rbac:
        fail(
            "clusterContext.enabled=false rendered RBAC — the default install "
            "must grant the agent nothing",
            failures,
        )
    agent = render(chart_dir, show_only="templates/llopster-agent.yaml")
    for doc in agent:
        if doc.get("kind") != "Deployment":
            continue
        spec = doc["spec"]["template"]["spec"]
        if spec.get("automountServiceAccountToken") is not False:
            fail(
                "agent pod mounts a ServiceAccount token with cluster access "
                "disabled — Kubernetes mounts one unless told not to",
                failures,
            )
        for rule in [d for d in agent if d.get("kind") in RBAC_KINDS]:
            fail(f"unexpected RBAC object {rule['kind']} in the agent template", failures)

    # 4. Enabled but scoped to nothing must fail the render, not grant nothing.
    proc = subprocess.run(
        ["helm", "template", "llopster", chart_dir, "-n", "llopster",
         "--set", "agent.clusterContext.enabled=true"],
        capture_output=True, text=True,
    )
    if proc.returncode == 0:
        fail(
            "enabled with no namespaces and allNamespaces=false rendered "
            "successfully — it should fail loudly rather than silently grant nothing",
            failures,
        )

    if failures:
        print("cluster RBAC check FAILED:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        return 1
    print("cluster RBAC check passed (read-only, no secrets, default renders nothing)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

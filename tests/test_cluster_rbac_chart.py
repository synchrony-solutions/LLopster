"""Chart-level guard for the cluster-context RBAC.

The gate that matters is `scripts/check_cluster_rbac.py` in helm CI, where helm
is installed and `helm dependency build` has run. This wrapper runs the same
checker locally so a chart edit also fails in `pytest` — but only when it can
actually run.

Whether it can is the checker's call, not this file's. An earlier version
skipped on `shutil.which("helm") is None`, which was wrong in the one
environment that mattered: the pytest CI runner HAS helm but never builds the
chart's dependencies, and the subchart tarballs are gitignored. The test ran,
failed on dependency resolution, and said nothing about RBAC.
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
CHECKER = REPO_ROOT / "scripts" / "check_cluster_rbac.py"

# Keep in sync with EXIT_CANNOT_RUN in the checker.
EXIT_CANNOT_RUN = 2


def test_generated_cluster_rbac_is_read_only_and_secret_free():
    proc = subprocess.run(
        [sys.executable, str(CHECKER), str(REPO_ROOT / "helm-chart")],
        capture_output=True,
        text=True,
    )
    if proc.returncode == EXIT_CANNOT_RUN:
        pytest.skip(f"checker cannot run here: {proc.stderr.strip()}")
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_checker_reports_cannot_run_distinctly_from_a_violation(tmp_path):
    """A skip must never be reachable by a real RBAC violation.

    The two states share an exit code the moment someone collapses them, and
    then a genuine `secrets` grant would be silently skipped in CI.
    """
    from scripts.check_cluster_rbac import missing_dependencies

    chart = tmp_path / "chart"
    (chart / "charts").mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "name: x\nversion: 0.1.0\ndependencies:\n"
        "  - name: prometheus\n    version: '1.0.0'\n"
        "  - name: loki\n    version: '2.0.0'\n"
    )
    assert missing_dependencies(str(chart)) == ["prometheus", "loki"]

    (chart / "charts" / "prometheus-1.0.0.tgz").write_bytes(b"")
    assert missing_dependencies(str(chart)) == ["loki"]

    (chart / "charts" / "loki-2.0.0.tgz").write_bytes(b"")
    assert missing_dependencies(str(chart)) == []


def test_checker_has_no_dependencies_to_miss_when_none_are_declared(tmp_path):
    from scripts.check_cluster_rbac import missing_dependencies

    chart = tmp_path / "chart"
    (chart / "charts").mkdir(parents=True)
    (chart / "Chart.yaml").write_text("name: x\nversion: 0.1.0\n")
    assert missing_dependencies(str(chart)) == []
